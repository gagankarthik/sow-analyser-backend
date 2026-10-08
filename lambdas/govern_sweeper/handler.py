"""govern-sweeper Lambda — EventBridge Scheduler, hourly.

For every tenant that uses Govern (govern-config ``TENANTS``):

1. SLA — an open contract whose days in stage passed its target (amber) or
   target × redAfterMultiple (red) gets ONE ``overdue`` entry per stage visit
   (``overdueNotifiedFor`` = the stageEnteredAt it was raised for);
2. obligations — open obligations due within 14 days get one "due" entry,
   overdue ones one "overdue" entry (flags on the obligation);
3. RECONCILIATION — capture must never miss a contract: every READY document
   of the tenant that no contract accounts for (and that is not a revision
   already linked) is re-enqueued to the intake queue; FAILED and stalled
   analyses are recorded for ``GET /reports/capture`` with
   ``lastReconciledAt``.

Every write goes through workflow.py, so a sweep that overlaps a reviewer's
click is safe; re-running a sweep writes nothing new. A failing tenant does
not stop the others; the run then fails so the error is visible.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from aws_lambda_powertools import Tracer

from shared import aws
from shared.config import settings
from shared.dynamodb import list_tenant_docs
from shared.govern import capture, store, workflow
from shared.logger import get_logger

log = get_logger("blue-iq.govern-sweeper")
tracer = Tracer(service="blue-iq.govern-sweeper")

_OBLIGATION_WINDOW_DAYS = 14
_MAX_REQUEUE_PER_RUN = 200
_MAX_TENANT_DOCS = 50_000


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return sweep(datetime.now(timezone.utc))


def sweep(now: datetime) -> dict[str, Any]:
    totals = {"tenants": 0, "overdue": 0, "obligations": 0, "requeued": 0, "missed": 0, "failedTenants": 0}
    for tenant_id in store.config.tenants():
        totals["tenants"] += 1
        try:
            result = sweep_tenant(tenant_id, now)
        except Exception as exc:  # noqa: BLE001 — keep sweeping the other tenants
            log.exception("govern_sweeper.tenant_failed", tenantId=tenant_id, error_type=type(exc).__name__)
            totals["failedTenants"] += 1
            continue
        for k in ("overdue", "obligations", "requeued", "missed"):
            totals[k] += result[k]
    log.info("govern_sweeper.done", **totals)
    if totals["failedTenants"]:
        raise RuntimeError(f"{totals['failedTenants']} tenant(s) failed to sweep")
    return totals


def sweep_tenant(tenant_id: str, now: datetime) -> dict[str, int]:
    cfg = workflow.get_settings(tenant_id)
    contracts = store.contracts.for_tenant(tenant_id)
    overdue = 0
    for c in contracts:
        if c.get("state") not in workflow.OPEN_STATES or c.get("overdueNotifiedFor") == c.get("stageEnteredAt"):
            continue
        if workflow.sla(c, cfg, now)["slaStatus"] in ("amber", "red"):
            overdue += int(workflow.mark_overdue(c["contractId"], now=now, cfg=cfg))
    obligations = _sweep_obligations(tenant_id, now)
    requeued, missed = reconcile(tenant_id, contracts, now)
    return {"overdue": overdue, "obligations": obligations, "requeued": requeued, "missed": missed}


def _sweep_obligations(tenant_id: str, now: datetime) -> int:
    if not settings.feature_enabled("obligations"):
        return 0                    # a "Later" feature: no due / overdue entries while it is off
    today = now.date().isoformat()
    due = store.contracts.obligations_due(tenant_id, (now + timedelta(days=_OBLIGATION_WINDOW_DAYS)).date().isoformat())
    if not due:
        return 0
    contracts = store.contracts.get_many({o["contractId"] for o in due if o.get("contractId")})
    written = 0
    for o in due:
        c = contracts.get(o.get("contractId") or "")
        if c is None or o.get("status", "open") != "open" or not o.get("dueDate"):
            continue
        kind = "overdue" if str(o["dueDate"])[:10] < today else "due"
        flag = "notifiedOverdue" if kind == "overdue" else "notifiedDue"
        if o.get(flag):
            continue
        workflow.note_obligation(c, o, kind, now)
        store.contracts.put_obligation(c["contractId"], tenant_id, {**o, flag: store.iso(now)})
        written += 1
    return written


def reconcile(tenant_id: str, contracts: list[dict[str, Any]], now: datetime) -> tuple[int, int]:
    """Re-enqueue READY documents without a contract; record what is missed."""
    docs = list_tenant_docs(tenant_id, limit=_MAX_TENANT_DOCS)
    candidates = capture.requeue_candidates(docs, contracts)
    requeued: list[str] = []
    if candidates and settings.intake_queue_url:
        for meta in candidates[:_MAX_REQUEUE_PER_RUN]:
            aws.sqs_client().send_message(QueueUrl=settings.intake_queue_url, MessageBody=json.dumps({
                "source": "blue-iq.govern-sweeper", "detail-type": "Document Analysed",
                "detail": {"docId": meta["docId"], "tenantId": tenant_id, "status": "READY", "reconciled": True},
            }))
            requeued.append(meta["docId"])
    elif candidates:
        log.warning("govern_sweeper.no_intake_queue", candidates=len(candidates))
    missed = capture.missed_documents(docs, contracts, now)
    capture.put_reconcile_state(tenant_id, requeued=requeued, missed=missed, now=now)
    if requeued or missed:
        log.info("govern_sweeper.reconciled", tenantId=tenant_id, requeued=len(requeued), missed=len(missed))
    return len(requeued), len(missed)
