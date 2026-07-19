"""OpenAI-based relevance scoring stage."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

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

    def __init__(
        self,
        llm_client: LLMClient,
        profile: Profile,
        model: str,
        *,
        use_v4: bool = False,
    ) -> None:
        self.llm_client = llm_client
        self.profile = profile
        self.model = model
        self.version = "v4" if use_v4 else "v3"

    def render(self, articles: list[Any]) -> str:
        """Render this stage's selected prompt without making an LLM call."""

        residence = _residence_context(self.profile.residence_country)
        return render_prompt(
            f"relevance_{self.version}.j2",
            profile=self.profile,
            articles=articles,
            residence=residence,
        )

    async def evaluate(self, event: EventDTO, ctx: Context) -> StageResult[ScoredEventDTO]:
        """Evaluate an event without persisting a decision."""

        articles = await load_event_articles(ctx.session, event.id)
        rendered_prompt = self.render(articles)
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

    async def process(self, event: EventDTO, ctx: Context) -> StageResult[ScoredEventDTO]:
        """Score one event for personal relevance."""

        return await self.evaluate(event, ctx)


@lru_cache(maxsize=1)
def _country_registry() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[2] / "config/countries.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    countries = payload.get("countries") if isinstance(payload, dict) else None
    if not isinstance(countries, dict):
        msg = f"{path} must contain a countries mapping"
        raise RuntimeError(msg)
    return countries


def _residence_context(country_code: str | None) -> dict[str, Any] | None:
    if country_code is None or country_code == "ZZ":
        return None
    country = _country_registry().get(country_code)
    if country is None:
        return None
    labels = country.get("labels", {})
    return {
        "code": country_code,
        "name": str(labels.get("en", country_code)),
        "slots": country.get("residence_prompt_slots") or {},
    }
