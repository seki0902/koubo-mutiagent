"""Small structured logging setup for the Agent Service."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


LOGGER_NAME = "agent_service"


class JsonFormatter(logging.Formatter):
    """Emit stable JSON fields without serialising arbitrary record extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field in ("request_id", "path", "status", "latency_ms"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the service logger and return it.

    Only explicitly selected request fields are emitted.  In particular, no
    settings object or request body is attached to these records, preventing
    API keys from entering logs through this layer.
    """

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger
