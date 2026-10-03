"""Supervisor wrapper that verifies facts about the current topic.

The wrapper never decides the answer itself: the Research Agent produces a
source-traceable ``ResearchResult`` and ``RecruitmentStatusService`` turns it
into a deterministic recruitment status.  The Supervisor may only report the
status that comes back in the tool result.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_service.agent_tools.topic_expert import extract_display_index
from agent_service.agents.research import ResearchAgentError, ResearchAgentService
from agent_service.runtime.agent_status import (
    AGENT_RESEARCH,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import (
    claim_research_round,
    claim_specialist,
)
from agent_service.runtime.context import ContentAppContext
from agent_service.schemas.research import ResearchResult
from agent_service.schemas.state import ContentAgentState
from agent_service.services.recruitment_status import determine_recruitment_status
from agent_service.services.research_store import ResearchStore
from agent_service.services.task_store import TaskStore


def _tool_command(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    payload: dict[str, Any],
    *,
    current_topic_id: str | None = None,
    current_research_result_id: str | None = None,
) -> Command:
    if not runtime.tool_call_id:
        raise RuntimeError("research_expert requires a tool call ID")
    message = ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id,
    )
    update: dict[str, Any] = {"messages": [message]}
    if current_topic_id is not None:
        update["current_topic_id"] = current_topic_id
    if current_research_result_id is not None:
        update["current_research_result_id"] = current_research_result_id
    return Command(update=update)


def _context_dependencies(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
) -> tuple[ContentAppContext, TaskStore, ResearchStore, str, ResearchAgentService]:
    context = runtime.context
    if not isinstance(context, ContentAppContext):
        raise RuntimeError("research_expert requires ContentAppContext")
    if not isinstance(context.task_store, TaskStore):
        raise RuntimeError("research_expert requires a Business TaskStore")
    research_store = context.stores.get("research_store")
    if not isinstance(research_store, ResearchStore):
        raise RuntimeError("research_expert requires a Business ResearchStore")
    task_id = context.services.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError("research_expert requires a task_id")
    research_service = context.services.get("research_agent_service")
    if not isinstance(research_service, ResearchAgentService):
        raise RuntimeError("research_expert requires a ResearchAgentService")
    return context, context.task_store, research_store, task_id, research_service


def _decode_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
    return []


def _resolve_topic(
    task_store: TaskStore,
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    task: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the topic to verify, or return the error payload to report.

    A confirmed topic wins.  Otherwise a ``第 N 个`` reference is resolved
    against the persisted current candidate batch, so a user can ask about a
    candidate that topic_expert has not been asked to select yet.
    """

    topic_id = runtime.state.get("current_topic_id")
    if isinstance(topic_id, str) and topic_id.strip():
        row = task_store.get_candidate_by_topic_id(topic_id)
        if row is None:
            return None, {"error_code": "TOPIC_NOT_FOUND", "topic_id": topic_id}
        return row, None

    display_index = extract_display_index(task)
    if display_index is None:
        return None, {
            "error_code": "NO_CURRENT_TOPIC",
            "message": "There is no confirmed topic to verify.",
        }

    batch_id = runtime.state.get("current_candidate_batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        return None, {
            "error_code": "NO_CURRENT_CANDIDATE_BATCH",
            "message": "There is no current candidate batch to resolve.",
        }
    candidate = task_store.get_candidate_by_display_index(batch_id, display_index)
    if candidate is None:
        return None, {
            "error_code": "CANDIDATE_NOT_FOUND",
            "candidate_batch_id": batch_id,
            "display_index": display_index,
        }
    return candidate, None


def _research_instruction(task: str, candidate: Mapping[str, Any]) -> str:
    """Carry the confirmed candidate context into the Research Agent."""

    lines = [f"User question: {task.strip()}"]
    title = str(candidate.get("title", "")).strip()
    if title:
        lines.append(f"Candidate topic: {title}")
    for label, column in (
        ("Why now", "why_now"),
        ("Target audience", "target_audience"),
        ("Content angle", "content_angle"),
    ):
        value = str(candidate.get(column) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    key_points = _decode_list(candidate.get("key_points_json"))
    if key_points:
        lines.append("Key points to verify: " + "; ".join(key_points))
    candidate_sources = _decode_list(candidate.get("sources_json"))
    if candidate_sources:
        lines.append("Candidate source leads: " + "; ".join(candidate_sources))
    lines.append(
        "Return the current, source-traceable facts for this topic. Verify any "
        "recruitment window against an original source and record unknown when "
        "the evidence is insufficient."
    )
    return "\n".join(lines)


def _fact_payload(result: ResearchResult) -> list[dict[str, Any]]:
    return [
        {
            "fact_id": fact.fact_id,
            "claim": fact.claim,
            "source_ids": list(fact.source_ids),
            "verification_status": fact.verification_status,
            "valid_until": (
                fact.valid_until.isoformat() if fact.valid_until is not None else None
            ),
            "recruitment_status": fact.recruitment_status,
            "notes": fact.notes,
        }
        for fact in result.facts
    ]


def _source_payload(result: ResearchResult) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "source_type": source.source_type,
        }
        for source in result.sources
    ]


def create_research_expert_tool() -> Any:
    """Create the Research subagent tool mounted on Supervisor."""

    @tool(
        "research_expert",
        description=(
            "Verify whether the current topic or a numbered candidate is still "
            "current and still recruiting. Returns source-traceable facts and a "
            "deterministic recruitment status. Use for fact verification, not "
            "for writing a script."
        ),
    )
    async def research_expert(
        task: str,
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    ) -> Command:
        (
            context,
            task_store,
            research_store,
            task_id,
            research_service,
        ) = _context_dependencies(runtime)
        instruction = task.strip()
        if not instruction:
            return _tool_command(
                runtime,
                {"error_code": "INVALID_REQUEST", "message": "research task is blank"},
            )

        refusal = claim_specialist(context, 'research_expert')
        if refusal is not None:
            return _tool_command(runtime, refusal)
        refusal = claim_research_round(context)
        if refusal is not None:
            return _tool_command(runtime, refusal)

        candidate, error_payload = _resolve_topic(task_store, runtime, instruction)
        if error_payload is not None:
            return _tool_command(runtime, error_payload)
        assert candidate is not None  # _resolve_topic returns one or the other
        topic_id = str(candidate["topic_id"])

        await agent_status(AGENT_RESEARCH, STATUS_WORKING, message="正在核实事实")
        await tool_status("research_expert", STATUS_WORKING, agent=AGENT_RESEARCH)
        try:
            # Persistence stays with this wrapper, matching topic_expert and
            # script_expert: the exact result the Supervisor sees is the row
            # that was committed, and a service-level store is never written
            # twice for one call.
            produced = await research_service.generate(
                request_id=context.request_id,
                task=_research_instruction(instruction, candidate),
                task_id=None,
            )
            result = research_store.save_research_result(task_id, produced)
        except (ResearchAgentError, ValueError, sqlite3.Error) as exc:
            await agent_status(AGENT_RESEARCH, STATUS_ERROR, message="核实失败")
            await tool_status("research_expert", STATUS_FINISHED, agent=AGENT_RESEARCH)
            return _tool_command(
                runtime,
                {
                    "error_code": "RESEARCH_FAILED",
                    "message": str(exc),
                    "topic_id": topic_id,
                },
                current_topic_id=topic_id,
            )

        decision = determine_recruitment_status(result)
        await agent_status(AGENT_RESEARCH, STATUS_FINISHED, message="核实完成")
        await tool_status("research_expert", STATUS_FINISHED, agent=AGENT_RESEARCH)
        return _tool_command(
            runtime,
            {
                "kind": "research_result",
                "research_result_id": result.research_result_id,
                "topic_id": topic_id,
                "summary": result.summary,
                "answer_status": research_service.answer_status(result),
                "recruitment_status": decision.status,
                "reason_code": decision.reason_code,
                "reason": decision.reason,
                "conflict": decision.conflict,
                "evaluated_at": decision.evaluated_at.isoformat(),
                "facts": _fact_payload(result),
                "sources": _source_payload(result),
                "open_questions": list(result.open_questions),
            },
            current_topic_id=topic_id,
            current_research_result_id=result.research_result_id,
        )

    return research_expert


__all__ = ["create_research_expert_tool"]
