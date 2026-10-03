"""Bridge between a running Agent turn and the event bus.

Tools and services never need to know whether they run inside a streamed
request.  They call :func:`emit`; outside a streamed request there is no bound
emitter and the call is a no-op, which is what lets the blocking chat path and
the streaming path share one implementation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agent_service.schemas.events import AgentEvent, AgentEventType, make_event

__all__ = [
    "bind_emitter",
    "current_emitter",
    "emit",
    "is_text_chunk",
    "text_of",
]


@dataclass(frozen=True, slots=True)
class _Emitter:
    """The bus and correlation identifiers for the request being streamed."""

    bus: Any
    request_id: str
    session_id: str
    task_id: str


_CURRENT: ContextVar[_Emitter | None] = ContextVar(
    "koubo_agent_event_emitter", default=None
)


@contextlib.contextmanager
def bind_emitter(
    bus: Any,
    *,
    request_id: str,
    session_id: str,
    task_id: str,
) -> Iterator[None]:
    """Bind the emitter for the duration of a streamed turn.

    The binding travels into LangGraph's tool tasks because they are created
    from this context, so tools emit without being handed a bus explicitly.
    """

    token = _CURRENT.set(
        _Emitter(
            bus=bus,
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
        )
    )
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_emitter() -> _Emitter | None:
    """Return the bound emitter, or ``None`` outside a streamed request."""

    return _CURRENT.get()


async def emit(
    event_type: AgentEventType | str,
    payload: BaseModel | dict[str, Any] | None = None,
) -> AgentEvent | None:
    """Publish one business event, or do nothing when no stream is bound.

    Returns the published event so the caller can log or trace it, and
    ``None`` when the turn is not being streamed.
    """

    emitter = _CURRENT.get()
    if emitter is None:
        return None

    event = make_event(
        event_type,
        request_id=emitter.request_id,
        session_id=emitter.session_id,
        task_id=emitter.task_id,
        payload=payload,
    )
    await emitter.bus.publish(emitter.request_id, event)
    return event


def is_text_chunk(chunk: Any) -> bool:
    """Report whether a streamed message is assistant text worth forwarding."""

    return getattr(chunk, "type", "") == "AIMessageChunk"


def thinking_of(chunk: Any) -> str:
    """Extract the model's reasoning from a streamed chunk, when it has any.

    DeepSeek's thinking mode returns its reasoning beside the answer, and
    LangChain exposes it in ``additional_kwargs``. The runtimes that stream it
    in typed content blocks are covered too, because a provider that switches
    shape must not silently lose the progress a waiting user is watching.
    """

    extra = getattr(chunk, "additional_kwargs", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value

    content = getattr(chunk, "content", "")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"thinking", "reasoning"}:
            text = block.get("thinking") or block.get("text") or block.get("reasoning")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def text_of(chunk: Any) -> str:
    """Extract plain text from a streamed message chunk.

    Content is a string for most providers and a list of typed blocks for
    others; anything that is not text (tool-call blocks, for instance) is
    skipped rather than stringified into the user's transcript.
    """

    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)
