"""Pipeline Lambda entry point — single function, seven stages.

Step Functions injects `_stage` via States.JsonMerge. The handler pops it,
dispatches to the matching stage module, and returns the enriched event so the
next state receives a clean pipeline payload.

Failure contract: a stage failure is re-raised as ``PipelineStageError`` whose
message is written for the person who uploaded the document — never a stack
trace, an SDK error body or a bucket name (the original exception is chained and
logged). Step Functions catches it and routes to the MarkFailed state, which
writes status=FAILED and that message to DynamoDB.

State size: Step Functions caps the payload passed between states at 256 KB. The
stages hand each other the full document text and the extracted clauses, which
blows that limit for any contract longer than a few dozen pages (the run then
fails with States.DataLimitExceeded). So the bulky keys are parked in S3 between
stages and only a pointer (`_stateKey`) travels through the state machine — which
also keeps contract text out of the Step Functions execution history and logs.

Logging: one ``pipeline.stage_done`` / ``pipeline.stage_failed`` line per stage
with docId, stage, duration and counts (pages, clauses, chunks, tokens, retries,
coverage). Log lines never carry contract text, party names or other document
content — only ids, counts, durations and exception TYPES.
"""
from __future__ import annotations

import importlib
import os
import time
import uuid
from typing import Any

from aws_lambda_powertools import Tracer
from shared import openai_client
from shared.dynamodb import get_doc_meta, update_doc_fields
from shared.errors import PipelineStageError, safe_message
from shared.logger import get_logger, safe_trace
from shared.s3 import delete_object, delete_prefix, get_json, processed_key, put_json

log = get_logger("blue-iq.pipeline")
tracer = Tracer(service="blue-iq.pipeline")

_STAGES: dict[str, str] = {
    "01_parse":    "stages.parse",
    "02_classify": "stages.classify",
    "03_embed":    "stages.embed",
    "04_graph":    "stages.graph",
    "05_diff":     "stages.diff",
    "06_timeline": "stages.timeline",
    "07_persist":  "stages.persist",
}

# Stage outputs that carry document content and can be arbitrarily large.
_HEAVY_KEYS = ("parsed", "classification", "embeddings", "lineage", "diffs", "timeline")


def _hydrate(event: dict[str, Any]) -> str | None:
    """Load the parked stage outputs back into the event. Returns the S3 key."""
    state_key = event.pop("_stateKey", None)
    if state_key:
        event.update(get_json(event["processedBucket"], state_key))
    return state_key


def _park(event: dict[str, Any], state_key: str | None) -> dict[str, Any]:
    """Move the bulky keys to S3 and return the slim event for the next state."""
    heavy = {k: event.pop(k) for k in _HEAVY_KEYS if k in event}
    event.pop("_remainingMs", None)
    if not heavy:
        return event
    # One object per run (not per document) so two overlapping runs of the same
    # document can never read each other's state.
    state_key = state_key or processed_key(
        event["tenantId"], event["docId"], f"_pipeline/{uuid.uuid4().hex}.json"
    )
    put_json(event["processedBucket"], state_key, heavy)
    event["_stateKey"] = state_key
    return event


def _counts(event: dict[str, Any]) -> dict[str, Any]:
    """Numbers worth logging for a finished stage — counts only, no content."""
    out: dict[str, Any] = {}
    parsed = event.get("parsed") or {}
    if parsed:
        out["pages"] = len(parsed.get("pages") or [])
        out["chars"] = len(parsed.get("text") or "")
    cls = event.get("classification") or {}
    if cls:
        ext = cls.get("extraction") or {}
        out["clauses"] = len(cls.get("clauses") or [])
        out["coverageRatio"] = ext.get("coverageRatio")
        out["unclassified"] = ext.get("unclassifiedCount")
        out["keyDates"] = len(cls.get("keyDates") or [])
    emb = event.get("embeddings") or {}
    if emb:
        out["chunks"] = emb.get("chunkCount")
        out["indexedChunks"] = emb.get("embeddedCount")
    diffs = event.get("diffs") or {}
    if diffs:
        out["changes"] = len(diffs.get("changes") or [])
    return out


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    stage = event.pop("_stage", None) or os.environ.get("PIPELINE_STAGE", "")
    if not stage:
        raise ValueError("_stage must be present in the event or PIPELINE_STAGE env var")

    module_path = _STAGES.get(stage)
    if not module_path:
        raise ValueError(f"Unknown pipeline stage: {stage!r}. Valid: {list(_STAGES)}")

    log.append_keys(stage=stage, docId=event.get("docId", "?"))

    # Surface remaining Lambda time for timeout-aware stages, and give the OpenAI
    # client a deadline so no single request (or retry) can outlive the invocation.
    remaining_ms = None
    if context and hasattr(context, "get_remaining_time_in_millis"):
        remaining_ms = context.get_remaining_time_in_millis()
        event["_remainingMs"] = remaining_ms
    openai_client.set_deadline(remaining_ms)
    openai_client.usage_snapshot(reset=True)

    started = time.monotonic()
    state_key: str | None = event.get("_stateKey")
    mod = importlib.import_module(module_path)
    try:
        state_key = _hydrate(event)
        result = mod.run(event)
        usage = openai_client.usage_snapshot()
        log.info("pipeline.stage_done", stage=stage, docId=event.get("docId"),
                 durationMs=int((time.monotonic() - started) * 1000),
                 llmCalls=usage["calls"], promptTokens=usage["prompt_tokens"],
                 completionTokens=usage["completion_tokens"], retries=usage["retries"],
                 **_counts(event))
        if stage == "07_persist":
            # Terminal stage: its result is already small; drop the parked state.
            _drop_state(event, state_key)
            return result
        return _park(result, state_key)
    except Exception as exc:
        usage = openai_client.usage_snapshot()
        message, code = safe_message(stage, exc)
        request_id = getattr(context, "aws_request_id", None)
        # Type and code location only — never the exception's own text, which can
        # quote the document (see shared.logger.safe_trace).
        log.error("pipeline.stage_failed", stage=stage, docId=event.get("docId"),
                  error_type=type(exc).__name__, errorCode=code, trace=safe_trace(exc),
                  requestId=request_id,
                  durationMs=int((time.monotonic() - started) * 1000),
                  llmCalls=usage["calls"], retries=usage["retries"])
        _discard_if_deleted(event)
        _record_failure(event, stage, code)
        # The run stops here (Step Functions does not retry a stage's own error),
        # so the parked document text is no longer needed.
        _drop_state(event, state_key)
        if request_id and code == "internal":
            message = f"{message} (ref {str(request_id)[:8]})"
        # `from None`: the Lambda runtime prints the raised error and its chain to
        # the log, and the original message is exactly what must not be printed.
        raise PipelineStageError(message, stage=stage, code=code) from None


def _drop_state(event: dict[str, Any], state_key: str | None) -> None:
    if not state_key or not event.get("processedBucket"):
        return
    try:
        delete_object(event["processedBucket"], state_key)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.warning("pipeline.state_cleanup_failed", error_type=type(exc).__name__)


def _record_failure(event: dict[str, Any], stage: str, code: str) -> None:
    """Note WHERE and WHY a run failed on the document (the message itself is
    written by the MarkFailed state from the raised error). Best-effort."""
    doc_id = event.get("docId")
    if not doc_id or "/" in str(doc_id):
        return
    try:
        update_doc_fields(doc_id, {"errorStage": stage, "errorCode": code})
    except Exception as exc:  # noqa: BLE001 — e.g. the document was deleted
        log.warning("pipeline.failure_note_failed", error_type=type(exc).__name__)


def _discard_if_deleted(event: dict[str, Any]) -> None:
    """A user can delete a document while its run is in flight. The stages then
    fail (their status update is conditional on the META row existing), but the
    run has already written artefacts and search vectors AFTER the delete purged
    them. Remove those so a deleted contract does not stay readable/searchable."""
    doc_id, tenant_id = event.get("docId"), event.get("tenantId")
    bucket = event.get("processedBucket")
    # Before parse resolves the ids, docId is still the raw S3 key — nothing to do.
    if not (doc_id and tenant_id and bucket) or "/" in str(doc_id):
        return
    try:
        if get_doc_meta(doc_id) is not None:
            return
        delete_prefix(bucket, f"{tenant_id}/{doc_id}/")
        from shared.opensearch import delete_doc
        delete_doc(doc_id)
        log.info("pipeline.discarded_deleted_document")
    except Exception as exc:  # noqa: BLE001 — never mask the original failure
        log.warning("pipeline.discard_failed", error_type=type(exc).__name__)
