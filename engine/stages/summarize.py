"""OpenAI-based digest summarization stage."""

from __future__ import annotations

from typing import Any

from engine.domain import Digest as DigestDTO
from engine.domain import VerifiedEvent as VerifiedEventDTO
from engine.llm.client import LLMClient
from engine.llm.prompts import render_prompt
from engine.llm.schemas import DigestPayload
from engine.models import Digest as DigestModel
from engine.profile import Profile
from engine.stages._event_context import load_event_articles
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


def _strip_nul(value: str) -> str:
    """Remove NUL bytes that PostgreSQL JSONB and text columns cannot store."""

    return value.replace("\x00", "")


def _strip_nul_in_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively strip NUL bytes from every string in a JSON-serializable dict."""

    return {key: _scrub(value) for key, value in payload.items()}


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_nul(value)
    if isinstance(value, dict):
        return _strip_nul_in_dict(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


class SummarizeStage(Stage[VerifiedEventDTO, DigestDTO]):
    """Produce and persist one user-facing digest for a verified event."""

    name = "summarize"
    version = "v3"

    def __init__(self, llm_client: LLMClient, profile: Profile, model: str) -> None:
        self.llm_client = llm_client
        self.profile = profile
        self.model = model

    async def process(self, item: VerifiedEventDTO, ctx: Context) -> StageResult[DigestDTO]:
        """Generate and persist one digest for a verified event."""

        articles = await load_event_articles(ctx.session, item.event.id)
        rendered_prompt = render_prompt(
            "summarize_v3.j2",
            profile=self.profile,
            articles=articles,
            verdict=item.verdict,
            report=item.report,
        )
        response = await self.llm_client.call_structured(
            model=self.model,
            system=(
                "You respond with a single DigestPayload object that satisfies the response schema."
            ),
            prompt=rendered_prompt,
            output_schema=DigestPayload,
            max_tokens=2048,
        )
        payload = response.output
        digest_row = DigestModel(
            event_id=item.event.id,
            profile_name=self.profile.name,
            headline=_strip_nul(payload.headline),
            summary=_strip_nul(payload.summary),
            why_it_matters=_strip_nul(payload.why_it_matters),
            confidence_level=payload.confidence_level,
            caveats=[_strip_nul(caveat) for caveat in payload.caveats],
            citations=[_strip_nul_in_dict(citation.model_dump()) for citation in payload.citations],
            stage_version=self.version,
        )
        ctx.session.add(digest_row)
        await ctx.session.flush()
        draft = DecisionDraft(
            target_type="digest",
            target_id=digest_row.id,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost_usd,
            decision_json={
                "action": "digested",
                "event_id": item.event.id,
                "confidence_level": payload.confidence_level,
                "headline": _strip_nul(payload.headline),
            },
        )
        return StageResult(
            output=DigestDTO.model_validate(digest_row),
            draft=draft,
            cost_usd=float(response.usage.cost_usd),
        )
