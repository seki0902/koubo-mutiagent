"""LangGraph SQLite checkpointer lifecycle helpers.

The checkpointer database is deliberately separate from the Business SQLite
database.  It stores thread/checkpoint state only; business repositories must
not use this connection for domain records.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Checkpointer


@asynccontextmanager
async def open_checkpointer(
    db_path: str | Path,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Open an initialized AsyncSqliteSaver for one service lifetime.

    ``AsyncSqliteSaver.from_conn_string`` owns the async connection and closes
    it when this context exits.  The parent directory is created here so a
    fresh deployment can start with an empty path.
    """

    path_value = str(db_path)
    if path_value != ":memory:":
        Path(path_value).parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(path_value) as saver:
        await saver.setup()
        yield saver


def thread_config(session_id: str) -> dict[str, dict[str, str]]:
    """Build the LangGraph config that binds a run to one session thread."""

    if not session_id.strip():
        raise ValueError("session_id must not be blank")
    return {"configurable": {"thread_id": session_id}}


def checkpointer_type() -> type[Checkpointer]:
    """Return the checkpointer protocol type for dependency annotations."""

    return Checkpointer
