"""Authorized, idempotent digital-human generation submission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from agent_service.schemas.generation import GenerationAuthorization
from agent_service.services.authorization_service import (
    AuthorizationService,
    hash_generation_params,
)
from agent_service.services.task_store import TaskStore
from agent_service.tools.digital_human import (
    DigitalHumanSubmission,
    DigitalHumanToolError,
)


class DigitalHumanSubmitter(Protocol):
    """Small seam allowing the provider capability to be replaced in tests."""

    def submit(
        self,
        *,
        task_id: str,
        draft_id: str,
        draft_version: int,
        authorization_id: str,
        idempotency_key: str,
        generation_id: str,
        script: str,
        params: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> DigitalHumanSubmission:
        ...


class GenerationServiceError(RuntimeError):
    """Base error for generation validation and submission failures."""


class GenerationSubmissionError(GenerationServiceError):
    """A definitive provider submission failure after recording the outcome."""

    def __init__(self, message: str, *, generation: dict[str, Any]) -> None:
        super().__init__(message)
        self.generation = generation


def generation_idempotency_key(
    authorization: GenerationAuthorization,
) -> str:
    """Build a stable key from the authorization's immutable target."""

    material = {
        "task_id": authorization.task_id,
        "draft_id": authorization.draft_id,
        "draft_version": authorization.draft_version,
        "params_hash": authorization.params_hash,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"gen_{hashlib.sha256(encoded).hexdigest()}"


class GenerationService:
    """Run the complete pre-submit and idempotency boundary for one generation."""

    def __init__(
        self,
        digital_human_tool: DigitalHumanSubmitter,
        *,
        authorization_service: AuthorizationService | None = None,
    ) -> None:
        self.digital_human_tool = digital_human_tool
        self.authorization_service = authorization_service or AuthorizationService()

    def submit(
        self,
        store: TaskStore,
        *,
        task_id: str,
        draft_id: str,
        draft_version: int,
        authorization_id: str,
        script: str,
        params: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit once, or return the existing generation for this authorization.

        The ordering in this method is intentional and is part of the Task
        2.10 contract: authorization and content validation happen before the
        key is calculated; the local pending row is reserved before the
        external side effect; the provider id is saved only after submit.
        """

        resolved_params = dict(params or {})
        authorization = self.authorization_service.validate_for_generation(
            store,
            authorization_id=authorization_id,
            task_id=task_id,
            draft_id=draft_id,
            draft_version=draft_version,
            params=resolved_params,
        )

        version_row = store.get_draft_version(draft_id, draft_version)
        if version_row is None:
            raise GenerationServiceError("draft version is not readable")
        if not script.strip():
            raise GenerationServiceError("script must not be blank")
        if script != str(version_row["body"]):
            raise GenerationServiceError("script does not match the authorized draft")

        # Keep this explicit even though authorization validation already
        # checked the same value.  It makes the submit boundary self-contained
        # and prevents a future caller from accidentally bypassing the params
        # check while reusing this service.
        if hash_generation_params(resolved_params) != authorization.params_hash:
            raise GenerationServiceError(
                "generation parameters do not match authorization"
            )

        idempotency_key = generation_idempotency_key(authorization)
        existing = store.get_generation_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        generation_id = f"generation_{uuid4().hex}"
        try:
            store.create_generation_record(
                generation_id,
                task_id,
                authorization_id,
                draft_id,
                draft_version,
                idempotency_key,
                status="pending",
            )
        except sqlite3.IntegrityError:
            # Another caller may have reserved the same idempotency key between
            # the lookup and insert.  Read it back and never submit twice.
            existing = store.get_generation_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
            raise

        try:
            submission = self.digital_human_tool.submit(
                task_id=task_id,
                draft_id=draft_id,
                draft_version=draft_version,
                authorization_id=authorization_id,
                idempotency_key=idempotency_key,
                generation_id=generation_id,
                script=script,
                params=resolved_params,
                request_id=request_id,
            )
        except DigitalHumanToolError as exc:
            status = "unknown_submission" if exc.unknown_submission else "failed"
            generation = store.update_generation_submission(
                generation_id,
                status=status,
                error_code=exc.error_code,
                error_message=str(exc),
            )
            if exc.unknown_submission:
                return generation
            raise GenerationSubmissionError(
                "digital-human provider rejected the submission",
                generation=generation,
            ) from exc

        provider_task_id = submission.provider_task_id.strip()
        if not provider_task_id:
            generation = store.update_generation_submission(
                generation_id,
                status="unknown_submission",
                error_code="UPSTREAM_ERROR",
                error_message="provider returned no task id",
            )
            return generation

        status = submission.status
        if status not in {"pending", "generating"}:
            status = "pending"
        return store.update_generation_submission(
            generation_id,
            status=status,
            provider_task_id=provider_task_id,
        )

    def submit_generation(self, store: TaskStore, **kwargs: Any) -> dict[str, Any]:
        """Descriptive alias for callers that name the side effect explicitly."""

        return self.submit(store, **kwargs)


__all__ = [
    "DigitalHumanSubmitter",
    "GenerationService",
    "GenerationServiceError",
    "GenerationSubmissionError",
    "generation_idempotency_key",
]
