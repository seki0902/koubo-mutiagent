"""Small repository for the Business SQLite Schema V1 tables.

Candidate batches are persisted as one transaction so the visible
``display_index`` mapping and each stable ``topic_id`` are created together.
Authorization and provider submission decisions live in services; this module
persists their authorization and generation records.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_service.db.database import connect_database, initialize_database
from agent_service.schemas.topic import TopicResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_list(values: Iterable[str] | None) -> str:
    return json.dumps(list(values or ()), ensure_ascii=False)


def _require_non_blank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


class TaskStore:
    """Connection-scoped repository backed by the Business SQLite database."""

    def __init__(self, db_path: str | Path):
        self.connection = connect_database(db_path)
        initialize_database(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def create_content_task(
        self,
        task_id: str,
        session_id: str,
        *,
        status: str = "active",
        current_goal: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO content_tasks
                    (task_id, session_id, status, current_goal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, status, current_goal, now, now),
            )
        return self.get_content_task(task_id)  # type: ignore[return-value]

    def get_content_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM content_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_candidate_batch(
        self, candidate_batch_id: str, task_id: str, *, summary: str = ""
    ) -> dict[str, Any]:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO candidate_batches
                    (candidate_batch_id, task_id, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (candidate_batch_id, task_id, summary, _now()),
            )
        row = self.connection.execute(
            "SELECT * FROM candidate_batches WHERE candidate_batch_id = ?",
            (candidate_batch_id,),
        ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    def get_candidate_batch(self, candidate_batch_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM candidate_batches WHERE candidate_batch_id = ?",
            (candidate_batch_id,),
        ).fetchone()
        return dict(row) if row else None

    def save_candidate_batch(
        self, task_id: str, result: TopicResult
    ) -> dict[str, Any]:
        """Persist one candidate batch and its display mapping atomically.

        The order of ``result.topic_candidates`` is treated as generation
        order only. The user-facing order is exclusively the persisted
        ``display_index`` value, so a later resolver can safely answer
        ``get_candidate_by_display_index(batch_id, 3)`` without indexing a
        Python list.
        """

        if not task_id.strip():
            raise ValueError("task_id must not be blank")
        if not result.candidate_batch_id.strip():
            raise ValueError("candidate_batch_id must not be blank")

        display_indexes = [
            candidate.display_index for candidate in result.topic_candidates
        ]
        if len(display_indexes) != len(set(display_indexes)):
            raise ValueError("display_index must be unique within a candidate batch")

        topic_ids = [candidate.topic_id for candidate in result.topic_candidates]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("topic_id must be unique within a candidate batch")

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO candidate_batches
                    (candidate_batch_id, task_id, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    result.candidate_batch_id,
                    task_id,
                    result.summary,
                    _now(),
                ),
            )
            for candidate in result.topic_candidates:
                self.connection.execute(
                    """
                    INSERT INTO candidate_items
                        (topic_id, candidate_batch_id, display_index, title, why_now,
                         target_audience, content_angle, key_points_json, sources_json,
                         freshness, risks_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.topic_id,
                        result.candidate_batch_id,
                        candidate.display_index,
                        candidate.title,
                        candidate.why_now,
                        candidate.target_audience,
                        candidate.content_angle,
                        _json_list(candidate.key_points),
                        _json_list(candidate.sources),
                        candidate.freshness,
                        _json_list(candidate.risks),
                        _now(),
                    ),
                )

        return self.get_candidate_batch(result.candidate_batch_id)  # type: ignore[return-value]

    def list_candidate_items(self, candidate_batch_id: str) -> list[dict[str, Any]]:
        """Return one batch in the persisted user-facing display order."""

        rows = self.connection.execute(
            """
            SELECT * FROM candidate_items
            WHERE candidate_batch_id = ?
            ORDER BY display_index ASC
            """,
            (candidate_batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_candidate_item(
        self,
        candidate_batch_id: str,
        topic_id: str,
        display_index: int,
        title: str,
        *,
        why_now: str = "",
        target_audience: str = "",
        content_angle: str = "",
        key_points: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        freshness: str = "",
        risks: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO candidate_items
                    (topic_id, candidate_batch_id, display_index, title, why_now,
                     target_audience, content_angle, key_points_json, sources_json,
                     freshness, risks_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic_id,
                    candidate_batch_id,
                    display_index,
                    title,
                    why_now,
                    target_audience,
                    content_angle,
                    _json_list(key_points),
                    _json_list(sources),
                    freshness,
                    _json_list(risks),
                    _now(),
                ),
            )
        return self.get_candidate_by_topic_id(topic_id)  # type: ignore[return-value]

    def get_candidate_by_display_index(
        self, candidate_batch_id: str, display_index: int
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM candidate_items
            WHERE candidate_batch_id = ? AND display_index = ?
            """,
            (candidate_batch_id, display_index),
        ).fetchone()
        return dict(row) if row else None

    def get_candidate_by_topic_id(self, topic_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM candidate_items WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_draft(
        self,
        draft_id: str,
        task_id: str,
        topic_id: str,
        body: str | None = None,
        *,
        current_version: int = 0,
        change_instruction: str = "",
        created_by: str = "system",
    ) -> dict[str, Any]:
        """Create a draft record, optionally with its first version.

        The no-body form remains available as a low-level compatibility
        primitive for callers that need to insert a draft and version
        separately.  The normal creation path supplies ``body`` and writes
        the draft plus immutable v1 in one transaction.
        """

        if body is not None:
            return self.create_draft_with_version(
                draft_id,
                task_id,
                topic_id,
                body,
                change_instruction=change_instruction,
                created_by=created_by,
            )

        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO drafts
                    (draft_id, task_id, topic_id, current_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (draft_id, task_id, topic_id, current_version, now, now),
            )
        row = self.connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    def create_draft_with_version(
        self,
        draft_id: str,
        task_id: str,
        topic_id: str,
        body: str,
        *,
        change_instruction: str = "",
        created_by: str = "system",
    ) -> dict[str, Any]:
        """Create a draft and commit its immutable v1 atomically."""

        _require_non_blank(body, "body")
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO drafts
                    (draft_id, task_id, topic_id, current_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (draft_id, task_id, topic_id, 1, now, now),
            )
            self.connection.execute(
                """
                INSERT INTO draft_versions
                    (draft_id, version, body, change_instruction, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (draft_id, 1, body, change_instruction, created_by, now),
            )
        return self.get_draft(draft_id)  # type: ignore[return-value]

    def create_draft_version(
        self,
        draft_id: str,
        version: int,
        body: str,
        *,
        change_instruction: str = "",
        created_by: str = "system",
    ) -> dict[str, Any]:
        _require_non_blank(body, "body")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO draft_versions
                    (draft_id, version, body, change_instruction, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (draft_id, version, body, change_instruction, created_by, _now()),
            )
            # Keep the pointer monotonic for callers that use this explicit
            # version API.  The INSERT happens first, so a duplicate version
            # is rejected by the database and cannot move the pointer.
            self.connection.execute(
                """
                UPDATE drafts
                SET current_version = CASE
                        WHEN current_version < ? THEN ?
                        ELSE current_version
                    END,
                    updated_at = CASE
                        WHEN current_version < ? THEN ?
                        ELSE updated_at
                    END
                WHERE draft_id = ?
                """,
                (version, version, version, _now(), draft_id),
            )
        row = self.connection.execute(
            """
            SELECT * FROM draft_versions
            WHERE draft_id = ? AND version = ?
            """,
            (draft_id, version),
        ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    def save_draft_version(
        self,
        draft_id: str,
        body: str,
        *,
        change_instruction: str = "",
        created_by: str = "system",
        invalidate_authorizations: bool = False,
    ) -> dict[str, Any]:
        """Append the next immutable version and then advance the pointer.

        The INSERT and pointer UPDATE share one SQLite transaction.  If the
        version cannot be committed, the current pointer remains unchanged.

        The next version is reserved with a single conditional UPDATE rather
        than read-then-written: two writers that read the same
        ``current_version`` would otherwise both try to insert it, and the
        loser would surface an integrity error instead of the next version.
        """

        _require_non_blank(body, "body")
        now = _now()
        with self.connection:
            draft_row = self.connection.execute(
                "SELECT current_version FROM drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if draft_row is None:
                raise ValueError(f"unknown draft_id: {draft_id}")

            reserved = self.connection.execute(
                """
                UPDATE drafts
                SET current_version = current_version + 1, updated_at = ?
                WHERE draft_id = ?
                RETURNING current_version
                """,
                (now, draft_id),
            ).fetchone()
            if reserved is None:
                raise ValueError(f"unknown draft_id: {draft_id}")
            next_version = int(reserved["current_version"])

            self.connection.execute(
                """
                INSERT INTO draft_versions
                    (draft_id, version, body, change_instruction, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    next_version,
                    body,
                    change_instruction,
                    created_by,
                    now,
                ),
            )
            if invalidate_authorizations:
                self.connection.execute(
                    """
                    UPDATE generation_authorizations
                    SET status = 'revoked', revoked_at = ?
                    WHERE draft_id = ?
                      AND draft_version < ?
                      AND status = 'authorized'
                    """,
                    (now, draft_id, next_version),
                )

        return self.get_draft_version(draft_id, next_version)  # type: ignore[return-value]

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_current_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Return the version selected by the draft's committed pointer."""

        draft = self.get_draft(draft_id)
        if draft is None or int(draft["current_version"]) < 1:
            return None
        return self.get_draft_version(draft_id, int(draft["current_version"]))

    def get_draft_version(self, draft_id: str, version: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM draft_versions
            WHERE draft_id = ? AND version = ?
            """,
            (draft_id, version),
        ).fetchone()
        return dict(row) if row else None

    def create_generation_authorization(
        self,
        authorization_id: str,
        task_id: str,
        draft_id: str,
        draft_version: int,
        params_hash: str,
        *,
        status: str = "authorized",
    ) -> dict[str, Any]:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO generation_authorizations
                    (authorization_id, task_id, draft_id, draft_version,
                     params_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization_id,
                    task_id,
                    draft_id,
                    draft_version,
                    params_hash,
                    status,
                    _now(),
                ),
            )
        row = self.connection.execute(
            """
            SELECT * FROM generation_authorizations
            WHERE authorization_id = ?
            """,
            (authorization_id,),
        ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    def revoke_generation_authorizations(
        self, draft_id: str, *, before_version: int
    ) -> int:
        """Revoke authorized generations for versions superseded by a draft."""

        if before_version < 1:
            raise ValueError("before_version must be at least 1")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE generation_authorizations
                SET status = 'revoked', revoked_at = ?
                WHERE draft_id = ?
                  AND draft_version < ?
                  AND status = 'authorized'
                """,
                (_now(), draft_id, before_version),
            )
        return int(cursor.rowcount)

    def get_generation_authorization(
        self, authorization_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM generation_authorizations
            WHERE authorization_id = ?
            """,
            (authorization_id,),
        ).fetchone()
        return dict(row) if row else None

    def create_generation_record(
        self,
        generation_id: str,
        task_id: str,
        authorization_id: str,
        draft_id: str,
        draft_version: int,
        idempotency_key: str,
        *,
        status: str = "pending",
        progress: int = 0,
        provider_task_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO generation_records
                    (generation_id, task_id, authorization_id, draft_id, draft_version,
                     idempotency_key, provider_task_id, status, progress,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    task_id,
                    authorization_id,
                    draft_id,
                    draft_version,
                    idempotency_key,
                    provider_task_id,
                    status,
                    progress,
                    now,
                    now,
                ),
            )
        return self.get_generation_by_idempotency_key(idempotency_key)  # type: ignore[return-value]

    def get_generation_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM generation_records
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None

    def list_generation_records_for_task(self, task_id: str) -> list[dict[str, Any]]:
        """Return one task's generation records, newest first.

        The thread pointer names the current submission; this history answers a
        refreshed page that asks what else this task has already generated.
        """

        rows = self.connection.execute(
            """
            SELECT * FROM generation_records
            WHERE task_id = ?
            ORDER BY rowid DESC
            """,
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        """Return one generation record by its stable local identifier."""

        row = self.connection.execute(
            """
            SELECT * FROM generation_records
            WHERE generation_id = ?
            """,
            (generation_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_generation_submission(
        self,
        generation_id: str,
        *,
        status: str,
        provider_task_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Persist the provider submission outcome for one generation.

        The generation row is reserved before the provider call.  Therefore a
        timeout can be recorded as ``unknown_submission`` while keeping the
        same idempotency key available for later reads, instead of allowing a
        retry to create a second provider task.
        """

        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE generation_records
                SET provider_task_id = ?,
                    status = ?,
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE generation_id = ?
                """,
                (
                    provider_task_id,
                    status,
                    error_code,
                    error_message,
                    now,
                    generation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"generation not found: {generation_id}")
        return self.get_generation(generation_id)  # type: ignore[return-value]
