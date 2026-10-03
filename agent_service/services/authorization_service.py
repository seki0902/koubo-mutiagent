"""Business authorization for digital-human generation.

This service deliberately stops at an authorization record. It does not call
the digital-human provider or create a generation record; those are later
side-effect and idempotency tasks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent_service.schemas.generation import GenerationAuthorization, GenerationIntent
from agent_service.services.task_store import TaskStore


class AuthorizationError(RuntimeError):
    """Raised when an authorization request is not safe to issue or use."""


def hash_generation_params(params: Mapping[str, Any] | None = None) -> str:
    """Return a stable, secret-free hash for generation parameters."""

    payload = dict(params or {})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def classify_generation_intent(
    message: str,
    *,
    previous_assistant_text: str = "",
) -> GenerationIntent:
    """Classify only explicit generation consent, not general approval.

    ``就这版`` is conditional: it authorizes only after the previous
    assistant turn clearly asked whether to generate. ``这版可以`` remains a
    draft confirmation and never authorizes by itself.
    """

    text = " ".join(message.strip().split())
    previous = " ".join(previous_assistant_text.strip().split())
    if not text:
        return GenerationIntent(
            should_authorize=False,
            reason="not_authorized",
        )

    if "这版可以" in text and "这版可以生成" not in text:
        return GenerationIntent(
            should_authorize=False,
            reason="not_authorized",
            matched_phrase="这版可以",
        )

    for phrase in ("生成吧", "可以生成"):
        if phrase in text:
            return GenerationIntent(
                should_authorize=True,
                reason="explicit",
                matched_phrase=phrase,
            )

    if "就这版" in text or "就这一版" in text:
        confirmation_markers = (
            "要不要生成",
            "是否生成",
            "是否需要生成",
            "可以生成吗",
            "需要生成吗",
            "要生成吗",
            "准备生成",
            "确认生成",
        )
        if any(marker in previous for marker in confirmation_markers):
            return GenerationIntent(
                should_authorize=True,
                reason="conditional_confirmation",
                matched_phrase="就这版",
            )
        return GenerationIntent(
            should_authorize=False,
            reason="not_authorized",
            matched_phrase="就这版",
        )

    return GenerationIntent(
        should_authorize=False,
        reason="not_authorized",
    )


class AuthorizationService:
    """Issue and validate authorization objects without side effects."""

    def authorize_generation(
        self,
        store: TaskStore,
        *,
        task_id: str,
        draft_id: str,
        draft_version: int,
        params: Mapping[str, Any] | None = None,
        params_hash: str | None = None,
        authorization_id: str | None = None,
    ) -> GenerationAuthorization:
        if params is not None and params_hash is not None:
            raise ValueError("pass either params or params_hash, not both")
        if not task_id.strip() or not draft_id.strip():
            raise ValueError("task_id and draft_id must not be blank")
        if draft_version < 1:
            raise ValueError("draft_version must be at least 1")

        draft = store.get_draft(draft_id)
        if draft is None:
            raise AuthorizationError(f"draft not found: {draft_id}")
        if draft["task_id"] != task_id:
            raise AuthorizationError("task_id does not match the draft")
        if int(draft["current_version"]) != draft_version:
            raise AuthorizationError(
                "authorization must target the current draft version"
            )
        if store.get_draft_version(draft_id, draft_version) is None:
            raise AuthorizationError("requested draft version is not readable")

        resolved_params_hash = params_hash or hash_generation_params(params)
        row = store.create_generation_authorization(
            authorization_id or f"auth_{uuid4().hex}",
            task_id,
            draft_id,
            draft_version,
            resolved_params_hash,
            status="authorized",
        )
        return GenerationAuthorization.model_validate(row)

    def authorize(self, store: TaskStore, **kwargs: Any) -> GenerationAuthorization:
        """Short alias for callers treating authorization as a command."""

        return self.authorize_generation(store, **kwargs)

    def validate_for_generation(
        self,
        store: TaskStore,
        *,
        authorization_id: str,
        task_id: str,
        draft_id: str,
        draft_version: int,
        params: Mapping[str, Any] | None = None,
        params_hash: str | None = None,
    ) -> GenerationAuthorization:
        if params is not None and params_hash is not None:
            raise ValueError("pass either params or params_hash, not both")
        row = store.get_generation_authorization(authorization_id)
        if row is None:
            raise AuthorizationError(
                f"generation authorization not found: {authorization_id}"
            )
        authorization = GenerationAuthorization.model_validate(row)
        if authorization.status != "authorized":
            raise AuthorizationError(
                f"generation authorization is {authorization.status}"
            )
        if (
            authorization.task_id != task_id
            or authorization.draft_id != draft_id
            or authorization.draft_version != draft_version
        ):
            raise AuthorizationError("authorization binding does not match request")

        draft = store.get_draft(draft_id)
        if draft is None or int(draft["current_version"]) != draft_version:
            raise AuthorizationError("authorization targets a stale draft version")

        expected_hash = params_hash or (
            hash_generation_params(params) if params is not None else None
        )
        if expected_hash is not None and authorization.params_hash != expected_hash:
            raise AuthorizationError("generation parameters do not match authorization")

        if authorization.expires_at is not None:
            now = datetime.now(timezone.utc)
            if authorization.expires_at <= now:
                raise AuthorizationError("generation authorization has expired")
        return authorization

    def validate(self, store: TaskStore, **kwargs: Any) -> GenerationAuthorization:
        """Short alias for the pre-generation validation boundary."""

        return self.validate_for_generation(store, **kwargs)


parse_generation_intent = classify_generation_intent


__all__ = [
    "AuthorizationError",
    "AuthorizationService",
    "classify_generation_intent",
    "hash_generation_params",
    "parse_generation_intent",
]
