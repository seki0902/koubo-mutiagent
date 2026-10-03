"""Repository for draft-version-bound review records."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_service.db.database import connect_database, initialize_database
from agent_service.schemas.review import ReviewResult


_REVIEW_COLUMNS = (
    "review_id, task_id, draft_id, draft_version, status, summary, "
    "issues_json, must_fix_json, risk_items_json, revision_request, "
    "research_request, ready_for_generation, created_at"
)


def _decode_list(value: Any) -> list[str]:
    if not isinstance(value, str):
        raise ValueError("stored review list column must be JSON text")
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError("stored review list column must decode to a list")
    return [str(item) for item in decoded]


class ReviewStore:
    """Connection-scoped repository backed by the Business SQLite database.

    The table enforces the version binding at the storage layer: a row whose
    ``(draft_id, draft_version)`` pair does not exist in ``draft_versions`` is
    rejected by the foreign key, so a review can never describe a body that was
    never committed.
    """

    def __init__(self, db_path: str | Path):
        self.connection = connect_database(db_path)
        initialize_database(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def save_review(self, task_id: str, review: ReviewResult) -> ReviewResult:
        """Persist one review verdict bound to its exact draft version."""

        if not task_id.strip():
            raise ValueError("task_id must not be blank")

        with self.connection:
            # ``task_id`` is denormalised for cheap per-task reads, so it can
            # disagree with the draft's real owner.  Reject that before the
            # row exists rather than trusting the caller.
            owner = self.connection.execute(
                "SELECT task_id FROM drafts WHERE draft_id = ?",
                (review.draft_id,),
            ).fetchone()
            if owner is not None and str(owner["task_id"]) != task_id:
                raise ValueError(
                    f"draft {review.draft_id} belongs to task "
                    f"{owner['task_id']}, not {task_id}"
                )

            self.connection.execute(
                """
                INSERT INTO reviews (review_id, task_id, draft_id, draft_version,
                                     status, summary, issues_json, must_fix_json,
                                     risk_items_json, revision_request,
                                     research_request, ready_for_generation,
                                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    task_id,
                    review.draft_id,
                    review.draft_version,
                    review.status,
                    review.summary,
                    json.dumps(review.issues, ensure_ascii=False),
                    json.dumps(review.must_fix, ensure_ascii=False),
                    json.dumps(review.risk_items, ensure_ascii=False),
                    review.revision_request,
                    review.research_request,
                    1 if review.ready_for_generation else 0,
                    review.created_at.isoformat(),
                ),
            )

        saved = self.get_review(review.review_id)
        if saved is None:
            raise RuntimeError("review disappeared after commit")
        return saved

    def create_review(self, task_id: str, review: ReviewResult) -> ReviewResult:
        """Descriptive alias for callers treating persistence as creation."""

        return self.save_review(task_id, review)

    def get_review(self, review_id: str) -> ReviewResult | None:
        row = self.connection.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        return self._to_model(row) if row is not None else None

    def list_reviews_for_draft(self, draft_id: str) -> list[ReviewResult]:
        """Return every verdict ever recorded for a draft, oldest first.

        Older versions are kept as history; callers must not treat the last
        row of this list as the verdict for a newer version.
        """

        rows = self.connection.execute(
            f"""
            SELECT {_REVIEW_COLUMNS} FROM reviews
            WHERE draft_id = ?
            ORDER BY draft_version ASC, rowid ASC
            """,
            (draft_id,),
        ).fetchall()
        return [self._to_model(row) for row in rows]

    def get_active_review(self, draft_id: str) -> ReviewResult | None:
        """Return the verdict that still describes the draft's current version.

        A verdict is only active while it describes the version the draft
        currently points at.  Once a revision advances the version, the older
        verdict stays readable through ``list_reviews_for_draft`` as history
        but is never returned here, so a superseded ``pass`` can never be
        reported as the current state of the draft.
        """

        row = self.connection.execute(
            "SELECT current_version FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            return None
        current_version = int(row["current_version"])
        if current_version < 1:
            return None
        return self.get_latest_review_for_version(draft_id, current_version)

    def is_review_current(self, review: ReviewResult) -> bool:
        """Whether this verdict still describes the draft's current version."""

        active = self.get_active_review(review.draft_id)
        return active is not None and active.review_id == review.review_id

    def get_latest_review_for_version(
        self, draft_id: str, draft_version: int
    ) -> ReviewResult | None:
        """Return the newest verdict recorded for exactly this version."""

        row = self.connection.execute(
            f"""
            SELECT {_REVIEW_COLUMNS} FROM reviews
            WHERE draft_id = ? AND draft_version = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (draft_id, draft_version),
        ).fetchone()
        return self._to_model(row) if row is not None else None

    def _to_model(self, row: sqlite3.Row) -> ReviewResult:
        return ReviewResult(
            review_id=str(row["review_id"]),
            draft_id=str(row["draft_id"]),
            draft_version=int(row["draft_version"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            issues=_decode_list(row["issues_json"]),
            must_fix=_decode_list(row["must_fix_json"]),
            risk_items=_decode_list(row["risk_items_json"]),
            revision_request=row["revision_request"],
            research_request=row["research_request"],
            ready_for_generation=bool(row["ready_for_generation"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )


__all__ = ["ReviewStore"]
