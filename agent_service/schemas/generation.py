"""Schemas for generation authorization and intent classification."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AuthorizationStatus = Literal["authorized", "consumed", "revoked"]
AuthorizationIntentReason = Literal[
    "explicit",
    "conditional_confirmation",
    "not_authorized",
]


class GenerationAuthorization(BaseModel):
    """A permission bound to one exact task, draft version, and parameters."""

    model_config = ConfigDict(extra="forbid")

    authorization_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    params_hash: str = Field(min_length=1)
    status: AuthorizationStatus
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class GenerationIntent(BaseModel):
    """Deterministic interpretation of a user's generation wording."""

    model_config = ConfigDict(extra="forbid")

    should_authorize: bool
    reason: AuthorizationIntentReason
    matched_phrase: str | None = None

    @property
    def authorized(self) -> bool:
        """Readable alias for callers that use authorization terminology."""

        return self.should_authorize


__all__ = [
    "AuthorizationIntentReason",
    "AuthorizationStatus",
    "GenerationAuthorization",
    "GenerationIntent",
]
