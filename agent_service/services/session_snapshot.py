"""Read one session's current business state without running the graph.

The right-hand panel has to survive a page refresh, and a refresh has no
stream to replay: the events already happened and the browser was not there.
This module answers the same questions from where the truth lives -- the
LangGraph checkpointer holds the thread's pointers, and the Business SQLite
database holds the objects those pointers name.

It is deliberately read-only.  Nothing here writes, emits, or mutates a
pointer, so calling it can never change what the next turn will do.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from agent_service.runtime.checkpointer import open_checkpointer, thread_config
from agent_service.services.research_store import ResearchStore
from agent_service.services.review_store import ReviewStore
from agent_service.services.task_store import TaskStore

__all__ = ["SessionSnapshotError", "read_session_snapshot"]


class SessionSnapshotError(RuntimeError):
    """The session cannot be reported as a snapshot."""


def _task_id(session_id: str) -> str:
    """The task identity for a session, matching the Supervisor's own naming."""

    return f"task_{session_id.strip()}"


def _json_ready(value: Any) -> Any:
    """Convert SQLite values into JSON-compatible data."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return one row as a JSON-ready dict, or ``None`` when there is no row."""

    if row is None:
        return None
    return {str(key): _json_ready(value) for key, value in dict(row).items()}


def _decode_list(value: Any) -> list[str]:
    """Read a ``*_json`` column back into a list of strings."""

    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
    return []


def _candidate_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Render one candidate with its persisted display mapping and lists decoded."""

    payload = {str(key): _json_ready(value) for key, value in dict(row).items()}
    payload["key_points"] = _decode_list(row.get("key_points_json"))
    payload["sources"] = _decode_list(row.get("sources_json"))
    payload["risks"] = _decode_list(row.get("risks_json"))
    return payload


def _draft_payload(
    task_store: TaskStore, draft_id: str | None
) -> dict[str, Any] | None:
    """Return the current draft version, body included, or ``None``."""

    if not draft_id:
        return None
    draft = task_store.get_draft(draft_id)
    if draft is None:
        return None
    current = task_store.get_current_draft(draft_id)
    version = int(current["version"]) if current is not None else None

    versions: list[dict[str, Any]] = []
    if version:
        rows = task_store.connection.execute(
            "SELECT version, created_at FROM draft_versions"
            " WHERE draft_id = ? ORDER BY version ASC",
            (draft_id,),
        ).fetchall()
        versions = [
            {"version": int(row["version"]), "created_at": _json_ready(row["created_at"])}
            for row in rows
        ]

    return {
        "draft_id": draft_id,
        "topic_id": draft.get("topic_id"),
        "current_version": version,
        "body": current["body"] if current is not None else None,
        "version_count": len(versions) or (1 if version else 0),
        "versions": versions,
    }


def _message_text(message: Any) -> str:
    """Read one checkpointed message's text, tolerating structured content."""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(part.strip() for part in parts if part.strip()).strip()
    return ""


#: How much of the conversation a refresh restores. Enough to keep reading the
#: current exchange, bounded so the snapshot stays small.
TRANSCRIPT_LIMIT = 40


def _transcript_payload(messages: Any) -> list[dict[str, str]]:
    """Return the conversation as the chat page renders it.

    Only what a reader sees is included: the user's turns and the assistant's
    text.  Tool messages and an assistant turn that only asked for a tool are
    internal, and an empty assistant turn would render as a blank bubble.
    """

    if not isinstance(messages, list):
        return []
    transcript: list[dict[str, str]] = []
    for message in messages:
        kind = getattr(message, "type", "")
        if kind == "human":
            role = "user"
        elif kind == "ai":
            role = "assistant"
        else:
            continue
        text = _message_text(message)
        if not text:
            continue
        transcript.append({"role": role, "text": text})
    return transcript[-TRANSCRIPT_LIMIT:]


def _review_payload(
    review_store: ReviewStore, draft_id: str | None
) -> dict[str, Any] | None:
    """Return the verdict that still describes the draft's current version."""

    if not draft_id:
        return None
    active = review_store.get_active_review(draft_id)
    if active is None:
        return None
    return {
        "review_id": active.review_id,
        "draft_id": active.draft_id,
        "draft_version": active.draft_version,
        "status": active.status,
        "summary": active.summary,
        "issues": list(active.issues),
        "must_fix": list(active.must_fix),
        "risk_items": list(active.risk_items),
        "revision_request": active.revision_request,
        "research_request": active.research_request,
        "ready_for_generation": active.ready_for_generation,
        "created_at": active.created_at.isoformat(),
    }


def _generation_payload(
    task_store: TaskStore,
    generation_id: str | None,
    draft_id: str | None,
    *,
    task_id: str,
    provider_status: Any | None = None,
) -> dict[str, Any] | None:
    """Return the generation record for this task, newest first.

    The thread pointer wins.  A session that has submitted before but whose
    pointer was cleared by a revision still has a record worth showing, so the
    fallback is the newest record for the draft rather than nothing.

    ``provider_status`` is the live provider view, when the caller asked for
    one.  It is merged rather than stored: the business record keeps recording
    what the submission did, and the provider keeps reporting how the render is
    going.
    """

    generation = (
        task_store.get_generation(generation_id) if generation_id else None
    )
    if generation is None and draft_id:
        row = task_store.connection.execute(
            """
            SELECT * FROM generation_records
            WHERE draft_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
        generation = dict(row) if row is not None else None
    if generation is None:
        return None
    payload = {
        **{str(key): _json_ready(value) for key, value in generation.items()},
        "history": [
            {
                "generation_id": row["generation_id"],
                "draft_version": row["draft_version"],
                "status": row["status"],
                "provider_task_id": row.get("provider_task_id"),
                "created_at": _json_ready(row.get("created_at")),
            }
            for row in task_store.list_generation_records_for_task(task_id)
        ],
    }
    if provider_status is not None:
        payload["provider"] = {
            "provider_task_id": provider_status.provider_task_id,
            "status": provider_status.status,
            "raw_status": provider_status.raw_status,
            "progress": provider_status.progress,
            "video_url": provider_status.video_url,
            "error": provider_status.error,
            "created_at": provider_status.created_at,
            "completed_at": provider_status.completed_at,
            "failed": provider_status.failed,
        }
    return payload


def _research_payload(
    research_store: ResearchStore, research_result_id: str | None
) -> dict[str, Any] | None:
    """Return a bounded summary of the research the session is pointing at."""

    if not research_result_id:
        return None
    result = research_store.get_research_result(research_result_id)
    if result is None:
        return None
    return {
        "research_result_id": result.research_result_id,
        "summary": result.summary,
        "answer_status": (
            "verified"
            if result.facts
            and all(
                fact.verification_status == "verified"
                and fact.recruitment_status != "unknown"
                for fact in result.facts
            )
            else "unknown"
        ),
        "fact_count": len(result.facts),
        "source_count": len(result.sources),
        "open_questions": list(result.open_questions),
    }


def _provider_task_id(
    task_store: TaskStore,
    generation_id: str | None,
    draft_id: str | None,
) -> str | None:
    """Find the provider task id a refresh should ask about."""

    generation = task_store.get_generation(generation_id) if generation_id else None
    if generation is None and draft_id:
        row = task_store.connection.execute(
            """
            SELECT * FROM generation_records
            WHERE draft_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
        generation = dict(row) if row is not None else None
    if generation is None:
        return None
    provider_task_id = generation.get("provider_task_id")
    if not isinstance(provider_task_id, str) or not provider_task_id.strip():
        return None
    return provider_task_id.strip()


def _provider_status(
    task_store: TaskStore,
    provider_status_client: Any,
    generation_id: str | None,
    draft_id: str | None,
) -> Any | None:
    """Ask the provider how one generation is going, or report that it could not.

    A refresh is best effort.  Without provider credentials, or with the
    capability down, the panel still has the business record to show, so a
    failed refresh must not fail the page.
    """

    provider_task_id = _provider_task_id(task_store, generation_id, draft_id)
    if provider_task_id is None:
        return None
    try:
        return provider_status_client.fetch(
            provider_task_id=provider_task_id, refresh=True
        )
    except Exception:  # noqa: BLE001 - the panel reports what it could not read
        return None


async def read_session_snapshot(
    *,
    checkpointer_path: str | Path,
    db_path: str | Path,
    session_id: str,
    provider_status_client: Any | None = None,
    refresh_generation: bool = False,
) -> dict[str, Any]:
    """Read the current topic, draft, review, and generation for one session.

    ``provider_status_client`` plus ``refresh_generation`` ask the provider how
    the render is going and attach that view to the generation section.  It is
    opt-in because it is the only part of this read that leaves the process,
    and a panel that cannot reach the provider must still render.

    Raises :class:`SessionSnapshotError` when the session is blank or the
    checkpointed thread cannot be read.  An existing session that has done
    nothing yet is a valid snapshot with nothing in it.
    """

    normalized = session_id.strip()
    if not normalized:
        raise SessionSnapshotError("session_id must not be blank")
    task_id = _task_id(normalized)

    try:
        async with open_checkpointer(checkpointer_path) as checkpointer:
            snapshot = await checkpointer.aget_tuple(thread_config(normalized))
    except Exception as exc:  # noqa: BLE001 - reported as an unreadable session
        raise SessionSnapshotError(
            f"session thread {normalized} could not be read: {exc}"
        ) from exc

    values: Mapping[str, Any] = {}
    if snapshot is not None:
        raw = snapshot.checkpoint.get("channel_values")
        if isinstance(raw, Mapping):
            values = raw

    batch_id = values.get("current_candidate_batch_id")
    topic_id = values.get("current_topic_id")
    draft_id = values.get("current_draft_id")
    research_result_id = values.get("current_research_result_id")
    generation_id = values.get("generation_id")
    messages = values.get("messages")

    task_store = TaskStore(db_path)
    research_store = ResearchStore(db_path)
    review_store = ReviewStore(db_path)
    try:
        batch = (
            task_store.get_candidate_batch(batch_id)
            if isinstance(batch_id, str) and batch_id.strip()
            else None
        )
        candidates = (
            [
                _candidate_payload(row)
                for row in task_store.list_candidate_items(batch_id)
            ]
            if batch is not None
            else []
        )
        topic = (
            _candidate_payload(task_store.get_candidate_by_topic_id(topic_id))
            if isinstance(topic_id, str) and topic_id.strip()
            else None
        )
        draft = _draft_payload(
            task_store, draft_id if isinstance(draft_id, str) else None
        )
        provider_status = None
        if refresh_generation and provider_status_client is not None:
            provider_status = _provider_status(
                task_store,
                provider_status_client,
                generation_id if isinstance(generation_id, str) else None,
                draft_id if isinstance(draft_id, str) else None,
            )
        return {
            "session_id": normalized,
            "task_id": task_id,
            "task": _row(task_store.get_content_task(task_id)),
            "pointers": {
                "current_candidate_batch_id": batch_id,
                "current_topic_id": topic_id,
                "current_draft_id": draft_id,
                "current_draft_version": values.get("current_draft_version"),
                "current_research_result_id": research_result_id,
                "active_review_id": values.get("active_review_id"),
                "generation_id": generation_id,
            },
            "candidate_batch": (
                {
                    "candidate_batch_id": batch["candidate_batch_id"],
                    "summary": batch.get("summary"),
                    "created_at": _json_ready(batch.get("created_at")),
                    "candidates": candidates,
                }
                if batch is not None
                else None
            ),
            "topic": topic,
            "draft": draft,
            "review": _review_payload(
                review_store, draft_id if isinstance(draft_id, str) else None
            ),
            "generation": _generation_payload(
                task_store,
                generation_id if isinstance(generation_id, str) else None,
                draft_id if isinstance(draft_id, str) else None,
                task_id=task_id,
                provider_status=provider_status,
            ),
            "research": _research_payload(
                research_store,
                research_result_id if isinstance(research_result_id, str) else None,
            ),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            # The conversation itself, so a refresh shows the same chat instead of
            # an empty log: the thread lives in the checkpoint, and a page reload
            # must read it back rather than start over.
            "transcript": _transcript_payload(messages),
        }
    finally:
        review_store.close()
        research_store.close()
        task_store.close()
