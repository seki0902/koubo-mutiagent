"""Repository for source-traceable ResearchResult records."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_service.db.database import connect_database, initialize_database
from agent_service.schemas.research import ResearchFact, ResearchResult, SourceItem


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime_to_db(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime_from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ResearchStore:
    """Connection-scoped repository backed by the Business SQLite database."""

    def __init__(self, db_path: str | Path):
        self.connection = connect_database(db_path)
        initialize_database(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def save_research_result(
        self,
        task_id: str,
        result: ResearchResult,
    ) -> ResearchResult:
        """Persist one result, its sources, and fact-to-source links atomically."""

        if not task_id.strip():
            raise ValueError("task_id must not be blank")

        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO research_results
                    (research_result_id, task_id, summary,
                     open_questions_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.research_result_id,
                    task_id,
                    result.summary,
                    json.dumps(result.open_questions, ensure_ascii=False),
                    now,
                ),
            )
            for source in result.sources:
                self.connection.execute(
                    """
                    INSERT INTO research_sources
                        (source_id, research_result_id, title, url, source_type,
                         published_at, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.source_id,
                        result.research_result_id,
                        source.title,
                        source.url,
                        source.source_type,
                        _datetime_to_db(source.published_at),
                        _datetime_to_db(source.fetched_at),
                    ),
                )
            for fact in result.facts:
                self.connection.execute(
                    """
                    INSERT INTO research_facts
                        (fact_id, research_result_id, statement,
                         source_ids_json, verification_status, verified_at,
                         valid_until, recruitment_status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.fact_id,
                        result.research_result_id,
                        fact.claim,
                        json.dumps(fact.source_ids, ensure_ascii=False),
                        fact.verification_status,
                        _datetime_to_db(fact.verified_at),
                        _datetime_to_db(fact.valid_until),
                        fact.recruitment_status,
                        fact.notes,
                    ),
                )
                for source_id in fact.source_ids:
                    self.connection.execute(
                        """
                        INSERT INTO research_fact_sources
                            (research_result_id, fact_id, source_id)
                        VALUES (?, ?, ?)
                        """,
                        (result.research_result_id, fact.fact_id, source_id),
                    )

        return self.get_research_result(result.research_result_id)  # type: ignore[return-value]

    def create_research_result(
        self,
        task_id: str,
        result: ResearchResult,
    ) -> ResearchResult:
        """Descriptive alias for callers treating persistence as creation."""

        return self.save_research_result(task_id, result)

    def get_research_result(
        self, research_result_id: str
    ) -> ResearchResult | None:
        row = self.connection.execute(
            """
            SELECT research_result_id, summary, open_questions_json
            FROM research_results
            WHERE research_result_id = ?
            """,
            (research_result_id,),
        ).fetchone()
        if row is None:
            return None

        sources = [
            SourceItem(
                source_id=str(source["source_id"]),
                title=str(source["title"]),
                url=str(source["url"]),
                source_type=str(source["source_type"]),
                published_at=_datetime_from_db(source["published_at"]),
                fetched_at=_datetime_from_db(source["fetched_at"]),
            )
            for source in self.connection.execute(
                """
                SELECT source_id, title, url, source_type,
                       published_at, fetched_at
                FROM research_sources
                WHERE research_result_id = ?
                ORDER BY rowid ASC
                """,
                (research_result_id,),
            ).fetchall()
        ]

        facts: list[ResearchFact] = []
        fact_rows = self.connection.execute(
            """
            SELECT fact_id, statement, verification_status, verified_at,
                   valid_until, recruitment_status, notes
            FROM research_facts
            WHERE research_result_id = ?
            ORDER BY rowid ASC
            """,
            (research_result_id,),
        ).fetchall()
        for fact in fact_rows:
            source_rows = self.connection.execute(
                """
                SELECT source_id
                FROM research_fact_sources
                WHERE research_result_id = ? AND fact_id = ?
                ORDER BY rowid ASC
                """,
                (research_result_id, fact["fact_id"]),
            ).fetchall()
            facts.append(
                ResearchFact(
                    fact_id=str(fact["fact_id"]),
                    claim=str(fact["statement"]),
                    source_ids=[str(source["source_id"]) for source in source_rows],
                    verification_status=str(fact["verification_status"]),
                    verified_at=_datetime_from_db(fact["verified_at"]),
                    valid_until=_datetime_from_db(fact["valid_until"]),
                    recruitment_status=str(fact["recruitment_status"]),
                    notes=fact["notes"],
                )
            )

        open_questions = json.loads(row["open_questions_json"])
        if not isinstance(open_questions, list):
            raise ValueError("stored open_questions_json must be a list")
        return ResearchResult(
            research_result_id=str(row["research_result_id"]),
            summary=str(row["summary"]),
            facts=facts,
            sources=sources,
            open_questions=[str(item) for item in open_questions],
        )

    def get_fact_sources(
        self,
        research_result_id: str,
        fact_id: str,
    ) -> list[SourceItem]:
        """Return the concrete SourceItems supporting one fact."""

        rows = self.connection.execute(
            """
            SELECT s.source_id, s.title, s.url, s.source_type,
                   s.published_at, s.fetched_at
            FROM research_fact_sources AS links
            JOIN research_sources AS s
              ON s.research_result_id = links.research_result_id
             AND s.source_id = links.source_id
            WHERE links.research_result_id = ? AND links.fact_id = ?
            ORDER BY links.rowid ASC
            """,
            (research_result_id, fact_id),
        ).fetchall()
        return [
            SourceItem(
                source_id=str(row["source_id"]),
                title=str(row["title"]),
                url=str(row["url"]),
                source_type=str(row["source_type"]),
                published_at=_datetime_from_db(row["published_at"]),
                fetched_at=_datetime_from_db(row["fetched_at"]),
            )
            for row in rows
        ]


__all__ = ["ResearchStore"]
