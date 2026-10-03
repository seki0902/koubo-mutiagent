"""Typed models for durable draft records and immutable draft versions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class Draft(BaseModel):
    """The mutable draft pointer stored for one task/topic pair."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    current_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class DraftVersion(BaseModel):
    """One immutable body revision belonging to a draft."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    body: str = Field(min_length=1)
    change_instruction: str = ""
    created_by: str = Field(default="system", min_length=1)
    created_at: datetime


class DraftSection(BaseModel):
    """One ordered, independently addressable section of a generated script."""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1)
    section_type: Literal["hook", "body", "cta", "other"]
    order: int = Field(ge=1)
    text: str = Field(min_length=1)


class DraftPayload(BaseModel):
    """Structured Script Agent output before any persistence takes place.

    ``full_script`` is derived from ``sections`` and is intentionally not a
    second independently writable source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1)
    research_result_id: str | None = None
    sections: list[DraftSection] = Field(min_length=3)
    sources_used: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_script_sections(self) -> "DraftPayload":
        section_types = {section.section_type for section in self.sections}
        missing = {"hook", "body", "cta"} - section_types
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"draft sections must include: {missing_text}")

        orders = sorted(section.order for section in self.sections)
        if orders != list(range(1, len(self.sections) + 1)):
            raise ValueError("draft section order must be contiguous and start at 1")
        return self

    @computed_field
    @property
    def full_script(self) -> str:
        """Compose the user-facing script from the canonical sections."""

        return compose_full_script(self.sections)


def compose_full_script(sections: list[DraftSection]) -> str:
    """Join sections in their explicit order; section order is canonical."""

    return "\n\n".join(
        section.text.strip() for section in sorted(sections, key=lambda item: item.order)
    )


# A descriptive alias for callers that want to distinguish generation output
# from the persisted Draft pointer model above.
ScriptDraft = DraftPayload


__all__ = [
    "Draft",
    "DraftPayload",
    "DraftSection",
    "DraftVersion",
    "ScriptDraft",
    "compose_full_script",
]
