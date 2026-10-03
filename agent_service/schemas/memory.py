"""Typed models for the long-term content memory layer.

These records outlive a single thread.  Following the document's layering rule
(checkpointer tables != business tables != long-term content tables), they
reference business identifiers such as ``draft_id`` as plain values instead of
foreign keys, so the layer can move to another database later without
rewriting cross-layer constraints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ContentStatus = Literal["draft", "scheduled", "published", "archived"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContentItem(BaseModel):
    """One published or publishable piece of content."""

    model_config = ConfigDict(extra="forbid")

    content_id: str = Field(min_length=1)
    account_id: str = ""
    topic: str = ""
    title: str = ""
    script: str = ""
    content_type: str = ""
    platform: str = ""
    publish_date: datetime | None = None
    status: ContentStatus = "draft"
    tags: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    hook: str = ""
    cta: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ContentMetrics(BaseModel):
    """One collected performance snapshot for a content item.

    Snapshots are append-only: the same item collected twice keeps both rows,
    distinguished by ``collected_at``.  Counts and rates default to zero/None
    because a platform generally exposes only part of them.
    """

    model_config = ConfigDict(extra="forbid")

    content_id: str = Field(min_length=1)
    platform: str = ""
    impressions: int = Field(default=0, ge=0)
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    favorites: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    leads: int = Field(default=0, ge=0)
    valid_leads: int = Field(default=0, ge=0)
    # Rates are unit-agnostic; only "not negative" is enforced.
    ctr: float | None = Field(default=None, ge=0)
    conversion_rate: float | None = Field(default=None, ge=0)
    lead_rate: float | None = Field(default=None, ge=0)
    collected_at: datetime = Field(default_factory=_now)


class TopicHistoryEntry(BaseModel):
    """One topic that was considered, independently of any single thread."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    content_angle: str = ""
    source: str = ""
    selected: bool = False
    used: bool = False
    publish_date: datetime | None = None
    performance_summary: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class RevisionRecord(BaseModel):
    """One user-driven revision of a draft version.

    ``version_after`` is unset when the revision was proposed but not accepted,
    so a rejected instruction is still recorded as feedback.
    """

    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    version_before: int = Field(ge=0)
    version_after: int | None = Field(default=None, ge=1)
    user_instruction: str = ""
    changed_sections: list[str] = Field(default_factory=list)
    change_summary: str = ""
    accepted: bool = False
    created_at: datetime = Field(default_factory=_now)


class SimilarScript(BaseModel):
    """One historical script returned for a current topic.

    ``matched_on`` records why the item came back, so the caller can see that
    the match is a phrase or tag overlap rather than a semantic one.
    """

    model_config = ConfigDict(extra="forbid")

    content_id: str = Field(min_length=1)
    topic: str = ""
    title: str = ""
    script: str = ""
    platform: str = ""
    status: ContentStatus = "draft"
    publish_date: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    latest_metrics: ContentMetrics | None = None
    matched_on: list[str] = Field(default_factory=list)


__all__ = [
    "ContentItem",
    "ContentMetrics",
    "ContentStatus",
    "RevisionRecord",
    "SimilarScript",
    "TopicHistoryEntry",
]
