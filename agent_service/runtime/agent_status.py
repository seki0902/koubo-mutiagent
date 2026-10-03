"""The section 41 working-state events, emitted by the work itself.

The frontend's "working" indicator has to come from events, never from reading
the assistant text back.  A tool wrapper knows exactly when it starts and when
it stops, so it is the only layer that can say so without guessing; these
helpers keep the vocabulary and the status values in one place so five
specialists cannot drift into five dialects.

Names are stable by design: ``topic``, ``research``, ``script``, ``review``,
``generation``.  The frontend keys its indicator off them, and the doc's
example uses the same lowercase style.
"""

from __future__ import annotations

from agent_service.runtime.streaming import emit
from agent_service.schemas.events import (
    AgentEventType,
    AgentStatusPayload,
    ToolEventPayload,
)

__all__ = [
    "AGENT_RESEARCH",
    "AGENT_REVIEW",
    "AGENT_SCRIPT",
    "AGENT_TOPIC",
    "STATUS_ERROR",
    "STATUS_FINISHED",
    "STATUS_WORKING",
    "agent_status",
    "tool_status",
]

AGENT_TOPIC = "topic"
AGENT_RESEARCH = "research"
AGENT_SCRIPT = "script"
AGENT_REVIEW = "review"
AGENT_GENERATION = "generation"

STATUS_WORKING = "working"
STATUS_FINISHED = "finished"
STATUS_ERROR = "error"


async def agent_status(
    agent: str,
    status: str,
    *,
    message: str = "",
) -> None:
    """Publish one working-state event for a specialist or the runtime.

    A no-op outside a streamed turn, so the blocking chat path and the tools it
    shares stay free of stream concerns.
    """

    await emit(
        AgentEventType.AGENT_STATUS,
        AgentStatusPayload(agent=agent, status=status, message=message),
    )


async def tool_status(
    tool: str,
    status: str,
    *,
    agent: str = "",
    message: str = "",
) -> None:
    """Publish one tool start/finish event.

    ``tool`` is the model-facing tool name, so the indicator can say which
    capability is running instead of only which specialist owns it.
    """

    await emit(
        AgentEventType.TOOL_STARTED if status == STATUS_WORKING else AgentEventType.TOOL_FINISHED,
        ToolEventPayload(tool=tool, agent=agent, message=message),
    )
