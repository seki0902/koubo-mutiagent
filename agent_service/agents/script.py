"""Standalone Script Agent for deterministic first-pass draft generation.

The agent uses the configured DeepSeek model directly through the shared model
factory. It returns validated structured payloads and never saves a
DraftVersion or calls Supervisor; the revision entry point only returns the
proposed v2/v3 payload for the deterministic wrapper to persist.

The writing Skill is part of this Agent, not of the Supervisor: the caller
resolves the material for the topic and the service puts it into the request,
and the Agent may also pull it through the ``load_script_skill`` Tool when it
wants another category.  Neither path lets the model invent the playbook.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import BaseTool, tool
from pydantic import BaseModel

from agent_service.config import AgentSettings, load_settings
from agent_service.runtime.budget import RECURSION_LIMIT
from agent_service.runtime.model_factory import get_model
from agent_service.schemas.draft import DraftPayload
from agent_service.schemas.topic import TopicCandidate
from agent_service.services.content_memory_store import ContentMemoryStore
from agent_service.skills.skill_loader import ScriptSkill, build_script_skill
from agent_service.tools.memory import get_revision_history, get_similar_scripts


class ScriptAgentError(RuntimeError):
    """Raised when the standalone Script Agent cannot produce valid output."""


def load_script_principles() -> str:
    """Load the human-editable Script Agent principles."""

    path = Path(__file__).resolve().parents[1] / "principles" / "script.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScriptAgentError(f"Script principles are unavailable: {path}") from exc


def _build_similar_scripts_tool(
    memory_store: ContentMemoryStore, account_id: str | None
) -> BaseTool:
    """Create the historical-script Tool allowed on this Agent."""

    @tool(
        "get_similar_scripts",
        description=(
            "Return historical content whose topic phrase or tags overlap the "
            "given topic, with its script, tags and latest collected metrics. "
            "Use it to see how this account covered a similar topic before."
        ),
    )
    async def get_similar_scripts_tool(
        topic: str, tags: list[str] | None = None, limit: int = 5
    ) -> str:
        results = get_similar_scripts(
            memory_store,
            topic=topic,
            account_id=account_id,
            tags=tags or [],
            limit=limit,
        )
        return json.dumps(
            {"scripts": [item.model_dump(mode="json") for item in results]},
            ensure_ascii=False,
        )

    return get_similar_scripts_tool


def _build_revision_history_tool(memory_store: ContentMemoryStore) -> BaseTool:
    """Create the revision-preference Tool allowed on this Agent."""

    @tool(
        "get_revision_history",
        description=(
            "Return how this user asked for script changes in the past, "
            "oldest first, including the ones that were not accepted. Use it "
            "before revising so you do not repeat a corrected mistake."
        ),
    )
    async def get_revision_history_tool(
        accepted: bool | None = None, limit: int = 10
    ) -> str:
        records = get_revision_history(
            memory_store, accepted=accepted, limit=limit
        )
        return json.dumps(
            {"revisions": [record.model_dump(mode="json") for record in records]},
            ensure_ascii=False,
        )

    return get_revision_history_tool


def _build_skill_tool() -> BaseTool:
    """Create the Tool that pulls the writing Skill for a topic.

    It exists so the model can ask for the playbook itself — including a
    category other than the one the caller pre-selected — instead of the
    material only ever arriving pre-loaded.
    """

    @tool(
        "load_script_skill",
        description=(
            "Load the writing Skill (playbook) for a topic: structure "
            "templates, jargon, emotional protocol, and the matching category "
            "library. Call it once before writing a script, and again only if "
            "the topic clearly belongs to a different category."
        ),
    )
    async def load_script_skill_tool(topic_title: str) -> str:
        skill = build_script_skill(topic_title)
        if skill is None:
            return json.dumps(
                {
                    "error_code": "SKILL_UNAVAILABLE",
                    "message": "Skill 资源不可用，请按已给的原则写作，不要编造打法。",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "topic_title": skill.topic_title,
                "library_file": skill.library_file,
                "files": list(skill.files),
                "skill": skill.prompt,
            },
            ensure_ascii=False,
        )

    return load_script_skill_tool


def resolve_script_skill(
    confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
    topic_id: str | None,
) -> ScriptSkill | None:
    """Resolve the Skill for one confirmed topic, or report none is available.

    A missing skill directory must not fail a turn: writing without the
    playbook is worse, but writing nothing is worse still.  The category is
    chosen from the candidate's title, because that is the field the user saw
    when they picked it.
    """

    _, topic_payload = _normalize_topic(confirmed_topic, topic_id)
    title = str(
        topic_payload.get("title") or topic_payload.get("topic_id") or ""
    ).strip()
    if not title:
        return None
    return build_script_skill(title)


def create_script_agent(
    *,
    model: Any | None = None,
    settings: AgentSettings | None = None,
    memory_store: ContentMemoryStore | None = None,
    memory_account_id: str | None = None,
):
    """Build an isolated Script Agent using the direct DeepSeek model seam.

    Long-term memory is mounted only when a store is supplied, and the account
    scope is fixed by the caller rather than chosen by the model. Nothing is
    pre-fetched into the prompt; the model decides when to consult history.
    """

    resolved_settings = settings or load_settings()
    resolved_model = (
        model
        if model is not None
        else get_model("script", resolved_settings, disable_thinking=True)
    )

    tools: list[BaseTool] = [_build_skill_tool()]
    if memory_store is not None:
        tools.append(_build_similar_scripts_tool(memory_store, memory_account_id))
        tools.append(_build_revision_history_tool(memory_store))

    return create_agent(
        model=resolved_model,
        tools=tools,
        system_prompt=load_script_principles(),
        # ToolStrategy, not the bare schema: a bare schema is auto-detected into
        # the provider's native structured output, and DeepSeek rejects the
        # OpenAI-style ``response_format: {"type": "json_schema"}`` that produces
        # with a 400.  The tool-calling route is the one DeepSeek supports.
        response_format=ToolStrategy(DraftPayload),
        name="script_agent",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _normalize_topic(
    confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
    topic_id: str | None,
) -> tuple[str, dict[str, Any]]:
    if isinstance(confirmed_topic, TopicCandidate):
        payload = confirmed_topic.model_dump(mode="json", exclude_none=True)
        return confirmed_topic.topic_id, payload

    if isinstance(confirmed_topic, Mapping):
        payload = _jsonable(confirmed_topic)
        candidate_topic_id = payload.get("topic_id")
        resolved_topic_id = topic_id or candidate_topic_id
        if not isinstance(resolved_topic_id, str) or not resolved_topic_id.strip():
            raise ValueError("confirmed_topic must include a non-blank topic_id")
        payload["topic_id"] = resolved_topic_id
        return resolved_topic_id, payload

    topic_text = confirmed_topic.strip()
    if not topic_text:
        raise ValueError("confirmed_topic must not be blank")
    if not topic_id or not topic_id.strip():
        raise ValueError("topic_id is required when confirmed_topic is text")
    return topic_id, {"topic_id": topic_id, "title": topic_text}


def _normalize_facts(facts: Iterable[Any] | None) -> list[Any]:
    if facts is None:
        return []
    if isinstance(facts, (str, bytes)):
        return [facts.decode() if isinstance(facts, bytes) else facts]
    return [_jsonable(fact) for fact in facts]


def build_script_prompt(
    *,
    confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
    facts: Iterable[Any] | None = None,
    topic_id: str | None = None,
    research_result_id: str | None = None,
) -> tuple[str, str]:
    """Build a bounded, auditable input payload for the Script Agent."""

    resolved_topic_id, topic_payload = _normalize_topic(confirmed_topic, topic_id)
    request_payload = {
        "confirmed_topic": topic_payload,
        "verified_facts": _normalize_facts(facts),
        "research_result_id": research_result_id,
        "output_rules": {
            "required_section_types": ["hook", "body", "cta"],
            "full_script": "join sections by ascending order with one blank line",
            "do_not_invent": True,
        },
    }
    return resolved_topic_id, json.dumps(
        request_payload, ensure_ascii=False, sort_keys=True
    )


def build_revision_prompt(
    *,
    confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
    current_draft: str | DraftPayload,
    edit_instruction: str,
    facts: Iterable[Any] | None = None,
    topic_id: str | None = None,
    research_result_id: str | None = None,
) -> tuple[str, str]:
    """Build a bounded revision request that names the existing script."""

    instruction = edit_instruction.strip()
    if not instruction:
        raise ValueError("edit_instruction must not be blank")

    resolved_topic_id, topic_payload = _normalize_topic(confirmed_topic, topic_id)
    if isinstance(current_draft, DraftPayload):
        draft_payload: Any = {
            "sections": [section.model_dump() for section in current_draft.sections],
            "full_script": current_draft.full_script,
        }
    else:
        draft_payload = {"full_script": current_draft.strip()}
    if not draft_payload.get("full_script"):
        raise ValueError("current_draft must not be blank")

    request_payload = {
        "confirmed_topic": topic_payload,
        "verified_facts": _normalize_facts(facts),
        "research_result_id": research_result_id,
        "current_draft": draft_payload,
        "edit_instruction": instruction,
        "edit_rules": {
            "only_change_targeted_part": True,
            "preserve_untargeted_sections": True,
            "return_hook_body_cta_sections": True,
            "full_script": "join sections by ascending order with one blank line",
        },
    }
    return resolved_topic_id, json.dumps(
        request_payload, ensure_ascii=False, sort_keys=True
    )


class ScriptAgentService:
    """Independent entry point for generating one structured script payload."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        settings: AgentSettings | None = None,
    ) -> None:
        self.model = model
        self.settings = settings

    async def _invoke(
        self,
        user_prompt: str,
        *,
        skill: ScriptSkill | None = None,
        memory_store: ContentMemoryStore | None = None,
        memory_account_id: str | None = None,
    ) -> DraftPayload:
        agent = create_script_agent(
            model=self.model,
            settings=self.settings,
            memory_store=memory_store,
            memory_account_id=memory_account_id,
        )
        messages: list[dict[str, str]] = [
            {"role": "user", "content": user_prompt}
        ]
        if skill is not None:
            # The playbook travels as its own message rather than inside the
            # JSON request: it is reference material, not a field of the case.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"【写稿 Skill｜已按选题匹配到 {skill.library_file}】\n\n"
                        f"{skill.prompt}\n\n"
                        "以上是本次写稿必须遵循的打法。请只依据它和上面的结构化请求"
                        "产出稿件，不要编造来源或数据。"
                    ),
                }
            )
        result = await agent.ainvoke(
            {"messages": messages}, config={"recursion_limit": RECURSION_LIMIT}
        )
        structured = result.get("structured_response")
        if isinstance(structured, DraftPayload):
            return structured
        try:
            return DraftPayload.model_validate(structured)
        except (TypeError, ValueError) as exc:
            raise ScriptAgentError(
                "Script Agent did not return a structured DraftPayload"
            ) from exc

    @staticmethod
    def _finalize_payload(
        payload: DraftPayload,
        *,
        topic_id: str,
        research_result_id: str | None,
    ) -> DraftPayload:
        updates: dict[str, Any] = {"topic_id": topic_id}
        if research_result_id is not None:
            updates["research_result_id"] = research_result_id
        return payload.model_copy(update=updates)

    async def generate(
        self,
        *,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        topic_id: str | None = None,
        research_result_id: str | None = None,
        skill: ScriptSkill | None = None,
        memory_store: ContentMemoryStore | None = None,
        memory_account_id: str | None = None,
    ) -> DraftPayload:
        """Generate one validated payload without persisting a version."""

        if facts is not None and verified_facts is not None:
            raise ValueError("pass either facts or verified_facts, not both")
        supplied_facts = facts if facts is not None else verified_facts
        resolved_topic_id, user_prompt = build_script_prompt(
            confirmed_topic=confirmed_topic,
            facts=supplied_facts,
            topic_id=topic_id,
            research_result_id=research_result_id,
        )
        skill = skill if skill is not None else resolve_script_skill(
            confirmed_topic, resolved_topic_id
        )

        payload = await self._invoke(
            user_prompt,
            skill=skill,
            memory_store=memory_store,
            memory_account_id=memory_account_id,
        )
        # The confirmed topic identity and research pointer belong to the
        # caller, not the model. Carry them forward deterministically.
        return self._finalize_payload(
            payload,
            topic_id=resolved_topic_id,
            research_result_id=research_result_id,
        )

    async def revise(
        self,
        *,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
        current_draft: str | DraftPayload,
        edit_instruction: str,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        topic_id: str | None = None,
        research_result_id: str | None = None,
        skill: ScriptSkill | None = None,
        memory_store: ContentMemoryStore | None = None,
        memory_account_id: str | None = None,
    ) -> DraftPayload:
        """Return a locally revised payload without saving a version."""

        if facts is not None and verified_facts is not None:
            raise ValueError("pass either facts or verified_facts, not both")
        supplied_facts = facts if facts is not None else verified_facts
        resolved_topic_id, user_prompt = build_revision_prompt(
            confirmed_topic=confirmed_topic,
            current_draft=current_draft,
            edit_instruction=edit_instruction,
            facts=supplied_facts,
            topic_id=topic_id,
            research_result_id=research_result_id,
        )
        skill = skill if skill is not None else resolve_script_skill(
            confirmed_topic, resolved_topic_id
        )
        payload = await self._invoke(
            user_prompt,
            skill=skill,
            memory_store=memory_store,
            memory_account_id=memory_account_id,
        )
        return self._finalize_payload(
            payload,
            topic_id=resolved_topic_id,
            research_result_id=research_result_id,
        )

    async def edit(
        self,
        *,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
        current_draft: str | DraftPayload,
        edit_instruction: str,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        topic_id: str | None = None,
        research_result_id: str | None = None,
        memory_store: ContentMemoryStore | None = None,
        memory_account_id: str | None = None,
    ) -> DraftPayload:
        """Alias for callers that describe revision as an edit."""

        return await self.revise(
            confirmed_topic=confirmed_topic,
            current_draft=current_draft,
            edit_instruction=edit_instruction,
            facts=facts,
            verified_facts=verified_facts,
            topic_id=topic_id,
            research_result_id=research_result_id,
            memory_store=memory_store,
            memory_account_id=memory_account_id,
        )

    async def run(
        self,
        *,
        confirmed_topic: TopicCandidate | Mapping[str, Any] | str,
        facts: Iterable[Any] | None = None,
        verified_facts: Iterable[Any] | None = None,
        topic_id: str | None = None,
        research_result_id: str | None = None,
        memory_store: ContentMemoryStore | None = None,
        memory_account_id: str | None = None,
    ) -> DraftPayload:
        """Alias for callers that use a specialist ``run`` convention."""

        return await self.generate(
            confirmed_topic=confirmed_topic,
            facts=facts,
            verified_facts=verified_facts,
            topic_id=topic_id,
            research_result_id=research_result_id,
            memory_store=memory_store,
            memory_account_id=memory_account_id,
        )


__all__ = [
    "ScriptAgentError",
    "ScriptAgentService",
    "build_revision_prompt",
    "build_script_prompt",
    "create_script_agent",
    "load_script_principles",
]
