"""activity-stream Lambda — DynamoDB Stream of ``govern-activity`` →
EventBridge ``blue-iq.govern`` / ``Govern.<action>`` + trend aggregates.

The activity log is the single source of Govern domain events: a state change
and its event can never disagree, because the event IS the log entry. For
every new entry this Lambda

1. adds its counters to the tenant's day item in govern-metrics
   (shared/govern/aggregates.py — counted once, guarded by a SEEN marker);
2. publishes it to the platform bus, where rules route it to the notifier and
   the connectors (Huron push-back).

Failures are reported per record (``batchItemFailures`` with the sequence
number of the first failed record): the stream retries from there, so
counting is protected by the marker and consumers of the events de-duplicate
by entry id.
"""
from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Tracer
from boto3.dynamodb.types import TypeDeserializer

from shared import aws
from shared.config import settings
from shared.dynamodb import _from_ddb
from shared.govern import aggregates
from shared.logger import get_logger

log = get_logger("blue-iq.govern-stream")
tracer = Tracer(service="blue-iq.govern-stream")

SOURCE = "blue-iq.govern"
_MAX_DETAIL_BYTES = 200_000
_deserialise = TypeDeserializer().deserialize


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    for record in (event or {}).get("Records") or []:
        if record.get("eventName") != "INSERT":
            continue
        sequence = (record.get("dynamodb") or {}).get("SequenceNumber") or ""
        try:
            entry = entry_of(record)
            aggregates.record(entry)
            publish(entry)
        except Exception as exc:  # noqa: BLE001 — retried from this record
            log.error("govern_stream.record_failed", sequence=sequence, error_type=type(exc).__name__)
            return {"batchItemFailures": [{"itemIdentifier": sequence}]}
    return {"batchItemFailures": []}


def entry_of(record: dict[str, Any]) -> dict[str, Any]:
    """The activity entry from a stream record's NEW_IMAGE."""
    image = (record.get("dynamodb") or {}).get("NewImage") or {}
    item = {k: _from_ddb(_deserialise(v)) for k, v in image.items()}
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


def publish(entry: dict[str, Any]) -> None:
    detail = json.dumps(entry, default=str)
    if len(detail.encode("utf-8")) > _MAX_DETAIL_BYTES:
        detail = json.dumps({**entry, "detail": {"truncated": True}}, default=str)
    resp = aws.events_client().put_events(Entries=[{
        "Source": SOURCE, "DetailType": f"Govern.{entry.get('action')}", "Detail": detail,
        "EventBusName": settings.event_bus_name,
    }])
    if resp.get("FailedEntryCount"):
        code = ((resp.get("Entries") or [{}])[0] or {}).get("ErrorCode")
        raise RuntimeError(f"PutEvents failed ({code})")
