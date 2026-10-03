"""Minimal standalone Agent Service HTTP application.

Business agents, model providers, Go capability calls, and databases are
intentionally added in later tasks.  This module owns only runtime setup,
request correlation, and the liveness endpoint for now.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_service.agents.supervisor import SupervisorService
from agent_service.config import load_settings
from agent_service.runtime.logging import configure_logging
from agent_service.runtime.tracing import configure_tracing
from agent_service.schemas.events import AgentEventType, ErrorPayload, make_event
from agent_service.services.session_snapshot import (
    SessionSnapshotError,
    read_session_snapshot,
)
from agent_service.services.task_store import TaskStore
from agent_service.tools.digital_human_status import DigitalHumanStatusClient


settings = load_settings()
logger = configure_logging(settings.log_level)
configure_tracing(settings)


app = FastAPI(title="Koubo Agent Service", version="v1")
#: Deliberately not handed an AgentSettings: the supervisor loads settings
#: itself and re-reads them between turns, so a model key saved in the browser
#: panel takes effect without restarting this process.
supervisor_service = SupervisorService(
    Path(settings.db_path).with_name("checkpoints.sqlite"),
)
#: Provider progress for a submitted generation is read on demand, not polled
#: here: a local checkout without provider credentials never calls it.
digital_human_status_client = DigitalHumanStatusClient(settings.go_capability_base_url)


class AgentChatRequest(BaseModel):
    """Minimal request accepted by the internal chat endpoints.

    Both the blocking and the streaming endpoint take this payload, so a
    caller can switch between them without reshaping its request.
    """

    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    request_id: str | None = None
    generation_params: dict[str, Any] | None = None


#: How long a stream may go quiet before a comment frame is sent.  Proxies and
#: browsers drop idle connections; a comment is ignored by any SSE parser.
STREAM_KEEPALIVE_SECONDS = 15.0


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Attach a request ID and emit one structured completion record."""

    request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
    request.state.request_id = request_id
    started = perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info(
            "request completed",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "status": status_code,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
            },
        )


#: Stable error codes for the failures this service reports itself.  Go maps an
#: upstream status onto its public contract, so the code only has to be stable
#: here, not identical to the public one.
_ERROR_CODES_BY_STATUS = {
    400: "INVALID_REQUEST",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
    503: "CAPABILITY_UNAVAILABLE",
}


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Report every service-side failure in the one error contract.

    Temporary shim: the earlier endpoints returned FastAPI's ``{"detail": ...}``
    shape.  A client should not have to speak two error dialects, so all of
    them answer with the same envelope the internal contract documents.
    """

    detail = exc.detail
    message = (
        detail
        if isinstance(detail, str)
        else json.dumps(detail, ensure_ascii=False)
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": _ERROR_CODES_BY_STATUS.get(exc.status_code, "INTERNAL_ERROR"),
            "message": message,
            "retryable": exc.status_code >= 500,
            "request_id": getattr(request.state, "request_id", ""),
            "details": {},
        },
        headers=getattr(exc, "headers", None),
    )


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "service": "agent_service",
        "version": "v1",
        "request_id": request.state.request_id,
    }


@app.post("/internal/agent/chat")
async def agent_chat(payload: AgentChatRequest, request: Request) -> dict[str, object]:
    """Run one minimal Supervisor turn for a session thread."""

    request_id = payload.request_id or request.state.request_id
    try:
        return await supervisor_service.chat(
            request_id=request_id,
            session_id=payload.session_id,
            message=payload.message,
            generation_params=payload.generation_params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Keep provider/configuration details out of a successful response while
        # preserving a useful error for the internal caller during this phase.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/internal/agent/session/{session_id}")
async def agent_session(session_id: str, request: Request) -> dict[str, object]:
    """Read one session's current business state for a refresh.

    A refresh has no event stream to replay, so the frontend restores the
    right-hand panel from here.  This is read-only: it never runs the graph and
    never moves a pointer.
    """

    normalized = session_id.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="session_id must not be blank")

    store = TaskStore(settings.db_path)
    try:
        has_task = store.get_content_task(f"task_{normalized}") is not None
    finally:
        store.close()
    if not has_task:
        raise HTTPException(
            status_code=404, detail=f"no task is recorded for session {normalized}"
        )

    try:
        snapshot = await read_session_snapshot(
            checkpointer_path=Path(settings.db_path).with_name("checkpoints.sqlite"),
            db_path=settings.db_path,
            session_id=normalized,
            provider_status_client=digital_human_status_client,
            refresh_generation=request.query_params.get("refresh") == "1",
        )
    except SessionSnapshotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Same request correlation rule as every other endpoint: the caller's ID is
    # returned in the body, and the middleware adds it as a header.
    snapshot["request_id"] = request.state.request_id
    return snapshot


def _error_frame(    *,
    payload: AgentChatRequest,
    request_id: str,
    error_code: str,
    message: str,
) -> str:
    """Build one SSE frame reporting a failure that happened before output."""

    event = make_event(
        AgentEventType.ERROR,
        request_id=request_id,
        session_id=payload.session_id.strip() or "unknown-session",
        task_id=f"task_{payload.session_id.strip() or 'unknown-session'}",
        payload=ErrorPayload(error_code=error_code, message=message or error_code),
    )
    return f"data: {event.to_json()}\n\n"


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """Retrieve a detached task's outcome so it is not reported as unhandled."""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


@app.post("/internal/agent/chat/stream")
async def agent_chat_stream(
    payload: AgentChatRequest, request: Request
) -> StreamingResponse:
    """Stream one Supervisor turn as Server-Sent Events."""

    request_id = payload.request_id or request.state.request_id
    frames: asyncio.Queue[str | None] = asyncio.Queue()

    async def produce() -> None:
        """Run the turn and hand its frames to the response loop."""

        try:
            async for event in supervisor_service.chat_stream(
                request_id=request_id,
                session_id=payload.session_id,
                message=payload.message,
                generation_params=payload.generation_params,
            ):
                frames.put_nowait(f"data: {event.to_json()}\n\n")
        except ValueError as exc:
            frames.put_nowait(
                _error_frame(
                    payload=payload,
                    request_id=request_id,
                    error_code="INVALID_REQUEST",
                    message=str(exc),
                )
            )
        except Exception as exc:
            frames.put_nowait(
                _error_frame(
                    payload=payload,
                    request_id=request_id,
                    error_code="INTERNAL_ERROR",
                    message=str(exc) or type(exc).__name__,
                )
            )
        finally:
            frames.put_nowait(None)

    async def stream() -> AsyncIterator[str]:
        """Emit frames, with a comment when the turn goes quiet.

        The turn runs on its own task rather than being read directly here:
        timing out a pending ``anext`` to send a keepalive would cancel it and
        leave the generator unusable.
        """

        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(
                        frames.get(), timeout=STREAM_KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if frame is None:
                    break
                yield frame
        finally:
            if not producer.done():
                producer.cancel()
            # Deliberately not awaited here.  This generator runs inside the
            # server's cancel scope, which re-delivers cancellation for as long
            # as the scope is open; suspending on the producer would let that
            # land inside the Agent turn's own teardown and cancel it halfway,
            # leaving the turn's node tasks running with nothing to stop them.
            # The producer unwinds on its own task and is collected there.
            producer.add_done_callback(_consume_task_result)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
