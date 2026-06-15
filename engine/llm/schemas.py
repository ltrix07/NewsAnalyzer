"""Pydantic schemas for structured LLM tool-use outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RelevanceVerdict(BaseModel):
    """Structured verdict returned by the relevance stage."""

    relevant: bool
    categories: list[str]
    why: str
    confidence: float = Field(ge=0.0, le=1.0)


class VerificationReport(BaseModel):
    """Structured verification report returned by the verify stage."""

    sources_count: int = Field(ge=1)
    primary_source_present: bool
    speaker_type: Literal[
        "official",
        "expert",
        "journalist",
        "blogger",
        "anonymous",
        "unknown",
    ]
    is_speculation: bool
    hype_score: float = Field(ge=0.0, le=1.0)
    contradictions: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str


class Citation(BaseModel):
    """Citation for one source article used in a digest."""

    source: str
    title: str
    url: str


class DigestPayload(BaseModel):
    """LLM output for the summarize stage."""

    headline: str
    summary: str
    why_it_matters: str
    confidence_level: Literal["high", "medium", "low"]
    caveats: list[str]
    citations: list[Citation]


class DiscussionReply(BaseModel):
    """Grounded single-turn answer for a digest discussion."""

    answer: str
