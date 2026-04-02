"""Structured JSON logging for Recall v2, built on structlog."""
from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def bind_request_id(correlation_id: str) -> None:
    """Bind a correlation ID to the current async context."""
    _correlation_id.set(correlation_id)


def _add_correlation_id(logger: Any, method: str, event_dict: dict) -> dict:
    """structlog processor that injects the current correlation ID."""
    cid = _correlation_id.get("")
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


_LOG_LEVEL = os.getenv("RECALL_LOG_LEVEL", "INFO").upper()
_JSON_LOGS = os.getenv("RECALL_LOG_FORMAT", "json").lower() == "json"


def _configure() -> None:
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if _JSON_LOGS:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *shared_processors,
            renderer,
        ]
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))


_configure()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger for the given module name."""
    return structlog.get_logger(name)
