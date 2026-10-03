"""Video submission tools: the same entry point the classic tool's button uses.

The Agent does not produce video. It submits one: the script text plus a chosen
avatar and voice go to ``POST /api/tasks`` — the exact request the tool's own
"生成视频" button sends — and the provider task id comes back. Keeping this on
the tool path (rather than hidden in post-turn code) is what lets the model
report success or failure honestly instead of claiming it has no such ability.

This path is deliberately the direct one, like the tool's button: it does not
create a local generation record, so the provider task id is kept in
``video_task_id`` rather than in ``generation_id`` (which names a local record).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_service.config import load_settings
from agent_service.runtime.agent_status import (
    AGENT_GENERATION,
    STATUS_ERROR,
    STATUS_FINISHED,
    STATUS_WORKING,
    agent_status,
    tool_status,
)
from agent_service.runtime.budget import DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS
from agent_service.runtime.context import ContentAppContext
from agent_service.schemas.state import ContentAgentState

#: Defaults the tool's own page uses when nothing else is chosen.
DEFAULT_SPEED = 1.0
DEFAULT_VOLUME = 100
DEFAULT_BG_COLOR = "#ffffff"
DEFAULT_RESOLUTION = "1080P"

#: How many voices to try before giving up. A provider-side TTS voice can be
#: broken for this account (one real voice answered code=50000) while the first
#: candidate is not necessarily a working one.
VOICE_ATTEMPTS = 3

#: How long one task's polled status is reused, so asking twice in a row does
#: not hammer the provider.
STATUS_CACHE_SECONDS = 30.0
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _gateway(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Call the Go gateway and return (status, decoded body). Synchronous."""

    base_url = load_settings().go_capability_base_url.rstrip("/")
    request = Request(
        f"{base_url}{path}",
        method=method,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"error_code": "CAPABILITY_UNAVAILABLE", "message": str(exc)}
    try:
        return status, json.loads(body) if body else {}
    except json.JSONDecodeError:
        return status, {"message": body}


async def _gateway_async(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """The same call, off the event loop.

    These requests take seconds; running them inline stalls the stream the user
    is watching, heartbeats included, for that whole time.
    """

    return await asyncio.to_thread(_gateway, method, path, payload, timeout=timeout)


async def _options(kind: str) -> list[dict[str, str]]:
    """Return the selectable avatars or voices as id/name pairs."""

    status, body = await _gateway_async("GET", f"/api/{kind}")
    if status != 200:
        return []
    rows = body.get(kind) if isinstance(body, dict) else None
    if not isinstance(rows, list):
        rows = body if isinstance(body, list) else []
    options: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("id") or row.get("avatar_id") or row.get("voice_id") or "")
        if identifier:
            options.append({"id": identifier, "name": str(row.get("name") or identifier)})
    return options


def _reply(
    runtime: ToolRuntime[ContentAppContext, ContentAgentState], payload: dict[str, Any]
) -> Command:
    if not runtime.tool_call_id:
        raise RuntimeError("video tool requires a tool call ID")
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False),
                    tool_call_id=runtime.tool_call_id,
                )
            ]
        }
    )


async def _submit_once(
    script: str,
    avatar: str,
    voice: str,
    speed: float,
    resolution: str,
    subtitle_enabled: bool,
    bg_color: str,
) -> tuple[int, Any]:
    return await _gateway_async(
        "POST",
        "/api/tasks",
        {
            "script": script,
            "avatar_id": avatar,
            "audio_man_id": voice,
            "speed": speed or DEFAULT_SPEED,
            "volume": DEFAULT_VOLUME,
            "bg_color": bg_color or DEFAULT_BG_COLOR,
            "resolution": resolution or DEFAULT_RESOLUTION,
            "subtitle_enabled": bool(subtitle_enabled),
        },
    )


def create_check_video_status_tool() -> Any:
    """Tool: report where one submitted video actually is.

    Submission and status are two halves of the same job: without this the model
    could start a video but never answer "好了吗", which is exactly the gap it
    had to explain away to the user. The caller decides when to ask — this tool
    never polls on its own.
    """

    @tool(
        "check_video_status",
        description=(
            "Check one submitted video's current status and, when it is finished, "
            "its video URL. Pass task_id, or leave it empty to check the task this "
            "session submitted last. Call it only when the user asks about the "
            "video's progress — do not poll on your own."
        ),
    )
    async def check_video_status(
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
        task_id: str = "",
    ) -> Command:
        resolved = (task_id or "").strip()
        if not resolved:
            from_state = runtime.state.get("video_task_id")
            if isinstance(from_state, str):
                resolved = from_state.strip()
        if not resolved:
            return _reply(
                runtime,
                {
                    "error_code": "INVALID_REQUEST",
                    "message": "这个会话还没有提交过视频，没有可查的任务。",
                },
            )

        cached = _STATUS_CACHE.get(resolved)
        if cached is not None and (time.time() - cached[0]) < STATUS_CACHE_SECONDS:
            return _reply(runtime, {**cached[1], "cached": True})

        # Poll the provider first; a failed refresh still leaves the local record.
        status, body = await _gateway_async("POST", f"/api/tasks/{resolved}/refresh")
        if status != 200:
            status, body = await _gateway_async("GET", f"/api/tasks/{resolved}")
        if status != 200:
            message = ""
            if isinstance(body, dict):
                message = str(body.get("error") or body.get("message") or "")
            return _reply(
                runtime,
                {
                    "error_code": "TASK_NOT_FOUND",
                    "task_id": resolved,
                    "message": message or f"查不到任务 {resolved}（HTTP {status}）",
                },
            )

        task = body if isinstance(body, dict) else {}
        # The refresh endpoint answers {"status":"ok","task":{...}}: the outer
        # status is the transport's, and reading it as the task's is what made
        # this tool report "ok" with an empty URL for a finished video.
        nested = task.get("task")
        if isinstance(nested, dict):
            task = nested
        task_status = str(task.get("status") or "unknown")
        video_url = str(task.get("video_url") or "")
        payload: dict[str, Any] = {
            "kind": "generation_status",
            "task_id": resolved,
            "status": task_status,
            "progress": task.get("progress"),
            "video_url": video_url,
            "error": str(task.get("error") or ""),
        }
        if task_status in {"done", "completed"} and not video_url:
            payload["message"] = (
                "服务方把任务标为完成但没给成片地址，这种情况需要在提交平台查看任务详情。"
            )
        _STATUS_CACHE[resolved] = (time.time(), payload)
        return _reply(runtime, payload)

    return check_video_status


def create_list_digital_humans_tool() -> Any:
    """Tool: what the account can submit with."""

    @tool(
        "list_digital_humans",
        description=(
            "List the digital-human avatars and voices this account can use, as "
            "id and name pairs. Call it when the user has not chosen an avatar or "
            "voice yet, so they can pick by name instead of by id."
        ),
    )
    async def list_digital_humans(
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
    ) -> Command:
        avatars = await _options("avatars")
        voices = await _options("voices")
        payload: dict[str, Any] = {
            "kind": "digital_human_options",
            "avatars": avatars,
            "voices": voices,
        }
        if not avatars or not voices:
            payload["error_code"] = "CAPABILITY_UNAVAILABLE"
            payload["message"] = "没有读到可选形象或人声：请确认数字人凭证已保存，再试一次。"
        return _reply(runtime, payload)

    return list_digital_humans


def create_generate_video_tool() -> Any:
    """Tool: submit one video through the same endpoint the tool page uses."""

    @tool(
        "generate_video",
        description=(
            "Submit the current script to the digital human and start the video "
            "generation. Call it only when the user clearly asks for the video "
            "(生成吧 / 提交 / 开始做视频). Needs the script text; avatar_id and "
            "audio_man_id are optional — when they are missing the first available "
            "ones are used. Returns the provider task id, or the reason it failed."
        ),
    )
    async def generate_video(
        script: str,
        runtime: ToolRuntime[ContentAppContext, ContentAgentState],
        avatar_id: str = "",
        audio_man_id: str = "",
        speed: float = DEFAULT_SPEED,
        resolution: str = DEFAULT_RESOLUTION,
        subtitle_enabled: bool = False,
        bg_color: str = DEFAULT_BG_COLOR,
    ) -> Command:
        text = (script or "").strip()
        if not text:
            return _reply(
                runtime,
                {
                    "error_code": "INVALID_REQUEST",
                    "message": "稿件正文是空的，先写稿再生成视频。",
                },
            )

        resolved_avatar = avatar_id.strip()
        if not resolved_avatar:
            avatars = await _options("avatars")
            if avatars:
                resolved_avatar = avatars[0]["id"]

        chosen_voice = audio_man_id.strip()
        if chosen_voice:
            voices = [{"id": chosen_voice, "name": chosen_voice}]
        else:
            voices = (await _options("voices"))[:VOICE_ATTEMPTS]

        if not resolved_avatar or not voices:
            return _reply(
                runtime,
                {
                    "error_code": "CAPABILITY_UNAVAILABLE",
                    "message": "缺少可用的数字人形象或人声，无法提交。",
                },
            )

        await agent_status(AGENT_GENERATION, STATUS_WORKING, message="正在提交数字人")
        await tool_status("generate_video", STATUS_WORKING, agent=AGENT_GENERATION)

        status, body = 0, {}
        used_voice = voices[0]
        for candidate in voices:
            used_voice = candidate
            status, body = await _submit_once(
                text,
                resolved_avatar,
                candidate["id"],
                speed,
                resolution,
                subtitle_enabled,
                bg_color,
            )
            if status == 200:
                break
            if status == 0:
                # The request may have reached the provider: retrying another
                # voice here could submit (and bill) a second video. Report the
                # uncertainty instead and let the user check the task list.
                await agent_status(AGENT_GENERATION, STATUS_ERROR, message="提交结果未知")
                await tool_status("generate_video", STATUS_FINISHED, agent=AGENT_GENERATION)
                return _reply(
                    runtime,
                    {
                        "error_code": "UNKNOWN_SUBMISSION",
                        "unknown_submission": True,
                        "message": (
                            "提交请求没有得到确认（超时或连接中断），无法确定服务方是否已经收单。"
                            "不要让用户重复提交：先用任务列表确认是否已经有一条任务。"
                        ),
                        "audio_man_id": candidate["id"],
                    },
                )

        if status != 200:
            message = ""
            if isinstance(body, dict):
                message = str(body.get("error") or body.get("message") or "")
            await agent_status(AGENT_GENERATION, STATUS_ERROR, message="提交失败")
            await tool_status("generate_video", STATUS_FINISHED, agent=AGENT_GENERATION)
            return _reply(
                runtime,
                {
                    "error_code": "SUBMISSION_FAILED",
                    "message": message or f"提交失败（HTTP {status}）",
                    "tried_voices": [item["name"] for item in voices],
                    "retryable": status >= 500,
                },
            )

        task_id = str((body or {}).get("task_id") or "")
        if not task_id:
            # A 200 without a task id is not a submission. Reporting success here
            # would send the user looking for a video that was never queued.
            await agent_status(AGENT_GENERATION, STATUS_ERROR, message="提交未确认")
            await tool_status("generate_video", STATUS_FINISHED, agent=AGENT_GENERATION)
            return _reply(
                runtime,
                {
                    "error_code": "SUBMISSION_UNCONFIRMED",
                    "unknown_submission": False,
                    "message": (
                        "服务方返回了成功，但没有给出任务号，无法确认视频是否已排队。"
                        "请在任务列表里确认，不要直接重复提交。"
                    ),
                    "response": body if isinstance(body, dict) else {},
                },
            )
        await agent_status(AGENT_GENERATION, STATUS_FINISHED, message="已提交数字人")
        await tool_status("generate_video", STATUS_FINISHED, agent=AGENT_GENERATION)
        command = _reply(
            runtime,
            {
                "kind": "generation_submitted",
                "task_id": task_id,
                "status": str((body or {}).get("status") or "pending"),
                "avatar_id": resolved_avatar,
                "audio_man_id": used_voice["id"],
                "audio_man_name": used_voice["name"],
                "note": "用户问进度时用 check_video_status 查这个任务。",
            },
        )
        # video_task_id, not generation_id: no local generation record exists on
        # this direct path, and the panel reads the task list instead.
        updates = dict(command.update or {})
        updates["video_task_id"] = task_id
        return Command(update=updates)

    return generate_video


__all__ = [
    "create_check_video_status_tool",
    "create_generate_video_tool",
    "create_list_digital_humans_tool",
]
