"""connectors Lambda — the connector framework's runner
(shared/govern/connectors.py).

Two triggers:

* EventBridge Scheduler, daily (``{"trigger": "schedule"}``): for every
  tenant, the scheduled run of each connector — Huron pull + push-back,
  Workday award / spend pull (a dry run while not connected);
* SQS ← EventBridge ``Govern.*`` (minus ``sync``, ``conflict`` and
  ``notification_sent``, which connectors and the notifier write themselves,
  so no event loops): Huron push-back of the contract's status, findings,
  blockers and next step, when the contract carries a Huron record id.

Every run writes a SyncRun (``GET /integrations/sync-log``). SQS failures are
reported per record and retried, then DLQ'd.

Integrations are a "Later" feature: unless GOVERN_FEATURES includes
``integrations``, both triggers log "feature disabled" and do nothing.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.govern import connectors, sqs_batch, store
from shared.logger import get_logger

log = get_logger("blue-iq.govern-connectors")
tracer = Tracer(service="blue-iq.govern-connectors")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if isinstance(event, dict) and "Records" in event:
        return sqs_batch.process(event, handle_govern_event, name="govern_connectors")
    return run_schedule()


def run_schedule() -> dict[str, Any]:
    if not settings.feature_enabled("integrations"):
        log.info("govern_connectors.feature_disabled", feature="integrations", trigger="schedule")
        return {"runs": 0, "status": "disabled"}
    runs = failed = 0
    for tenant_id in store.config.tenants():
        try:
            runs += len(connectors.run_scheduled(tenant_id))
        except Exception as exc:  # noqa: BLE001 — one tenant must not stop the others
            failed += 1
            log.exception("govern_connectors.tenant_failed", tenantId=tenant_id, error_type=type(exc).__name__)
    log.info("govern_connectors.scheduled", runs=runs, failedTenants=failed)
    if failed:
        raise RuntimeError(f"{failed} tenant(s) failed")
    return {"runs": runs}


def handle_govern_event(evt: dict[str, Any]) -> str:
    """Huron push-back for one Govern event. Returns what was done."""
    if not settings.feature_enabled("integrations"):
        log.info("govern_connectors.feature_disabled", feature="integrations", trigger="event")
        return "disabled"
    entry = evt.get("detail") or {}
    if entry.get("action") in ("sync", "conflict", "notification_sent"):
        return "ignored"
    contract = store.contracts.get(str(entry.get("contractId") or ""))
    if contract is None or not contract.get("huronRecordId"):
        return "no_huron_record"
    run = connectors.run_connector(contract["tenantId"], "huron", "event", contract_ids=[contract["contractId"]])
    return run["status"]
