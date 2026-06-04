"""Embedding client abstractions and the default OpenAI implementation."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from openai.types import CreateEmbeddingResponse
from pydantic import BaseModel

from engine._retry import retry_async
from engine.config import Settings

# Update this if the configured OpenAI embedding model price changes.
# text-embedding-3-large is $0.130 / 1M tokens (cross-lingual quality > 3-small).
_PRICE_PER_MTOK = Decimal("0.130")
_USD_QUANTUM = Decimal("0.000001")


class EmbedResult(BaseModel):
    """Embedding response payload normalized for stage consumption."""

    vectors: list[list[float]]
    total_tokens: int
    cost_usd: Decimal


class Embedder(Protocol):
    """Protocol for embedding providers that support batch requests."""

    model: str
    dimensions: int

    async def embed_batch(self, texts: list[str]) -> EmbedResult:
        """Embed a batch of texts and return vectors plus usage metadata."""


class OpenAIEmbedder:
    """OpenAI embeddings client with retry and simple cost accounting."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-large",
        dimensions: int = 1536,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self._client = AsyncOpenAI(api_key=api_key)

    async def embed_batch(self, texts: list[str]) -> EmbedResult:
        """Request embeddings for a batch of input texts."""

        if not texts:
            return EmbedResult(vectors=[], total_tokens=0, cost_usd=Decimal("0.000000"))

        async def create_embeddings() -> CreateEmbeddingResponse:
            return await self._client.embeddings.create(
                input=texts,
                model=self.model,
                dimensions=self.dimensions,
            )

        response = await retry_async(
            create_embeddings,
            retryable=lambda exc: isinstance(
                exc,
                (APIConnectionError, APITimeoutError, RateLimitError),
            ),
        )
        total_tokens = 0 if response.usage is None else int(response.usage.total_tokens or 0)
        cost_usd = (Decimal(total_tokens) * _PRICE_PER_MTOK / Decimal(1_000_000)).quantize(
            _USD_QUANTUM
        )
        vectors = [list(item.embedding) for item in response.data]
        return EmbedResult(vectors=vectors, total_tokens=total_tokens, cost_usd=cost_usd)


def make_default_embedder(settings: Settings) -> OpenAIEmbedder:
    """Build the default OpenAI embedder from runtime settings."""

    if settings.openai_api_key is None:
        msg = "OPENAI_API_KEY is not configured; the embed command cannot call OpenAI embeddings."
        raise RuntimeError(msg)
    return OpenAIEmbedder(api_key=settings.openai_api_key)
