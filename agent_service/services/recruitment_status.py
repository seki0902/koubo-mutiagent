"""Deterministic recruitment-status evaluation for ResearchResult data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse

from agent_service.schemas.research import (
    RecruitmentStatusDecision,
    ResearchFact,
    ResearchResult,
    SourceItem,
)


_OFFICIAL_TYPES = {
    "official",
    "employer",
    "government",
    "university",
}
_RELIABLE_ALTERNATIVE_TYPES = {"recruitment_platform"}
_SECONDARY_MARKERS = {
    "aggregator",
    "blog",
    "notice",
    "repost",
    "转载",
    "其他",
    "web",
}
_INACCESSIBLE_MARKERS = {
    "blocked",
    "unavailable",
    "not_found",
    "404",
    "timeout",
    "inaccessible",
    "cannot access",
    "无法访问",
    "打不开",
    "访问被阻止",
    "超时",
}
_CONFLICT_MARKERS = {
    "conflict",
    "conflicting",
    "contradict",
    "冲突",
    "不一致",
}


def _normalized_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _source_is_official(source: SourceItem) -> bool:
    source_type = source.source_type.strip().lower()
    if source_type in _OFFICIAL_TYPES:
        return True
    parsed = urlparse(source.url)
    host = (parsed.hostname or "").lower()
    return host.endswith(".gov") or host.endswith(".gov.cn") or host.endswith(".edu.cn")


def _source_is_reliable(source: SourceItem) -> bool:
    source_type = source.source_type.strip().lower()
    if _source_is_official(source):
        return True
    return source_type in _RELIABLE_ALTERNATIVE_TYPES


def _text_has_marker(values: Iterable[str | None], markers: set[str]) -> bool:
    text = " ".join(value.strip().lower() for value in values if value).strip()
    return any(marker.lower() in text for marker in markers)


def _fact_sources(
    fact: ResearchFact,
    sources_by_id: dict[str, SourceItem],
) -> list[SourceItem]:
    return [
        sources_by_id[source_id]
        for source_id in fact.source_ids
        if source_id in sources_by_id
    ]


def _fact_candidate(
    fact: ResearchFact,
    *,
    now: datetime,
) -> tuple[str, str] | None:
    """Apply hard date/status rules to one already-traceable fact."""

    if fact.valid_until is not None:
        valid_until = fact.valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        else:
            valid_until = valid_until.astimezone(timezone.utc)
        if valid_until <= now:
            return "closed", "past_deadline"

    if fact.recruitment_status == "closed":
        return "closed", "explicit_closed"

    # A usable deadline is harder evidence than a status label, so it wins over
    # an explicit "open" and keeps the reason code traceable to the date rule.
    if fact.valid_until is not None:
        return "open", "future_deadline"
    if fact.recruitment_status == "open":
        return "open", "explicit_open"

    return None


class RecruitmentStatusService:
    """Evaluate ResearchResult recruitment facts without model inference."""

    def evaluate(
        self,
        result: ResearchResult,
        *,
        now: datetime | None = None,
    ) -> RecruitmentStatusDecision:
        evaluated_at = _normalized_now(now)
        sources_by_id = {source.source_id: source for source in result.sources}
        candidates: list[tuple[str, ResearchFact, str, str, list[SourceItem]]] = []
        relevant_fact_ids: list[str] = []
        relevant_source_ids: list[str] = []
        inaccessible = False
        unreliable_only = False

        for fact in result.facts:
            if (
                fact.recruitment_status == "not_applicable"
                and fact.valid_until is None
            ):
                continue
            relevant_fact_ids.append(fact.fact_id)
            fact_sources = _fact_sources(fact, sources_by_id)
            for source in fact_sources:
                if source.source_id not in relevant_source_ids:
                    relevant_source_ids.append(source.source_id)

            fact_text = [fact.claim, fact.notes]
            if _text_has_marker(fact_text, _INACCESSIBLE_MARKERS):
                inaccessible = True
                continue
            if _text_has_marker(fact_text, _CONFLICT_MARKERS) or fact.verification_status == "conflicting":
                return RecruitmentStatusDecision(
                    status="unknown",
                    reason_code="source_conflict",
                    reason="Credible evidence contains a conflict, so recruitment status cannot be confirmed.",
                    conflict=True,
                    fact_ids=relevant_fact_ids,
                    source_ids=relevant_source_ids,
                    evaluated_at=evaluated_at,
                )
            if fact.verification_status != "verified":
                continue

            if not fact_sources or not any(_source_is_reliable(source) for source in fact_sources):
                unreliable_only = True
                continue

            candidate = _fact_candidate(fact, now=evaluated_at)
            if candidate is not None:
                candidates.append(
                    (fact.fact_id, fact, candidate[0], candidate[1], fact_sources)
                )

        if not candidates:
            if inaccessible:
                reason_code = "source_inaccessible"
                reason = "The official source is inaccessible and no reliable alternative evidence is available."
            elif unreliable_only:
                reason_code = "unreliable_source"
                reason = "Only secondary or unverified sources support the recruitment claim."
            elif relevant_fact_ids:
                reason_code = "missing_evidence"
                reason = "The available evidence does not provide a verified recruitment status or usable deadline."
            else:
                reason_code = "missing_evidence"
                reason = "No recruitment-status fact or usable deadline was provided."
            return RecruitmentStatusDecision(
                status="unknown",
                reason_code=reason_code,
                reason=reason,
                fact_ids=relevant_fact_ids,
                source_ids=relevant_source_ids,
                evaluated_at=evaluated_at,
            )

        candidate_statuses = {candidate[2] for candidate in candidates}
        candidate_fact_ids = [candidate[0] for candidate in candidates]
        candidate_source_ids: list[str] = []
        for _, _, _, _, sources in candidates:
            for source in sources:
                if source.source_id not in candidate_source_ids:
                    candidate_source_ids.append(source.source_id)

        if len(candidate_statuses) > 1:
            return RecruitmentStatusDecision(
                status="unknown",
                reason_code="source_conflict",
                reason="Credible sources disagree about whether the recruitment window is open.",
                conflict=True,
                fact_ids=candidate_fact_ids,
                source_ids=candidate_source_ids,
                evaluated_at=evaluated_at,
            )

        status = next(iter(candidate_statuses))
        candidate_reason_codes = {candidate[3] for candidate in candidates}
        if "past_deadline" in candidate_reason_codes:
            reason_code = "past_deadline"
        elif "future_deadline" in candidate_reason_codes:
            reason_code = "future_deadline"
        else:
            reason_code = "explicit_status"
        return RecruitmentStatusDecision(
            status=status,
            reason_code=reason_code,
            reason=(
                "A verified reliable source provides a future application deadline."
                if status == "open"
                else "A verified reliable source indicates that the application deadline has passed or the listing is closed."
            ),
            fact_ids=candidate_fact_ids,
            source_ids=candidate_source_ids,
            evaluated_at=evaluated_at,
        )

    def classify(
        self,
        result: ResearchResult,
        *,
        now: datetime | None = None,
    ) -> RecruitmentStatusDecision:
        """Alias for callers that use classification terminology."""

        return self.evaluate(result, now=now)


def determine_recruitment_status(
    result: ResearchResult,
    *,
    now: datetime | None = None,
) -> RecruitmentStatusDecision:
    """Stateless convenience wrapper for deterministic status evaluation."""

    return RecruitmentStatusService().evaluate(result, now=now)


classify_recruitment_status = determine_recruitment_status
evaluate_recruitment_status = determine_recruitment_status


__all__ = [
    "RecruitmentStatusService",
    "classify_recruitment_status",
    "determine_recruitment_status",
    "evaluate_recruitment_status",
]
