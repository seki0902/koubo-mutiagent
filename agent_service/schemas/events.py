"""Unified Agent event contract.

Task 6.1 defines the event vocabulary and its serialized shape only.  No SSE
endpoint, event bus, or stream translation lives here; those arrive in later
tasks.  Everything that eventually reaches the browser or the local business
trace passes through :class:`AgentEvent`, so this module is the single place
where the contract can drift.

Two event groups exist for the same underlying progress.  ``agent_status`` is
the convenience shape the frontend animation consumes (see the V6.1 document,
section 41); the more specific types carry the detail.  Emitters choose which
one to forward, but both are part of this one contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "AGENT_STATUS_BY_EVENT",
    "AgentEvent",
    "AgentEventType",
    "AgentLifecyclePayload",
    "AgentStatusPayload",
    "CandidateBatchUpdatedPayload",
    "CandidateCardPayload",
    "CorrelationId",
    "DraftUpdatedPayload",
    "ErrorPayload",
    "GenerationCompletedPayload",
    "GenerationStartedPayload",
    "MessageChunkPayload",
    "MessageCompletedPayload",
    "PAYLOAD_MODELS",
    "ReviewUpdatedPayload",
    "ToolEventPayload",
    "WaitingUserPayload",
    "make_event",
    "new_event_id",
    "utc_now_iso",
]


class AgentEventType(str, Enum):
    """Every event the Agent runtime may publish.

    The value is the wire name and must stay stable: the frontend and the
    business trace both match on it.
    """

    # Lifecycle: one Agent or Tool starts or stops doing work.
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"

    # Business progress: durable content changed.
    CANDIDATE_BATCH_UPDATED = "candidate_batch_updated"
    DRAFT_UPDATED = "draft_updated"
    REVIEW_UPDATED = "review_updated"
    WAITING_USER = "waiting_user"

    # Digital-human generation submission.
    GENERATION_STARTED = "generation_started"
    GENERATION_COMPLETED = "generation_completed"

    # Terminal failure of one step.  Carries a stable error_code.
    ERROR = "error"

    # Assistant text, streamed incrementally and then closed out.
    MESSAGE_CHUNK = "message_chunk"
    MESSAGE_COMPLETED = "message_completed"

    # The model's own reasoning, streamed while it works. Sent separately from
    # the answer so the UI can show it as progress without mixing it into the
    # transcript the user reads.
    MESSAGE_THINKING = "message_thinking"

    # Frontend animation shape, per the V6.1 document section 41.
    AGENT_STATUS = "agent_status"


def utc_now_iso() -> str:
    """Return the current time as a timezone-aware ISO 8601 string."""

    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    """Return a fresh event identifier with a recognizable prefix."""

    return f"evt_{uuid4().hex}"


#: Correlation identifiers match the runtime convention used by
#: :class:`~agent_service.runtime.context.ContentAppContext`: surrounding
#: whitespace is trimmed and a whitespace-only value counts as blank.
CorrelationId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Payload(BaseModel):
    """Base for every payload model: reject unknown keys, never mutate."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentLifecyclePayload(_Payload):
    """One Agent starting or finishing its work in a request."""

    agent: str = Field(min_length=1)
    message: str = ""


class ToolEventPayload(_Payload):
    """One tool call starting or finishing."""

    tool: str = Field(min_length=1)
    agent: str = ""
    message: str = ""


class AgentStatusPayload(_Payload):
    """The flattened status shape the frontend animation consumes."""

    agent: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str = ""


class CandidateCardPayload(_Payload):
    """One topic card, identified by the same mapping the resolver uses.

    ``display_index`` is the persisted user-facing position, not an array
    position, so the card the user clicks and the ordinal the user says
    resolve through the same stored value.
    """

    topic_id: str = Field(min_length=1)
    display_index: int = Field(ge=1)
    title: str = Field(min_length=1)


class CandidateBatchUpdatedPayload(_Payload):
    """A new topic candidate batch is now the current one.

    ``candidates`` carries exactly what the frontend needs to render the batch
    as cards and to name one of them later: the persisted ``display_index``
    mapping and the stable ``topic_id``.  The list stays optional so a payload
    written before it existed, and read back from the trace, is still valid.
    """

    candidate_batch_id: str = Field(min_length=1)
    candidate_count: int = Field(ge=0)
    summary: str = ""
    candidates: list[CandidateCardPayload] = Field(default_factory=list)


class DraftUpdatedPayload(_Payload):
    """A draft version was written and is now current."""

    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)


class ReviewUpdatedPayload(_Payload):
    """A review verdict is bound to the current draft version."""

    review_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class WaitingUserPayload(_Payload):
    """The Agent stopped because it needs a decision from the user."""

    message: str = ""


class GenerationStartedPayload(_Payload):
    """An authorized draft is being handed to the digital-human provider.

    There is deliberately no ``generation_id`` here.  The local generation
    record is created by the submission this event announces, so the
    identifier does not exist yet; it arrives with ``generation_completed``.
    """

    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    idempotency_key: str = ""


class GenerationCompletedPayload(_Payload):
    """The submission phase ended.

    ``status`` describes the submission outcome only.  Video rendering
    continues on the provider side and is observed through the existing task
    polling, not through this stream.
    """

    generation_id: str = Field(min_length=1)
    status: Literal["submitted", "unknown_submission", "failed"]
    provider_task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ErrorPayload(_Payload):
    """A step failed.  ``error_code`` is stable; the message is for humans."""

    error_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class MessageChunkPayload(_Payload):
    """One incremental piece of assistant text."""

    text: str


class MessageCompletedPayload(_Payload):
    """The complete assistant text for this turn."""

    text: str


#: Payload model for each event type.  ``make_event`` validates against it, so
#: a malformed payload fails at the emit site rather than in the browser.
PAYLOAD_MODELS: dict[AgentEventType, type[_Payload]] = {
    AgentEventType.AGENT_STARTED: AgentLifecyclePayload,
    AgentEventType.AGENT_FINISHED: AgentLifecyclePayload,
    AgentEventType.TOOL_STARTED: ToolEventPayload,
    AgentEventType.TOOL_FINISHED: ToolEventPayload,
    AgentEventType.CANDIDATE_BATCH_UPDATED: CandidateBatchUpdatedPayload,
    AgentEventType.DRAFT_UPDATED: DraftUpdatedPayload,
    AgentEventType.REVIEW_UPDATED: ReviewUpdatedPayload,
    AgentEventType.WAITING_USER: WaitingUserPayload,
    AgentEventType.GENERATION_STARTED: GenerationStartedPayload,
    AgentEventType.GENERATION_COMPLETED: GenerationCompletedPayload,
    AgentEventType.ERROR: ErrorPayload,
    AgentEventType.MESSAGE_CHUNK: MessageChunkPayload,
    AgentEventType.MESSAGE_THINKING: MessageChunkPayload,
    AgentEventType.MESSAGE_COMPLETED: MessageCompletedPayload,
    AgentEventType.AGENT_STATUS: AgentStatusPayload,
}

#: ``(agent, status)`` the frontend should show for each lifecycle event.  It
#: lives here so the streaming layer does not hard-code the mapping twice.
AGENT_STATUS_BY_EVENT: dict[AgentEventType, tuple[str, str]] = {
    AgentEventType.AGENT_STARTED: ("supervisor", "working"),
    AgentEventType.AGENT_FINISHED: ("supervisor", "finished"),
    AgentEventType.TOOL_STARTED: ("", "working"),
    AgentEventType.TOOL_FINISHED: ("", "finished"),
    AgentEventType.GENERATION_STARTED: ("digital_human", "working"),
    AgentEventType.GENERATION_COMPLETED: ("digital_human", "finished"),
    AgentEventType.WAITING_USER: ("supervisor", "waiting_user"),
    AgentEventType.ERROR: ("", "error"),
}


class AgentEvent(BaseModel):
    """The envelope every emitted event shares.

    Identifiers are required and non-blank: an event that cannot be correlated
    back to a request, session, and task is not worth sending.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: CorrelationId
    request_id: CorrelationId
    session_id: CorrelationId
    task_id: CorrelationId
    type: AgentEventType
    timestamp: CorrelationId
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize for the wire, keeping non-ASCII text readable."""

        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> AgentEvent:
        """Parse a wire payload, rejecting unknown types and fields."""

        return cls.model_validate_json(raw)


def make_event(
    event_type: AgentEventType | str,
    *,
    request_id: str,
    session_id: str,
    task_id: str,
    payload: BaseModel | dict[str, Any] | None = None,
) -> AgentEvent:
    """Build one validated event.

    The payload is checked against :data:`PAYLOAD_MODELS` before it is wrapped,
    so an emitter cannot publish a shape the consumer does not understand.
    """

    resolved_type = AgentEventType(event_type)
    model = PAYLOAD_MODELS[resolved_type]
    if payload is None:
        validated = model()
    elif isinstance(payload, model):
        validated = payload
    elif isinstance(payload, BaseModel):
        # A typed payload for a different event type is a programming error at
        # the emit site, not bad user data, so it fails loudly as a TypeError.
        raise TypeError(
            f"payload model {type(payload).__name__} does not match "
            f"event type {resolved_type.value}"
        )
    else:
        validated = model.model_validate(payload)

    return AgentEvent(
        event_id=new_event_id(),
        request_id=request_id,
        session_id=session_id,
        task_id=task_id,
        type=resolved_type,
        timestamp=utc_now_iso(),
        payload=validated.model_dump(),
    )
