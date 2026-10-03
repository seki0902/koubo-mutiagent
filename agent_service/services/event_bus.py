"""Per-request in-process fan-out for Agent events.

The bus is the single sink every event passes through, whether it originates
inside a LangGraph run (a tool reporting business progress) or outside it (the
turn's own generation submission).  Keeping one sink means the streaming
endpoint and the local business trace observe the same ordered sequence.

Publishing never blocks the producer.  When a slow consumer lets the bounded
queue fill, the oldest *progress* event is evicted so the newest state survives
-- a progress stream is worth less when it reports stale state.  Terminal
events and errors are never evicted; if nothing but terminal events is left,
the incoming event is dropped instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from agent_service.schemas.events import (
    AgentEvent,
    AgentEventType,
    ErrorPayload,
    make_event,
)

__all__ = [
    "DEFAULT_QUEUE_CAPACITY",
    "EventBus",
    "EventBusError",
    "STREAM_OVERRUN_ERROR_CODE",
    "Subscription",
    "TERMINAL_EVENT_TYPES",
]

DEFAULT_QUEUE_CAPACITY: Final = 512

#: Reported to the consumer when it fell behind far enough to lose progress.
STREAM_OVERRUN_ERROR_CODE: Final = "STREAM_OVERRUN"

#: Events that end the conversation for this request.  Losing one would leave
#: the client waiting forever, so the eviction policy never removes them.
TERMINAL_EVENT_TYPES: Final[frozenset[AgentEventType]] = frozenset(
    {
        AgentEventType.AGENT_FINISHED,
        AgentEventType.WAITING_USER,
        AgentEventType.GENERATION_COMPLETED,
        AgentEventType.MESSAGE_COMPLETED,
        AgentEventType.ERROR,
    }
)

_CLOSED: Final = object()


class EventBusError(RuntimeError):
    """The bus was used in a way that cannot be reconciled with its contract."""


@dataclass
class _Channel:
    """One request's queue plus the bookkeeping the eviction policy needs."""

    queue: asyncio.Queue[Any]
    trace: Any = None
    overrun_reported: bool = False
    closed: bool = False


class Subscription:
    """Async iterator over one request's events.

    Iteration ends when the publishing side closes the channel and the queue
    has drained, so a consumer always sees the terminal event before the
    stream finishes.
    """

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._channel.queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item


class EventBus:
    """Route events to the consumer subscribed to a request.

    A request with no subscriber is not an error: the blocking chat path
    publishes nothing, and ``publish`` quietly discards in that case.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        trace_factory: Callable[[], Any] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._trace_factory = trace_factory
        self._channels: dict[str, _Channel] = {}
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def channel(self, request_id: str) -> AsyncIterator[Subscription]:
        """Open the only subscription for ``request_id`` and close it after.

        Closing always runs, so a consumer that raises or disconnects cannot
        leave a channel behind for a later request to collide with.
        """

        subscription = await self._open(request_id)
        try:
            yield subscription
        finally:
            await self.close(request_id)

    async def publish(self, request_id: str, event: AgentEvent) -> None:
        """Enqueue one event without ever blocking the producer."""

        async with self._lock:
            channel = self._channels.get(request_id)
            if channel is None or channel.closed:
                return
            if channel.trace is not None:
                # Recorded before any eviction: the local trace explains the
                # turn, so it wants every step, not just the ones that fit in
                # the consumer's queue.
                with contextlib.suppress(Exception):
                    channel.trace.record(event)
            if channel.queue.qsize() >= self._capacity:
                if not self._evict_oldest_progress(channel):
                    # Only terminal events are queued, and those are never
                    # dropped; the incoming event is the one that goes.
                    return
                if not channel.overrun_reported:
                    # Tell the consumer it missed something before continuing.
                    channel.overrun_reported = True
                    channel.queue.put_nowait(self._overrun_event(event))
                    return
            channel.queue.put_nowait(event)

    async def _open(self, request_id: str) -> Subscription:
        if not request_id.strip():
            raise EventBusError("request_id must not be blank")
        async with self._lock:
            if request_id in self._channels:
                raise EventBusError(f"a channel is already open for {request_id}")
            # One slot beyond the capacity is reserved for the end-of-stream
            # sentinel, so closing never has to evict an event to make room.
            # Publishing counts against the capacity, not the queue size.
            channel = _Channel(
                queue=asyncio.Queue(maxsize=self._capacity + 1),
                # One trace per request, opened and closed with the channel, so
                # concurrent requests cannot write over each other's history.
                trace=self._trace_factory() if self._trace_factory else None,
            )
            self._channels[request_id] = channel
        return Subscription(channel)

    async def close(self, request_id: str) -> None:
        """End the subscription for ``request_id``; safe to call more than once.

        The producer calls this when a turn finishes, which is what lets the
        consumer's ``async for`` terminate.  Relying on the consumer's own
        context manager alone would deadlock: it would wait for a close that
        only it could trigger.

        There is deliberately no ``await`` below.  This runs from a ``finally``
        while a disconnected client's cancellation is being delivered, and an
        await point there would let that cancellation land before the trace is
        closed -- leaking the connection and, on Windows, locking the file.  A
        coroutine with no awaits runs to completion in one step, so the whole
        close is atomic with respect to cancellation.
        """

        channel = self._channels.pop(request_id, None)
        if channel is None:
            return
        channel.closed = True
        if channel.trace is not None:
            with contextlib.suppress(Exception):
                channel.trace.close()
        # The reserved slot guarantees this never blocks and never costs
        # the consumer a queued event.
        channel.queue.put_nowait(_CLOSED)

    @staticmethod
    def _evict_oldest_progress(channel: _Channel) -> bool:
        """Drop the oldest non-terminal event, keeping the newest state.

        Returns ``False`` when the queue holds nothing but terminal events, in
        which case the caller must not evict anything.
        """

        buffered: list[AgentEvent] = []
        while True:
            try:
                buffered.append(channel.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        victim = next(
            (
                index
                for index, event in enumerate(buffered)
                if event.type not in TERMINAL_EVENT_TYPES
            ),
            None,
        )
        if victim is None:
            for event in buffered:
                channel.queue.put_nowait(event)
            return False

        buffered.pop(victim)
        for event in buffered:
            channel.queue.put_nowait(event)
        return True

    @staticmethod
    def _overrun_event(source: AgentEvent) -> AgentEvent:
        """Build the one-time notice that progress events were dropped."""

        return make_event(
            AgentEventType.ERROR,
            request_id=source.request_id,
            session_id=source.session_id,
            task_id=source.task_id,
            payload=ErrorPayload(
                error_code=STREAM_OVERRUN_ERROR_CODE,
                message=(
                    "the consumer fell behind and some progress events were "
                    "dropped; the newest events follow"
                ),
                retryable=False,
            ),
        )
