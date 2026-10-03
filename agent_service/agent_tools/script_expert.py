"""Supervisor wrapper that generates and persists the first script version."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_service.agents.script import ScriptAgentError, ScriptAgentService
from agent_service.runtime.agent_status import (
    AGENT_SCRIPT,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import claim_specialist
from agent_service.runtime.context import ContentAppContext
from agent_service.runtime.streaming import emit
from agent_service.schemas.draft import DraftPayload
from agent_service.schemas.events import AgentEventType, DraftUpdatedPayload
from agent_service.schemas.state import ContentAgentState
from agent_service.schemas.topic import TopicCandidate
from agent_service.services.content_memory_store import ContentMemoryStore
from agent_service.services.task_store import TaskStore
from agent_service.tools.drafts import create_draft, save_draft_version


def _tool_command(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    payload: dict[str, Any],
    *,
    current_topic_id: str | None = None,
    current_draft_id: str | None = None,
    current_draft_version: int | None = None,
    clear_review: bool = False,
    clear_generation: bool = False,
) -> Command:
    if not runtime.tool_call_id:
        raise RuntimeError("script_expert requires a tool call ID")
    message = ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id,
    )
    update: dict[str, Any] = {"messages": [message]}
    if current_topic_id is not None:
        update["current_topic_id"] = current_topic_id
    if current_draft_id is not None:
        update["current_draft_id"] = current_draft_id
    if current_draft_version is not None:
        update["current_draft_version"] = current_draft_version
    if clear_review:
        update["active_review_id"] = None
    if clear_generation:
        update["generation_id"] = None
    return Command(update=update)


def _context_dependencies(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
) -> tuple[ContentAppContext, TaskStore, str, ScriptAgentService]:
    context = runtime.context
    if not isinstance(context, ContentAppContext):
        raise RuntimeError("script_expert requires ContentAppContext")
    if not isinstance(context.task_store, TaskStore):
        raise RuntimeError("script_expert requires a Business TaskStore")
    task_id = context.services.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError("script_expert requires a task_id")
    script_service = context.services.get("script_agent_service")
    if not isinstance(script_service, ScriptAgentService):
        raise RuntimeError("script_expert requires a ScriptAgentService")
    return context, context.task_store, task_id, script_service


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


def _candidate_from_row(row: dict[str, Any]) -> TopicCandidate:
    """Rehydrate the canonical candidate before passing it to Script Agent."""

    return TopicCandidate.model_validate(
        {
            "topic_id": row["topic_id"],
            "display_index": row["display_index"],
            "title": row["title"],
            "why_now": row.get("why_now", ""),
            "target_audience": row.get("target_audience", ""),
            "content_angle": row.get("content_angle", ""),
            "key_points": _decode_list(row.get("key_points_json", [])),
            "sources": _decode_list(row.get("sources_json", [])),
            "freshness": row.get("freshness", ""),
            "risks": _decode_list(row.get("risks_json", [])),
        }
    )


def _candidate_facts(candidate: TopicCandidate) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for point in candidate.key_points:
        facts.append({"statement": point, "source_id": "candidate_key_point"})
    for source in candidate.sources:
        facts.append({"statement": "Candidate source reference", "source_id": source})
    return facts


def create_script_expert_tool() -> Any:
    """Create the Script subagent tool mounted on Supervisor."""

    @tool(
        "script_expert",
        description=(
            "Generate one first-pass short-video script for the currently "
            "confirmed topic and save it as draft version 1. Use only after "
            "topic_expert has confirmed a topic."
        ),
    )
    async def script_expert(
        task: str,
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    ) -> Command:
        context, task_store, task_id, script_service = _context_dependencies(runtime)
        instruction = task.strip()
        if not instruction:
            return _tool_command(
                runtime,
                {"error_code": "INVALID_REQUEST", "message": "script task is blank"},
            )

        refusal = claim_specialist(context, 'script_expert')
        if refusal is not None:
            return _tool_command(runtime, refusal)

        topic_id = runtime.state.get("current_topic_id")
        if not isinstance(topic_id, str) or not topic_id.strip():
            return _tool_command(
                runtime,
                {
                    "error_code": "NO_CURRENT_TOPIC",
                    "message": "There is no confirmed topic for script generation.",
                },
            )

        candidate_row = task_store.get_candidate_by_topic_id(topic_id)
        if candidate_row is None:
            return _tool_command(
                runtime,
                {
                    "error_code": "TOPIC_NOT_FOUND",
                    "topic_id": topic_id,
                },
                current_topic_id=topic_id,
            )

        candidate = _candidate_from_row(candidate_row)
        current_draft_id = runtime.state.get("current_draft_id")
        current_draft = None
        editing_existing_draft = False
        if isinstance(current_draft_id, str) and current_draft_id.strip():
            draft_row = task_store.get_draft(current_draft_id)
            if draft_row is not None:
                editing_existing_draft = (
                    draft_row["task_id"] == task_id
                    and draft_row["topic_id"] == topic_id
                    and int(draft_row["current_version"]) > 0
                )
                if editing_existing_draft:
                    current_draft = task_store.get_current_draft(current_draft_id)
                    if current_draft is None:
                        raise RuntimeError("current draft pointer has no readable version")
        memory_store = context.stores.get("memory_store")
        if memory_store is not None and not isinstance(
            memory_store, ContentMemoryStore
        ):
            raise RuntimeError("script_expert received an invalid memory store")
        await agent_status(AGENT_SCRIPT, STATUS_WORKING, message="正在写稿")
        await tool_status("script_expert", STATUS_WORKING, agent=AGENT_SCRIPT)
        try:
            if editing_existing_draft:
                payload = await script_service.revise(
                    confirmed_topic=candidate,
                    current_draft=current_draft["body"],
                    edit_instruction=instruction,
                    verified_facts=_candidate_facts(candidate),
                    memory_store=memory_store,
                )
            else:
                payload = await script_service.generate(
                    confirmed_topic=candidate,
                    verified_facts=_candidate_facts(candidate),
                    memory_store=memory_store,
                )
        except (ScriptAgentError, ValueError) as exc:
            await agent_status(AGENT_SCRIPT, STATUS_ERROR, message="写稿失败")
            await tool_status("script_expert", STATUS_FINISHED, agent=AGENT_SCRIPT)
            return _tool_command(
                runtime,
                {
                    "error_code": "SCRIPT_GENERATION_FAILED",
                    "message": str(exc),
                    "topic_id": topic_id,
                },
                current_topic_id=topic_id,
            )

        if not isinstance(payload, DraftPayload):
            raise RuntimeError("script_agent_service returned an invalid DraftPayload")

        if editing_existing_draft:
            draft_id = str(current_draft_id)
        else:
            # The business ID is generated outside the model. create_draft
            # writes the drafts row and v1 in one transaction before the
            # checkpoint pointer is returned in the Command below.
            draft_id = f"draft_{uuid4().hex}"

        try:
            if editing_existing_draft:
                version_model = save_draft_version(
                    task_store,
                    draft_id,
                    payload.full_script,
                    change_instruction=instruction,
                    created_by="script_agent",
                    invalidate_authorizations=True,
                )
                draft = task_store.get_draft(draft_id)
                if draft is None:
                    raise RuntimeError("revised draft disappeared after commit")
                version = version_model.model_dump(mode="json")
                kind = "draft_revised"
            else:
                draft_model = create_draft(
                    task_store,
                    draft_id,
                    task_id,
                    topic_id,
                    payload.full_script,
                    created_by="script_agent",
                )
                draft = draft_model.model_dump(mode="json")
                version = task_store.get_current_draft(draft_id)
                kind = "draft_created"
        except (sqlite3.Error, ValueError) as exc:
            # A commit that failed leaves the committed version untouched, so
            # the turn reports a save failure instead of a saved draft.
            await agent_status(AGENT_SCRIPT, STATUS_ERROR, message="稿件保存失败")
            await tool_status("script_expert", STATUS_FINISHED, agent=AGENT_SCRIPT)
            return _tool_command(
                runtime,
                {
                    "error_code": "DRAFT_SAVE_FAILED",
                    "message": str(exc),
                    "topic_id": topic_id,
                },
                current_topic_id=topic_id,
            )
        if version is None:
            raise RuntimeError("draft v1 was not readable after commit")

        # Every write of a draft version is announced, so the business trace
        # can show when the current version changed and to what.
        await emit(
            AgentEventType.DRAFT_UPDATED,
            DraftUpdatedPayload(
                draft_id=draft_id, draft_version=int(version["version"])
            ),
        )
        await agent_status(AGENT_SCRIPT, STATUS_FINISHED, message="稿件已就绪")
        await tool_status("script_expert", STATUS_FINISHED, agent=AGENT_SCRIPT)

        return _tool_command(
            runtime,
            {
                "kind": kind,
                "draft_id": draft_id,
                "draft_version": version["version"],
                "topic_id": topic_id,
                "script": version["body"],
                "change_instruction": instruction if editing_existing_draft else "",
                "sections": [section.model_dump() for section in payload.sections],
                "sources_used": payload.sources_used,
                "uncertainties": payload.uncertainties,
            },
            current_topic_id=topic_id,
            current_draft_id=draft_id,
            current_draft_version=version["version"],
            clear_review=True,
            clear_generation=True,
        )

    return script_expert


__all__ = ["create_script_expert_tool"]
