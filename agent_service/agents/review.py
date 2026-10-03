"""Standalone Content Review Agent.

The agent judges one immutable draft version and returns a structured
``ReviewResult``.  It never writes, rewrites, or persists anything: the
deterministic wrapper owns persistence, exactly as with the Script and
Research agents.

The review identity (``review_id``, ``draft_id``, ``draft_version``) and the
``ready_for_generation`` gate belong to the caller and to deterministic code,
not to the model.  A verdict can therefore never be attached to a version the
model picked, and a `revise` verdict can never be marked ready for generation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel

from agent_service.config import AgentSettings, load_settings
from agent_service.runtime.budget import RECURSION_LIMIT
from agent_service.runtime.model_factory import get_model
from agent_service.schemas.draft import DraftPayload
from agent_service.schemas.review import ReviewResult
from agent_service.schemas.topic import TopicCandidate


class ContentReviewAgentError(RuntimeError):
    """Raised when the standalone Content Review Agent cannot produce output."""


def load_content_review_principles() -> str:
    """Load the human-editable Content Review principles."""

    path = (
        Path(__file__).resolve().parents[1] / "principles" / "content_review.md"
    )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContentReviewAgentError(
            f"Content Review principles are unavailable: {path}"
        ) from exc


def create_content_review_agent(
    *,
    model: Any | None = None,
    settings: AgentSettings | None = None,
):
    """Build an isolated Content Review Agent that returns a ReviewResult.

    Section 51 of the development document keeps Supervisor, Topic, Research,
    and Review on one shared tool-calling-stable model, so this agent reuses
    the ``supervisor`` model role until per-agent models are justified.
    """

    resolved_settings = settings or load_settings()
    resolved_model = (
        model
        if model is not None
        else get_model("supervisor", resolved_settings, disable_thinking=True)
    )

    return create_agent(
        model=resolved_model,
        tools=[],
        system_prompt=load_content_review_principles(),
        # ToolStrategy, not the bare schema: a bare schema is auto-detected into
        # the provider's native structured output, and DeepSeek rejects the
        # OpenAI-style ``response_format: {"type": "json_schema"}`` that produces
        # with a 400.  The tool-calling route is the one DeepSeek supports.
        response_format=ToolStrategy(ReviewResult),
        name="content_review_agent",
    )


def _payload(value: Any) -> Any:
    """Reduce a model, mapping, or sequence to a JSON-ready payload."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload(item) for item in value]
    return value


def _draft_payload(draft: str | DraftPayload) -> dict[str, Any]:
    if isinstance(draft, DraftPayload):
        return {
            "sections": [section.model_dump() for section in draft.sections],
            "full_script": draft.full_script,
        }
    text = draft.strip()
    if not text:
        raise ValueError("draft must not be blank")
    return {"full_script": text}


def build_review_prompt(
    *,
    draft: str | DraftPayload,
    draft_id: str,
    draft_version: int,
    review_id: str,
    confirmed_topic: TopicCandidate | Mapping[str, Any] | str | None = None,
    facts: Iterable[Any] | None = None,
    research_result_id: str | None = None,
) -> str:
    """Build a bounded, auditable review request for one draft version."""

    if not draft_id.strip():
        raise ValueError("draft_id must not be blank")
    if draft_version < 1:
        raise ValueError("draft_version must be at least 1")
    if not review_id.strip():
        raise ValueError("review_id must not be blank")

    request_payload = {
        "review_target": {
            "review_id": review_id,
            "draft_id": draft_id,
            "draft_version": draft_version,
        },
        "script": _draft_payload(draft),
        "confirmed_topic": (
            _payload(confirmed_topic) if confirmed_topic is not None else None
        ),
        "verified_facts": _payload(list(facts)) if facts is not None else [],
        "research_result_id": research_result_id,
        "output_rules": {
            "status_values": ["pass", "revise", "need_research"],
            "copy_review_target_fields_exactly": True,
            "do_not_rewrite_the_script": True,
            "ready_for_generation_only_for_pass_without_must_fix": True,
        },
    }
    return json.dumps(request_payload, ensure_ascii=False, sort_keys=True)


class ContentReviewAgentService:
    """Independent entry point for reviewing one draft version."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        settings: AgentSettings | None = None,
    ) -> None:
        self.model = model
        self.settings = settings

    async def _invoke(self, user_prompt: str) -> ReviewResult:
        agent = create_content_review_agent(
            model=self.model,
            settings=self.settings,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_prompt}]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        structured = result.get("structured_response")
        if isinstance(structured, ReviewResult):
            return structured
        try:
            return ReviewResult.model_validate(structured)
        except (TypeError, ValueError) as exc:
            raise ContentReviewAgentError(
                "Content Review Agent did not return a structured ReviewResult"
            ) from exc

    @staticmethod
    def _finalize_result(
        result: ReviewResult,
        *,
        draft_id: str,
        draft_version: int,
        review_id: str,
        reviewed_at: datetime,
    ) -> ReviewResult:
        """Stamp the caller-owned identity and fail-closed the generation gate."""

        return result.model_copy(
            update={
                "review_id": review_id,
                "draft_id": draft_id,
                "draft_version": draft_version,
                # Lowering only: a verdict that still needs work can never be
                # reported as ready for generation.
                "ready_for_generation": (
                    result.ready_for_generation
                    and result.status == "pass"
                    and not result.must_fix
                ),
                "created_at": reviewed_at,
            }
        )

    async def review(
        self,
        *,
        draft: str | DraftPayload,
        draft_id: str,
        draft_version: int,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str | None = None,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        research_result_id: str | None = None,
    ) -> ReviewResult:
        """Review one draft version and return a validated verdict."""

        if facts is not None and verified_facts is not None:
            raise ValueError("pass either facts or verified_facts, not both")
        supplied_facts = facts if facts is not None else verified_facts
        review_id = f"review_{uuid4().hex}"

        user_prompt = build_review_prompt(
            draft=draft,
            draft_id=draft_id,
            draft_version=draft_version,
            review_id=review_id,
            confirmed_topic=confirmed_topic,
            facts=supplied_facts,
            research_result_id=research_result_id,
        )

        result = await self._invoke(user_prompt)
        return self._finalize_result(
            result,
            draft_id=draft_id,
            draft_version=draft_version,
            review_id=review_id,
            reviewed_at=datetime.now(timezone.utc),
        )

    async def run(
        self,
        *,
        draft: str | DraftPayload,
        draft_id: str,
        draft_version: int,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str | None = None,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        research_result_id: str | None = None,
    ) -> ReviewResult:
        """Alias for callers that use a specialist ``run`` convention."""

        return await self.review(
            draft=draft,
            draft_id=draft_id,
            draft_version=draft_version,
            confirmed_topic=confirmed_topic,
            facts=facts,
            verified_facts=verified_facts,
            research_result_id=research_result_id,
        )


__all__ = [
    "ContentReviewAgentError",
    "ContentReviewAgentService",
    "build_review_prompt",
    "create_content_review_agent",
    "load_content_review_principles",
]
