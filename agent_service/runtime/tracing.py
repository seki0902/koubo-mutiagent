"""Remote tracing, enabled only when there is somewhere to send it.

LangChain already knows how to trace models, subagents, tools, and how long
each took; what it needs is for tracing to be switched on before the first
run.  That is all this module does.

It is deliberately conditional.  A local checkout has no LangSmith key and
must not have its turns shipped anywhere, so without one this is a no-op
rather than a default-on integration.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["configure_tracing"]


def configure_tracing(settings: Any) -> bool:
    """Turn on LangSmith tracing when the settings carry a key.

    Returns whether tracing was enabled, which gives the caller something to
    log and gives tests something to assert without reading the environment.
    """

    key = getattr(settings, "langsmith_api_key", None)
    if key is None:
        return False

    value = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    if not value.strip():
        return False

    project = str(getattr(settings, "langsmith_project", "") or "").strip()
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = value
    if project:
        os.environ["LANGSMITH_PROJECT"] = project
    return True
