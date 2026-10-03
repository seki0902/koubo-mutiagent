"""LangGraph thread state and application runtime context.

``ContentAgentState`` is the small, checkpointable pointer set.  The Business
SQLite database remains canonical truth for drafts, candidates, authorizations,
and generation records; those complete objects must never be copied into this
state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from langchain.agents import AgentState
from langchain_core.messages import BaseMessage
from typing_extensions import NotRequired


class ContentAgentState(AgentState):
    """Small checkpointed state for one conversation thread.

    The inherited ``messages`` field is the framework-managed conversation
    history.  The remaining fields are pointers and bounded counters only;
    canonical business objects are loaded from Business SQLite by services.
    """

    session_id: NotRequired[str]
    task_id: NotRequired[str | None]
    state_revision: NotRequired[int]

    current_candidate_batch_id: NotRequired[str | None]
    current_topic_id: NotRequired[str | None]
    current_draft_id: NotRequired[str | None]
    current_draft_version: NotRequired[int | None]
    current_research_result_id: NotRequired[str | None]
    active_review_id: NotRequired[str | None]
    generation_id: NotRequired[str | None]
    #: The provider's own task id for the video this thread submitted. It is a
    #: different thing from ``generation_id`` (a local generation record): the
    #: direct submission path does not create one of those.
    video_task_id: NotRequired[str | None]

    # Bounded runtime accounting, not business history.
    specialist_call_count: NotRequired[int]


_STATE_FIELDS = frozenset(
    {
        "messages",
        "session_id",
        "task_id",
        "state_revision",
        "current_candidate_batch_id",
        "current_topic_id",
        "current_draft_id",
        "current_draft_version",
        "current_research_result_id",
        "active_review_id",
        "generation_id",
        "video_task_id",
        "specialist_call_count",
        # Framework-managed optional fields inherited from AgentState.
        "jump_to",
        "structured_response",
    }
)


def _json_ready(value: Any) -> Any:
    """Convert framework message values into JSON-compatible data."""

    if isinstance(value, BaseMessage):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"state value {type(value).__name__} is not JSON serializable")


def serialize_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-ready state and reject accidental business snapshots.

    This explicit allow-list is a guardrail: adding a full draft history or a
    complete authorization object to the checkpoint state must be a deliberate
    schema change rather than an accidental dict update.
    """

    unexpected = set(state) - _STATE_FIELDS
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ValueError(f"state contains fields outside the thread boundary: {names}")
    return {key: _json_ready(value) for key, value in state.items()}
