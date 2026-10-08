"""govern-sweeper (lambdas/govern_sweeper/handler.py): one overdue entry per
stage visit, obligation reminders, and the capture reconciliation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from govern_intake import handler as intake
from govern_support import analysed_event, install_analysis, seed_doc
from govern_sweeper import handler as sweeper
from shared.config import settings
from shared.govern import capture, store, workflow
from shared.govern.store import iso

TENANT = "u-aaaaaaaa-0000-4000-8000-0000000000a1"
DANA = {"email": "dana@northfield.edu", "name": "Dana Ruiz"}


def _age_stage(contract_id: str, days: int, now: datetime) -> None:
    store.contracts.mutate(contract_id, lambda c: c.update(stageEnteredAt=iso(now - timedelta(days=days))))


def test_overdue_is_written_once_per_stage_visit(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))
    workflow.perform_action("lic-1", "assign", {"owner": DANA}, DANA)
    now = datetime.now(timezone.utc)
    _age_stage("lic-1", 3, now)
    assert sweeper.sweep(now)["overdue"] == 0                         # within the 5-day target
    _age_stage("lic-1", 7, now)
    assert sweeper.sweep(now)["overdue"] == 1
    last = gov.activity_for("lic-1")[-1]
    assert last["action"] == "overdue" and last["detail"]["slaStatus"] == "amber"
    assert last["summary"] == ("This has been in review for 7 days, past its 5-day target. "
                               "Waiting on reviewer (Dana Ruiz).")
    assert sweeper.sweep(now + timedelta(days=10))["overdue"] == 0   # now red, but already raised
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, DANA)    # new stage visit
    assert sweeper.sweep(now + timedelta(days=11))["overdue"] == 1
    assert gov.actions("lic-1").count("overdue") == 2


def test_obligation_reminders_are_written_once(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    for oid, due in (("soon", "2026-10-15"), ("late", "2026-10-01"), ("later", "2026-12-01")):
        store.contracts.put_obligation("lic-1", TENANT, {"id": oid, "kind": "sponsor_report", "title": f"Report {oid}",
                                                         "dueDate": due, "status": "open", "source": "manual"})
    assert sweeper.sweep(now)["obligations"] == 2
    texts = [a["summary"] for a in gov.activity_for("lic-1") if a["action"] == "overdue"]
    assert sorted(texts) == ["Report late was due on 2026-10-01 and is overdue.", "Report soon is due on 2026-10-15."]
    assert sweeper.sweep(now)["obligations"] == 0
    assert sweeper.sweep(now + timedelta(days=10))["obligations"] == 1      # "soon" is now overdue


def test_reconciliation_requeues_missed_documents_and_records_failures(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    monkeypatch.setattr(settings, "intake_queue_url", "https://sqs/intake")
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))
    seed_doc(ddb, "missed-1")
    seed_doc(ddb, "broken", status="FAILED", errorMessage="Parse: unreadable")
    seed_doc(ddb, "stalled", status="CLASSIFYING", updatedAt="2026-10-01T00:00:00Z")
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    result = sweeper.sweep(now)
    assert (result["requeued"], result["missed"]) == (1, 3)
    message = json.loads(gov.sqs_messages[0]["MessageBody"])
    assert message["detail"] == {"docId": "missed-1", "tenantId": TENANT, "status": "READY", "reconciled": True}
    state = capture.get_reconcile_state(TENANT)
    assert state["lastReconciledAt"] == iso(now) and state["requeued"] == ["missed-1"]
    assert {m["docId"]: m["status"] for m in state["missed"]} == {"missed-1": "READY", "broken": "FAILED",
                                                                  "stalled": "CLASSIFYING"}
    assert intake.handle_event(message) == "created"                    # the requeued event completes capture
    assert sweeper.sweep(now)["requeued"] == 0


def test_a_failing_tenant_does_not_stop_the_others(gov, ddb, monkeypatch):
    store.config.register_tenant("t-bad")
    store.config.register_tenant("t-good")
    real = sweeper.sweep_tenant
    monkeypatch.setattr(sweeper, "sweep_tenant",
                        lambda tid, now: (_ for _ in ()).throw(RuntimeError("x")) if tid == "t-bad" else real(tid, now))
    import pytest
    with pytest.raises(RuntimeError, match="1 tenant"):
        sweeper.sweep(datetime.now(timezone.utc))
    assert capture.get_reconcile_state("t-good")["lastReconciledAt"]
