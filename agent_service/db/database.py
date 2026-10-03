"""SQLite connection and versioned schema initialization for business state.

This database is intentionally separate from the LangGraph checkpointer.  The
checkpointer is introduced by Task 1.4 and must not read or mutate these
business tables directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final


SCHEMA_VERSION: Final = 5

#: How long a writer waits for another writer to release the SQLite file.
BUSY_TIMEOUT_SECONDS: Final = 5.0

SCHEMA_V1_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS content_tasks (
        task_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        current_goal TEXT,
        state_revision INTEGER NOT NULL DEFAULT 0
            CHECK (state_revision >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_batches (
        candidate_batch_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_items (
        topic_id TEXT PRIMARY KEY,
        candidate_batch_id TEXT NOT NULL,
        display_index INTEGER NOT NULL CHECK (display_index > 0),
        title TEXT NOT NULL,
        why_now TEXT NOT NULL DEFAULT '',
        target_audience TEXT NOT NULL DEFAULT '',
        content_angle TEXT NOT NULL DEFAULT '',
        key_points_json TEXT NOT NULL DEFAULT '[]',
        sources_json TEXT NOT NULL DEFAULT '[]',
        freshness TEXT NOT NULL DEFAULT '',
        risks_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        UNIQUE (candidate_batch_id, display_index),
        FOREIGN KEY (candidate_batch_id) REFERENCES candidate_batches(candidate_batch_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        topic_id TEXT NOT NULL,
        current_version INTEGER NOT NULL DEFAULT 0
            CHECK (current_version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE,
        FOREIGN KEY (topic_id) REFERENCES candidate_items(topic_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_versions (
        draft_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        body TEXT NOT NULL,
        change_instruction TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT 'system',
        created_at TEXT NOT NULL,
        PRIMARY KEY (draft_id, version),
        FOREIGN KEY (draft_id) REFERENCES drafts(draft_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation_authorizations (
        authorization_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        draft_version INTEGER NOT NULL,
        params_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'authorized'
            CHECK (status IN ('authorized', 'consumed', 'revoked')),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE,
        FOREIGN KEY (draft_id, draft_version)
            REFERENCES draft_versions(draft_id, version)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation_records (
        generation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        authorization_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        draft_version INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        provider_task_id TEXT UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN (
                'pending', 'generating', 'done', 'failed',
                'timeout', 'unknown_submission'
            )),
        progress INTEGER NOT NULL DEFAULT 0
            CHECK (progress >= 0 AND progress <= 100),
        video_url TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE,
        FOREIGN KEY (authorization_id)
            REFERENCES generation_authorizations(authorization_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (draft_id, draft_version)
            REFERENCES draft_versions(draft_id, version)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_candidate_batches_task_id
        ON candidate_batches(task_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_candidate_items_batch_id
        ON candidate_items(candidate_batch_id, display_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_drafts_task_id
        ON drafts(task_id, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_generation_records_task_id
        ON generation_records(task_id, created_at)
    """,
)


SCHEMA_V2_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS research_results (
        research_result_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        open_questions_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_sources (
        source_id TEXT PRIMARY KEY,
        research_result_id TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        source_type TEXT NOT NULL,
        published_at TEXT,
        fetched_at TEXT,
        FOREIGN KEY (research_result_id)
            REFERENCES research_results(research_result_id)
            ON DELETE CASCADE,
        UNIQUE (research_result_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_facts (
        fact_id TEXT PRIMARY KEY,
        research_result_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        source_ids_json TEXT NOT NULL DEFAULT '[]',
        verification_status TEXT NOT NULL
            CHECK (verification_status IN (
                'verified', 'partially_verified', 'not_verified', 'conflicting'
            )),
        verified_at TEXT,
        valid_until TEXT,
        recruitment_status TEXT NOT NULL DEFAULT 'not_applicable'
            CHECK (recruitment_status IN (
                'open', 'closed', 'unknown', 'not_applicable'
            )),
        notes TEXT,
        FOREIGN KEY (research_result_id)
            REFERENCES research_results(research_result_id)
            ON DELETE CASCADE,
        UNIQUE (research_result_id, fact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_fact_sources (
        research_result_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        PRIMARY KEY (research_result_id, fact_id, source_id),
        FOREIGN KEY (research_result_id, fact_id)
            REFERENCES research_facts(research_result_id, fact_id)
            ON DELETE CASCADE,
        FOREIGN KEY (research_result_id, source_id)
            REFERENCES research_sources(research_result_id, source_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_research_results_task_id
        ON research_results(task_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_research_sources_result_id
        ON research_sources(research_result_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_research_facts_result_id
        ON research_facts(research_result_id)
    """,
)


SCHEMA_V3_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS reviews (
        review_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        draft_version INTEGER NOT NULL CHECK (draft_version > 0),
        status TEXT NOT NULL
            CHECK (status IN ('pass', 'revise', 'need_research')),
        summary TEXT NOT NULL DEFAULT '',
        issues_json TEXT NOT NULL DEFAULT '[]',
        must_fix_json TEXT NOT NULL DEFAULT '[]',
        risk_items_json TEXT NOT NULL DEFAULT '[]',
        revision_request TEXT,
        research_request TEXT,
        ready_for_generation INTEGER NOT NULL DEFAULT 0
            CHECK (ready_for_generation IN (0, 1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES content_tasks(task_id)
            ON DELETE CASCADE,
        FOREIGN KEY (draft_id, draft_version)
            REFERENCES draft_versions(draft_id, version)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reviews_draft_version
        ON reviews(draft_id, draft_version, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reviews_task_id
        ON reviews(task_id, created_at)
    """,
)


SCHEMA_V4_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS content_items (
        content_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL DEFAULT '',
        topic TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        script TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '',
        publish_date TEXT,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'scheduled', 'published', 'archived')),
        tags_json TEXT NOT NULL DEFAULT '[]',
        source_urls_json TEXT NOT NULL DEFAULT '[]',
        hook TEXT NOT NULL DEFAULT '',
        cta TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS content_metrics (
        content_id TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        impressions INTEGER NOT NULL DEFAULT 0 CHECK (impressions >= 0),
        views INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
        likes INTEGER NOT NULL DEFAULT 0 CHECK (likes >= 0),
        comments INTEGER NOT NULL DEFAULT 0 CHECK (comments >= 0),
        shares INTEGER NOT NULL DEFAULT 0 CHECK (shares >= 0),
        favorites INTEGER NOT NULL DEFAULT 0 CHECK (favorites >= 0),
        clicks INTEGER NOT NULL DEFAULT 0 CHECK (clicks >= 0),
        leads INTEGER NOT NULL DEFAULT 0 CHECK (leads >= 0),
        valid_leads INTEGER NOT NULL DEFAULT 0 CHECK (valid_leads >= 0),
        ctr REAL CHECK (ctr IS NULL OR ctr >= 0),
        conversion_rate REAL CHECK (conversion_rate IS NULL OR conversion_rate >= 0),
        lead_rate REAL CHECK (lead_rate IS NULL OR lead_rate >= 0),
        collected_at TEXT NOT NULL,
        PRIMARY KEY (content_id, platform, collected_at),
        FOREIGN KEY (content_id) REFERENCES content_items(content_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_history (
        topic_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        content_angle TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
        used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
        publish_date TEXT,
        performance_summary TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revision_history (
        revision_id TEXT PRIMARY KEY,
        draft_id TEXT NOT NULL,
        version_before INTEGER NOT NULL CHECK (version_before >= 0),
        version_after INTEGER CHECK (version_after IS NULL OR version_after >= 1),
        user_instruction TEXT NOT NULL DEFAULT '',
        changed_sections_json TEXT NOT NULL DEFAULT '[]',
        change_summary TEXT NOT NULL DEFAULT '',
        accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_content_items_account_publish
        ON content_items(account_id, publish_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_content_items_status
        ON content_items(status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_content_metrics_content
        ON content_metrics(content_id, collected_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_topic_history_created
        ON topic_history(created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_revision_history_draft
        ON revision_history(draft_id, version_after)
    """,
)


SCHEMA_V5_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS business_trace_events (
        trace_id INTEGER PRIMARY KEY,
        request_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS business_trace_events_request
        ON business_trace_events(request_id, trace_id)
    """,
)


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    """Open a business database connection with foreign keys enabled.

    The busy timeout covers the one case the in-process GIL does not: a second
    writer holding the file (an extra worker, or a second process pointed at
    the same data directory).  Without it SQLite fails immediately instead of
    waiting the few milliseconds such a writer needs.
    """

    path_value = str(db_path)
    if path_value != ":memory:":
        Path(path_value).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path_value, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create or migrate the database to the current business schema.

    SQLite's ``user_version`` pragma keeps migration metadata out of the
    business table namespace.  Each schema version is applied in order, so an
    existing V1 database is upgraded without recreating existing tables.
    """

    connection.execute("PRAGMA foreign_keys = ON")
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current_version} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )

    with connection:
        if current_version < 1:
            for statement in SCHEMA_V1_STATEMENTS:
                connection.execute(statement)
            current_version = 1
        if current_version < 2:
            for statement in SCHEMA_V2_STATEMENTS:
                connection.execute(statement)
            current_version = 2
        if current_version < 3:
            for statement in SCHEMA_V3_STATEMENTS:
                connection.execute(statement)
            current_version = 3
        if current_version < 4:
            for statement in SCHEMA_V4_STATEMENTS:
                connection.execute(statement)
            current_version = 4
        if current_version < 5:
            for statement in SCHEMA_V5_STATEMENTS:
                connection.execute(statement)
            current_version = 5
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
