"""Deterministic long-term memory read and write tools.

The read tools return historical facts.  They never decide whether a topic is
worth doing, whether a script was good, or whether something should be made
again — that judgement belongs to the agents.

Similarity is phrase and tag overlap, not semantic search: the first version
stays on structured SQLite queries (document 13.5.16) and every result carries
``matched_on`` so the caller can see why it came back.

The write tools are the long-term memory boundary.  Only stable, reusable
information belongs in this layer, so they refuse candidates, unadopted drafts,
execution-time commentary and performance data for unpublished content.  The
store below them stays permissive; the policy lives here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from agent_service.schemas.memory import (
    ContentItem,
    ContentMetrics,
    RevisionRecord,
    SimilarScript,
    TopicHistoryEntry,
)
from agent_service.services.content_memory_store import ContentMemoryStore


class MemoryWriteRejected(ValueError):
    """Raised when a write would put non-reusable data into long-term memory."""


# A content item only becomes a long-term asset once it has been adopted.
# ``draft`` therefore never reaches this layer.
_ADOPTED_CONTENT_STATUSES = frozenset({"scheduled", "published", "archived"})
# Performance data exists only for content that actually went out.
_MEASURABLE_CONTENT_STATUSES = frozenset({"published", "archived"})


def _normalize(value: str) -> str:
    """Lowercase and drop whitespace so phrase matching ignores formatting."""

    return "".join(value.lower().split())


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for tag in tags:
        cleaned = _normalize(tag)
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _require_positive_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")


def _match_signals(item: ContentItem, topic: str, tags: list[str]) -> list[str]:
    """Return the topic phrases and tags that link an item to the query."""

    signals: list[str] = []
    query = _normalize(topic)
    if query:
        item_topic = _normalize(item.topic)
        item_title = _normalize(item.title)
        if item_topic and (query in item_topic or item_topic in query):
            signals.append(f"topic:{item.topic}")
        if item_title and (query in item_title or item_title in query):
            signals.append(f"title:{item.title}")
    item_tags = _normalize_tags(item.tags)
    for tag in tags:
        if tag in item_tags:
            signals.append(f"tag:{tag}")
    return signals


def get_recent_topics(
    store: ContentMemoryStore,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    only_used: bool = False,
    only_selected: bool = False,
    limit: int = 10,
) -> list[TopicHistoryEntry]:
    """Return recently recorded topics, newest first.

    ``since``/``until`` bound the time the topic entered memory.
    """

    _require_positive_limit(limit)
    return store.list_topic_history(
        only_used=only_used,
        only_selected=only_selected,
        since=since,
        until=until,
        limit=limit,
    )


def get_similar_scripts(
    store: ContentMemoryStore,
    *,
    topic: str,
    account_id: str | None = None,
    tags: Iterable[str] | None = None,
    since: datetime | None = None,
    limit: int = 5,
) -> list[SimilarScript]:
    """Return historical content whose topic phrase or tags overlap ``topic``.

    The database narrows by account and creation time; phrase/tag matching and
    ranking happen in Python over that bounded set, because the first version
    deliberately has no embedding index.
    """

    if not topic.strip():
        raise ValueError("topic must not be blank")
    _require_positive_limit(limit)

    query_tags = _normalize_tags(tags or [])
    candidates = store.list_content_items(account_id=account_id, since=since)

    ranked: list[tuple[int, datetime, ContentItem, list[str]]] = []
    for item in candidates:
        signals = _match_signals(item, topic, query_tags)
        if signals:
            ranked.append((len(signals), item.created_at, item, signals))
    # Strongest match first; ties fall back to the most recent record and then
    # to the stable content id, so the order is reproducible.
    ranked.sort(key=lambda entry: (-entry[0], -entry[1].timestamp(), entry[2].content_id))

    return [
        SimilarScript(
            content_id=item.content_id,
            topic=item.topic,
            title=item.title,
            script=item.script,
            platform=item.platform,
            status=item.status,
            publish_date=item.publish_date,
            tags=item.tags,
            latest_metrics=store.get_latest_content_metrics(item.content_id),
            matched_on=signals,
        )
        for _, _, item, signals in ranked[:limit]
    ]


def get_revision_history(
    store: ContentMemoryStore,
    *,
    draft_id: str | None = None,
    accepted: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
) -> list[RevisionRecord]:
    """Return how the user asked for changes, oldest first.

    Without ``draft_id`` this covers every draft, which is the form the Script
    Agent needs in order to stop repeating the same mistake.
    """

    if draft_id is not None and not draft_id.strip():
        raise ValueError("draft_id must not be blank when provided")
    _require_positive_limit(limit)
    return store.list_revision_history(
        draft_id,
        accepted=accepted,
        since=since,
        until=until,
        limit=limit,
    )


def save_content_item(
    store: ContentMemoryStore,
    item: ContentItem,
) -> ContentItem:
    """Store one content item that has actually been adopted.

    A candidate or an unadopted draft is not a long-term asset, and a content
    item without its adopted script has nothing reusable to keep, so both are
    refused here.
    """

    if item.status not in _ADOPTED_CONTENT_STATUSES:
        allowed = ", ".join(sorted(_ADOPTED_CONTENT_STATUSES))
        raise MemoryWriteRejected(
            f"content {item.content_id} is {item.status}: only adopted content "
            f"({allowed}) belongs in long-term memory"
        )
    if not item.script.strip():
        raise MemoryWriteRejected(
            f"content {item.content_id} has no script: a long-term content item "
            "must carry the script that was adopted"
        )
    return store.save_content_item(item)


def save_revision_record(
    store: ContentMemoryStore,
    record: RevisionRecord,
) -> RevisionRecord:
    """Record one explicit user revision instruction.

    Only an explicit user request is long-term feedback; model guesses and
    intermediate reasoning are not.  An accepted revision has to name the
    version it produced, and a rejected one must not claim a new version.
    """

    if not record.user_instruction.strip():
        raise MemoryWriteRejected(
            "only an explicit user instruction is long-term revision feedback"
        )
    if record.accepted:
        if record.version_after is None:
            raise MemoryWriteRejected(
                "an accepted revision must name the version it produced"
            )
        if record.version_after <= record.version_before:
            raise MemoryWriteRejected(
                f"an accepted revision must advance the version: "
                f"{record.version_before} -> {record.version_after}"
            )
    elif record.version_after is not None:
        raise MemoryWriteRejected(
            f"revision {record.revision_id} was not accepted and must not "
            f"claim version {record.version_after}"
        )
    return store.save_revision_record(record)


def update_content_metrics(
    store: ContentMemoryStore,
    metrics: ContentMetrics,
) -> ContentMetrics:
    """Append one collected performance snapshot for a published item.

    Snapshots are append-only: collecting again at a later time adds a row, and
    re-collecting the same instant is rejected rather than silently overwriting
    the earlier numbers.
    """

    item = store.get_content_item(metrics.content_id)
    if item is None:
        raise MemoryWriteRejected(
            f"unknown content_id: {metrics.content_id} is not in long-term memory"
        )
    if item.status not in _MEASURABLE_CONTENT_STATUSES:
        allowed = ", ".join(sorted(_MEASURABLE_CONTENT_STATUSES))
        raise MemoryWriteRejected(
            f"content {metrics.content_id} is {item.status}: performance data "
            f"can only be collected for {allowed} content"
        )
    return store.save_content_metrics(metrics)


__all__ = [
    "MemoryWriteRejected",
    "get_recent_topics",
    "get_revision_history",
    "get_similar_scripts",
    "save_content_item",
    "save_revision_record",
    "update_content_metrics",
]
