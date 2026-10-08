"""govern-intake Lambda — SQS ← EventBridge ``Document Analysed``.

For each analysed (READY) document:

* a REVISION (META ``revisionOf`` = a contract): the document becomes the
  contract's current version, is graded against the current matrix, and the
  contract goes back to ``in_review`` (activity ``revision_received`` +
  ``rescored``; ``rounds`` unchanged);
* any other document: its contract is created (or, if POST /contracts made it
  at upload, completed): agreement type and direction inferred, value,
  counterparty, PI, department, Huron / Workday references read from the
  document (never over a field a person entered), matrix review, Sonar
  blockers, licensing income, auto-assignment and routing (activity
  ``intake`` … ``rescored``).

IDEMPOTENT. SQS delivers at least once and the hourly reconciliation may
re-enqueue a document: a contract already reviewed on this document version
is left alone, creation is conditional, and every write goes through the
optimistic-locked workflow. Failures are reported per record
(``batchItemFailures``); after the queue's retries they reach the DLQ.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.dynamodb import get_doc_meta
from shared.govern import sqs_batch, store, workflow
from shared.logger import get_logger

log = get_logger("blue-iq.govern-intake")
tracer = Tracer(service="blue-iq.govern-intake")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return sqs_batch.process(event, handle_event, name="govern_intake")


def handle_event(evt: dict[str, Any]) -> str:
    """Process one ``Document Analysed`` event. Returns what was done (for
    logs and tests): skipped_* | created | completed | reanalysed | revision | unchanged."""
    detail = evt.get("detail") or {}
    doc_id = detail.get("docId")
    if not doc_id:
        return "skipped_no_doc"
    meta = get_doc_meta(doc_id)
    if not meta:
        return "skipped_deleted"
    meta = store.clean_item(meta) or {}
    if str(meta.get("status") or "").upper() != "READY":
        return "skipped_not_ready"
    log.append_keys(docId=doc_id)
    version = int(meta.get("latestVersion") or 0)

    parent_id = meta.get("revisionOf")
    if parent_id:
        parent = store.contracts.get(parent_id)
        if parent is not None:
            if parent.get("reviewedDocId") == doc_id and int(parent.get("reviewedDocVersion") or -1) == version:
                return "unchanged"
            workflow.rescore(parent_id, None, doc_id=doc_id, reason="revision", doc_meta=meta)
            log.info("govern_intake.revision_linked", contractId=parent_id)
            return "revision"
        log.warning("govern_intake.revision_parent_missing", contractId=parent_id)

    existing = store.contracts.get(doc_id)
    if existing is not None and existing.get("reviewedDocId") == doc_id \
            and int(existing.get("reviewedDocVersion") or -1) == version:
        return "unchanged"
    classification = workflow.load_classification(doc_id)
    if classification is None:
        # READY but the artefact is not readable yet: retry (then DLQ).
        raise workflow.NotReady("classification not available")
    header = workflow.load_header_text(doc_id)
    if existing is None:
        _, created = workflow.create_from_document(meta, None, source="intake", classification=classification,
                                                   header_text=header)
        store.config.register_tenant(meta["tenantId"])
        reason, outcome = "intake", "created" if created else "completed"
    elif existing.get("matrix") is None:
        reason, outcome = "intake", "completed"
    else:
        reason, outcome = "reanalysed", "reanalysed"
    workflow.rescore(doc_id, None, doc_id=doc_id, reason=reason, doc_meta=meta, classification=classification,
                     header_text=header, assign=True)
    log.info("govern_intake.done", outcome=outcome)
    return outcome

