"""SQS batch processing with partial-failure reporting.

Every SQS-triggered Govern Lambda (intake, notifier, connectors) handles its
records one by one and reports only the failed ones back
(``ReportBatchItemFailures``): SQS retries just those, and after the queue's
``maxReceiveCount`` they land in its DLQ (alarmed). Handlers must therefore
be idempotent — a record may be delivered more than once.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ..logger import get_logger

log = get_logger("blue-iq.govern.sqs")


def event_of(record: dict[str, Any]) -> dict[str, Any]:
    """The EventBridge event carried in an SQS record body."""
    body = json.loads(record.get("body") or "{}")
    if not isinstance(body, dict):
        raise ValueError("SQS body is not a JSON object")
    return body


def process(event: dict[str, Any], handle: Callable[[dict[str, Any]], None], *, name: str) -> dict[str, Any]:
    """Run ``handle(eventbridge_event)`` per record; collect the failures."""
    failures: list[dict[str, str]] = []
    for record in (event or {}).get("Records") or []:
        message_id = record.get("messageId") or ""
        try:
            handle(event_of(record))
        except Exception as exc:  # noqa: BLE001 — reported, retried, then DLQ'd
            log.error(f"{name}.record_failed", messageId=message_id, error_type=type(exc).__name__)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
