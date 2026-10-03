"""Typed model for content review verdicts.

A review only ever describes one draft version.  When a draft moves to a new
version the old review stays readable as history, but it must never be reused
as the verdict for the new body; that rule is enforced by binding every record
to an explicit ``(draft_id, draft_version)`` pair.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


ReviewStatus = Literal["pass", "revise", "need_research"]


class ReviewResult(BaseModel):
    """One review verdict bound to one immutable draft version."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    review_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    status: ReviewStatus
    summary: str = ""
    issues: list[str] = Field(default_factory=list)
    must_fix: list[str] = Field(default_factory=list)
    risk_items: list[str] = Field(default_factory=list)
    revision_request: str | None = None
    research_request: str | None = None
    # Fail closed: a verdict only allows generation when it says so explicitly.
    ready_for_generation: bool = False
    # The Task 4.1 card names this field ``created_at``; the Content Review
    # Schema section names it ``reviewed_at``.  Accept both spellings and
    # expose both names to callers.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        validation_alias=AliasChoices("created_at", "reviewed_at"),
    )

    @property
    def reviewed_at(self) -> datetime:
        """Schema-section terminology for the canonical ``created_at`` field."""

        return self.created_at


__all__ = ["ReviewResult", "ReviewStatus"]
