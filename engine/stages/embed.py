"""Embedding stage for generating and persisting article vectors."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from engine.domain import Article as ArticleDTO
from engine.domain import Embedding as EmbeddingDTO
from engine.llm.embeddings import Embedder
from engine.models import Embedding as EmbeddingModel
from engine.stages.base import Context, DecisionDraft, Stage, StageResult

_USD_QUANTUM = Decimal("0.000001")


class EmbedStage(Stage[ArticleDTO, EmbeddingDTO]):
    """Embed stored article text and persist one vector per article.

    Embedding input is the article's title with a short body excerpt appended.
    Title-led embeddings cluster cross-language same-event articles tighter than
    raw_text embeddings, which diverge by author style, source boilerplate, and
    length even when the underlying event is identical.
    """

    name = "embed"
    version = "v2"

    def __init__(self, embedder: Embedder, max_chars: int = 600) -> None:
        self.embedder = embedder
        self.max_chars = max_chars

    @staticmethod
    def _build_embed_input(article: ArticleDTO, max_chars: int) -> str:
        """Compose the text passed to the embedder: title plus a short body lead."""

        title = (article.title or "").strip()
        body = (article.raw_text or "").strip()
        combined = f"{title}\n\n{body}" if title and body else title or body
        return combined[:max_chars]

    async def process(self, item: ArticleDTO, ctx: Context) -> StageResult[EmbeddingDTO]:
        """Delegate single-item processing to the batch implementation."""

        return (await self.process_batch([item], ctx))[0]

    async def process_batch(
        self,
        items: list[ArticleDTO],
        ctx: Context,
    ) -> list[StageResult[EmbeddingDTO]]:
        """Embed articles in one provider batch while preserving input order."""

        if not items:
            return []

        article_ids = [article.id for article in items]
        existing_rows = await ctx.session.scalars(
            select(EmbeddingModel).where(EmbeddingModel.article_id.in_(article_ids))
        )
        existing_by_article_id = {row.article_id: row for row in existing_rows}

        results_by_article_id: dict[int, StageResult[EmbeddingDTO]] = {}
        pending_articles: list[ArticleDTO] = []
        pending_texts: list[str] = []
        pending_truncation: dict[int, tuple[bool, int]] = {}

        for article in items:
            existing = existing_by_article_id.get(article.id)
            if existing is not None:
                results_by_article_id[article.id] = StageResult(
                    output=None,
                    draft=DecisionDraft(
                        target_type="article",
                        target_id=article.id,
                        decision_json={"action": "already_embedded"},
                    ),
                )
                continue

            embed_input = self._build_embed_input(article, self.max_chars)
            raw_text_length = len(article.raw_text or "")
            truncated = raw_text_length > self.max_chars
            pending_articles.append(article)
            pending_texts.append(embed_input)
            pending_truncation[article.id] = (truncated, raw_text_length)

        if pending_articles:
            embed_result = await self.embedder.embed_batch(pending_texts)
            if len(embed_result.vectors) != len(pending_articles):
                msg = (
                    "Embedder returned a vector count that does not match the requested batch "
                    f"size: {len(embed_result.vectors)} != {len(pending_articles)}."
                )
                raise RuntimeError(msg)

            batch_size = len(pending_articles)
            per_item_tokens = embed_result.total_tokens // batch_size
            per_item_cost = (embed_result.cost_usd / Decimal(batch_size)).quantize(
                _USD_QUANTUM,
                rounding=ROUND_HALF_UP,
            )

            persisted_rows: list[EmbeddingModel] = []
            for article, vector in zip(pending_articles, embed_result.vectors, strict=True):
                row = EmbeddingModel(
                    article_id=article.id,
                    vector=vector,
                    model=self.embedder.model,
                )
                ctx.session.add(row)
                persisted_rows.append(row)

            await ctx.session.flush()

            for article, row in zip(pending_articles, persisted_rows, strict=True):
                truncated, original_length = pending_truncation[article.id]
                results_by_article_id[article.id] = StageResult(
                    output=EmbeddingDTO.model_validate(row),
                    draft=DecisionDraft(
                        target_type="article",
                        target_id=article.id,
                        model=self.embedder.model,
                        input_tokens=per_item_tokens,
                        cost_usd=per_item_cost,
                        decision_json={
                            "action": "embedded",
                            "cost_split": "uniform",
                            "dim": len(row.vector),
                            "truncated": truncated,
                            "original_length": original_length,
                        },
                    ),
                    cost_usd=float(per_item_cost),
                )

        return [results_by_article_id[article.id] for article in items]
