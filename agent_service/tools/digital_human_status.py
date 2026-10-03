"""Read one generation's provider progress through the Go capability boundary.

The submission path already tells the user that a video was accepted.  This is
the other half: whether it finished, and where the result is.  Go owns the
provider credentials and the polling vocabulary, so Python asks it rather than
calling Chanjing directly.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent_service.runtime.budget import DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS

__all__ = [
    "DigitalHumanStatus",
    "DigitalHumanStatusClient",
    "DigitalHumanStatusError",
]


#: The provider statuses Go reports.  Anything else is reported as unknown
#: rather than silently mapped onto the nearest-looking state.
KNOWN_STATUSES = frozenset({"pending", "processing", "completed", "failed"})


class DigitalHumanStatusError(RuntimeError):
    """A generation's provider status could not be read."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "INTERNAL_ERROR",
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class DigitalHumanStatus:
    """One generation's current provider state."""

    provider_task_id: str
    status: str
    raw_status: str
    progress: int
    video_url: str
    error: str
    created_at: str
    completed_at: str
    failed: bool

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    @property
    def has_video(self) -> bool:
        return bool(self.video_url.strip())


class DigitalHumanStatusClient:
    """Read one generation's status from the Go capability endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("base_url must not be blank")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = normalized
        self.timeout = timeout

    def fetch(
        self,
        *,
        provider_task_id: str,
        refresh: bool = False,
        request_id: str | None = None,
    ) -> DigitalHumanStatus:
        task_id = provider_task_id.strip()
        if not task_id:
            raise ValueError("provider_task_id must not be blank")

        url = f"{self.base_url}/internal/tools/generation/{quote(task_id, safe='')}"
        if refresh:
            url = f"{url}?refresh=1"
        headers = {"Accept": "application/json"}
        if request_id:
            headers["X-Request-ID"] = request_id
        request = Request(url, headers=headers, method="GET")

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            body = exc.read()
            raise self._error_from_body(exc.code, body) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise DigitalHumanStatusError(
                "generation status request timed out",
                error_code="CAPABILITY_TIMEOUT",
                retryable=True,
            ) from exc
        except (URLError, OSError) as exc:
            raise DigitalHumanStatusError(
                "generation status capability is unavailable",
                error_code="CAPABILITY_UNAVAILABLE",
                retryable=True,
            ) from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DigitalHumanStatusError(
                "generation status capability returned invalid JSON",
                error_code="UPSTREAM_ERROR",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise DigitalHumanStatusError(
                "generation status capability returned an unexpected payload",
                error_code="UPSTREAM_ERROR",
                retryable=True,
            )

        status = str(payload.get("status", "")).strip()
        if status not in KNOWN_STATUSES:
            raise DigitalHumanStatusError(
                f"generation status capability returned an unknown status: {status!r}",
                error_code="UPSTREAM_ERROR",
                retryable=True,
            )

        return DigitalHumanStatus(
            provider_task_id=str(payload.get("provider_task_id", task_id)),
            status=status,
            raw_status=str(payload.get("raw_status", "")),
            progress=int(payload.get("progress") or 0),
            video_url=str(payload.get("video_url") or ""),
            error=str(payload.get("error") or ""),
            created_at=str(payload.get("created_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
            failed=bool(payload.get("failed")),
        )

    @staticmethod
    def _error_from_body(status_code: int, body: bytes) -> DigitalHumanStatusError:
        """Read the capability's error contract without trusting its shape."""

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return DigitalHumanStatusError(
            str(payload.get("message") or "generation status request failed"),
            error_code=str(payload.get("error_code") or "UPSTREAM_ERROR"),
            retryable=bool(payload.get("retryable", status_code >= 500)),
            status_code=status_code,
        )
