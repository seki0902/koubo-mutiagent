"""Python adapter for the Go source-fetch capability.

The adapter owns transport/error normalization and returns a traceable
``SourceItem``.  It deliberately does not extract research facts or make
claims about the fetched page.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_service.config import load_settings
from agent_service.runtime.budget import FETCH_SOURCE_TIMEOUT_SECONDS, retry_once
from agent_service.schemas.research import SourceItem


class SourceFetchError(RuntimeError):
    """A structured failure while calling the source-fetch capability."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool,
        request_id: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.status_code = status_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
            "details": self.details,
        }


class SourceFetchResponse(BaseModel):
    """Normalized source metadata and bounded page content."""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(min_length=1)
    source: SourceItem
    content: str
    final_url: str = Field(min_length=1)


def _stable_source_id(url: str) -> str:
    return f"source_{sha256(url.encode('utf-8')).hexdigest()[:24]}"


class SourceFetchClient:
    """Call ``POST /internal/tools/fetch-source`` on the Go gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = FETCH_SOURCE_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        configured_base_url = base_url or load_settings().go_capability_base_url
        self.base_url = configured_base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    async def fetch_source(
        self,
        *,
        request_id: str,
        source_url: str,
        max_chars: int = 6000,
        source_id: str | None = None,
        source_type: str = "web",
    ) -> SourceFetchResponse:
        """Fetch one source, retrying a retryable failure once."""

        return await retry_once(
            lambda: self._fetch_source_once(
                request_id=request_id,
                source_url=source_url,
                max_chars=max_chars,
                source_id=source_id,
                source_type=source_type,
            )
        )

    async def _fetch_source_once(
        self,
        *,
        request_id: str,
        source_url: str,
        max_chars: int = 6000,
        source_id: str | None = None,
        source_type: str = "web",
    ) -> SourceFetchResponse:
        request_id = request_id.strip()
        source_url = source_url.strip() if isinstance(source_url, str) else ""
        source_type = source_type.strip() or "web"
        if not request_id or not source_url or max_chars < 1 or max_chars > 12000:
            raise SourceFetchError(
                "INVALID_REQUEST",
                "request_id, source_url and max_chars must be valid",
                retryable=False,
                request_id=request_id,
            )
        try:
            parsed_url = httpx.URL(source_url)
        except Exception as exc:
            raise SourceFetchError(
                "INVALID_REQUEST",
                "source_url must be an HTTP(S) URL",
                retryable=False,
                request_id=request_id,
            ) from exc
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise SourceFetchError(
                "INVALID_REQUEST",
                "source_url must be an HTTP(S) URL",
                retryable=False,
                request_id=request_id,
            )

        payload = {
            "request_id": request_id,
            "source_url": source_url,
            "max_chars": max_chars,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        url = f"{self.base_url}/internal/tools/fetch-source"

        try:
            if self._client is not None:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise SourceFetchError(
                "CAPABILITY_TIMEOUT",
                "source fetch capability timed out",
                retryable=True,
                request_id=request_id,
                status_code=504,
            ) from exc
        except httpx.RequestError as exc:
            raise SourceFetchError(
                "CAPABILITY_UNAVAILABLE",
                "source fetch capability is unavailable",
                retryable=True,
                request_id=request_id,
                status_code=503,
                details={"transport_error": exc.__class__.__name__},
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise self._error_from_response(response, request_id)

        try:
            raw_payload = response.json()
            final_url = str(raw_payload.get("final_url") or source_url)
            source_url_value = str(raw_payload.get("source_url") or source_url)
            source = SourceItem(
                source_id=source_id or _stable_source_id(final_url),
                title=str(raw_payload.get("title") or source_url_value),
                url=final_url,
                source_type=source_type,
                published_at=raw_payload.get("published_at") or None,
                fetched_at=raw_payload.get("fetched_at") or None,
            )
            return SourceFetchResponse(
                request_id=str(raw_payload.get("request_id") or request_id),
                source=source,
                content=str(raw_payload.get("content") or ""),
                final_url=final_url,
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise SourceFetchError(
                "UPSTREAM_ERROR",
                "source fetch capability returned an invalid response",
                retryable=True,
                request_id=request_id,
                status_code=502,
            ) from exc

    async def fetch(self, **kwargs: Any) -> SourceFetchResponse:
        """Short alias for callers that treat tools as generic fetchers."""

        return await self.fetch_source(**kwargs)

    @staticmethod
    def _error_from_response(
        response: httpx.Response, request_id: str
    ) -> SourceFetchError:
        status = response.status_code
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if isinstance(payload, dict) and isinstance(payload.get("error_code"), str):
            details = payload.get("details")
            if not isinstance(details, dict):
                details = {}
            return SourceFetchError(
                payload["error_code"],
                str(payload.get("message") or "source fetch failed"),
                retryable=bool(payload.get("retryable", status >= 500)),
                request_id=str(payload.get("request_id") or request_id),
                status_code=status,
                details=details,
            )

        if status == 400:
            error_code, retryable = "INVALID_REQUEST", False
        elif status == 403:
            error_code, retryable = "CAPABILITY_BLOCKED", False
        elif status == 404:
            error_code, retryable = "CAPABILITY_NOT_FOUND", False
        elif status == 504:
            error_code, retryable = "CAPABILITY_TIMEOUT", True
        elif status in {502, 503, 429}:
            error_code, retryable = "CAPABILITY_UNAVAILABLE", True
        else:
            error_code, retryable = "UPSTREAM_ERROR", status >= 500
        return SourceFetchError(
            error_code,
            "source fetch failed",
            retryable=retryable,
            request_id=request_id,
            status_code=status,
        )


__all__ = ["SourceFetchClient", "SourceFetchError", "SourceFetchResponse"]
