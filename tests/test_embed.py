"""DB-backed tests for the embedding stage and CLI command."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import embed as embed_cli
from engine.config import get_settings
from engine.domain import Article as ArticleDTO
from engine.llm.embeddings import EmbedResult
from engine.models import Article, Decision, Embedding, Source
from engine.stages.base import Context
from engine.stages.embed import EmbedStage


class FakeEmbedder:
    """Deterministic embedder used to avoid network calls in tests."""

    model = "fake-embedding-model"
    dimensions = 1536

    def __init__(
        self,
        *,
        total_tokens: int = 90,
        total_cost: Decimal = Decimal("0.000900"),
    ) -> None:
        self.total_tokens = total_tokens
        self.total_cost = total_cost
        self.seen_batches: list[list[str]] = []

    async def embed_batch(self, texts: list[str]) -> EmbedResult:
        self.seen_batches.append(texts)
        vectors = [[float(len(text))] * self.dimensions for text in texts]
        return EmbedResult(
            vectors=vectors,
            total_tokens=self.total_tokens,
            cost_usd=self.total_cost,
        )


async def _create_source(session: AsyncSession, name: str = "embed-source") -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


async def _create_article(
    session: AsyncSession,
    source_id: int,
    *,
    suffix: str,
    raw_text: str,
) -> Article:
    article = Article(
        source_id=source_id,
        url=f"https://example.com/{suffix}",
        url_hash=suffix.encode("utf-8").ljust(8, b"0")[:8],
        content_hash=suffix[::-1].encode("utf-8").ljust(8, b"1")[:8],
        title=f"Title {suffix}",
        raw_text=raw_text,
        lang="en",
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_embed_stage_persists_embeddings_and_decisions(db_session: AsyncSession) -> None:
    """New articles should be embedded, persisted, and logged."""

    source = await _create_source(db_session)
    articles = [
        await _create_article(db_session, source.id, suffix="a1", raw_text="alpha"),
        await _create_article(db_session, source.id, suffix="a2", raw_text="beta"),
        await _create_article(db_session, source.id, suffix="a3", raw_text="gamma"),
    ]
    fake_embedder = FakeEmbedder()
    stage = EmbedStage(fake_embedder)
    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())

    article_dtos = [ArticleDTO.model_validate(article) for article in articles]

    results = await stage.run_batch(article_dtos, ctx)
    stored_embeddings = (
        await db_session.scalars(select(Embedding).order_by(Embedding.article_id))
    ).all()
    stored_decisions = (
        await db_session.scalars(
            select(Decision)
            .where(Decision.stage_name == "embed", Decision.run_id == ctx.run_id)
            .order_by(Decision.target_id)
        )
    ).all()

    assert [result.draft.decision_json["action"] for result in results] == ["embedded"] * 3
    assert len(stored_embeddings) == 3
    assert len(stored_decisions) == 3
    assert list(stored_embeddings[0].vector) == [5.0] * fake_embedder.dimensions
    assert sum((decision.cost_usd or Decimal("0")) for decision in stored_decisions) == Decimal(
        "0.000900"
    )


@pytest.mark.asyncio
async def test_embed_stage_skips_preexisting_embedding(db_session: AsyncSession) -> None:
    """Already-embedded articles should not get duplicate embedding rows."""

    source = await _create_source(db_session)
    first = await _create_article(db_session, source.id, suffix="b1", raw_text="first")
    second = await _create_article(db_session, source.id, suffix="b2", raw_text="second")
    db_session.add(
        Embedding(
            article_id=first.id,
            vector=[1.0] * 1536,
            model="preexisting-model",
        )
    )
    await db_session.flush()

    stage = EmbedStage(FakeEmbedder())
    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())

    results = await stage.run_batch(
        [ArticleDTO.model_validate(first), ArticleDTO.model_validate(second)],
        ctx,
    )
    embedding_count = await db_session.scalar(select(func.count()).select_from(Embedding))
    first_rows = (
        await db_session.scalars(select(Embedding).where(Embedding.article_id == first.id))
    ).all()

    assert results[0].output is None
    assert results[0].draft.decision_json["action"] == "already_embedded"
    assert results[1].draft.decision_json["action"] == "embedded"
    assert embedding_count == 2
    assert len(first_rows) == 1


@pytest.mark.asyncio
async def test_embed_stage_records_truncation_metadata(db_session: AsyncSession) -> None:
    """Long article text should be truncated with metadata recorded in the decision."""

    source = await _create_source(db_session)
    article = await _create_article(db_session, source.id, suffix="c1", raw_text="x" * 25)
    stage = EmbedStage(FakeEmbedder(total_tokens=10, total_cost=Decimal("0.000010")), max_chars=10)
    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())

    result = await stage.run(ArticleDTO.model_validate(article), ctx)

    assert result.draft.decision_json["truncated"] is True
    assert result.draft.decision_json["original_length"] == 25
    assert stage.embedder.seen_batches == [["x" * 10]]


@pytest.mark.asyncio
async def test_embed_stage_process_batch_preserves_input_order(db_session: AsyncSession) -> None:
    """Mixed embedded and new items should keep the caller's original ordering."""

    source = await _create_source(db_session)
    first = await _create_article(db_session, source.id, suffix="d1", raw_text="uno")
    second = await _create_article(db_session, source.id, suffix="d2", raw_text="dos")
    third = await _create_article(db_session, source.id, suffix="d3", raw_text="tres")
    db_session.add(Embedding(article_id=second.id, vector=[2.0] * 1536, model="seed-model"))
    await db_session.flush()

    stage = EmbedStage(FakeEmbedder(total_tokens=20, total_cost=Decimal("0.000200")))
    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())

    ordered_results = await stage.process_batch(
        [
            ArticleDTO.model_validate(second),
            ArticleDTO.model_validate(first),
            ArticleDTO.model_validate(third),
        ],
        ctx,
    )

    assert [result.draft.decision_json["action"] for result in ordered_results] == [
        "already_embedded",
        "embedded",
        "embedded",
    ]
    assert ordered_results[0].output is None
    assert ordered_results[1].output is not None
    assert ordered_results[1].output.article_id == first.id
    assert ordered_results[2].output is not None
    assert ordered_results[2].output.article_id == third.id


@pytest.mark.asyncio
async def test_embed_command_rerun_logs_already_embedded_only(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running the CLI over the same limited slice should not create new embeddings."""

    source = await _create_source(db_session)
    await _create_article(db_session, source.id, suffix="e1", raw_text="one")
    await _create_article(db_session, source.id, suffix="e2", raw_text="two")
    await _create_article(db_session, source.id, suffix="e3", raw_text="three")
    fake_embedder = FakeEmbedder(total_tokens=30, total_cost=Decimal("0.000300"))

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(embed_cli, "make_default_embedder", lambda settings: fake_embedder)
    monkeypatch.setattr(embed_cli, "session_scope", fake_session_scope)

    await embed_cli.embed_command(limit=3, batch_size=2, source=source.name)
    first_count = await db_session.scalar(select(func.count()).select_from(Embedding))

    await embed_cli.embed_command(limit=3, batch_size=2, source=source.name)
    stored_decisions = (
        await db_session.scalars(
            select(Decision).where(Decision.stage_name == "embed").order_by(Decision.id)
        )
    ).all()
    second_count = await db_session.scalar(select(func.count()).select_from(Embedding))

    assert first_count == 3
    assert second_count == 3
    assert [decision.decision_json["action"] for decision in stored_decisions[:3]] == [
        "embedded"
    ] * 3
    assert [decision.decision_json["action"] for decision in stored_decisions[3:]] == [
        "already_embedded"
    ] * 3
