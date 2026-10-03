"""Traceable research result schemas.

Research facts and sources are intentionally modeled together.  A fact keeps
the stable source IDs that support it, while the result also carries the
source metadata needed to explain those IDs to a caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


VerificationStatus = Literal[
    "verified",
    "partially_verified",
    "not_verified",
    "conflicting",
]
RecruitmentStatus = Literal["open", "closed", "unknown", "not_applicable"]
RecruitmentDecisionStatus = Literal["open", "closed", "unknown"]


class SourceItem(BaseModel):
    """One fetched or referenced source used by a research result."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    published_at: datetime | None = None
    fetched_at: datetime | None = None


class ResearchFact(BaseModel):
    """One claim whose supporting sources are explicitly addressable."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    fact_id: str = Field(min_length=1)
    # The document uses ``claim`` while the Task 3.1 card calls this field a
    # ``statement``.  Accept both spellings and expose both names to callers.
    claim: str = Field(
        min_length=1,
        validation_alias=AliasChoices("claim", "statement"),
    )
    source_ids: list[str] = Field(min_length=1)
    verification_status: VerificationStatus
    verified_at: datetime | None = None
    valid_until: datetime | None = None
    recruitment_status: RecruitmentStatus = "not_applicable"
    notes: str | None = None

    @property
    def statement(self) -> str:
        """Task-card terminology for the canonical ``claim`` field."""

        return self.claim


class ResearchResult(BaseModel):
    """A complete, source-traceable research result."""

    model_config = ConfigDict(extra="forbid")

    research_result_id: str = Field(min_length=1)
    summary: str = ""
    facts: list[ResearchFact] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_traceability(self) -> "ResearchResult":
        fact_ids = [fact.fact_id for fact in self.facts]
        source_ids = [source.source_id for source in self.sources]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("research fact_id values must be unique")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("research source_id values must be unique")

        source_id_set = set(source_ids)
        for fact in self.facts:
            missing = set(fact.source_ids) - source_id_set
            if missing:
                missing_text = ", ".join(sorted(missing))
                raise ValueError(
                    f"research fact {fact.fact_id} references unknown sources: "
                    f"{missing_text}"
                )
        return self


class RecruitmentStatusDecision(BaseModel):
    """Deterministic result of evaluating recruitment evidence."""

    model_config = ConfigDict(extra="forbid")

    status: RecruitmentDecisionStatus
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    conflict: bool = False
    fact_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def recruitment_status(self) -> RecruitmentDecisionStatus:
        """Compatibility name used by business callers and API serializers."""

        return self.status

    @property
    def is_verified(self) -> bool:
        """Whether the deterministic layer reached an open/closed answer."""

        return self.status != "unknown" and not self.conflict


__all__ = [
    "RecruitmentDecisionStatus",
    "RecruitmentStatusDecision",
    "RecruitmentStatus",
    "ResearchFact",
    "ResearchResult",
    "SourceItem",
    "VerificationStatus",
]
