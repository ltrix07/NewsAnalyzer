"""Tests for the OpenAI structured-output client wrapper."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from engine.llm.client import LLMClient
from engine.llm.schemas import RelevanceVerdict


class _FakeCompletions:
    def __init__(self, response: object) -> None:
        self.response = response

    async def parse(self, **_: object) -> object:
        return self.response


class _FakeChat:
    def __init__(self, response: object) -> None:
        self.completions = _FakeCompletions(response)


class _FakeBeta:
    def __init__(self, response: object) -> None:
        self.chat = _FakeChat(response)


class _FakeOpenAI:
    def __init__(self, response: object) -> None:
        self.beta = _FakeBeta(response)


def _parsed_response(
    parsed: RelevanceVerdict | None,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> object:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


@pytest.mark.asyncio
async def test_call_structured_parses_output_through_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LLMClient(api_key="test-key")
    monkeypatch.setattr(
        client,
        "_client",
        _FakeOpenAI(
            _parsed_response(
                RelevanceVerdict(
                    relevant=True,
                    categories=["Major EU policy decisions"],
                    why="Directly relevant.",
                    confidence=0.9,
                ),
                prompt_tokens=100,
                completion_tokens=50,
            )
        ),
    )

    response = await client.call_structured(
        model="gpt-4o-mini",
        system="system",
        prompt="prompt",
        output_schema=RelevanceVerdict,
    )

    assert response.output.relevant is True
    assert response.output.categories == ["Major EU policy decisions"]


@pytest.mark.asyncio
async def test_call_structured_computes_cost_from_model_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LLMClient(api_key="test-key")
    monkeypatch.setattr(
        client,
        "_client",
        _FakeOpenAI(
            _parsed_response(
                RelevanceVerdict(
                    relevant=False,
                    categories=[],
                    why="Not relevant.",
                    confidence=0.1,
                ),
                prompt_tokens=1000,
                completion_tokens=2000,
            )
        ),
    )

    response = await client.call_structured(
        model="gpt-4o-mini",
        system="system",
        prompt="prompt",
        output_schema=RelevanceVerdict,
    )

    assert response.usage.cost_usd == Decimal("0.001350")


@pytest.mark.asyncio
async def test_call_structured_raises_when_parsed_message_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LLMClient(api_key="test-key")
    monkeypatch.setattr(
        client,
        "_client",
        _FakeOpenAI(_parsed_response(None, prompt_tokens=10, completion_tokens=5)),
    )

    with pytest.raises(RuntimeError, match="parsed structured message"):
        await client.call_structured(
            model="gpt-4o-mini",
            system="system",
            prompt="prompt",
            output_schema=RelevanceVerdict,
        )
