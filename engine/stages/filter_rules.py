"""Deterministic keyword and regex prefilter stage."""

from __future__ import annotations

import re

from engine.domain import Event as EventDTO
from engine.profile import KeywordRules
from engine.stages._event_context import load_event_articles
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


class KeywordFilterStage(Stage[EventDTO, EventDTO]):
    """Cheap profile-driven filter that prefers recall over precision."""

    name = "keyword_filter"
    version = "v1"

    def __init__(self, rules: KeywordRules) -> None:
        self.keep_patterns = [re.compile(pattern) for pattern in rules.keep_if_matches]
        self.drop_patterns = [re.compile(pattern) for pattern in rules.drop_if_matches]

    async def process(self, event: EventDTO, ctx: Context) -> StageResult[EventDTO]:
        """Apply regex keep/drop rules to one event's article texts."""

        articles = await load_event_articles(ctx.session, event.id)
        haystack = " ".join(
            f"{article.title or ''} {article.excerpt}".strip() for article in articles
        )

        for pattern in self.keep_patterns:
            if pattern.search(haystack):
                return StageResult(
                    output=event,
                    draft=DecisionDraft(
                        target_type="event",
                        target_id=event.id,
                        decision_json={
                            "action": "passed_keyword_filter",
                            "matched": pattern.pattern,
                            "rule": "keep",
                        },
                    ),
                )

        for pattern in self.drop_patterns:
            if pattern.search(haystack):
                return StageResult(
                    output=None,
                    draft=DecisionDraft(
                        target_type="event",
                        target_id=event.id,
                        decision_json={
                            "action": "dropped_by_keyword_filter",
                            "matched": pattern.pattern,
                            "rule": "drop",
                        },
                    ),
                )

        return StageResult(
            output=event,
            draft=DecisionDraft(
                target_type="event",
                target_id=event.id,
                decision_json={
                    "action": "passed_keyword_filter",
                    "matched": None,
                    "rule": "default",
                },
            ),
        )
