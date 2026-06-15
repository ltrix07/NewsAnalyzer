"""OpenAI-based relevance scoring stage."""

from __future__ import annotations

from engine.domain import Event as EventDTO
from engine.domain import ScoredEvent as ScoredEventDTO
from engine.llm.client import LLMClient
from engine.llm.prompts import render_prompt
from engine.llm.schemas import RelevanceVerdict
from engine.profile import Profile
from engine.stages._event_context import load_event_articles
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


class RelevanceStage(Stage[EventDTO, ScoredEventDTO]):
    """Score post-filter events for personal relevance using OpenAI."""

    name = "relevance"
    version = "v3"

    def __init__(self, llm_client: LLMClient, profile: Profile, model: str) -> None:
        self.llm_client = llm_client
        self.profile = profile
        self.model = model

    async def process(self, event: EventDTO, ctx: Context) -> StageResult[ScoredEventDTO]:
        """Score one event for personal relevance."""

        articles = await load_event_articles(ctx.session, event.id)
        rendered_prompt = render_prompt(
            "relevance_v3.j2",
            profile=self.profile,
            articles=articles,
        )
        response = await self.llm_client.call_structured(
            model=self.model,
            system="You output only via the submit_verdict tool.",
            prompt=rendered_prompt,
            output_schema=RelevanceVerdict,
            max_tokens=512,
        )
        verdict = response.output
        action = "relevant" if verdict.relevant else "irrelevant"
        draft = DecisionDraft(
            target_type="event",
            target_id=event.id,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost_usd,
            decision_json={
                "action": action,
                "verdict": verdict.model_dump(),
            },
        )
        return StageResult(
            output=ScoredEventDTO(event=event, verdict=verdict) if verdict.relevant else None,
            draft=draft,
            cost_usd=float(response.usage.cost_usd),
        )
