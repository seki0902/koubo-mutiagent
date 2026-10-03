"""Repository for the long-term content memory layer.

This is the data layer only.  It stores what happened across threads
(``content_items``, ``content_metrics``, ``topic_history``,
``revision_history``); the agent-facing memory tools land in later tasks.

The tables live in the Business SQLite file but stay a separate logical layer:
business identifiers such as ``draft_id`` and ``topic_id`` are stored as plain
values, not as foreign keys into the business tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_service.db.database import connect_database, initialize_database
from agent_service.schemas.memory import (
    ContentItem,
    ContentMetrics,
    RevisionRecord,
    TopicHistoryEntry,
)


_CONTENT_ITEM_COLUMNS = (
    "content_id, account_id, topic, title, script, content_type, platform, "
    "publish_date, status, tags_json, source_urls_json, hook, cta, "
    "created_at, updated_at"
)
_CONTENT_METRIC_COLUMNS = (
    "content_id, platform, impressions, views, likes, comments, shares, "
    "favorites, clicks, leads, valid_leads, ctr, conversion_rate, lead_rate, "
    "collected_at"
)
_TOPIC_HISTORY_COLUMNS = (
    "topic_id, topic, content_angle, source, selected, used, publish_date, "
    "performance_summary, tags_json, created_at"
)
_REVISION_COLUMNS = (
    "revision_id, draft_id, version_before, version_after, user_instruction, "
    "changed_sections_json, change_summary, accepted, created_at"
)


def _to_utc(value: datetime) -> datetime:
    """Normalise to UTC so stored text also compares correctly lexicographically."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_to_db(value: datetime | None) -> str | None:
    return _to_utc(value).isoformat() if value is not None else None


def _datetime_from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _append_range(
    clauses: list[str],
    parameters: list[Any],
    column: str,
    since: datetime | None,
    until: datetime | None,
) -> None:
    if since is not None:
        clauses.append(f"{column} >= ?")
        parameters.append(_to_utc(since).isoformat())
    if until is not None:
        clauses.append(f"{column} <= ?")
        parameters.append(_to_utc(until).isoformat())


def _append_limit(sql: str, parameters: list[Any], limit: int | None) -> str:
    if limit is None:
        return sql
    if limit < 1:
        raise ValueError("limit must be at least 1")
    parameters.append(limit)
    return f"{sql} LIMIT ?"


def _decode_list(value: Any, column: str) -> list[str]:
    if not isinstance(value, str):
        raise ValueError(f"stored {column} must be JSON text")
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"stored {column} must decode to a list")
    return [str(item) for item in decoded]


def _require_non_blank(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


class ContentMemoryStore:
    """Connection-scoped repository for long-term content memory."""

    def __init__(self, db_path: str | Path):
        self.connection = connect_database(db_path)
        initialize_database(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ContentMemoryStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- content

    def save_content_item(self, item: ContentItem) -> ContentItem:
        """Insert or replace one content item by ``content_id``."""

        _require_non_blank(item.content_id, "content_id")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO content_items
                    (content_id, account_id, topic, title, script, content_type,
                     platform, publish_date, status, tags_json,
                     source_urls_json, hook, cta, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    topic = excluded.topic,
                    title = excluded.title,
                    script = excluded.script,
                    content_type = excluded.content_type,
                    platform = excluded.platform,
                    publish_date = excluded.publish_date,
                    status = excluded.status,
                    tags_json = excluded.tags_json,
                    source_urls_json = excluded.source_urls_json,
                    hook = excluded.hook,
                    cta = excluded.cta,
                    updated_at = excluded.updated_at
                """,
                (
                    item.content_id,
                    item.account_id,
                    item.topic,
                    item.title,
                    item.script,
                    item.content_type,
                    item.platform,
                    _datetime_to_db(item.publish_date),
                    item.status,
                    json.dumps(item.tags, ensure_ascii=False),
                    json.dumps(item.source_urls, ensure_ascii=False),
                    item.hook,
                    item.cta,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )
        saved = self.get_content_item(item.content_id)
        if saved is None:
            raise RuntimeError("content item disappeared after commit")
        return saved

    def get_content_item(self, content_id: str) -> ContentItem | None:
        row = self.connection.execute(
            f"SELECT {_CONTENT_ITEM_COLUMNS} FROM content_items "
            "WHERE content_id = ?",
            (content_id,),
        ).fetchone()
        return self._to_content_item(row) if row is not None else None

    def list_content_items(
        self,
        *,
        account_id: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[ContentItem]:
        """Return items newest first, narrowed by account, status or creation time.

        ``since``/``until`` bound ``created_at`` — when the record entered
        memory.  ``publish_date`` is nullable and is therefore not used for
        range filtering.
        """

        clauses: list[str] = []
        parameters: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            parameters.append(account_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        _append_range(clauses, parameters, "created_at", since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_CONTENT_ITEM_COLUMNS} FROM content_items {where} "
            "ORDER BY created_at DESC, rowid DESC"
        )
        sql = _append_limit(sql, parameters, limit)
        rows = self.connection.execute(sql, tuple(parameters)).fetchall()
        return [self._to_content_item(row) for row in rows]

    # ---------------------------------------------------------------- metrics

    def save_content_metrics(self, metrics: ContentMetrics) -> ContentMetrics:
        """Append one collected snapshot for a content item."""

        _require_non_blank(metrics.content_id, "content_id")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO content_metrics
                    (content_id, platform, impressions, views, likes, comments,
                     shares, favorites, clicks, leads, valid_leads, ctr,
                     conversion_rate, lead_rate, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metrics.content_id,
                    metrics.platform,
                    metrics.impressions,
                    metrics.views,
                    metrics.likes,
                    metrics.comments,
                    metrics.shares,
                    metrics.favorites,
                    metrics.clicks,
                    metrics.leads,
                    metrics.valid_leads,
                    metrics.ctr,
                    metrics.conversion_rate,
                    metrics.lead_rate,
                    metrics.collected_at.isoformat(),
                ),
            )
        saved = self.get_content_metrics(
            metrics.content_id, metrics.platform, metrics.collected_at
        )
        if saved is None:
            raise RuntimeError("content metrics disappeared after commit")
        return saved

    def get_content_metrics(
        self, content_id: str, platform: str, collected_at: datetime
    ) -> ContentMetrics | None:
        row = self.connection.execute(
            f"SELECT {_CONTENT_METRIC_COLUMNS} FROM content_metrics "
            "WHERE content_id = ? AND platform = ? AND collected_at = ?",
            (content_id, platform, collected_at.isoformat()),
        ).fetchone()
        return self._to_content_metrics(row) if row is not None else None

    def list_content_metrics(self, content_id: str) -> list[ContentMetrics]:
        """Return every snapshot for one item, oldest collection first."""

        rows = self.connection.execute(
            f"SELECT {_CONTENT_METRIC_COLUMNS} FROM content_metrics "
            "WHERE content_id = ? ORDER BY collected_at ASC, rowid ASC",
            (content_id,),
        ).fetchall()
        return [self._to_content_metrics(row) for row in rows]

    def get_latest_content_metrics(self, content_id: str) -> ContentMetrics | None:
        """Return the most recently collected snapshot for one item."""

        row = self.connection.execute(
            f"SELECT {_CONTENT_METRIC_COLUMNS} FROM content_metrics "
            "WHERE content_id = ? ORDER BY collected_at DESC, rowid DESC LIMIT 1",
            (content_id,),
        ).fetchone()
        return self._to_content_metrics(row) if row is not None else None

    # ----------------------------------------------------------- topic history

    def save_topic_history(self, entry: TopicHistoryEntry) -> TopicHistoryEntry:
        """Insert or replace one topic record by ``topic_id``."""

        _require_non_blank(entry.topic_id, "topic_id")
        _require_non_blank(entry.topic, "topic")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO topic_history
                    (topic_id, topic, content_angle, source, selected, used,
                     publish_date, performance_summary, tags_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    topic = excluded.topic,
                    content_angle = excluded.content_angle,
                    source = excluded.source,
                    selected = excluded.selected,
                    used = excluded.used,
                    publish_date = excluded.publish_date,
                    performance_summary = excluded.performance_summary,
                    tags_json = excluded.tags_json
                """,
                (
                    entry.topic_id,
                    entry.topic,
                    entry.content_angle,
                    entry.source,
                    1 if entry.selected else 0,
                    1 if entry.used else 0,
                    _datetime_to_db(entry.publish_date),
                    entry.performance_summary,
                    json.dumps(entry.tags, ensure_ascii=False),
                    entry.created_at.isoformat(),
                ),
            )
        saved = self.get_topic_history(entry.topic_id)
        if saved is None:
            raise RuntimeError("topic history disappeared after commit")
        return saved

    def get_topic_history(self, topic_id: str) -> TopicHistoryEntry | None:
        row = self.connection.execute(
            f"SELECT {_TOPIC_HISTORY_COLUMNS} FROM topic_history "
            "WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        return self._to_topic_history(row) if row is not None else None

    def list_topic_history(
        self,
        *,
        only_used: bool = False,
        only_selected: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[TopicHistoryEntry]:
        """Return topics newest first, optionally only used/selected ones."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if only_used:
            clauses.append("used = 1")
        if only_selected:
            clauses.append("selected = 1")
        _append_range(clauses, parameters, "created_at", since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_TOPIC_HISTORY_COLUMNS} FROM topic_history {where} "
            "ORDER BY created_at DESC, rowid DESC"
        )
        sql = _append_limit(sql, parameters, limit)
        rows = self.connection.execute(sql, tuple(parameters)).fetchall()
        return [self._to_topic_history(row) for row in rows]

    # -------------------------------------------------------- revision history

    def save_revision_record(self, record: RevisionRecord) -> RevisionRecord:
        """Record one user-driven revision instruction."""

        _require_non_blank(record.revision_id, "revision_id")
        _require_non_blank(record.draft_id, "draft_id")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO revision_history
                    (revision_id, draft_id, version_before, version_after,
                     user_instruction, changed_sections_json, change_summary,
                     accepted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.revision_id,
                    record.draft_id,
                    record.version_before,
                    record.version_after,
                    record.user_instruction,
                    json.dumps(record.changed_sections, ensure_ascii=False),
                    record.change_summary,
                    1 if record.accepted else 0,
                    record.created_at.isoformat(),
                ),
            )
        saved = self.get_revision_record(record.revision_id)
        if saved is None:
            raise RuntimeError("revision record disappeared after commit")
        return saved

    def get_revision_record(self, revision_id: str) -> RevisionRecord | None:
        row = self.connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM revision_history "
            "WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        return self._to_revision_record(row) if row is not None else None

    def list_revision_history(
        self,
        draft_id: str | None = None,
        *,
        accepted: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[RevisionRecord]:
        """Return revision instructions oldest first.

        Without ``draft_id`` this returns every recorded instruction, which is
        how the long-term layer answers "how does this user usually ask for
        changes" across drafts.
        """

        clauses: list[str] = []
        parameters: list[Any] = []
        if draft_id is not None:
            clauses.append("draft_id = ?")
            parameters.append(draft_id)
        if accepted is not None:
            clauses.append("accepted = ?")
            parameters.append(1 if accepted else 0)
        _append_range(clauses, parameters, "created_at", since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_REVISION_COLUMNS} FROM revision_history {where} "
            "ORDER BY created_at ASC, rowid ASC"
        )
        sql = _append_limit(sql, parameters, limit)
        rows = self.connection.execute(sql, tuple(parameters)).fetchall()
        return [self._to_revision_record(row) for row in rows]

    # ------------------------------------------------------------------ rows

    def _to_content_item(self, row: sqlite3.Row) -> ContentItem:
        return ContentItem(
            content_id=str(row["content_id"]),
            account_id=str(row["account_id"]),
            topic=str(row["topic"]),
            title=str(row["title"]),
            script=str(row["script"]),
            content_type=str(row["content_type"]),
            platform=str(row["platform"]),
            publish_date=_datetime_from_db(row["publish_date"]),
            status=str(row["status"]),
            tags=_decode_list(row["tags_json"], "tags_json"),
            source_urls=_decode_list(row["source_urls_json"], "source_urls_json"),
            hook=str(row["hook"]),
            cta=str(row["cta"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _to_content_metrics(self, row: sqlite3.Row) -> ContentMetrics:
        return ContentMetrics(
            content_id=str(row["content_id"]),
            platform=str(row["platform"]),
            impressions=int(row["impressions"]),
            views=int(row["views"]),
            likes=int(row["likes"]),
            comments=int(row["comments"]),
            shares=int(row["shares"]),
            favorites=int(row["favorites"]),
            clicks=int(row["clicks"]),
            leads=int(row["leads"]),
            valid_leads=int(row["valid_leads"]),
            ctr=row["ctr"],
            conversion_rate=row["conversion_rate"],
            lead_rate=row["lead_rate"],
            collected_at=datetime.fromisoformat(str(row["collected_at"])),
        )

    def _to_topic_history(self, row: sqlite3.Row) -> TopicHistoryEntry:
        return TopicHistoryEntry(
            topic_id=str(row["topic_id"]),
            topic=str(row["topic"]),
            content_angle=str(row["content_angle"]),
            source=str(row["source"]),
            selected=bool(row["selected"]),
            used=bool(row["used"]),
            publish_date=_datetime_from_db(row["publish_date"]),
            performance_summary=str(row["performance_summary"]),
            tags=_decode_list(row["tags_json"], "tags_json"),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _to_revision_record(self, row: sqlite3.Row) -> RevisionRecord:
        version_after = row["version_after"]
        return RevisionRecord(
            revision_id=str(row["revision_id"]),
            draft_id=str(row["draft_id"]),
            version_before=int(row["version_before"]),
            version_after=None if version_after is None else int(version_after),
            user_instruction=str(row["user_instruction"]),
            changed_sections=_decode_list(
                row["changed_sections_json"], "changed_sections_json"
            ),
            change_summary=str(row["change_summary"]),
            accepted=bool(row["accepted"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )


__all__ = ["ContentMemoryStore"]
