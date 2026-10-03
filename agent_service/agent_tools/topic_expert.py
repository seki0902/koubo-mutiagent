"""Supervisor wrapper for the Topic Agent and candidate ordinal resolver."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_service.agents.topic import TopicAgentService
from agent_service.runtime.agent_status import (
    AGENT_TOPIC,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import claim_specialist
from agent_service.runtime.context import ContentAppContext
from agent_service.runtime.streaming import emit
from agent_service.schemas.events import (
    AgentEventType,
    CandidateBatchUpdatedPayload,
    CandidateCardPayload,
)
from agent_service.schemas.state import ContentAgentState
from agent_service.services.content_memory_store import ContentMemoryStore
from agent_service.services.task_store import TaskStore


#: A reference to a candidate is "第 N 个/条/篇/项".  Requiring a measure word
#: keeps step references such as "第一步" out, and accepting the words people
#: actually use matters: with only "个" recognized, "第 1 条" fell through to
#: the generate branch, so every selection produced a new batch and the topic
#: was never confirmed.
_ORDINAL_RE = re.compile(
    r"\u7b2c\s*(\d+|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341])\s*"
    r"[\u4e2a\u6761\u7bc7\u9879]"
)
_CHINESE_DIGITS = {
    "\u4e00": 1,
    "\u4e8c": 2,
    "\u4e09": 3,
    "\u56db": 4,
    "\u4e94": 5,
    "\u516d": 6,
    "\u4e03": 7,
    "\u516b": 8,
    "\u4e5d": 9,
    "\u5341": 10,
}


def extract_display_index(text: str) -> int | None:
    """Extract a Chinese/Arabic ``第 N 个`` reference, if present."""

    match = _ORDINAL_RE.search(text)
    if match is None:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else _CHINESE_DIGITS[value]


def _tool_command(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    payload: dict[str, Any],
    *,
    current_candidate_batch_id: str | None = None,
    current_topic_id: str | None = None,
) -> Command:
    if not runtime.tool_call_id:
        raise RuntimeError("topic_expert requires a tool call ID")
    message = ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id,
    )
    return Command(
        update={
            "messages": [message],
            "current_candidate_batch_id": current_candidate_batch_id,
            "current_topic_id": current_topic_id,
        }
    )


def _context_dependencies(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
) -> tuple[ContentAppContext, TaskStore, str, TopicAgentService]:
    context = runtime.context
    if not isinstance(context, ContentAppContext):
        raise RuntimeError("topic_expert requires ContentAppContext")
    if not isinstance(context.task_store, TaskStore):
        raise RuntimeError("topic_expert requires a Business TaskStore")
    task_id = context.services.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError("topic_expert requires a task_id")
    topic_service = context.services.get("topic_agent_service")
    if not isinstance(topic_service, TopicAgentService):
        raise RuntimeError("topic_expert requires a TopicAgentService")
    return context, context.task_store, task_id, topic_service


def _cards(candidates: list[dict[str, Any]]) -> list[CandidateCardPayload]:
    """Render persisted candidates as cards carrying their stored mapping.

    The values come from the committed rows rather than from the model's own
    output, so the card cannot show an index the resolver would not answer to.
    """

    return [
        CandidateCardPayload(
            topic_id=str(candidate["topic_id"]),
            display_index=int(candidate["display_index"]),
            title=str(candidate["title"]),
        )
        for candidate in candidates
    ]


def create_topic_expert_tool() -> Any:
    """Create the only Topic subagent tool mounted on Supervisor."""

    @tool(
        "topic_expert",
        description=(
            "Generate a current batch of 3-5 topic candidates, or resolve a "
            "reference such as 'the third one' against the current persisted "
            "candidate batch. Use for topic discovery and topic selection only."
        ),
    )
    async def topic_expert(
        task: str,
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    ) -> Command:
        context, task_store, task_id, topic_service = _context_dependencies(runtime)
        task = task.strip()
        if not task:
            return _tool_command(
                runtime,
                {"error_code": "INVALID_REQUEST", "message": "topic task is blank"},
            )

        refusal = claim_specialist(context, 'topic_expert')
        if refusal is not None:
            return _tool_command(runtime, refusal)

        await agent_status(AGENT_TOPIC, STATUS_WORKING, message="正在准备选题")
        await tool_status("topic_expert", STATUS_WORKING, agent=AGENT_TOPIC)
        try:
            return await _run_topic_expert(
                task=task,
                runtime=runtime,
                context=context,
                task_store=task_store,
                task_id=task_id,
                topic_service=topic_service,
            )
        except Exception:
            # A raised failure still has to stop the indicator, otherwise the
            # frontend keeps showing a specialist that is no longer running.
            await agent_status(AGENT_TOPIC, STATUS_ERROR, message="选题处理失败")
            await tool_status("topic_expert", STATUS_FINISHED, agent=AGENT_TOPIC)
            raise

    return topic_expert


async def _run_topic_expert(
    *,
    task: str,
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    context: ContentAppContext,
    task_store: TaskStore,
    task_id: str,
    topic_service: TopicAgentService,
) -> Command:
    """Do the Topic work, with the working state already announced."""

    display_index = extract_display_index(task)
    if display_index is not None:
        batch_id = runtime.state.get("current_candidate_batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            await _topic_finished()
            return _tool_command(
                runtime,
                {
                    "error_code": "NO_CURRENT_CANDIDATE_BATCH",
                    "message": "There is no current candidate batch to resolve.",
                },
            )
        candidate = task_store.get_candidate_by_display_index(
            batch_id, display_index
        )
        if candidate is None:
            await _topic_finished()
            return _tool_command(
                runtime,
                {
                    "error_code": "CANDIDATE_NOT_FOUND",
                    "candidate_batch_id": batch_id,
                    "display_index": display_index,
                },
                current_candidate_batch_id=batch_id,
            )
        await _topic_finished()
        return _tool_command(
            runtime,
            {
                "kind": "topic_selected",
                "candidate_batch_id": batch_id,
                "display_index": display_index,
                "topic_id": candidate["topic_id"],
                "title": candidate["title"],
            },
            current_candidate_batch_id=batch_id,
            current_topic_id=str(candidate["topic_id"]),
        )

    memory_store = context.stores.get("memory_store")
    if memory_store is not None and not isinstance(
        memory_store, ContentMemoryStore
    ):
        raise RuntimeError("topic_expert received an invalid memory store")
    result = await topic_service.generate(
        request_id=context.request_id,
        task=task,
        memory_store=memory_store,
    )
    task_store.save_candidate_batch(task_id, result)
    # Cards are read back from the committed batch, so the event carries the
    # same display mapping that the ordinal resolver later answers with.
    candidates = task_store.list_candidate_items(result.candidate_batch_id)
    # A no-op unless this turn is being streamed: the blocking path shares
    # the tool and must not learn about the bus.
    await emit(
        AgentEventType.CANDIDATE_BATCH_UPDATED,
        CandidateBatchUpdatedPayload(
            candidate_batch_id=result.candidate_batch_id,
            candidate_count=len(candidates),
            summary=result.summary,
            candidates=_cards(candidates),
        ),
    )
    await _topic_finished()
    return _tool_command(
        runtime,
        {
            "kind": "candidate_batch",
            "candidate_batch_id": result.candidate_batch_id,
            "summary": result.summary,
            "candidates": candidates,
            "research_needed": result.research_needed,
            "uncertainties": result.uncertainties,
        },
        current_candidate_batch_id=result.candidate_batch_id,
        current_topic_id=None,
    )


async def _topic_finished() -> None:
    """Close out one Topic working state."""

    await agent_status(AGENT_TOPIC, STATUS_FINISHED, message="选题已就绪")
    await tool_status("topic_expert", STATUS_FINISHED, agent=AGENT_TOPIC)
