"""Supervisor wrapper that reviews the current draft version.

The wrapper resolves the version deterministically from the draft's committed
``current_version`` pointer and persists the verdict against that exact
version.  The Supervisor never names a version, so a review cannot be attached
to a body the user is not looking at.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_service.agents.review import (
    ContentReviewAgentError,
    ContentReviewAgentService,
)
from agent_service.runtime.agent_status import (
    AGENT_REVIEW,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import claim_specialist
from agent_service.runtime.context import ContentAppContext
from agent_service.runtime.streaming import emit
from agent_service.schemas.events import AgentEventType, ReviewUpdatedPayload
from agent_service.schemas.research import ResearchResult
from agent_service.schemas.state import ContentAgentState
from agent_service.services.research_store import ResearchStore
from agent_service.services.review_store import ReviewStore
from agent_service.services.task_store import TaskStore


def _tool_command(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    payload: dict[str, Any],
    *,
    active_review_id: str | None = None,
) -> Command:
    if not runtime.tool_call_id:
        raise RuntimeError("content_reviewer requires a tool call ID")
    message = ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id,
    )
    update: dict[str, Any] = {"messages": [message]}
    # A verdict that is not the draft's active verdict never becomes the
    # pointer; the thread only ever points at a review of the current version.
    if active_review_id is not None:
        update["active_review_id"] = active_review_id
    return Command(update=update)


def _context_dependencies(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
) -> tuple[
    ContentAppContext, TaskStore, ReviewStore, ResearchStore, str,
    ContentReviewAgentService,
]:
    context = runtime.context
    if not isinstance(context, ContentAppContext):
        raise RuntimeError("content_reviewer requires ContentAppContext")
    if not isinstance(context.task_store, TaskStore):
        raise RuntimeError("content_reviewer requires a Business TaskStore")

    review_store = context.stores.get("review_store")
    if not isinstance(review_store, ReviewStore):
        raise RuntimeError("content_reviewer requires a Business ReviewStore")

    research_store = context.stores.get("research_store")
    if not isinstance(research_store, ResearchStore):
        raise RuntimeError("content_reviewer requires a Business ResearchStore")

    task_id = context.services.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError("content_reviewer requires a task_id")

    review_service = context.services.get("content_review_service")
    if not isinstance(review_service, ContentReviewAgentService):
        raise RuntimeError("content_reviewer requires a ContentReviewAgentService")

    return (
        context,
        context.task_store,
        review_store,
        research_store,
        task_id,
        review_service,
    )


def _verified_facts(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    research_store: ResearchStore,
) -> tuple[list[dict[str, Any]], str | None]:
    """Collect the facts the user's latest research actually established.

    An unreadable result is reported as no evidence at all, so the reviewer
    falls back to its fact-risk rules instead of assuming the record exists.
    """

    research_result_id = runtime.state.get("current_research_result_id")
    if not isinstance(research_result_id, str) or not research_result_id.strip():
        return [], None
    result: ResearchResult | None = research_store.get_research_result(
        research_result_id
    )
    if result is None:
        return [], None
    return [fact.model_dump(mode="json") for fact in result.facts], research_result_id


def create_content_reviewer_tool() -> Any:
    """Create the Content Review subagent tool mounted on Supervisor."""

    @tool(
        "content_reviewer",
        description=(
            "Review the current draft version for factual, content, platform, "
            "and persona risk, and report whether it is ready for generation. "
            "Returns a verdict bound to the version it reviewed. Use for "
            "review requests, not for changing the draft."
        ),
    )
    async def content_reviewer(
        task: str,
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    ) -> Command:
        (
            context,
            task_store,
            review_store,
            research_store,
            task_id,
            review_service,
        ) = _context_dependencies(runtime)
        instruction = task.strip()
        if not instruction:
            return _tool_command(
                runtime,
                {"error_code": "INVALID_REQUEST", "message": "review task is blank"},
            )

        refusal = claim_specialist(context, 'content_reviewer')
        if refusal is not None:
            return _tool_command(runtime, refusal)

        draft_id = runtime.state.get("current_draft_id")
        if not isinstance(draft_id, str) or not draft_id.strip():
            return _tool_command(
                runtime,
                {
                    "error_code": "NO_CURRENT_DRAFT",
                    "message": "There is no current draft to review.",
                },
            )

        # The committed pointer decides which version is under review.
        current = task_store.get_current_draft(draft_id)
        if current is None:
            return _tool_command(
                runtime,
                {
                    "error_code": "NO_CURRENT_DRAFT_VERSION",
                    "message": "The current draft has no committed version.",
                    "draft_id": draft_id,
                },
            )
        draft_version = int(current["version"])

        draft_row = task_store.get_draft(draft_id)
        topic_row = (
            task_store.get_candidate_by_topic_id(str(draft_row["topic_id"]))
            if draft_row is not None
            else None
        )
        facts, research_result_id = _verified_facts(runtime, research_store)

        await agent_status(AGENT_REVIEW, STATUS_WORKING, message="正在审核稿件")
        await tool_status("content_reviewer", STATUS_WORKING, agent=AGENT_REVIEW)
        try:
            produced = await review_service.review(
                draft=str(current["body"]),
                draft_id=draft_id,
                draft_version=draft_version,
                confirmed_topic=topic_row,
                facts=facts,
                research_result_id=research_result_id,
            )
            saved = review_store.save_review(task_id, produced)
        except (
            ContentReviewAgentError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            await agent_status(AGENT_REVIEW, STATUS_ERROR, message="审核失败")
            await tool_status("content_reviewer", STATUS_FINISHED, agent=AGENT_REVIEW)
            return _tool_command(
                runtime,
                {
                    "error_code": "REVIEW_FAILED",
                    "message": str(exc),
                    "draft_id": draft_id,
                    "draft_version": draft_version,
                },
            )

        # A review that is not the active one for its version is still a real
        # verdict that was reached, so it is announced either way.
        await emit(
            AgentEventType.REVIEW_UPDATED,
            ReviewUpdatedPayload(review_id=saved.review_id, status=saved.status),
        )

        review_done_message = {
            "pass": "审核通过",
            "revise": "审核要求修改",
            "need_research": "审核需要补充证据",
        }.get(saved.status, "审核完成")
        await agent_status(AGENT_REVIEW, STATUS_FINISHED, message=review_done_message)
        await tool_status("content_reviewer", STATUS_FINISHED, agent=AGENT_REVIEW)

        active = review_store.get_active_review(draft_id)
        is_active = active is not None and active.review_id == saved.review_id
        return _tool_command(
            runtime,
            {
                "kind": "review_result",
                "review_id": saved.review_id,
                "draft_id": saved.draft_id,
                "draft_version": saved.draft_version,
                "status": saved.status,
                "summary": saved.summary,
                "issues": saved.issues,
                "must_fix": saved.must_fix,
                "risk_items": saved.risk_items,
                "revision_request": saved.revision_request,
                "research_request": saved.research_request,
                "ready_for_generation": saved.ready_for_generation,
                "is_active_review": is_active,
            },
            active_review_id=saved.review_id if is_active else None,
        )

    return content_reviewer


__all__ = ["create_content_reviewer_tool"]
