"""OpenAI-based verification stage."""

from __future__ import annotations

from engine.domain import ScoredEvent as ScoredEventDTO
from engine.domain import VerifiedEvent as VerifiedEventDTO
from engine.llm.client import LLMClient
from engine.llm.prompts import render_prompt
from engine.llm.schemas import VerificationReport
from engine.stages._event_context import load_event_articles
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


class VerifyStage(Stage[ScoredEventDTO, VerifiedEventDTO]):
    """Verify relevance-approved events for credibility and tone."""

    name = "verify"
    version = "v1"

    def __init__(self, llm_client: LLMClient, model: str) -> None:
        self.llm_client = llm_client
        self.model = model

    async def process(self, item: ScoredEventDTO, ctx: Context) -> StageResult[VerifiedEventDTO]:
        """Produce a structured verification report for one scored event."""

        articles = await load_event_articles(ctx.session, item.event.id)
        rendered_prompt = render_prompt("verify_v1.j2", articles=articles)
        response = await self.llm_client.call_structured(
            model=self.model,
            system=(
                "You always respond with a single VerificationReport object that satisfies "
                "the response schema."
            ),
            prompt=rendered_prompt,
            output_schema=VerificationReport,
            max_tokens=1024,
        )
        report = response.output
        draft = DecisionDraft(
            target_type="event",
            target_id=item.event.id,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost_usd,
            decision_json={
                "action": "verified",
                "report": report.model_dump(),
            },
        )
        return StageResult(
            output=VerifiedEventDTO(event=item.event, verdict=item.verdict, report=report),
            draft=draft,
            cost_usd=float(response.usage.cost_usd),
        )
