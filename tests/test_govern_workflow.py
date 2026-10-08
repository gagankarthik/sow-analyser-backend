"""Govern workflow (shared/govern/workflow.py): every action and its invalid
transitions, routing, the recommended next step, SLA colours, value buckets,
fiscal year, capture gaps, allowed actions and optimistic locking.

Runs on the in-memory tables (tests/fakes.py) with the OSU sample agreements
as the analysed documents.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fakes import conditional_failure
from govern_intake import handler as intake
from govern_support import DANA, analysed_event, install_analysis, seed_doc
from shared.govern import store, workflow
from shared.govern.store import ContractConflict, iso

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
ELI = {"email": "eli@osu.edu", "name": "Eli Park"}


@pytest.fixture
def lic(gov, ddb, monkeypatch):
    """A licence agreement through intake: in review with 3 Sonar blockers
    (1 unacceptable → Legal Affairs routing)."""
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    return gov


def act(action: str, actor=DANA, **body):
    return workflow.perform_action("lic-1", action, body, actor)


def contract(contract_id: str = "lic-1") -> dict:
    return workflow.to_api(store.contracts.get(contract_id), cfg=workflow.default_settings())


def item(**fields) -> dict:
    """A stored-contract-shaped dict for pure (read-time) functions."""
    base = {"contractId": "c1", "tenantId": "t1", "state": "in_review", "stage": "review",
            "stageEnteredAt": iso(NOW - timedelta(days=1)), "createdAt": iso(NOW - timedelta(days=3)),
            "owner": DANA, "agreementType": "license", "direction": "incoming", "openBlockerRefs": [],
            "routing": {"required": [], "approvals": []}, "reviewIndex": {}, "manualValue": 100000}
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def test_assign_moves_intake_to_review_and_names_the_reviewer(lic):
    c = act("assign", owner=DANA)
    assert c["state"] == "in_review" and c["owner"] == DANA
    assert contract()["waitingOn"] == {"kind": "osu_reviewer", "label": "Waiting on OSU reviewer (Dana Ruiz)",
                                       "office": None, "person": DANA}
    act("assign", owner=ELI)
    entries = lic.activity_for("lic-1")
    assert [e["action"] for e in entries][-2:] == ["assigned", "reassigned"]
    assert entries[-1]["summary"] == "Dana Ruiz reassigned this from Dana Ruiz to Eli Park."


def test_approve_routes_to_the_pending_office_then_ready_to_sign(lic):
    act("assign", owner=DANA)
    c = act("approve")
    assert c["state"] == "escalated" and c["stage"] == "review"           # unacceptable term → Legal Affairs
    assert contract()["waitingOn"]["label"] == "Waiting on Legal Affairs"
    c = act("office_approve", office="legal_affairs")
    assert c["state"] == "ready_to_sign" and c["stage"] == "approval"
    view = contract()
    assert view["waitingOn"]["kind"] == "signatory"
    assert {a["office"] for a in view["routing"]["approvals"]} == {"reviewer", "legal_affairs"}
    assert lic.activity_for("lic-1")[-1]["summary"].startswith("Dana Ruiz approved this for Legal Affairs.")


def test_send_back_counts_a_round_and_waits_on_the_licensee(lic):
    act("assign", owner=DANA)
    clauses = [{"clauseType": "Royalties", "label": "Royalties", "suggestedLanguage": "3%"},
               {"clauseType": "LicenseScope", "label": "Licence scope", "suggestedLanguage": None},
               {"clauseType": "GoverningLaw", "label": "Governing law", "suggestedLanguage": None}]
    c = act("send_back", clauses=clauses, note="See redlines")
    assert (c["state"], c["stage"], c["rounds"]) == ("sent_back", "negotiation", 1)
    view = contract()
    assert view["waitingOn"] == {"kind": "counterparty", "label": "Waiting on licensee (Buckeye BioSensors, Inc.)",
                                 "office": None, "person": None}
    last = lic.activity_for("lic-1")[-1]
    assert last["summary"] == "Dana Ruiz sent this back to the licensee with 3 clauses to change."
    assert (last["fromStage"], last["toStage"]) == ("review", "negotiation")
    assert last["detail"]["stageExit"]["stage"] == "review"


def test_escalate_adds_the_office_to_routing(lic):
    act("assign", owner=DANA)
    c = act("escalate", office="export_control")
    assert c["state"] == "escalated"
    assert "export_control" in c["routing"]["required"]
    assert any("escalated" in r for r in contract()["routing"]["reasons"])


def editor_actions() -> list[str]:
    return workflow.allowed_actions(store.contracts.get("lic-1"), workflow.Viewer(can_edit=True))


def test_ask_pi_waits_on_the_pi_until_answered(lic):
    act("assign", owner=DANA)
    assert "ask_pi" in editor_actions() and "pi_answered" not in editor_actions()
    c = act("ask_pi", request="Confirm the field of use with the lab.")
    assert c["state"] == "in_review" and c["piRequest"]["request"] == "Confirm the field of use with the lab."
    view = contract()
    assert view["waitingOn"]["kind"] == "pi_department"
    assert view["waitingOn"]["label"].startswith("Waiting on PI or department")
    assert view["nextStep"]["action"] == "wait" and "Confirm the field of use" in view["nextStep"]["detail"]
    assert "pi_answered" in editor_actions() and "ask_pi" not in editor_actions()
    assert lic.activity_for("lic-1")[-1]["action"] == "pi_requested"
    with pytest.raises(workflow.InvalidTransition):
        act("ask_pi", request="Again")
    c = act("pi_answered")
    assert "piRequest" not in c
    assert contract()["waitingOn"]["kind"] == "osu_reviewer"
    assert lic.activity_for("lic-1")[-1]["action"] == "pi_answered"


def test_ask_pi_needs_a_request_and_a_move_clears_it(lic):
    act("assign", owner=DANA)
    with pytest.raises(workflow.BadRequest):
        act("ask_pi", request="  ")
    with pytest.raises(workflow.InvalidTransition):
        act("pi_answered")
    act("ask_pi", request="Budget sign-off from the department")
    c = act("escalate", office="export_control")
    assert "piRequest" not in c and contract()["waitingOn"]["kind"] == "osu_office"


def test_reject_keeps_the_stage_and_nothing_more_can_happen(lic):
    act("assign", owner=DANA)
    c = act("reject", reasonCode="unacceptable_terms", note="No Ohio law")
    assert c["state"] == "rejected" and c["stage"] == "review"
    view = contract()
    assert view["nextStep"]["action"] == "none" and view["waitingOn"]["kind"] == "nobody"
    assert view["valueBucket"] == "none"
    for action, body in (("approve", {}), ("send_back", {"clauses": []}), ("send_for_signature", {"provider": "manual"})):
        with pytest.raises(workflow.InvalidTransition):
            workflow.perform_action("lic-1", action, body, DANA)
    c = act("reopen")
    assert c["state"] == "in_review" and c["rejection"] is None


def test_signature_lifecycle_to_close_and_reopen(lic):
    act("assign", owner=DANA)
    act("approve")
    act("office_approve", office="legal_affairs")
    with pytest.raises(workflow.InvalidTransition):
        act("activate")
    c = act("send_for_signature", provider="docusign", signatory={"email": "sig@osu.edu", "name": "Signer"})
    assert c["state"] == "out_for_signature" and c["signature"]["provider"] == "docusign"
    with pytest.raises(workflow.InvalidTransition):
        act("send_for_signature", provider="manual")       # only from ready_to_sign
    workflow.update_fields("lic-1", {"manualValue": 250000}, DANA)
    c = act("mark_signed", signedAt="2026-10-09")
    assert c["state"] == "signed" and c["stage"] == "signed" and c["signedAt"] == "2026-10-09"
    view = contract()
    assert view["valueBucket"] == "current" and view["value"] == 250000
    assert lic.activity_for("lic-1")[-1]["summary"] == "Dana Ruiz marked this signed. $250,000 moves to current value."
    assert store.contracts.obligations("lic-1"), "obligations are extracted at signature"
    assert act("activate")["state"] == "active"
    c = act("close")
    assert (c["state"], c["stage"]) == ("closed", "expired")
    assert act("reopen")["state"] == "in_review"


def test_comment_is_allowed_in_every_state_and_changes_nothing(lic):
    act("reject", reasonCode="duplicate")
    before = store.contracts.get("lic-1")
    act("comment", text="Duplicate of AGR-1")
    assert store.contracts.get("lic-1")["rev"] == before["rev"]
    assert lic.activity_for("lic-1")[-1]["summary"] == "Dana Ruiz commented: “Duplicate of AGR-1”"


@pytest.mark.parametrize("action,body,problem", [
    ("fly", {}, "Unknown action"),
    ("assign", {"owner": {"email": "nope"}}, "owner.email"),
    ("escalate", {"office": "the_dean"}, "office must be"),
    ("reject", {"reasonCode": "bored"}, "reasonCode"),
    ("send_for_signature", {"provider": "fax"}, "provider"),
    ("mark_signed", {"signedAt": "yesterday"}, "signedAt"),
    ("comment", {"text": "  "}, "text is required"),
])
def test_bad_action_bodies_are_400(lic, action, body, problem):
    with pytest.raises(workflow.BadRequest, match=problem):
        workflow.perform_action("lic-1", action, body, DANA)


# ---------------------------------------------------------------------------
# Optimistic locking
# ---------------------------------------------------------------------------


def test_a_lost_race_is_retried_and_revalidated(lic, monkeypatch):
    """Another reviewer rejects the contract between our read and our write:
    our write fails its rev check, is retried on the fresh state, and the
    approve is then refused as an invalid transition — never written over."""
    act("assign", owner=DANA)
    table = lic.contracts
    real_put = table.put_item
    raced = {"done": False}

    def racing_put(Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):
        if Item.get("SK") == "META" and ConditionExpression == "rev = :rev" and not raced["done"]:
            raced["done"] = True
            stored = table.items[("CON#lic-1", "META")]
            stored.update(state="rejected", rev=stored["rev"] + 1)      # the other reviewer's write
        return real_put(Item=Item, ConditionExpression=ConditionExpression,
                        ExpressionAttributeValues=ExpressionAttributeValues, **kw)

    monkeypatch.setattr(table, "put_item", racing_put)
    with pytest.raises(workflow.InvalidTransition):
        act("approve")
    assert store.contracts.get("lic-1")["state"] == "rejected"


def test_repeated_conflicts_end_in_contract_conflict(lic, monkeypatch):
    table = lic.contracts
    real_put = table.put_item

    def always_conflict(Item, ConditionExpression=None, **kw):
        if ConditionExpression == "rev = :rev":
            raise conditional_failure()
        return real_put(Item=Item, ConditionExpression=ConditionExpression, **kw)

    monkeypatch.setattr(table, "put_item", always_conflict)
    with pytest.raises(ContractConflict):
        act("assign", owner=DANA)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _rules(*rules):
    return {"routingRules": [{"id": str(i), "name": f"r{i}", "enabled": True, **r} for i, r in enumerate(rules)]}


def test_routing_triggers_are_or_and_narrowing_is_and():
    cfg = _rules({"when": {"agreementTypes": ["license"], "direction": "incoming", "minValue": 500000,
                           "anyUnacceptable": True}, "route": ["legal_affairs"]})
    unacceptable = {"matrix": {"counts": {"unacceptable": 1}}}
    assert workflow.evaluate_routing(item(manualValue=10, **unacceptable), cfg)[0] == ["legal_affairs"]
    assert workflow.evaluate_routing(item(manualValue=600000), cfg)[0] == ["legal_affairs"]
    assert workflow.evaluate_routing(item(manualValue=10), cfg)[0] == []                    # no trigger holds
    assert workflow.evaluate_routing(item(manualValue=600000, agreementType="nda"), cfg)[0] == []  # narrowed out
    assert workflow.evaluate_routing(item(manualValue=600000, direction="outgoing"), cfg)[0] == []


def test_a_rule_without_triggers_fires_for_every_narrowed_contract():
    cfg = _rules({"when": {"agreementTypes": ["mta"]}, "route": ["export_control"]},
                 {"when": {"minRisk": "high"}, "route": ["risk_management"]}, {"enabled": False, "when": {}, "route": ["legal_affairs"]})
    offices, reasons = workflow.evaluate_routing(item(agreementType="mta", overallRisk="critical"), cfg)
    assert offices == ["export_control", "risk_management"]
    assert reasons[1] == "Risk Management must approve because its risk is critical."
    assert workflow.evaluate_routing(item(agreementType="license", overallRisk="medium"), cfg)[0] == []


def test_default_routing_sends_large_contracts_to_legal():
    cfg = workflow.default_settings()
    offices, reasons = workflow.evaluate_routing(item(manualValue=750000, currency="USD"), cfg)
    assert offices == ["legal_affairs"]
    assert "its value is $750,000, at or above $500,000" in reasons[0]


def test_auto_assignment_prefers_the_most_specific_rule():
    cfg = {"assignmentRules": [
        {"agreementType": "*", "department": "*", "reviewer": {"email": "any@osu.edu", "name": "Any"}},
        {"agreementType": "license", "department": "*", "reviewer": {"email": "lic@osu.edu", "name": "Lic"}},
        {"agreementType": "license", "department": "Department of Physics", "reviewer": {"email": "phys@osu.edu", "name": "P"}},
    ], "reviewers": []}
    assert workflow.auto_assignee(item(department="department of physics"), cfg)[0]["email"] == "phys@osu.edu"
    assert workflow.auto_assignee(item(department="Chemistry"), cfg)[0]["email"] == "lic@osu.edu"
    assert workflow.auto_assignee(item(agreementType="nda"), cfg)[0]["email"] == "any@osu.edu"
    by_type = {"assignmentRules": [], "reviewers": [{"email": "mta@osu.edu", "name": "M", "agreementTypes": ["mta"]}]}
    assert workflow.auto_assignee(item(agreementType="mta"), by_type)[0]["email"] == "mta@osu.edu"
    assert workflow.auto_assignee(item(agreementType="nda"), by_type) == (None, None)


# ---------------------------------------------------------------------------
# Recommended next step (first rule that matches)
# ---------------------------------------------------------------------------


def _step(**fields):
    return workflow.next_step(item(**fields), fields.pop("_analysis", "READY") if "_analysis" in fields else "READY", NOW)


def _unacceptable_law(**extra):
    return {"reviewIndex": {"GoverningLaw": {"label": "Governing law", "tier": "unacceptable", "hasFallback": False,
                                             "office": "legal_affairs", "suggestedLanguage": "Ohio law"},
                            "Royalties": {"label": "Royalties", "tier": "deviates", "hasFallback": True}},
            **extra}


def test_next_step_rules_in_order():
    assert _step(state="rejected", rejection={"reasonCode": "duplicate"})["action"] == "none"
    assert _step(state="closed")["action"] == "none"
    signed = _step(state="signed", obligationDueDates=["2026-11-01", "2027-01-01"])
    assert signed["action"] == "none" and signed["headline"] == "Signed. Track 2 open obligations; the next is due 2026-11-01."
    assert workflow.next_step(item(), "CLASSIFYING", NOW)["headline"] == "Sonar is still reading this agreement."
    assert _step(owner=None)["action"] == "assign"

    law = {"id": "b1", "clauseType": "GoverningLaw", "office": "legal_affairs"}
    step = _step(**_unacceptable_law(openBlockerRefs=[law]))
    assert (step["action"], step["office"]) == ("escalate", "legal_affairs")
    assert step["headline"] == "Escalate to Legal Affairs: 1 clause is unacceptable."
    assert step["clauses"] == [{"clauseType": "GoverningLaw", "label": "Governing law", "tier": "unacceptable",
                                "suggestedLanguage": "Ohio law"}]
    approved = {"required": ["legal_affairs"], "approvals": [{"office": "legal_affairs"}]}
    assert _step(**_unacceptable_law(openBlockerRefs=[law], routing=approved))["action"] == "reject"
    waiting = _step(**_unacceptable_law(openBlockerRefs=[law], state="escalated",
                                        routing={"required": ["legal_affairs"], "approvals": []}))
    assert waiting["action"] == "wait" and waiting["headline"].startswith("Waiting on Legal Affairs")

    office_blocker = {"id": "b2", "clauseType": "ExportControl", "office": "export_control"}
    assert _step(openBlockerRefs=[office_blocker])["action"] == "escalate"
    royalties = {"id": "b3", "clauseType": "Royalties", "office": None}
    step = _step(**_unacceptable_law(openBlockerRefs=[royalties]))
    assert step["action"] == "send_back" and step["headline"] == "Send back to the licensee: 1 clause needs changes."
    assert _step(**_unacceptable_law(openBlockerRefs=[royalties], state="sent_back"))["action"] == "wait"
    assert _step(state="ready_to_sign")["action"] == "send_for_signature"
    assert _step(state="out_for_signature")["headline"] == "Waiting on signature."
    assert _step(manualValue=None)["action"] == "add_value"
    assert _step()["action"] == "approve"


# ---------------------------------------------------------------------------
# SLA, value, fiscal year, capture gaps, allowed actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days,status", [(0, "on_track"), (5, "on_track"), (6, "amber"), (10, "amber"), (11, "red")])
def test_sla_colours_from_days_in_stage(days, status):
    c = item(stageEnteredAt=iso(NOW - timedelta(days=days, hours=1)))
    timing = workflow.sla(c, workflow.default_settings(), NOW)
    assert (timing["daysInStage"], timing["targetDays"], timing["slaStatus"]) == (days, 5, status)


def test_sla_is_none_without_a_target_or_after_signature():
    cfg = workflow.default_settings()
    assert workflow.sla(item(state="signed", stage="signed"), cfg, NOW)["slaStatus"] == "none"
    assert workflow.sla(item(state="rejected"), cfg, NOW)["slaStatus"] == "none"


def test_value_precedence_and_buckets():
    assert workflow.contract_value(item(manualValue=None, extractedValue=5, expectedValue=9)) == (5.0, "extracted")
    assert workflow.contract_value(item(manualValue=1, extractedValue=5)) == (1.0, "manual")
    assert workflow.contract_value(item(manualValue=None, expectedValue=9)) == (9.0, "expected")
    assert workflow.contract_value(item(manualValue=None)) == (None, None)
    assert workflow.value_bucket(item(stage="draft", state="intake")) == "potential"
    assert workflow.value_bucket(item(stage="approval", state="ready_to_sign")) == "potential"
    assert workflow.value_bucket(item(stage="active", state="active")) == "current"
    assert workflow.value_bucket(item(stage="review", state="rejected")) == "none"
    assert workflow.value_bucket(item(stage="expired", state="closed")) == "none"


@pytest.mark.parametrize("date,fy", [("2026-06-30", 2026), ("2026-07-01", 2027), ("2027-06-30T23:00:00Z", 2027),
                                     ("2027-07-01", 2028), (None, None)])
def test_osu_fiscal_year_starts_july_first(date, fy):
    assert workflow.fiscal_year(date) == fy


def test_capture_gaps_depend_on_the_agreement_type():
    sra = item(agreementType="sponsored_research", manualValue=None, counterparty="Acme", stage="approval",
               state="ready_to_sign", userFields=[])
    assert workflow.capture_gaps(sra) == ["value", "sponsor", "piName", "department", "requestedDate",
                                          "huronRecordId", "workdayRef"]
    nda = item(agreementType="nda", manualValue=None, counterparty="Acme", huronRecordId="AGR1")
    assert workflow.capture_gaps(nda) == []
    signed = item(state="signed", stage="signed", counterparty="Acme", huronRecordId="AGR1", workdayRef="WD-1",
                  effectiveDate="2026-10-01")
    assert workflow.capture_gaps(signed) == ["termEnd"]
    assert workflow.capture_gaps(item(agreementType="other", counterparty="A", huronRecordId="H")) == ["agreementTypeUnsure"]
    assert workflow.capture_gaps(item(agreementType="other", counterparty="A", huronRecordId="H",
                                      userFields=["agreementType"])) == []


def test_allowed_actions_follow_state_and_role():
    cfg = {"reviewers": [{"email": "legal@osu.edu", "offices": ["legal_affairs"]}]}
    escalated = item(state="escalated", routing={"required": ["legal_affairs"], "approvals": []})
    editor = workflow.Viewer(can_edit=True)
    assert workflow.allowed_actions(escalated, editor, cfg) == [
        "assign", "approve", "office_approve", "send_back", "escalate", "reject", "reopen", "ask_pi", "comment"]
    assert workflow.allowed_actions(item(state="ready_to_sign"), editor, cfg) == [
        "assign", "send_back", "escalate", "reject", "send_for_signature", "mark_signed", "reopen", "comment"]
    viewer = workflow.Viewer(can_edit=False, email="becky@osu.edu")
    assert workflow.allowed_actions(escalated, viewer, cfg) == ["comment"]
    approver = workflow.Viewer(can_edit=False, is_leader=True, email="legal@osu.edu")
    assert workflow.allowed_actions(escalated, approver, cfg) == ["office_approve", "comment"]
    leader_editor = workflow.Viewer(can_edit=True, is_leader=True, email="becky@osu.edu")
    assert workflow.allowed_actions(escalated, leader_editor, cfg) == ["comment"]


def test_user_set_fields_survive_reanalysis(lic, ddb):
    workflow.update_fields("lic-1", {"counterparty": "Buckeye BioSensors LLC", "effectiveDate": "2026-10-01",
                                     "termEndDate": "2041-10-01"}, DANA)
    ddb.items[("DOC#lic-1", "META")]["latestVersion"] = 2          # analysed again
    assert intake.handle_event(analysed_event("lic-1")) == "reanalysed"
    c = store.contracts.get("lic-1")
    assert c["counterparty"] == "Buckeye BioSensors LLC"
    assert (c["effectiveDate"], c["termEndDate"]) == ("2026-10-01", "2041-10-01")
    assert lic.actions("lic-1")[-1] == "rescored"
    with pytest.raises(workflow.BadRequest):
        workflow.clean_fields({"termEndDate": "soon"})
