"""Structured topic-candidate schemas shared by Topic Agent boundaries.

The schema deliberately does not generate identifiers.  ``topic_id`` is a
business-layer identifier and must be supplied by the caller that creates a
candidate batch; an LLM must not become the source of database primary keys.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TopicCandidate(BaseModel):
    """One topic candidate returned by the Topic Agent."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1)
    display_index: int = Field(ge=1)
    title: str = Field(min_length=1)
    why_now: str = ""
    target_audience: str = ""
    content_angle: str = ""
    key_points: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    freshness: str = ""
    risks: list[str] = Field(default_factory=list)


class TopicResult(BaseModel):
    """The bounded, reviewable result of one candidate-generation batch."""

    model_config = ConfigDict(extra="forbid")

    candidate_batch_id: str = Field(min_length=1)
    summary: str = ""
    topic_candidates: list[TopicCandidate] = Field(default_factory=list)
    research_needed: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
