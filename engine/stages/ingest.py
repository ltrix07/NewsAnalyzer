"""The ingest stage for extraction, normalization, and deduplication."""

from __future__ import annotations

import trafilatura
import xxhash
from sqlalchemy import select

from engine.domain import Article as ArticleDTO
from engine.models import Article as ArticleModel
from engine.models import Source as SourceModel
from engine.sources.base import RawArticle
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


def _strip_nul(value: str | None) -> str | None:
    """Remove NUL bytes that PostgreSQL cannot store in text or JSONB columns."""

    if value is None:
        return None
    return value.replace("\x00", "")


class IngestStage(Stage[RawArticle, ArticleDTO]):
    """Normalize and persist raw source output into stored articles."""

    name = "ingest"
    version = "v1"

    @staticmethod
    def _extract_text(raw_html: str) -> str | None:
        """Extract normalized text from raw HTML, including fragment fallback."""

        candidate_html_values = (raw_html, f"<html><body>{raw_html}</body></html>")
        for candidate_html in candidate_html_values:
            extracted = trafilatura.extract(
                candidate_html,
                include_links=False,
                include_images=False,
                favor_recall=True,
            )
            if extracted:
                normalized = extracted.strip()
                if normalized:
                    return normalized
        return None

    async def process(self, item: RawArticle, ctx: Context) -> StageResult[ArticleDTO]:
        """Persist one raw article unless it is filtered or deduplicated."""

        source = await ctx.session.scalar(
            select(SourceModel).where(SourceModel.name == item.source_name)
        )
        if source is None:
            return StageResult(
                output=None,
                draft=DecisionDraft(
                    target_type="source",
                    target_id=0,
                    decision_json={"action": "unknown_source", "name": item.source_name},
                ),
            )

        url_hash = xxhash.xxh64(item.url.encode("utf-8")).digest()
        existing = await ctx.session.scalar(
            select(ArticleModel).where(
                ArticleModel.source_id == source.id,
                ArticleModel.url_hash == url_hash,
            )
        )
        if existing is not None:
            return StageResult(
                output=None,
                draft=DecisionDraft(
                    target_type="article",
                    target_id=existing.id,
                    decision_json={"action": "deduped_url", "url": item.url},
                ),
            )

        extracted_text: str | None = None
        used_trafilatura = False
        if item.raw_html:
            extracted_text = self._extract_text(item.raw_html)
            used_trafilatura = bool(extracted_text)

        if not extracted_text and item.raw_text:
            extracted_text = item.raw_text.strip()

        if not extracted_text:
            return StageResult(
                output=None,
                draft=DecisionDraft(
                    target_type="source",
                    target_id=source.id,
                    decision_json={"action": "extraction_failed", "url": item.url},
                ),
            )

        clean_text = _strip_nul(extracted_text) or ""
        content_hash = xxhash.xxh64(clean_text.encode("utf-8")).digest()
        article = ArticleModel(
            source_id=source.id,
            url=item.url,
            url_hash=url_hash,
            content_hash=content_hash,
            title=_strip_nul(item.title),
            raw_text=clean_text,
            lang=item.language,
            published_at=item.published_at,
        )
        ctx.session.add(article)
        await ctx.session.flush()

        return StageResult(
            output=ArticleDTO.model_validate(article),
            draft=DecisionDraft(
                target_type="article",
                target_id=article.id,
                decision_json={
                    "action": "inserted",
                    "url": item.url,
                    "text_length": len(extracted_text),
                    "extracted_via": "trafilatura" if used_trafilatura else "raw_text",
                },
            ),
        )
