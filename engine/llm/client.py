"""OpenAI client helpers for retries, structured outputs, and cost accounting."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, TypeVar, cast

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from engine._retry import retry_async
from engine.config import Settings

T = TypeVar("T", bound=BaseModel)
_USD_QUANTUM = Decimal("0.000001")

# Verify these against https://openai.com/pricing before production changes go live.
_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
}


class LLMUsage(BaseModel):
    """Usage metadata normalized for stage accounting."""

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class LLMResponse(BaseModel, Generic[T]):
    """Structured LLM response with validated output and usage."""

    output: T
    usage: LLMUsage
    model: str


class LLMClient:
    """Async OpenAI wrapper with structured-output enforcement."""

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def call_structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output_schema: type[T],
        max_tokens: int = 1024,
    ) -> LLMResponse[T]:
        """Force the model to produce a validated Pydantic instance via structured output."""

        if model not in _PRICES:
            msg = f"No pricing is configured for OpenAI model {model!r}."
            raise RuntimeError(msg)

        async def create_message() -> Any:
            return cast(
                Any,
                await self._client.beta.chat.completions.parse(
                    model=model,
                    max_tokens=max_tokens,
                    response_format=output_schema,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                ),
            )

        response = await retry_async(
            create_message,
            retryable=lambda exc: isinstance(
                exc,
                (
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                    openai.RateLimitError,
                    openai.InternalServerError,
                ),
            ),
        )

        parsed_output = None
        if response.choices:
            parsed_output = response.choices[0].message.parsed

        if parsed_output is None:
            msg = "OpenAI response did not include a parsed structured message."
            raise RuntimeError(msg)

        validated_output = output_schema.model_validate(parsed_output)
        if response.usage is None:
            msg = "OpenAI response did not include usage metadata."
            raise RuntimeError(msg)

        input_tokens = int(response.usage.prompt_tokens)
        output_tokens = int(response.usage.completion_tokens)
        input_price, output_price = _PRICES[model]
        cost_usd = (
            (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price)
            / Decimal(1_000_000)
        ).quantize(_USD_QUANTUM)

        return LLMResponse[T](
            output=validated_output,
            usage=LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            ),
            model=model,
        )


def make_llm_client(settings: Settings) -> LLMClient:
    """Build the default OpenAI chat client. Raises if API key missing."""

    if settings.openai_api_key is None:
        msg = "OPENAI_API_KEY is not configured; the score command cannot call OpenAI."
        raise RuntimeError(msg)
    return LLMClient(api_key=settings.openai_api_key)
