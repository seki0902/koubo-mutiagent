"""Local business trace: what one request did, in the order it happened.

LangSmith traces the model, the subagents, and the tools.  It does not know
what a *draft version* or a *generation authorization* is, and it is remote
and conditional on a key being configured.  This table is the local half: the
same events the client was streamed, persisted so a request can be explained
after the fact without an account or a network.

The events are the ones the :class:`~agent_service.services.event_bus.EventBus`
already carries, so the trace cannot drift from what the user actually saw.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_service.db.database import connect_database, initialize_database
from agent_service.schemas.events import AgentEvent

__all__ = ["BusinessTraceStore"]


class BusinessTraceStore:
    """Append events to ``business_trace_events`` and read them back."""

    def __init__(self, db_path: str | Path):
        self.connection = connect_database(db_path)
        initialize_database(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "BusinessTraceStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def record(self, event: AgentEvent) -> None:
        """Append one event.  Tracing must never break the turn it observes."""

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO business_trace_events (
                    request_id, session_id, task_id, event_type,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.request_id,
                    event.session_id,
                    event.task_id,
                    # The wire name, not ``str(event.type)``: the latter renders
                    # as ``AgentEventType.AGENT_STARTED``, which would not match
                    # what the frontend receives.
                    event.type.value,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.timestamp,
                ),
            )

    def events_for(self, request_id: str) -> list[dict[str, Any]]:
        """Return one request's events in the order they happened."""

        rows = self.connection.execute(
            """
            SELECT request_id, session_id, task_id, event_type,
                   payload_json, created_at
            FROM business_trace_events
            WHERE request_id = ?
            ORDER BY trace_id
            """,
            (request_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {str(row[0]) for row in rows}
