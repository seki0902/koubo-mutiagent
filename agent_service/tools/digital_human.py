"""Digital-human side-effect capability.

The Agent owns authorization, draft/version validation, and the generation
record.  This module only adapts the provider submission to Go's internal
capability endpoint and turns ambiguous transport failures into an explicit
``unknown_submission`` signal so callers cannot blindly submit again.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from agent_service.runtime.budget import DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS


class DigitalHumanToolError(RuntimeError):
    """A structured failure from the Go digital-human capability."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "INTERNAL_ERROR",
        retryable: bool = False,
        unknown_submission: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.unknown_submission = unknown_submission
        self.status_code = status_code


@dataclass(frozen=True)
class DigitalHumanSubmission:
    """Provider submission result returned by the internal capability."""

    request_id: str
    generation_id: str
    provider_task_id: str
    status: str
    created_at: str


class DigitalHumanTool:
    """Submit one authorized generation through the Go capability boundary."""

    def __init__(
        self, base_url: str, *, timeout: float = DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("base_url must not be blank")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = normalized
        self.timeout = timeout

    def submit(
        self,
        *,
        task_id: str,
        draft_id: str,
        draft_version: int,
        authorization_id: str,
        idempotency_key: str,
        generation_id: str,
        script: str,
        params: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> DigitalHumanSubmission:
        call_request_id = request_id or f"req_generation_{uuid4().hex}"
        payload = {
            "request_id": call_request_id,
            "task_id": task_id,
            "draft_id": draft_id,
            "draft_version": draft_version,
            "authorization_id": authorization_id,
            "idempotency_key": idempotency_key,
            "generation_id": generation_id,
            "script": script,
            "params": dict(params or {}),
        }
        request = Request(
            f"{self.base_url}/internal/tools/generate-digital-human",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": call_request_id,
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status_code = int(response.status)
        except HTTPError as exc:
            body = exc.read()
            self._raise_http_error(exc.code, body)
        except (socket.timeout, TimeoutError) as exc:
            raise DigitalHumanToolError(
                "digital-human provider submission timed out",
                error_code="CAPABILITY_TIMEOUT",
                retryable=True,
                unknown_submission=True,
            ) from exc
        except URLError as exc:
            raise DigitalHumanToolError(
                "digital-human capability is unavailable",
                error_code="CAPABILITY_UNAVAILABLE",
                retryable=True,
                unknown_submission=True,
            ) from exc
        except OSError as exc:
            raise DigitalHumanToolError(
                "digital-human capability request failed",
                error_code="CAPABILITY_UNAVAILABLE",
                retryable=True,
                unknown_submission=True,
            ) from exc

        try:
            response_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DigitalHumanToolError(
                "digital-human capability returned invalid JSON",
                error_code="UPSTREAM_ERROR",
                retryable=True,
                unknown_submission=True,
                status_code=status_code,
            ) from exc

        provider_task_id = str(response_payload.get("provider_task_id", "")).strip()
        response_generation_id = str(
            response_payload.get("generation_id", generation_id)
        ).strip()
        if not provider_task_id or response_generation_id != generation_id:
            raise DigitalHumanToolError(
                "digital-human capability returned an incomplete submission",
                error_code="UPSTREAM_ERROR",
                retryable=True,
                unknown_submission=True,
                status_code=status_code,
            )

        return DigitalHumanSubmission(
            request_id=str(response_payload.get("request_id", call_request_id)),
            generation_id=response_generation_id,
            provider_task_id=provider_task_id,
            status=str(response_payload.get("status", "pending")),
            created_at=str(response_payload.get("created_at", "")),
        )

    @staticmethod
    def _raise_http_error(status_code: int, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}

        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        error_code = str(payload.get("error_code", "UPSTREAM_ERROR"))
        unknown_submission = bool(details.get("unknown_submission"))
        if not unknown_submission and error_code in {
            "CAPABILITY_TIMEOUT",
            "CAPABILITY_UNAVAILABLE",
        }:
            unknown_submission = True
        raise DigitalHumanToolError(
            str(payload.get("message", "digital-human capability rejected the request")),
            error_code=error_code,
            retryable=bool(payload.get("retryable", status_code >= 500)),
            unknown_submission=unknown_submission,
            status_code=status_code,
        )


__all__ = [
    "DigitalHumanSubmission",
    "DigitalHumanTool",
    "DigitalHumanToolError",
]
