"""Standalone Research & Verification Agent.

The Research Agent gathers evidence with the Search and Fetch capability
adapters, asks the model to produce a source-traceable ``ResearchResult``,
and optionally commits that result to the Business SQLite store.  It does not
recommend topics or write scripts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import BaseTool, tool
from pydantic import ValidationError

from agent_service.config import AgentSettings, load_settings
from agent_service.runtime.budget import RECURSION_LIMIT, search_call_limit
from agent_service.runtime.model_factory import get_model
from agent_service.schemas.research import ResearchFact, ResearchResult, SourceItem
from agent_service.services.research_store import ResearchStore
from agent_service.tools.go_search import GoSearchClient, GoSearchError
from agent_service.tools.source_fetch import (
    SourceFetchClient,
    SourceFetchError,
)


ResearchAnswerStatus = Literal["verified", "unknown"]


class ResearchAgentError(RuntimeError):
    """Raised when the standalone Research Agent cannot produce valid output."""


def load_research_principles() -> str:
    """Load the human-editable rules for source quality and uncertainty."""

    path = Path(__file__).resolve().parents[1] / "principles" / "research.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResearchAgentError(f"Research principles are unavailable: {path}") from exc


def _json_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("tool response must be a Pydantic model or mapping")
    if not isinstance(payload, dict):
        raise TypeError("tool response payload must be an object")
    return payload


def _record_source(evidence_sources: dict[str, SourceItem], source: SourceItem) -> None:
    evidence_sources[source.source_id] = source


def _build_search_tool(
    search_client: Any,
    request_id: str,
    evidence_sources: dict[str, SourceItem],
    tool_errors: list[str],
    *,
    max_calls: int | None = None,
) -> BaseTool:
    """Build the bounded search capability exposed to the Research Agent.

    Bounded in calls as well as in scope: an agent that re-searches instead of
    concluding is the same latency problem the Topic Agent had. Past the cap the
    call is answered with a bounded refusal that names the limit, so the answer
    can say the evidence is incomplete rather than pretend it is complete.
    """

    limit = search_call_limit() if max_calls is None else max_calls
    calls = {"count": 0}

    @tool(
        "search_recruitment",
        description=(
            "Search current recruitment and official public sources for facts. "
            "Return evidence only; do not recommend a topic or infer that an "
            f"unclosed listing is still open. Call it at most {limit} times "
            "for one request: after that, conclude from the evidence you have."
        ),
    )
    async def search_recruitment(query: str) -> str:
        calls["count"] += 1
        if calls["count"] > limit:
            return json.dumps(
                {
                    "error": {
                        "error_code": "SEARCH_BUDGET_REACHED",
                        "message": (
                            f"本次已联网搜索 {limit} 次，已达上限：请用已有证据给出结论，"
                            "证据不足就如实标记 unknown，并在 open_questions 里写明未查完。"
                        ),
                        "retryable": False,
                        "request_id": request_id,
                    }
                },
                ensure_ascii=False,
            )
        try:
            response = await search_client.search_platform(
                request_id=request_id,
                query=query,
                max_results=10,
            )
            payload = _json_payload(response)
            source_items = getattr(response, "source_items", [])
            payload["source_items"] = []
            for source in source_items:
                if isinstance(source, SourceItem):
                    _record_source(evidence_sources, source)
                    payload["source_items"].append(source.model_dump(mode="json"))
            return json.dumps(payload, ensure_ascii=False)
        except GoSearchError as exc:
            tool_errors.append(exc.error_code)
            return json.dumps({"error": exc.as_dict()}, ensure_ascii=False)
        except Exception as exc:
            tool_errors.append("SEARCH_TOOL_ERROR")
            return json.dumps(
                {
                    "error": {
                        "error_code": "SEARCH_TOOL_ERROR",
                        "message": str(exc),
                        "retryable": False,
                        "request_id": request_id,
                    }
                },
                ensure_ascii=False,
            )

    return search_recruitment


def _build_fetch_tool(
    fetch_client: Any,
    request_id: str,
    evidence_sources: dict[str, SourceItem],
    tool_errors: list[str],
) -> BaseTool:
    """Build the bounded source-fetch capability exposed to the Agent."""

    @tool(
        "fetch_source",
        description=(
            "Fetch one candidate source and return bounded readable content. "
            "Prefer the original official page before relying on a repost."
        ),
    )
    async def fetch_source(
        source_url: str,
        source_id: str | None = None,
        source_type: str = "web",
    ) -> str:
        try:
            response = await fetch_client.fetch_source(
                request_id=request_id,
                source_url=source_url,
                source_id=source_id,
                source_type=source_type,
            )
            source = getattr(response, "source", None)
            if isinstance(source, SourceItem):
                _record_source(evidence_sources, source)
            return json.dumps(_json_payload(response), ensure_ascii=False)
        except SourceFetchError as exc:
            tool_errors.append(exc.error_code)
            return json.dumps({"error": exc.as_dict()}, ensure_ascii=False)
        except Exception as exc:
            tool_errors.append("FETCH_TOOL_ERROR")
            return json.dumps(
                {
                    "error": {
                        "error_code": "FETCH_TOOL_ERROR",
                        "message": str(exc),
                        "retryable": False,
                        "request_id": request_id,
                    }
                },
                ensure_ascii=False,
            )

    return fetch_source


def _create_research_agent_with_evidence(
    *,
    model: Any,
    search_client: Any,
    fetch_client: Any,
    request_id: str,
    evidence_sources: dict[str, SourceItem],
    tool_errors: list[str],
):
    tools = [
        _build_search_tool(
            search_client, request_id, evidence_sources, tool_errors
        ),
        _build_fetch_tool(fetch_client, request_id, evidence_sources, tool_errors),
    ]
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=load_research_principles(),
        # ToolStrategy, not the bare schema: a bare schema is auto-detected into
        # the provider's native structured output, and DeepSeek rejects the
        # OpenAI-style ``response_format: {"type": "json_schema"}`` that produces
        # with a 400.  The tool-calling route is the one DeepSeek supports.
        response_format=ToolStrategy(ResearchResult),
        name="research_agent",
    )


def create_research_agent(
    *,
    model: Any | None = None,
    search_client: GoSearchClient | None = None,
    fetch_client: SourceFetchClient | None = None,
    settings: AgentSettings | None = None,
    request_id: str = "research-agent",
):
    """Build an isolated Research Agent graph with structured output."""

    resolved_settings = settings or load_settings()
    resolved_model = (
        model
        if model is not None
        else get_model("supervisor", resolved_settings, disable_thinking=True)
    )
    resolved_search = search_client or GoSearchClient(
        base_url=resolved_settings.go_capability_base_url
    )
    resolved_fetch = fetch_client or SourceFetchClient(
        base_url=resolved_settings.go_capability_base_url
    )
    normalized_request_id = request_id.strip() or f"req_research_{uuid4().hex}"
    return _create_research_agent_with_evidence(
        model=resolved_model,
        search_client=resolved_search,
        fetch_client=resolved_fetch,
        request_id=normalized_request_id,
        evidence_sources={},
        tool_errors=[],
    )


def _coerce_research_result(value: Any) -> ResearchResult:
    if isinstance(value, ResearchResult):
        return value
    if isinstance(value, BaseException):
        raise ResearchAgentError("Research Agent returned an exception") from value
    try:
        payload = value.model_dump() if hasattr(value, "model_dump") else value
        if not isinstance(payload, Mapping):
            raise TypeError("structured response must be an object")
        normalized = dict(payload)
        # ``unknown`` is useful language for callers, while the persisted
        # schema uses the explicit not_verified state.
        for fact in normalized.get("facts", []):
            if isinstance(fact, Mapping) and fact.get("verification_status") == "unknown":
                fact["verification_status"] = "not_verified"
                fact.setdefault("recruitment_status", "unknown")
        return ResearchResult.model_validate(normalized)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ResearchAgentError(
            "Research Agent did not return a structured ResearchResult"
        ) from exc


def _append_note(existing: str | None, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}; {addition}"


def _source_rank(source: SourceItem) -> int:
    source_type = source.source_type.strip().lower()
    if source_type in {"official", "government", "university", "employer"}:
        return 0
    if "recruitment" in source_type or "platform" in source_type:
        return 1
    return 2


def _normalize_evidence(
    result: ResearchResult,
    *,
    evidence_sources: dict[str, SourceItem],
    tool_errors: list[str],
) -> ResearchResult:
    """Keep model metadata traceable to observed tool evidence when possible."""

    observed_by_url = {source.url: source for source in evidence_sources.values()}
    source_id_replacements: dict[str, str] = {}
    normalized_sources: list[SourceItem] = []
    seen_source_ids: set[str] = set()
    for source in result.sources:
        observed = evidence_sources.get(source.source_id) or observed_by_url.get(source.url)
        canonical = observed or source
        source_id_replacements[source.source_id] = canonical.source_id
        if canonical.source_id not in seen_source_ids:
            normalized_sources.append(canonical)
            seen_source_ids.add(canonical.source_id)

    observed_ids = set(evidence_sources)
    normalized_facts: list[ResearchFact] = []
    for fact in result.facts:
        source_ids = [source_id_replacements.get(source_id, source_id) for source_id in fact.source_ids]
        missing_observed_sources = bool(observed_ids) and bool(
            set(source_ids) - observed_ids
        )
        evidence_gap = bool(tool_errors) or (not observed_ids and fact.verification_status == "verified")
        if missing_observed_sources or evidence_gap:
            fact = fact.model_copy(
                update={
                    "verification_status": "not_verified",
                    "recruitment_status": (
                        "unknown"
                        if fact.recruitment_status != "not_applicable"
                        else fact.recruitment_status
                    ),
                    "verified_at": None,
                    "source_ids": source_ids,
                    "notes": _append_note(
                        fact.notes,
                        "Evidence was insufficient for a verified conclusion.",
                    ),
                }
            )
        else:
            fact = fact.model_copy(update={"source_ids": source_ids})
        normalized_facts.append(fact)

    normalized_sources.sort(key=_source_rank)
    return result.model_copy(
        update={
            "research_result_id": f"research_{uuid4().hex}",
            "sources": normalized_sources,
            "facts": normalized_facts,
        }
    )


class ResearchAgentService:
    """Independent entry point for evidence collection and verification."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        search_client: GoSearchClient | None = None,
        fetch_client: SourceFetchClient | None = None,
        settings: AgentSettings | None = None,
        store: ResearchStore | None = None,
    ) -> None:
        self.model = model
        self.search_client = search_client
        self.fetch_client = fetch_client
        self.settings = settings
        self.store = store

    async def _invoke(
        self,
        *,
        request_id: str,
        task: str,
    ) -> ResearchResult:
        resolved_settings = self.settings or load_settings()
        resolved_model = (
            self.model
            if self.model is not None
            else get_model("supervisor", resolved_settings, disable_thinking=True)
        )
        resolved_search = self.search_client or GoSearchClient(
            base_url=resolved_settings.go_capability_base_url
        )
        resolved_fetch = self.fetch_client or SourceFetchClient(
            base_url=resolved_settings.go_capability_base_url
        )
        evidence_sources: dict[str, SourceItem] = {}
        tool_errors: list[str] = []
        agent = _create_research_agent_with_evidence(
            model=resolved_model,
            search_client=resolved_search,
            fetch_client=resolved_fetch,
            request_id=request_id,
            evidence_sources=evidence_sources,
            tool_errors=tool_errors,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": task.strip()}]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        structured = result.get("structured_response")
        return _normalize_evidence(
            _coerce_research_result(structured),
            evidence_sources=evidence_sources,
            tool_errors=tool_errors,
        )

    @staticmethod
    def answer_status(result: ResearchResult) -> ResearchAnswerStatus:
        """Expose the user-facing two-state answer without erasing detail."""

        if result.facts and all(
            fact.verification_status == "verified"
            and fact.recruitment_status != "unknown"
            for fact in result.facts
        ):
            return "verified"
        return "unknown"

    async def generate(
        self,
        *,
        request_id: str,
        task: str,
        task_id: str | None = None,
    ) -> ResearchResult:
        """Run the specialist and optionally persist its canonical result."""

        if not request_id.strip():
            raise ValueError("request_id must not be blank")
        if not task.strip():
            raise ValueError("task must not be blank")
        if task_id is not None and not task_id.strip():
            raise ValueError("task_id must not be blank when provided")

        result = await self._invoke(request_id=request_id.strip(), task=task)
        if task_id is None:
            return result
        if self.store is None:
            raise ResearchAgentError(
                "task_id was provided but no ResearchStore is configured"
            )
        try:
            return self.store.save_research_result(task_id, result)
        except Exception as exc:
            raise ResearchAgentError("ResearchResult persistence failed") from exc

    async def run(
        self,
        *,
        request_id: str,
        task: str,
        task_id: str | None = None,
    ) -> ResearchResult:
        """Alias for callers that use a specialist ``run`` convention."""

        return await self.generate(
            request_id=request_id,
            task=task,
            task_id=task_id,
        )


__all__ = [
    "ResearchAgentError",
    "ResearchAgentService",
    "ResearchAnswerStatus",
    "create_research_agent",
    "load_research_principles",
]
