"""structlog setup.

Two modes, picked automatically from stderr's TTY-ness:

- **TTY** (interactive): `ConsoleRenderer` — colorful, human-readable.
- **non-TTY** (pipes, Docker logs, CI): `JSONRenderer` — one JSON object per
  log event, safe to grep / ingest.

Callers get loggers via `get_logger(__name__)`. The module-level
`configure_logging` should be called exactly once, near program entry
(done automatically in the Typer callback in `zotai.cli`).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict, Processor

_LEVEL_BY_NAME: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return _LEVEL_BY_NAME.get(level.upper(), logging.INFO)


def _event_uppercase(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Uppercase the event name — a small UX nicety for console logs."""
    level = event_dict.get("level")
    if isinstance(level, str):
        event_dict["level"] = level.upper()
    return event_dict


def configure_logging(level: str | int = "INFO", json_logs: bool | None = None) -> None:
    """Configure the global structlog pipeline.

    Args:
        level: Minimum log level. Anything below is dropped.
        json_logs: Force JSON output (True) or console output (False). When
            `None` (default), auto-detect from `sys.stderr.isatty()`.
    """
    if json_logs is None:
        json_logs = not sys.stderr.isatty()

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _event_uppercase,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    processors: list[Processor]
    if json_logs:
        processors = [
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ]
    else:
        processors = [
            *shared,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)


def bind(**kwargs: Any) -> None:
    """Attach key/value pairs to the current contextvar scope.

    Values bound here are merged into every subsequent log event on the
    current task/thread until `clear()` is called.
    """
    bind_contextvars(**kwargs)


def clear() -> None:
    """Drop all contextvar bindings for the current task/thread."""
    clear_contextvars()


__all__ = ["bind", "clear", "configure_logging", "get_logger"]
