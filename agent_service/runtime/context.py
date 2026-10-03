"""Request-scoped runtime context for LangGraph tools and middleware."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from agent_service.runtime.budget import RequestBudget


@dataclass(frozen=True, slots=True)
class ContentAppContext:
    """Dependencies the model cannot edit or checkpoint.

    Handles are intentionally typed as ``object`` at this layer.  Concrete
    repositories and capability clients arrive in later tasks without making
    the thread state depend on their implementations.  Business DB remains
    canonical truth; this context only carries request-scoped handles.
    """

    request_id: str
    session_id: str
    task_store: object | None = None
    stores: Mapping[str, object] = field(default_factory=dict)
    services: Mapping[str, object] = field(default_factory=dict)
    go_capability_client: object | None = None
    config: object | None = None
    #: Mutable on purpose: freezing the context is what keeps the model from
    #: editing it, while the counters inside still have to move during a turn.
    budget: RequestBudget | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be blank")
        if not self.session_id.strip():
            raise ValueError("session_id must not be blank")
