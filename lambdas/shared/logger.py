"""Structured logging.

Each handler should call `get_logger(__name__)` once at module level and attach
correlation ids via `logger.append_keys(docId=..., tenantId=...)` at the start
of the handler.

We use `aws_lambda_powertools.Logger` (in shared/requirements.txt) as the
primary logger because it integrates with the Powertools tracer, JSON-formats
to CloudWatch automatically, and supports correlation-id propagation through
Step Functions.

`structlog` is also available for non-Powertools contexts (e.g. local CLI
scripts under `scripts/`).
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger as PowertoolsLogger

from .config import settings


_loggers: dict[str, PowertoolsLogger] = {}


def get_logger(name: str = "blue-iq", **default_keys: Any) -> PowertoolsLogger:
    """Return a memoised Powertools Logger with project defaults."""
    if name in _loggers:
        return _loggers[name]

    log = PowertoolsLogger(
        service=name,
        level=settings.log_level,
        log_uncaught_exceptions=True,
    )
    if default_keys:
        log.append_keys(**default_keys)
    log.append_keys(project=settings.project_name, stage=settings.stage)
    _loggers[name] = log
    return log


def get_structlog():
    """Lazy import of structlog for use outside Lambda runtime."""
    import structlog

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    return structlog.get_logger()
