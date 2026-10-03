"""Supervisor agent with thread-level SQLite persistence.

The Supervisor is the only user-facing agent.  It runs one turn either
blocking (:meth:`SupervisorService.chat`) or streamed
(:meth:`SupervisorService.chat_stream`).  Both paths share the same tool set,
the same turn bookkeeping, and the same post-graph authorization and
submission step, so the two cannot drift apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent_service.agent_tools.content_reviewer import create_content_reviewer_tool
from agent_service.agent_tools.video_generation import (
    create_check_video_status_tool,
    create_generate_video_tool,
    create_list_digital_humans_tool,
)
from agent_service.agent_tools.research_expert import create_research_expert_tool
from agent_service.agent_tools.script_expert import create_script_expert_tool
from agent_service.agent_tools.topic_expert import create_topic_expert_tool
from agent_service.agents.research import ResearchAgentService
from agent_service.agents.review import ContentReviewAgentService
from agent_service.agents.script import ScriptAgentService
from agent_service.config import AgentSettings, load_settings
from agent_service.runtime.agent_status import (
    AGENT_GENERATION,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import RECURSION_LIMIT, RequestBudget, bind_budget
from agent_service.runtime.checkpointer import open_checkpointer, thread_config
from agent_service.runtime.context import ContentAppContext
from agent_service.runtime.model_factory import get_model
from agent_service.runtime.streaming import (
    bind_emitter,
    current_emitter,
    emit,
    is_text_chunk,
    text_of,
    thinking_of,
)
from agent_service.schemas.events import (
    AgentEvent,
    AgentEventType,
    AgentLifecyclePayload,
    ErrorPayload,
    GenerationCompletedPayload,
    GenerationStartedPayload,
    MessageChunkPayload,
    MessageCompletedPayload,
    WaitingUserPayload,
)
from agent_service.schemas.state import ContentAgentState
from agent_service.services.content_memory_store import ContentMemoryStore
from agent_service.services.business_trace_store import BusinessTraceStore
from agent_service.services.event_bus import EventBus
from agent_service.services.research_store import ResearchStore
from agent_service.services.review_store import ReviewStore
from agent_service.services.task_store import TaskStore
from agent_service.agents.topic import TopicAgentService
from agent_service.schemas.generation import GenerationAuthorization, GenerationIntent
from agent_service.services.authorization_service import (
    AuthorizationError,
    AuthorizationService,
    classify_generation_intent,
)
from agent_service.services.generation_service import (
    GenerationService,
    GenerationServiceError,
)
from agent_service.tools.digital_human import DigitalHumanTool


SUPERVISOR_SYSTEM_PROMPT = """
You are the Content Production Supervisor.

Always answer the user in Simplified Chinese, whatever language the user's
message or a tool result happens to use. Tool names, ids, and error codes stay
as they are; everything the user reads is Chinese.

A request that names the content to make ("找个选题", "今天做啥好呢", "写个
海归招聘会的稿子") is an instruction, not an opening: call the matching tool in
this same turn. Ask a clarifying question only when the request itself is
genuinely ambiguous, never as a way to confirm a request you already
understood.

You are the only user-facing agent. For requests about what topic to make,
current topic candidates, or selecting a numbered candidate, call
topic_expert. When the user refers to a candidate such as 'the third one',
pass the short user intent to topic_expert so it can resolve the reference
against the persisted current candidate batch. Never invent a topic_id or
silently use a Python list position.

When the user asks to write the confirmed topic, first ensure the topic has
been resolved by topic_expert, then call script_expert. script_expert creates
draft v1 and returns the canonical saved script. After it returns, show the
script text from the tool result concisely. Do not claim that a draft was saved
if the tool returned an error.

When the user asks whether a topic, position, or opportunity is still current
or still recruiting, or asks for facts to be verified, call research_expert.
Report only the recruitment status and reason that the tool result contains,
and treat answer_status 'unknown' as an honest answer rather than a failure.
Missing closing information is not proof that something is still open.

Writing a script does not require research first. When the user asks to write
the confirmed topic, call script_expert directly unless the facts needed for
the script are still missing.

The review is optional, never a gate. Call content_reviewer when the user asks
for a review, asks whether the draft has risks, or asks whether it is good
enough to publish. Do not call it on your own initiative: writing a script and
submitting the draft to the digital human both work without a review, and a
request to generate is not a review request.

When the user says to skip the review ("跳过审核", "不用审", "别审了", "直接生成"),
skip it: call the tool they actually asked for (script_expert, or nothing at all
when they only authorized generation) and do not mention the review again. Once
the user has declined a review, do not raise it a second time, and never hold
back the script or the submission until a review has passed.

When a review does run, it reviews the current draft version and returns a
verdict bound to that version. Report the status, must_fix, and risk_items it
returns, and never claim a draft passed when the verdict says otherwise.

A request to change the draft is a script_expert request, not a review. Never
call content_reviewer instead of revising the draft.

Submitting the video is your own job, not the user's: when the user clearly asks
for the video ("生成吧", "提交", "开始做视频"), call generate_video with the current
script text — the same submission the classic tool's own button performs. Use
list_digital_humans to offer avatars and voices by name when the user has not
chosen them. Report the returned task id, and when the tool returns an error,
report that reason exactly: never claim a submission succeeded, and never say
you have no such capability — the tool exists and its failure reason is what the
user needs to hear. Do not submit twice for the same draft.

You can also follow a submission up: call check_video_status (with the task id,
or with none to check this session's last submission) whenever the user asks
whether the video is ready. It polls the provider, so report the status it
returns — pending, generating, done with the video URL, or failed — instead of
telling the user you cannot know. A background wait is not possible: the answer
is only as fresh as the moment you call the tool.

When a review returns status 'need_research', you may call research_expert once
to gather the missing evidence and then call content_reviewer again. Do not
keep researching in a loop; if the evidence is still missing, tell the user
what remains unknown instead of forcing a verdict.
""".strip()


def _run_config(session_id: str) -> dict[str, Any]:
    """Thread config plus the section 63.5 ceiling on super-steps.

    Deliberately not folded into ``thread_config``: that helper also builds the
    configs for state lookups, which do not run the graph and have no use for a
    run ceiling.
    """

    return {**thread_config(session_id), "recursion_limit": RECURSION_LIMIT}


def _model_supports_tool_binding(model: Any) -> bool:
    """Keep legacy offline chat fakes usable while production models bind tools."""

    bind_tools = getattr(type(model), "bind_tools", None)
    return callable(bind_tools) and bind_tools is not BaseChatModel.bind_tools


def _previous_assistant_text(messages: list[Any]) -> str:
    """Read the assistant turn immediately before the current user turn."""

    human_indexes = [
        index for index, message in enumerate(messages) if message.type == "human"
    ]
    if not human_indexes:
        return ""
    current_turn_start = human_indexes[-1]
    for message in reversed(messages[:current_turn_start]):
        if message.type != "ai":
            continue
        content = message.content
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


def _message_text(message: Any) -> str:
    """Read one message's text, tolerating structured content blocks."""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


#: Reported in place of a tool result that a cancelled turn never produced.
INTERRUPTED_TOOL_CONTENT = (
    '{"error_code": "TOOL_CALL_INTERRUPTED", "message": '
    '"The previous turn ended before this call returned, so it has no result.", '
    '"retryable": true}'
)


def dangling_tool_messages(messages: Sequence[Any]) -> list[ToolMessage]:
    """Return the replies a cancelled turn never wrote.

    A provider rejects a conversation whose assistant message has tool calls
    without a matching reply: "An assistant message with 'tool_calls' must be
    followed by tool messages responding to each 'tool_call_id'".  A turn that
    is cut off mid-call leaves exactly that shape behind in the checkpoint, and
    without this repair every later turn on that thread fails identically.
    """

    answered = {
        str(getattr(message, "tool_call_id", "") or "")
        for message in messages
        if getattr(message, "type", "") == "tool"
    }
    replies: list[ToolMessage] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or ():
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            answered.add(call_id)
            replies.append(
                ToolMessage(content=INTERRUPTED_TOOL_CONTENT, tool_call_id=call_id)
            )
    return replies


def _tool_call_ids(message: Any) -> list[str]:
    """The ids one assistant message asked for, in the order it asked."""

    return [
        str(call.get("id") or "")
        for call in (getattr(message, "tool_calls", None) or ())
    ]


def _is_tool_reply(message: Any) -> bool:
    return getattr(message, "type", "") == "tool"


def _replies_by_call_id(messages: Sequence[Any]) -> dict[str, Any]:
    """The first reply recorded for each call id, wherever it sits."""

    replies: dict[str, Any] = {}
    for message in messages:
        if not _is_tool_reply(message):
            continue
        call_id = str(getattr(message, "tool_call_id", "") or "")
        if call_id and call_id not in replies:
            replies[call_id] = message
    return replies


def repaired_history(messages: Sequence[Any]) -> list[Any]:
    """Return the history in the one shape a provider accepts.

    A provider requires every assistant message that asks for tools to be
    followed directly by one tool message per call.  Two recorded shapes break
    that rule, and both appear in a thread whose turn was interrupted:

    * a call with no reply at all -> a synthetic interrupted reply is placed
      directly after the assistant message that asked for it;
    * a call whose reply exists but sits further down -> the reply is moved
      back into place.  This is what a rejected turn leaves behind: the
      rejection is checkpointed with the message that triggered it, so later
      human messages land between the call and its reply, and merely adding the
      missing reply at the end would leave the history just as unpostable.

    A reply whose call is gone from the history, and a second reply for a call
    that already has one, are dropped: neither can be paired with a call, so
    neither can be sent.  An intact history is returned with the same messages
    in the same order, which is what lets the caller leave it untouched.
    """

    replies = _replies_by_call_id(messages)
    rebuilt: list[Any] = []
    placed: set[str] = set()
    for message in messages:
        if _is_tool_reply(message):
            # Emitted by the call it answers, never in place.
            continue
        rebuilt.append(message)
        for call_id in _tool_call_ids(message):
            if not call_id or call_id in placed:
                continue
            placed.add(call_id)
            rebuilt.append(
                replies.get(call_id)
                or ToolMessage(content=INTERRUPTED_TOOL_CONTENT, tool_call_id=call_id)
            )
    return rebuilt


def _is_unchanged(before: Sequence[Any], after: Sequence[Any]) -> bool:
    """Whether a repair kept every recorded message in its recorded place."""

    return len(before) == len(after) and all(
        original is repaired for original, repaired in zip(before, after)
    )


@dataclass(frozen=True, slots=True)
class _GenerationOutcome:
    """What happened after the graph ran: authorization and submission."""

    intent: GenerationIntent
    authorization: GenerationAuthorization | None = None
    authorization_error: str | None = None
    generation: dict[str, Any] | None = None
    generation_error: str | None = None


class _Turn:
    """Request-scoped stores plus the context the tools read from.

    Every store shares one Business SQLite file and must be closed together,
    so they are opened and closed as a unit rather than tracked individually.
    """

    __slots__ = (
        "task_id",
        "task_store",
        "research_store",
        "review_store",
        "memory_store",
        "budget",
        "context",
    )

    def __init__(
        self,
        *,
        task_id: str,
        request_id: str,
        session_id: str,
        settings: AgentSettings,
        services: Mapping[str, object],
    ) -> None:
        self.task_id = task_id
        self.task_store = TaskStore(settings.db_path)
        self.research_store = ResearchStore(settings.db_path)
        self.review_store = ReviewStore(settings.db_path)
        self.memory_store = ContentMemoryStore(settings.db_path)
        self.budget = RequestBudget()
        self.context = ContentAppContext(
            request_id=request_id,
            session_id=session_id,
            task_store=self.task_store,
            stores={
                "research_store": self.research_store,
                "review_store": self.review_store,
                "memory_store": self.memory_store,
            },
            services={"task_id": task_id, **services},
            config=settings,
            budget=self.budget,
        )

    def close(self) -> None:
        self.memory_store.close()
        self.review_store.close()
        self.research_store.close()
        self.task_store.close()


class SupervisorService:
    """Run one Supervisor turn per request against a persistent thread.

    The checkpointer connection is opened for each request and closed after the
    graph run.  This keeps the service lifecycle simple while preserving all
    state in the SQLite file, so a new service object can resume the same
    ``session_id`` later.
    """

    def __init__(
        self,
        checkpointer_path: str | Path,
        *,
        settings: AgentSettings | None = None,
        model: Any | None = None,
        topic_agent: TopicAgentService | None = None,
        script_agent: ScriptAgentService | None = None,
        research_agent: ResearchAgentService | None = None,
        content_review_agent: ContentReviewAgentService | None = None,
        authorization_service: AuthorizationService | None = None,
        generation_service: GenerationService | None = None,
        event_bus: EventBus | None = None,
    ):
        self.checkpointer_path = checkpointer_path
        self.settings = settings or load_settings()
        #: Settings that were passed in belong to the caller (a test, or a
        #: launcher with its own environment) and must never be replaced by
        #: whatever the machine happens to have saved in the settings file.
        self._settings_injected = settings is not None
        self._model = model
        self._injected_agents = any(
            agent is not None
            for agent in (
                topic_agent,
                script_agent,
                research_agent,
                content_review_agent,
            )
        )
        self._topic_agent = topic_agent or TopicAgentService(settings=self.settings)
        self._script_agent = script_agent or ScriptAgentService(settings=self.settings)
        self._research_agent = research_agent or ResearchAgentService(
            settings=self.settings
        )
        self._content_review_agent = (
            content_review_agent or ContentReviewAgentService(settings=self.settings)
        )
        self._authorization_service = authorization_service or AuthorizationService()
        self._generation_service = generation_service or GenerationService(
            DigitalHumanTool(self.settings.go_capability_base_url)
        )
        self.event_bus = event_bus or EventBus(trace_factory=self._open_trace)

    def refresh_settings(self) -> bool:
        """Re-read settings and rebuild what captured them, if anything changed.

        The Agent panel saves the model key into the settings file, and the
        next turn has to use it without a process restart.  Only services
        constructed here are rebuilt: an injected agent belongs to the caller
        (a test), and replacing it would silently change what is under test.
        """

        if self._model is not None:
            # An injected model is the caller's decision, not a setting.
            return False
        if self._settings_injected:
            return False
        resolved = load_settings()
        if resolved == self.settings:
            return False

        self.settings = resolved
        if not self._injected_agents:
            self._topic_agent = TopicAgentService(settings=resolved)
            self._script_agent = ScriptAgentService(settings=resolved)
            self._research_agent = ResearchAgentService(settings=resolved)
            self._content_review_agent = ContentReviewAgentService(settings=resolved)
        self._generation_service = GenerationService(
            DigitalHumanTool(resolved.go_capability_base_url)
        )
        return True

    def _open_trace(self) -> BusinessTraceStore:
        """Open the local business trace for one request's channel."""

        return BusinessTraceStore(self.settings.db_path)

    def _resolve_model(self) -> Any:
        if self._model is not None:
            return self._model
        return get_model("supervisor", self.settings)

    def _open_turn(self, *, request_id: str, session_id: str) -> _Turn:
        task_id = f"task_{session_id}"
        turn = _Turn(
            task_id=task_id,
            request_id=request_id,
            session_id=session_id,
            settings=self.settings,
            services={
                "topic_agent_service": self._topic_agent,
                "script_agent_service": self._script_agent,
                "research_agent_service": self._research_agent,
                "content_review_service": self._content_review_agent,
            },
        )
        if turn.task_store.get_content_task(task_id) is None:
            turn.task_store.create_content_task(task_id, session_id)
        return turn

    @contextlib.asynccontextmanager
    async def _open_session(
        self, session_id: str, model: Any, user_message: str
    ) -> AsyncIterator[Any]:
        """Yield a checkpointer, plus the input the next turn should send.

        A turn that was cancelled after the model asked for a tool leaves a
        tool call without a reply in the checkpoint.  A provider rejects such a
        history outright, and because the checkpoint keeps the shape, every
        later turn on that thread would fail the same way.

        Adding the missing replies at the end of the input is not enough on its
        own.  A rejected turn is checkpointed too — its input is written before
        the model runs — so the thread accumulates human messages behind the
        unanswered call, and from then on the replies belong directly after the
        call rather than at the end of the history.  The repaired order is
        therefore sent as this turn's input, which writes it to the checkpoint
        in the same step: the message reducer appends and replaces by id, so
        ``REMOVE_ALL_MESSAGES`` is the only way a moved reply can arrive.
        """

        async with open_checkpointer(self.checkpointer_path) as checkpointer:
            supervisor = self._build_supervisor(model, checkpointer)
            snapshot = await supervisor.aget_state(thread_config(session_id))
            history = list(getattr(snapshot, "values", {}).get("messages", []) or [])
            repaired = repaired_history(history)
            if _is_unchanged(history, repaired):
                turn_input: list[Any] = [{"role": "user", "content": user_message}]
            else:
                turn_input = [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *repaired,
                    {"role": "user", "content": user_message},
                ]
            yield checkpointer, turn_input

    def _build_supervisor(self, model: Any, checkpointer: Any) -> Any:
        tools = (
            [
                create_topic_expert_tool(),
                create_script_expert_tool(),
                create_research_expert_tool(),
                create_content_reviewer_tool(),
                create_list_digital_humans_tool(),
                create_generate_video_tool(),
                create_check_video_status_tool(),
            ]
            if _model_supports_tool_binding(model)
            else []
        )
        return create_agent(
            model=model,
            tools=tools,
            system_prompt=SUPERVISOR_SYSTEM_PROMPT if tools else None,
            state_schema=ContentAgentState,            context_schema=ContentAppContext,
            checkpointer=checkpointer,
            name="supervisor",
        )

    @staticmethod
    def _validate_turn_inputs(
        *, request_id: str, session_id: str, message: str
    ) -> None:
        if not request_id.strip():
            raise ValueError("request_id must not be blank")
        if not session_id.strip():
            raise ValueError("session_id must not be blank")
        if not message.strip():
            raise ValueError("message must not be blank")

    def _classify_intent(
        self, *, message: str, messages: list[Any]
    ) -> GenerationIntent:
        return classify_generation_intent(
            message,
            previous_assistant_text=_previous_assistant_text(messages),
        )

    def _authorize_and_submit(
        self,
        *,
        intent: GenerationIntent,
        state: Mapping[str, Any],
        turn: _Turn,
        generation_params: Mapping[str, Any] | None,
        request_id: str,
    ) -> _GenerationOutcome:
        """Authorize the current draft and submit it, when the user asked to.

        The ordering is part of the Task 2.10 contract: validate the draft and
        the parameters, reserve the local record through the idempotency key,
        and only then perform the external side effect.
        """

        if not intent.should_authorize:
            return _GenerationOutcome(intent=intent)

        # The model submits through generate_video; that path already recorded a
        # task id in the thread state. Submitting again here would spend the
        # account's provider quota on a second video of the same draft.
        already_submitted = state.get("video_task_id")
        if isinstance(already_submitted, str) and already_submitted.strip():
            return _GenerationOutcome(intent=intent)

        draft_id = state.get("current_draft_id")
        draft_version = state.get("current_draft_version")
        if not isinstance(draft_id, str) or not isinstance(draft_version, int):
            return _GenerationOutcome(
                intent=intent,
                authorization_error="no current draft is available for authorization",
            )

        try:
            authorization = self._authorization_service.authorize_generation(
                turn.task_store,
                task_id=turn.task_id,
                draft_id=draft_id,
                draft_version=draft_version,
                params=generation_params,
            )
        except (AuthorizationError, ValueError) as exc:
            return _GenerationOutcome(intent=intent, authorization_error=str(exc))

        draft_version_row = turn.task_store.get_draft_version(draft_id, draft_version)
        if draft_version_row is None:
            return _GenerationOutcome(
                intent=intent,
                authorization=authorization,
                generation_error="authorized draft version is not readable",
            )

        try:
            generation = self._generation_service.submit(
                turn.task_store,
                task_id=turn.task_id,
                draft_id=draft_id,
                draft_version=draft_version,
                authorization_id=authorization.authorization_id,
                script=str(draft_version_row["body"]),
                params=generation_params,
                request_id=request_id,
            )
        except GenerationServiceError as exc:
            return _GenerationOutcome(
                intent=intent,
                authorization=authorization,
                generation_error=str(exc),
            )

        return _GenerationOutcome(
            intent=intent, authorization=authorization, generation=generation
        )

    async def chat(
        self,
        *,
        request_id: str,
        session_id: str,
        message: str,
        generation_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_turn_inputs(
            request_id=request_id, session_id=session_id, message=message
        )
        # A model key saved in the browser panel applies from the next turn on.
        self.refresh_settings()

        model = self._resolve_model()
        turn = self._open_turn(request_id=request_id, session_id=session_id)
        try:
            async with self._open_session(
                session_id, model, message
            ) as (checkpointer, turn_input):
                supervisor = self._build_supervisor(model, checkpointer)
                with bind_budget(turn.budget):
                    result = await supervisor.ainvoke(
                        {"messages": turn_input},
                        config=_run_config(session_id),
                        context=turn.context,
                    )
            messages = result.get("messages", [])
            if not messages:
                raise RuntimeError("Supervisor returned no messages")
            # Authorization and submission read the same Business SQLite
            # handles the graph used, so they must run before the stores close.
            outcome = self._authorize_and_submit(
                intent=self._classify_intent(message=message, messages=messages),
                state=result,
                turn=turn,
                generation_params=generation_params,
                request_id=request_id,
            )
        finally:
            turn.close()

        content = _message_text(messages[-1])
        generation = outcome.generation

        return {
            "request_id": request_id,
            "session_id": session_id,
            "assistant_message_id": f"amsg_{uuid4().hex}",
            "status": "completed",
            "message": content,
            "message_count": len(messages),
            "current_candidate_batch_id": result.get("current_candidate_batch_id"),
            "current_topic_id": result.get("current_topic_id"),
            "current_draft_id": result.get("current_draft_id"),
            "current_draft_version": result.get("current_draft_version"),
            "current_research_result_id": result.get("current_research_result_id"),
            "active_review_id": result.get("active_review_id"),
            "generation_id": (
                generation["generation_id"]
                if generation is not None
                else result.get("generation_id")
            ),
            "generation_status": generation.get("status") if generation else None,
            "generation_intent": outcome.intent.model_dump(),
            "generation_authorized": outcome.authorization is not None,
            "generation_authorization_id": (
                outcome.authorization.authorization_id
                if outcome.authorization is not None
                else None
            ),
            "authorized_draft_version": (
                outcome.authorization.draft_version
                if outcome.authorization is not None
                else None
            ),
            "authorization_error": outcome.authorization_error,
            "generation_error": outcome.generation_error,
        }

    async def chat_stream(
        self,
        *,
        request_id: str,
        session_id: str,
        message: str,
        generation_params: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn and yield its events in order.

        The producer publishes into the event bus and the consumer reads from
        it, which is what keeps graph-internal and graph-external events in a
        single sequence.  Cancelling this generator (a disconnected client)
        propagates to the producer task and releases the stores and
        checkpointer.
        """

        self._validate_turn_inputs(
            request_id=request_id, session_id=session_id, message=message
        )
        # A model key saved in the browser panel applies from the next turn on.
        self.refresh_settings()

        model = self._resolve_model()
        turn = self._open_turn(request_id=request_id, session_id=session_id)
        try:
            async with self.event_bus.channel(request_id) as subscription:
                producer = asyncio.create_task(
                    self._run_streamed_turn(
                        model=model,
                        turn=turn,
                        request_id=request_id,
                        session_id=session_id,
                        message=message,
                        generation_params=generation_params,
                    )
                )
                try:
                    async for event in subscription:
                        yield event
                finally:
                    if not producer.done():
                        producer.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await producer
        finally:
            turn.close()

    async def _run_streamed_turn(
        self,
        *,
        model: Any,
        turn: _Turn,
        request_id: str,
        session_id: str,
        message: str,
        generation_params: Mapping[str, Any] | None,
    ) -> None:
        """Produce every event for one streamed turn, then close the channel."""

        with bind_emitter(
            self.event_bus,
            request_id=request_id,
            session_id=session_id,
            task_id=turn.task_id,
        ), bind_budget(turn.budget):
            try:
                await emit(
                    AgentEventType.AGENT_STARTED,
                    AgentLifecyclePayload(agent="supervisor"),
                )
                state = await self._stream_graph(
                    model=model,
                    turn=turn,
                    session_id=session_id,
                    message=message,
                )
                messages = list(state.get("messages", []))
                intent = self._classify_intent(message=message, messages=messages)
                draft_id = state.get("current_draft_id")
                draft_version = state.get("current_draft_version")
                if (
                    intent.should_authorize
                    and isinstance(draft_id, str)
                    and draft_id.strip()
                    and isinstance(draft_version, int)
                    and draft_version >= 1
                ):
                    # Only announced when there is a draft to submit; when
                    # there is not, the turn ends with an authorization error
                    # instead, which is what the user needs to hear.
                    await emit(
                        AgentEventType.GENERATION_STARTED,
                        GenerationStartedPayload(
                            draft_id=draft_id, draft_version=draft_version
                        ),
                    )
                    await agent_status(
                        AGENT_GENERATION, STATUS_WORKING, message="正在提交数字人"
                    )
                    await tool_status(
                        "generate_digital_human",
                        STATUS_WORKING,
                        agent=AGENT_GENERATION,
                    )
                outcome = self._authorize_and_submit(
                    intent=intent,
                    state=state,
                    turn=turn,
                    generation_params=generation_params,
                    request_id=request_id,
                )
                await self._emit_turn_end(outcome=outcome, messages=messages)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                await emit(
                    AgentEventType.ERROR,
                    ErrorPayload(
                        error_code="INTERNAL_ERROR",
                        message=str(exc) or type(exc).__name__,
                        retryable=False,
                    ),
                )
            finally:
                await self.event_bus.close(request_id)

    async def _stream_graph(
        self,
        *,
        model: Any,
        turn: _Turn,
        session_id: str,
        message: str,
    ) -> Mapping[str, Any]:
        """Stream one graph run and return the state it finished with."""

        async with self._open_session(
            session_id, model, message
        ) as (checkpointer, turn_input):
            supervisor = self._build_supervisor(model, checkpointer)
            async for item in supervisor.astream(
                {"messages": turn_input},
                config=_run_config(session_id),
                context=turn.context,
                stream_mode=["messages", "custom"],
                version="v2",
            ):
                mode, data = _split_stream_item(item)
                if mode == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    if is_text_chunk(chunk):
                        # Reasoning first: it arrives while the turn is still
                        # working, which is the only progress a waiting user can
                        # see during a long search.
                        thinking = thinking_of(chunk)
                        if thinking:
                            await emit(
                                AgentEventType.MESSAGE_THINKING,
                                MessageChunkPayload(text=thinking),
                            )
                        text = text_of(chunk)
                        if text:
                            await emit(
                                AgentEventType.MESSAGE_CHUNK,
                                MessageChunkPayload(text=text),
                            )
                elif mode == "custom":
                    await self._forward_custom_event(data)

            snapshot = await supervisor.aget_state(thread_config(session_id))
        return dict(getattr(snapshot, "values", None) or {})

    @staticmethod
    async def _forward_custom_event(data: Any) -> None:
        """Forward a tool's custom stream payload onto the bus.

        Tools may publish through LangGraph's stream writer instead of calling
        ``emit``.  Anything that does not match the event contract is ignored
        rather than allowed to break the turn.
        """

        emitter = current_emitter()
        if emitter is None:
            return
        if isinstance(data, AgentEvent):
            await emitter.bus.publish(emitter.request_id, data)
            return
        if isinstance(data, dict) and "type" in data:
            payload = {key: value for key, value in data.items() if key != "type"}
            with contextlib.suppress(Exception):
                await emit(str(data["type"]), payload)

    async def _emit_turn_end(
        self, *, outcome: _GenerationOutcome, messages: list[Any]
    ) -> None:
        """Close out a turn with its generation result and final text."""

        if outcome.generation is not None:
            generation = outcome.generation
            await emit(
                AgentEventType.GENERATION_COMPLETED,
                GenerationCompletedPayload(
                    generation_id=str(generation.get("generation_id") or "unknown"),
                    status=_submission_status(str(generation.get("status") or "")),
                    provider_task_id=(
                        str(generation["provider_task_id"])
                        if generation.get("provider_task_id")
                        else None
                    ),
                    error_code=(
                        str(generation["error_code"])
                        if generation.get("error_code")
                        else None
                    ),
                    error_message=(
                        str(generation["error_message"])
                        if generation.get("error_message")
                        else None
                    ),
                ),
            )
            await agent_status(
                AGENT_GENERATION,
                STATUS_FINISHED,
                message=f"数字人提交结果：{_submission_status(str(generation.get('status') or ''))}",
            )
            await tool_status(
                "generate_digital_human", STATUS_FINISHED, agent=AGENT_GENERATION
            )
        elif outcome.authorization_error is not None or outcome.generation_error is not None:
            await emit(
                AgentEventType.ERROR,
                ErrorPayload(
                    error_code=(
                        "AUTHORIZATION_FAILED"
                        if outcome.authorization_error is not None
                        else "GENERATION_FAILED"
                    ),
                    message=str(
                        outcome.authorization_error or outcome.generation_error
                    ),
                    retryable=False,
                ),
            )
            await agent_status(
                AGENT_GENERATION, STATUS_ERROR, message="数字人提交未完成"
            )
            await tool_status(
                "generate_digital_human", STATUS_FINISHED, agent=AGENT_GENERATION
            )

        if messages:
            await emit(
                AgentEventType.MESSAGE_COMPLETED,
                MessageCompletedPayload(text=_message_text(messages[-1])),
            )

        if outcome.generation is None:
            await emit(AgentEventType.WAITING_USER, WaitingUserPayload())

        await emit(
            AgentEventType.AGENT_FINISHED,
            AgentLifecyclePayload(agent="supervisor"),
        )


def _split_stream_item(item: Any) -> tuple[str, Any]:
    """Read ``(mode, data)`` from one ``astream`` item.

    A v2 run with several stream modes yields a mapping carrying ``type``,
    ``ns``, and ``data``; older shapes yield a plain pair.  Both are accepted
    so a library upgrade degrades into an ignored frame instead of a turn that
    fails halfway through.
    """

    if isinstance(item, Mapping):
        return str(item.get("type", "")), item.get("data")
    if isinstance(item, tuple) and len(item) == 2:
        return str(item[0]), item[1]
    if isinstance(item, tuple) and len(item) == 3:
        return str(item[1]), item[2]
    return "", None


def _submission_status(status: str) -> str:
    """Map a generation record status onto the streaming contract.

    Rendering continues on the provider side; this reports only how the
    submission itself resolved.
    """

    if status in {"pending", "generating", "submitted"}:
        return "submitted"
    if status == "unknown_submission":
        return "unknown_submission"
    return "failed"
