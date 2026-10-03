"""Standalone Topic Strategy Agent.

This module intentionally exposes only the specialist graph. Supervisor
integration, candidate-batch persistence, and display-index resolution belong
to later tasks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import BaseTool, tool

from agent_service.config import AgentSettings, load_settings
from agent_service.runtime.budget import RECURSION_LIMIT, search_call_limit
from agent_service.runtime.agent_status import (
    AGENT_TOPIC, STATUS_WORKING, STATUS_FINISHED, agent_status, tool_status,
)
from agent_service.runtime.model_factory import get_model
from agent_service.schemas.topic import TopicCandidate, TopicResult
from agent_service.services.content_memory_store import ContentMemoryStore
from agent_service.tools.go_search import GoSearchClient, GoSearchError
from agent_service.tools.memory import get_recent_topics


class TopicAgentError(RuntimeError):
    """Raised when the standalone Topic Agent cannot produce a valid result."""


def load_topic_principles() -> str:
    """Load the human-editable principles used as the system prompt."""

    path = Path(__file__).resolve().parents[1] / "principles" / "topic.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TopicAgentError(f"Topic principles are unavailable: {path}") from exc


def _build_search_tool(
    search_client: GoSearchClient, request_id: str, *, max_calls: int | None = None
) -> BaseTool:
    """Create the one capability Tool allowed in this Task.

    The tool is capped per request. A turn that keeps re-searching is what made
    a topic request take a minute and more: one real turn called it six times at
    3-11 seconds each. Past the cap the extra call is answered with a bounded
    result that says the search stopped early, so the model can pass that limit
    on to the user instead of quietly returning a thinner batch.
    """

    limit = search_call_limit() if max_calls is None else max_calls
    calls = {"count": 0}

    @tool(
        "search_platform_content",
        description=(
            "Search current platform content and return structured evidence. "
            "Use it for time-sensitive or factual topic claims; it does not "
            f"recommend a topic. Call it at most {limit} times for one "
            "request: after that, work from the results you already have."
        ),
    )
    async def search_platform_content(query: str) -> str:
        calls["count"] += 1
        if calls["count"] > limit:
            return json.dumps(
                {
                    "error_code": "SEARCH_BUDGET_REACHED",
                    "message": (
                        f"本次已联网搜索 {limit} 次，已达上限，请直接用已有结果给出候选选题，"
                        "不要再调用搜索；并在 uncertainties 里写明“检索次数已达上限，可能不全”。"
                    ),
                    "retryable": False,
                },
                ensure_ascii=False,
            )
        started = perf_counter()
        progress_message = "搜索暂未完成，正在整理已知信息"
        await agent_status(AGENT_TOPIC, STATUS_WORKING, message="正在联网搜索招聘信息")
        await tool_status("search_platform_content", STATUS_WORKING, agent=AGENT_TOPIC)
        try:
            response = await search_client.search_platform(
                request_id=request_id,
                query=query,
                max_results=10,
            )
            logging.getLogger(__name__).info(
                "topic_search request_id=%s seconds=%.3f provider=%s web_calls=%d results=%d",
                request_id, perf_counter() - started, response.search_provider,
                response.web_search_call_count, len(response.results),
            )
            progress_message = f"找到 {len(response.results)} 条招聘线索，正在整理选题"
            return response.model_dump_json()
        except GoSearchError as exc:
            # Return a bounded, structured tool result so the Agent can mark
            # uncertainty instead of turning an evidence gap into a fact.
            return json.dumps({"error": exc.as_dict()}, ensure_ascii=False)
        finally:
            await tool_status("search_platform_content", STATUS_FINISHED, agent=AGENT_TOPIC)
            await agent_status(AGENT_TOPIC, STATUS_WORKING, message=progress_message)

    return search_platform_content


def _build_recent_topics_tool(memory_store: ContentMemoryStore) -> BaseTool:
    """Create the one long-term memory Tool allowed on this Agent.

    The body is async on purpose: the business store opens its SQLite
    connection on the event loop thread, and a sync tool body would be run in a
    worker thread where that connection may not be used.
    """

    @tool(
        "get_recent_topics",
        description=(
            "Return the topics this account has already recorded, newest "
            "first, as historical fact. Use it when the request depends on "
            "past coverage; it does not recommend what to do next."
        ),
    )
    async def get_recent_topics_tool(limit: int = 10, only_used: bool = False) -> str:
        entries = get_recent_topics(
            memory_store, limit=limit, only_used=only_used
        )
        return json.dumps(
            {"topics": [entry.model_dump(mode="json") for entry in entries]},
            ensure_ascii=False,
        )

    return get_recent_topics_tool


def create_topic_agent(
    *,
    model: Any | None = None,
    search_client: GoSearchClient | None = None,
    settings: AgentSettings | None = None,
    request_id: str = "topic-agent",
    memory_store: ContentMemoryStore | None = None,
):
    """Build an isolated Topic Agent graph with structured output.

    Long-term memory is mounted only when a store is supplied, so an Agent
    without memory cannot reach the history tables at all.  The model decides
    when to call the Tool; nothing is pre-fetched into the prompt.
    """

    resolved_settings = settings or load_settings()
    resolved_model = (
        model
        if model is not None
        else get_model("supervisor", resolved_settings, disable_thinking=True)
    )
    resolved_client = search_client or GoSearchClient(
        base_url=resolved_settings.go_capability_base_url
    )
    normalized_request_id = request_id.strip() or f"req_topic_{uuid4().hex}"

    tools: list[BaseTool] = [
        _build_search_tool(resolved_client, normalized_request_id)
    ]
    if memory_store is not None:
        tools.append(_build_recent_topics_tool(memory_store))

    return create_agent(
        model=resolved_model,
        tools=tools,
        system_prompt=load_topic_principles(),
        # ToolStrategy, not the bare schema: a bare schema is auto-detected into
        # the provider's native structured output, and DeepSeek rejects the
        # OpenAI-style ``response_format: {"type": "json_schema"}`` that produces
        # with a 400.  The tool-calling route is the one DeepSeek supports.
        response_format=ToolStrategy(TopicResult),
        name="topic_agent",
    )


def _assign_business_ids(result: TopicResult) -> TopicResult:
    """Replace model placeholders with IDs owned by the business layer.

    Persistence and long-term resolution are intentionally deferred. The IDs
    created here are unique for this independent run and are not database
    primary keys chosen by the model.
    """

    candidate_batch_id = f"batch_{uuid4().hex}"
    candidates: list[TopicCandidate] = []
    for display_index, candidate in enumerate(result.topic_candidates, start=1):
        candidates.append(
            candidate.model_copy(
                update={
                    "topic_id": f"topic_{uuid4().hex}",
                    "display_index": display_index,
                }
            )
        )
    return result.model_copy(
        update={
            "candidate_batch_id": candidate_batch_id,
            "topic_candidates": candidates,
        }
    )


class TopicAgentService:
    """Independent entry point for invoking the Topic Agent."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        search_client: GoSearchClient | None = None,
        settings: AgentSettings | None = None,
    ) -> None:
        self.model = model
        self.search_client = search_client
        self.settings = settings

    async def generate(
        self,
        *,
        request_id: str,
        task: str,
        memory_store: ContentMemoryStore | None = None,
    ) -> TopicResult:
        """Run the specialist and return a validated topic result.

        The batch is bounded to five, but it is not required to reach three: the
        search gate is strict, so a narrow query can honestly yield one or two
        current events.  Failing the whole turn for that (the previous rule)
        turned a thin result into an error the user could do nothing with.
        """

        if not request_id.strip():
            raise ValueError("request_id must not be blank")
        if not task.strip():
            raise ValueError("task must not be blank")

        agent = create_topic_agent(
            model=self.model,
            search_client=self.search_client,
            settings=self.settings,
            request_id=request_id,
            memory_store=memory_store,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": task.strip()}]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        structured = result.get("structured_response")
        if not isinstance(structured, TopicResult):
            raise TopicAgentError(
                "Topic Agent did not return a structured TopicResult"
            )
        candidates = list(structured.topic_candidates)
        if not candidates:
            raise TopicAgentError(
                "搜索没有找到符合时效和范围的招聘活动，本轮没有可用选题"
            )
        update: dict[str, Any] = {}
        if len(candidates) > 5:
            update["topic_candidates"] = candidates[:5]
        if len(candidates) < 3:
            update["uncertainties"] = [
                *structured.uncertainties,
                f"本轮只检索到 {len(candidates)} 条通过时效与范围校验的候选，少于常规的 3-5 条",
            ]
        if update:
            structured = structured.model_copy(update=update)
        return _assign_business_ids(structured)

    async def run(
        self,
        *,
        request_id: str,
        task: str,
        memory_store: ContentMemoryStore | None = None,
    ) -> TopicResult:
        """Alias kept for callers that treat specialist execution as a run."""

        return await self.generate(
            request_id=request_id, task=task, memory_store=memory_store
        )
