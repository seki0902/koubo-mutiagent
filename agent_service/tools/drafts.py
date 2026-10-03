"""Deterministic draft-version tools backed by :class:`TaskStore`.

These functions deliberately contain no LLM or agent dependency.  They are
the storage boundary used by later agents, while the repository remains the
source of truth for immutable versions and the committed current pointer.
"""

from __future__ import annotations

from agent_service.schemas.draft import Draft, DraftVersion
from agent_service.services.task_store import TaskStore


def create_draft(
    store: TaskStore,
    draft_id: str,
    task_id: str,
    topic_id: str,
    body: str,
    *,
    change_instruction: str = "",
    created_by: str = "system",
) -> Draft:
    """Create a draft and its first immutable version, v1."""

    row = store.create_draft(
        draft_id,
        task_id,
        topic_id,
        body,
        change_instruction=change_instruction,
        created_by=created_by,
    )
    return Draft.model_validate(row)


def save_draft_version(
    store: TaskStore,
    draft_id: str,
    body: str,
    *,
    change_instruction: str = "",
    created_by: str = "system",
    invalidate_authorizations: bool = False,
) -> DraftVersion:
    """Append the next version and advance the draft pointer atomically."""

    row = store.save_draft_version(
        draft_id,
        body,
        change_instruction=change_instruction,
        created_by=created_by,
        invalidate_authorizations=invalidate_authorizations,
    )
    return DraftVersion.model_validate(row)


def get_draft(store: TaskStore, draft_id: str) -> Draft | None:
    row = store.get_draft(draft_id)
    return Draft.model_validate(row) if row is not None else None


def get_current_draft(store: TaskStore, draft_id: str) -> DraftVersion | None:
    """Read the version selected by the draft's committed current pointer."""

    row = store.get_current_draft(draft_id)
    return DraftVersion.model_validate(row) if row is not None else None


def invalidate_draft_dependencies(
    store: TaskStore, draft_id: str, new_version: int
) -> int:
    """Invalidate old generation authorization state after a revision."""

    return store.revoke_generation_authorizations(
        draft_id, before_version=new_version
    )


def get_draft_version(
    store: TaskStore, draft_id: str, version: int
) -> DraftVersion | None:
    row = store.get_draft_version(draft_id, version)
    return DraftVersion.model_validate(row) if row is not None else None


__all__ = [
    "create_draft",
    "get_current_draft",
    "get_draft",
    "get_draft_version",
    "invalidate_draft_dependencies",
    "save_draft_version",
]
