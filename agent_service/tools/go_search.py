"""Python client for the Go search capability endpoint.

This module is intentionally a data adapter.  It validates transport and
response shape, but it does not rank candidates or decide which topic is
recommended; those decisions belong to the Topic Agent/business layer.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_service.config import load_settings
from agent_service.runtime.budget import SEARCH_TIMEOUT_SECONDS, retry_once
from agent_service.schemas.research import SourceItem


def _source_id(url: str) -> str:
    """Create a stable, non-provider-specific identifier for a source URL."""

    return f"source_{sha256(url.encode('utf-8')).hexdigest()[:24]}"


def _parse_published_at(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SearchResult(BaseModel):
    """One structured result returned by Go's topicsearch service."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    type: str = ""
    title: str = ""
    organization: str = ""
    region: list[str] = Field(default_factory=list)
    time: str = ""
    location: str = ""
    target_audience: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    positions: list[str] = Field(default_factory=list)
    position_count: str = ""
    education_requirement: str = ""
    school_requirement: str = ""
    application_period: str = ""
    deadline: str = ""
    benefits: str = ""
    key_info: list[str] = Field(default_factory=list)
    source_title: str = ""
    source_url: str = ""
    source_type: str = ""
    published_at: str = ""

    def to_source_item(self, source_id: str | None = None) -> SourceItem | None:
        """Normalize a search result into the traceable research source shape."""

        source_url = self.source_url.strip()
        if not source_url:
            return None
        return SourceItem(
            source_id=source_id or _source_id(source_url),
            title=(self.source_title or self.title or source_url).strip(),
            url=source_url,
            source_type=(self.source_type or "web").strip(),
            published_at=_parse_published_at(self.published_at),
        )


class SearchPlatformResponse(BaseModel):
    """Parseable, recommendation-neutral search response from Go."""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(min_length=1)
    query: str = ""
    total_found: int = Field(default=0, ge=0)
    results: list[SearchResult] = Field(default_factory=list)
    ai_processed: bool = False
    search_provider: str = ""
    search_executed: bool = False
    web_search_call_count: int = Field(default=0, ge=0)
    raw_result_count: int = Field(default=0, ge=0)
    accepted_result_count: int = Field(default=0, ge=0)
    filtered_result_count: int = Field(default=0, ge=0)

    @property
    def source_items(self) -> list[SourceItem]:
        """Return only results that contain a usable source URL."""

        return [
            source
            for result in self.results
            if (source := result.to_source_item()) is not None
        ]


class GoSearchError(RuntimeError):
    """A structured failure while calling the Go search capability."""

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
        """Return the internal error contract without provider secrets."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
            "details": self.details,
        }


class GoSearchClient:
    """Call ``POST /internal/tools/search-platform`` on the Go gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = SEARCH_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        configured_base_url = base_url or load_settings().go_capability_base_url
        self.base_url = configured_base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    async def search_platform(
        self,
        *,
        request_id: str,
        query: str,
        max_results: int = 10,
    ) -> SearchPlatformResponse:
        """Fetch structured search data, retrying a retryable failure once."""

        return await retry_once(
            lambda: self._search_platform_once(
                request_id=request_id, query=query, max_results=max_results
            )
        )

    async def _search_platform_once(
        self,
        *,
        request_id: str,
        query: str,
        max_results: int = 10,
    ) -> SearchPlatformResponse:
        """Fetch structured search data; do not make a recommendation."""

        request_id = request_id.strip()
        query = query.strip()
        if not request_id or not query or max_results < 1:
            raise GoSearchError(
                "INVALID_REQUEST",
                "request_id、query 和 max_results 必须有效",
                retryable=False,
                request_id=request_id,
            )

        payload = {
            "request_id": request_id,
            "query": query,
            "max_results": max_results,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        url = f"{self.base_url}/internal/tools/search-platform"

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
                    response = await client.post(
                        url,
                        json=payload,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise GoSearchError(
                "CAPABILITY_TIMEOUT",
                "Go 搜索能力请求超时",
                retryable=True,
                request_id=request_id,
                status_code=504,
            ) from exc
        except httpx.RequestError as exc:
            raise GoSearchError(
                "CAPABILITY_UNAVAILABLE",
                "Go 搜索能力暂不可用",
                retryable=True,
                request_id=request_id,
                status_code=503,
                details={"transport_error": exc.__class__.__name__},
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise self._error_from_response(response, request_id)

        try:
            raw_payload = response.json()
            return SearchPlatformResponse.model_validate(raw_payload)
        except (ValueError, ValidationError) as exc:
            raise GoSearchError(
                "UPSTREAM_ERROR",
                "Go 搜索能力返回了不可解析的数据",
                retryable=True,
                request_id=request_id,
                status_code=502,
            ) from exc

    @staticmethod
    def _error_from_response(
        response: httpx.Response, request_id: str
    ) -> GoSearchError:
        status = response.status_code
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if isinstance(payload, dict) and isinstance(payload.get("error_code"), str):
            error_code = payload["error_code"]
            message = str(payload.get("message") or "Go 搜索能力请求失败")
            retryable = bool(payload.get("retryable", status >= 500))
            response_request_id = str(payload.get("request_id") or request_id)
            details = payload.get("details")
            if not isinstance(details, dict):
                details = {}
            return GoSearchError(
                error_code,
                message,
                retryable=retryable,
                request_id=response_request_id,
                status_code=status,
                details=details,
            )

        if status == 400:
            error_code, retryable = "INVALID_REQUEST", False
        elif status == 504:
            error_code, retryable = "CAPABILITY_TIMEOUT", True
        elif status in {502, 503, 429}:
            error_code, retryable = "CAPABILITY_UNAVAILABLE", True
        else:
            error_code, retryable = "UPSTREAM_ERROR", status >= 500
        return GoSearchError(
            error_code,
            "Go 搜索能力请求失败",
            retryable=retryable,
            request_id=request_id,
            status_code=status,
        )
