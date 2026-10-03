"""The section 63.5 call limits for one Supervisor turn.

V6 drops the Agents SDK's ``max_turns`` in favour of a LangGraph
``recursion_limit`` plus application-level counting.  The numbers live here so
there is one place to read them, and so a turn that is stopped can say *why*
instead of quietly running long.

``ContentAppContext`` is frozen so the model cannot edit or checkpoint it.  The
counters still have to move while the turn runs, so they hang off that context
as one mutable object rather than as fields on the frozen dataclass.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

#: Super-steps LangGraph allows one run before it stops the graph.
RECURSION_LIMIT = 12

#: Specialist calls allowed per request, in total and per specialist.
MAX_SPECIALIST_CALLS = 6
MAX_SAME_SPECIALIST_CALLS = 2

#: Research rounds allowed per request.
MAX_RESEARCH_ROUNDS = 2

#: Platform searches one specialist turn may run, before the tool answers with a
#: bounded refusal. Configurable through ``SUPERVISOR_SEARCH_LIMIT`` because it
#: is a straight latency/thoroughness trade-off: each search costs 3-11 seconds
#: of a turn the user is waiting on.
SEARCH_CALLS_PER_TURN = 2


def search_call_limit(default: int = SEARCH_CALLS_PER_TURN) -> int:
    """Return the configured per-turn search cap."""

    raw = os.environ.get("SUPERVISOR_SEARCH_LIMIT", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default

#: Retries allowed per request, and only for a retryable failure.
MAX_TOOL_RETRIES = 1

#: Explicit timeouts for the Go capability tools, in seconds.
#: A platform search is not a single request: the Go side runs several web
#: searches and then one model extraction pass, which routinely takes 30-60s.
#: The old 20s cut those off as CAPABILITY_TIMEOUT, so searching looked broken
#: even when the capability itself worked.
SEARCH_TIMEOUT_SECONDS = 90.0
FETCH_SOURCE_TIMEOUT_SECONDS = 15.0
DIGITAL_HUMAN_SUBMIT_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


class BudgetExceeded(RuntimeError):
    """Raised when a turn asks for more work than section 63.5 allows."""

    def __init__(self, *, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit

    def as_payload(self) -> dict[str, Any]:
        """Render the refusal as a tool result the model can report."""

        return {
            "error_code": "BUDGET_EXCEEDED",
            "limit": self.limit,
            "message": str(self),
            "uncertainty": str(self),
        }


@dataclass(slots=True)
class RequestBudget:
    """Mutable counters for one Supervisor turn."""

    max_specialist_calls: int = MAX_SPECIALIST_CALLS
    max_same_specialist_calls: int = MAX_SAME_SPECIALIST_CALLS
    max_research_rounds: int = MAX_RESEARCH_ROUNDS
    specialist_call_count: int = 0
    specialist_calls: dict[str, int] = field(default_factory=dict)
    research_round_count: int = 0
    tool_retry_count: int = 0
    max_tool_retries: int = MAX_TOOL_RETRIES
    limitations: list[str] = field(default_factory=list)

    def claim_specialist(self, name: str) -> int:
        """Reserve one specialist call, or refuse it for a stated reason."""

        normalized = name.strip()
        if not normalized:
            raise ValueError("specialist name must not be blank")

        if self.specialist_call_count + 1 > self.max_specialist_calls:
            raise self._refuse(
                limit="specialist_call_count",
                message=(
                    f"This request has used all {self.max_specialist_calls} "
                    f"specialist calls, so {normalized} was not run."
                ),
            )

        used = self.specialist_calls.get(normalized, 0)
        if used + 1 > self.max_same_specialist_calls:
            raise self._refuse(
                limit="same_specialist_call_count",
                message=(
                    f"{normalized} has already run "
                    f"{self.max_same_specialist_calls} times in this request, "
                    "which is the limit."
                ),
            )

        self.specialist_call_count += 1
        self.specialist_calls[normalized] = used + 1
        return self.specialist_call_count

    def claim_research_round(self) -> int:
        """Reserve one Research round trip."""

        if self.research_round_count + 1 > self.max_research_rounds:
            raise self._refuse(
                limit="research_round_count",
                message=(
                    f"Research has already run {self.max_research_rounds} "
                    "rounds for this request, which is the limit."
                ),
            )
        self.research_round_count += 1
        return self.research_round_count

    def claim_tool_retry(self) -> bool:
        """Reserve one tool retry, answering whether one was available."""

        if self.tool_retry_count + 1 > self.max_tool_retries:
            self.note_limitation(
                "A retryable tool call failed and this request's retry budget "
                "was already spent."
            )
            return False
        self.tool_retry_count += 1
        return True

    def note_limitation(self, text: str) -> None:
        """Record something the user should know was left unresolved."""

        normalized = text.strip()
        if normalized and normalized not in self.limitations:
            self.limitations.append(normalized)

    def uncertainties(self) -> list[str]:
        """Everything this turn could not settle, ready to report."""

        return list(self.limitations)

    def _refuse(self, *, limit: str, message: str) -> BudgetExceeded:
        self.note_limitation(message)
        return BudgetExceeded(limit=limit, message=message)


_CURRENT: ContextVar[RequestBudget | None] = ContextVar(
    "koubo_request_budget", default=None
)


@contextlib.contextmanager
def bind_budget(budget: RequestBudget) -> Iterator[None]:
    """Bind the turn's budget for the duration of a run.

    The binding reaches LangGraph's tool tasks, and the capability clients they
    call, without every layer in between having to pass it along.
    """

    token = _CURRENT.set(budget)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_budget() -> RequestBudget | None:
    """Return the bound budget, or ``None`` outside a request."""

    return _CURRENT.get()


def claim_tool_retry() -> bool:
    """Reserve one tool retry, answering whether one was available.

    With no budget bound -- a client exercised directly in a test -- retries
    are unlimited, which keeps the clients usable on their own.
    """

    budget = current_budget()
    if budget is None:
        return True
    return budget.claim_tool_retry()


async def retry_once(call: Callable[[], Awaitable[T]]) -> T:
    """Run ``call``, retrying once when it fails for a retryable reason.

    Only the first failure is retried, and only if the request still has a
    retry: a busy or slow upstream must not be able to spend a whole turn.
    """

    try:
        return await call()
    except Exception as exc:  # noqa: BLE001 - re-raised when not retryable
        if not getattr(exc, "retryable", False) or not claim_tool_retry():
            raise
        return await call()


def claim_specialist(context: Any, name: str) -> dict[str, Any] | None:
    """Claim one specialist call, unless this request is already out.

    Returns the tool result to answer with when the call is refused, and
    ``None`` when it may go ahead.  A context without a budget -- tools are
    also built directly in tests -- is treated as unlimited.
    """

    budget = getattr(context, "budget", None)
    if not isinstance(budget, RequestBudget):
        return None
    try:
        budget.claim_specialist(name)
    except BudgetExceeded as exc:
        return exc.as_payload()
    return None


def claim_research_round(context: Any) -> dict[str, Any] | None:
    """Claim one Research round trip, answering ``None`` when it may proceed."""

    budget = getattr(context, "budget", None)
    if not isinstance(budget, RequestBudget):
        return None
    try:
        budget.claim_research_round()
    except BudgetExceeded as exc:
        return exc.as_payload()
    return None
