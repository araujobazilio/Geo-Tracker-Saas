"""Structured logging configuration.

Uses structlog for context-rich, JSON-serializable log events.
In production, logs are emitted as JSON. In development, a human-readable
console renderer is used.

Logs must NEVER contain secrets, credentials, or authentication tokens.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config import get_settings


def configure_logging() -> None:
    """Configure structlog + stdlib logging once at application startup."""
    settings = get_settings()
    level = getattr(logging, settings.log_level, logging.INFO)

    # Reset root logger to a known state.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_json or settings.is_production:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=not settings.is_test)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
