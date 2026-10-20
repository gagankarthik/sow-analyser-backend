"""Automations (shared/govern/automations.py): rule CRUD and validation over
the API, time rules in the sweeper (once per episode), event rules in the
activity stream (once per entry), conditions, every action's effect and its
activity entry, the dry run, and the feature switch."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from govern_intake import handler as intake
from govern_notifier import handler as notifier
from govern_stream import handler as stream
from govern_support import (
    DANA,
    EDITOR,
    OWNER,
    TENANT,
    analysed_event,
    call,
    install_analysis,
    lambda_context,
    seed_doc,
)
from govern_sweeper import handler as sweeper
from shared.config import parse_govern_features, settings
from shared.govern import automations, store, workflow
from shared.govern.store import iso
from test_govern_events import stream_event

ELI = {"email": "eli@northfield.edu", "name": "Eli Park"}


@pytest.fixture
def lic(gov, ddb, monkeypatch):
    """A licence through intake (unacceptable terms → open items), owned by Dana, in review."""
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1", value=200000)
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    workflow.perform_action("lic-1", "assign", {"owner": DANA}, DANA)
    return gov


def rule_body(**over):
    body = {"name": "Stuck in review", "enabled": True,
            "trigger": {"type": "in_stage_for", "stage": "review", "days": 10},
            "conditions": [], "actions": [{"type": "comment", "text": "Still in {stage} after {days} days."}]}
    body.update(over)
    return body


def create(**over) -> dict:
    status, out = call("POST", "/automations", OWNER, rule_body(**over))
    assert status == 201, out
    return out["automation"]


def age_stage(contract_id: str, days: int, now: datetime) -> None:
    store.contracts.mutate(contract_id, lambda c: c.update(stageEnteredAt=iso(now - timedelta(days=days))))


def entries(gov, contract_id: str, action: str | None = None) -> list[dict]:
    return [e for e in gov.activity_for(contract_id) if action is None or e["action"] == action]


def automated(gov, contract_id: str) -> list[dict]:
    return [e for e in gov.activity_for(contract_id) if (e.get("detail") or {}).get("automation")]


def replay_new(gov, seen: set[str]) -> None:
    """Feed the activity entries the stream has not seen yet through it."""
    items = [i for (_, sk), i in sorted(gov.activity.items.items(), key=lambda kv: kv[0][1]) if i["id"] not in seen]
    seen.update(i["id"] for i in items)
    if items:
        assert stream.handler(stream_event(items), lambda_context()) == {"batchItemFailures": []}


def snapshot(gov) -> dict:
    return {name: copy.deepcopy(t.items) for name, t in gov.tables.items()} | {"docs": copy.deepcopy(gov.docs.items)}


# ---------------------------------------------------------------------------
# CRUD, validation, admin gate
# ---------------------------------------------------------------------------


def test_admin_creates_lists_edits_and_deletes_a_rule(gov):
    rule = create()
    assert rule["id"] and len(rule["id"]) == 32 and rule["rev"] == 1
    assert rule["createdBy"]["email"] == "dana@northfield.edu"
    assert (rule["runCount"], rule["lastRunAt"]) == (0, None)
    assert gov.config.keys_with_prefix(f"T#{TENANT}#AUTOMATIONS") == [f"A#{rule['id']}"]

    status, listed = call("GET", "/automations", OWNER)
    assert status == 200 and listed["count"] == 1 and listed["limit"] == 50 and listed["enabled"] is True
    assert listed["automations"][0]["trigger"] == {"type": "in_stage_for", "stage": "review", "days": 10}

    status, out = call("PATCH", f"/automations/{rule['id']}", OWNER, {"name": "Stuck", "enabled": False, "rev": 1})
    assert status == 200 and out["automation"]["name"] == "Stuck" and out["automation"]["rev"] == 2
    assert out["automation"]["enabled"] is False and out["automation"]["actions"] == rule["actions"]
    stale = call("PATCH", f"/automations/{rule['id']}", OWNER, {"name": "Again", "rev": 1})
    assert stale[0] == 409 and stale[1]["code"] == "conflict"

    status, out = call("DELETE", f"/automations/{rule['id']}", OWNER)
    assert status == 200 and out == {"deleted": True, "id": rule["id"]}
    assert call("PATCH", f"/automations/{rule['id']}", OWNER, {"name": "x"})[0] == 404
    assert call("DELETE", "/automations/not-an-id", OWNER)[0] == 404
    assert call("GET", "/automations", OWNER)[1]["count"] == 0


@pytest.mark.parametrize("body", [
    {**rule_body(), "colour": "blue"},                                                   # unknown top-level key
    rule_body(trigger={"type": "in_stage_for", "stage": "review", "days": 10, "x": 1}),  # unknown trigger key
    rule_body(trigger={"type": "in_stage_for", "stage": "limbo", "days": 10}),
    rule_body(trigger={"type": "in_stage_for", "stage": "review"}),                      # days missing
    rule_body(trigger={"type": "in_stage_for", "stage": "review", "days": 2.5}),
    rule_body(trigger={"type": "term_ends_in", "days": 99999}),
    rule_body(trigger={"type": "whenever"}),
    rule_body(trigger={"type": "sent_back", "to": "review"}),
    rule_body(trigger={"type": "blocker_added", "tier": "missing"}),
    rule_body(conditions=[{"field": "value", "op": "gte", "value": 5, "extra": True}]),  # unknown condition key
    rule_body(conditions=[{"field": "value", "op": "between", "value": 5}]),
    rule_body(conditions=[{"field": "agreementType", "op": "in", "value": ["spaceship"]}]),
    rule_body(conditions=[{"field": "owner", "op": "set", "value": "x"}]),
    rule_body(conditions=[{"field": "colour", "op": "is", "value": "x"}]),
    rule_body(actions=[]),
    rule_body(actions=[{"type": "notify", "to": "owner", "cc": "x"}]),                   # unknown action key
    rule_body(actions=[{"type": "notify", "to": "office", "office": "the_dean"}]),
    rule_body(actions=[{"type": "notify", "to": "emails", "emails": ["not-an-email"]}]),
    rule_body(actions=[{"type": "notify", "to": "owner", "message": "Hi {name}"}]),      # unknown placeholder
    rule_body(actions=[{"type": "escalate"}]),
    rule_body(actions=[{"type": "assign", "owner": {"email": "x@y.edu", "phone": "1"}}]),
    rule_body(actions=[{"type": "launch"}]),
    rule_body(name="  "),
    rule_body(enabled="yes"),
])
def test_validation_is_strict(gov, body):
    status, out = call("POST", "/automations", OWNER, body)
    assert status == 400 and out["code"] == "bad_request"
    assert gov.config.keys_with_prefix(f"T#{TENANT}#AUTOMATIONS") == []


def test_patch_rejects_unknown_keys_and_invalid_merges(gov):
    rule = create()
    assert call("PATCH", f"/automations/{rule['id']}", OWNER, {"runCount": 9})[0] == 400
    assert call("PATCH", f"/automations/{rule['id']}", OWNER, {"trigger": {"type": "in_stage_for"}})[0] == 400


def test_only_admins_write_but_everyone_reads(gov, monkeypatch):
    rule = create()
    reviewer = ["govern-reviewer"]
    # EDITOR is a member of OWNER's workspace, not its admin; the pool-wide
    # govern-admin group does not change that.
    assert call("POST", "/automations", EDITOR, rule_body(), groups=["govern-admin"], tenant=TENANT)[0] == 403
    assert call("GET", "/automations", EDITOR, groups=reviewer, tenant=TENANT)[1]["count"] == 1
    assert call("POST", "/automations", EDITOR, rule_body(), groups=reviewer, tenant=TENANT)[0] == 403
    assert call("PATCH", f"/automations/{rule['id']}", EDITOR, {"name": "x"}, groups=reviewer, tenant=TENANT)[0] == 403
    assert call("DELETE", f"/automations/{rule['id']}", EDITOR, groups=reviewer, tenant=TENANT)[0] == 403
    assert call("POST", "/automations", OWNER, rule_body(), groups=["govern-admin"])[0] == 201


def test_at_most_fifty_rules_per_organization(gov):
    for i in range(automations.MAX_RULES):
        store.config.create_automation(TENANT, {"id": f"{i:032x}", **automations.clean_rule(rule_body()),
                                                "createdAt": iso()})
    status, out = call("POST", "/automations", OWNER, rule_body())
    assert status == 400 and out["code"] == "limit"


# ---------------------------------------------------------------------------
# Time rules (sweeper)
# ---------------------------------------------------------------------------


def test_a_time_rule_fires_once_per_stage_visit(lic):
    rule = create()
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 5, now)
    assert sweeper.sweep(now)["automations"] == 0                       # 5 of 10 days
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 1
    made = automated(lic, "lic-1")
    assert [e["action"] for e in made] == ["comment"]
    assert made[0]["summary"] == "Automation: Stuck in review commented: “Still in review after 11 days.”"
    assert made[0]["detail"]["automation"] == {"ruleId": rule["id"], "ruleName": "Stuck in review"}
    assert made[0]["actor"] is None
    assert sweeper.sweep(now + timedelta(hours=1))["automations"] == 0   # same visit: nothing new
    assert sweeper.sweep(now + timedelta(days=3))["automations"] == 0
    age_stage("lic-1", 12, now)                                           # a new stage visit
    assert sweeper.sweep(now)["automations"] == 1
    assert len(automated(lic, "lic-1")) == 2
    stored = call("GET", "/automations", OWNER)[1]["automations"][0]
    assert stored["runCount"] == 2 and stored["lastRunAt"] == iso(now)


def test_a_rule_that_is_off_or_a_stage_that_differs_does_not_fire(lic):
    rule = create()
    call("PATCH", f"/automations/{rule['id']}", OWNER, {"enabled": False})
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 30, now)
    assert sweeper.sweep(now)["automations"] == 0
    create(name="Negotiation", trigger={"type": "in_stage_for", "stage": "negotiation", "days": 1})
    assert sweeper.sweep(now)["automations"] == 0
    assert automated(lic, "lic-1") == []


def test_term_and_obligation_rules_fire_once_per_date(lic):
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    end = (now + timedelta(days=200)).date().isoformat()
    store.contracts.mutate("lic-1", lambda c: c.update(state="active", stage="active", termEndDate=end))
    create(name="Term", trigger={"type": "term_ends_in", "days": 30},
           actions=[{"type": "comment", "text": "Term ends {due} ({days} days)."}])
    create(name="Report", trigger={"type": "obligation_due_in", "days": 7},
           actions=[{"type": "comment", "text": "Due {due}."}])
    store.contracts.put_obligation("lic-1", TENANT, {"id": "rep-1", "kind": "sponsor_report", "title": "Annual report",
                                                     "dueDate": "2026-10-12", "status": "open", "source": "manual"})
    assert sweeper.sweep(now)["automations"] == 1                       # the report; the term is 200 days out
    assert sweeper.sweep(now)["automations"] == 0
    later = now + timedelta(days=175)                                    # 25 days before the term end
    assert sweeper.sweep(later)["automations"] == 1
    texts = [e["detail"]["text"] for e in automated(lic, "lic-1")]
    assert texts == ["Due 2026-10-12.", f"Term ends {end} (25 days)."]
    assert sweeper.sweep(later + timedelta(days=1))["automations"] == 0


def test_a_failing_action_is_retried_by_the_next_sweep_without_repeating_the_others(lic, monkeypatch):
    create(actions=[{"type": "comment", "text": "First"}, {"type": "comment", "text": "Second"}])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    real = automations.run_action
    calls = {"n": 0}

    def flaky(rule, action, *a, **k):
        if action.get("text") == "Second" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("DynamoDB throttled")
        return real(rule, action, *a, **k)

    monkeypatch.setattr(automations, "run_action", flaky)
    with pytest.raises(RuntimeError, match="1 tenant"):
        sweeper.sweep(now)
    assert [e["detail"]["text"] for e in automated(lic, "lic-1")] == ["First"]
    assert sweeper.sweep(now)["automations"] == 1
    assert [e["detail"]["text"] for e in automated(lic, "lic-1")] == ["First", "Second"]


# ---------------------------------------------------------------------------
# Event rules (activity stream)
# ---------------------------------------------------------------------------


def test_an_event_rule_fires_once_per_activity_entry(lic):
    seen: set[str] = set()
    replay_new(lic, seen)                                                # intake history, no rules yet
    create(name="Sent back", trigger={"type": "sent_back"},
           actions=[{"type": "comment", "text": "Chase {counterparty} in a week."}])
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, DANA)
    replay_new(lic, seen)
    made = automated(lic, "lic-1")
    assert len(made) == 1 and made[0]["summary"].startswith("Automation: Sent back commented:")
    items = [i for i in lic.activity.items.values()]
    stream.handler(stream_event(items), lambda_context())               # the stream re-delivers everything
    assert len(automated(lic, "lic-1")) == 1
    replay_new(lic, seen)                                                # the automation's own entry: no loop
    assert len(automated(lic, "lic-1")) == 1
    workflow.perform_action("lic-1", "reopen", {}, DANA)
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, DANA)
    replay_new(lic, seen)
    assert len(automated(lic, "lic-1")) == 2                             # a second send-back is a new entry


def test_stage_changed_signed_and_received_triggers(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    rules = {name: create(name=name, trigger=trigger, actions=[{"type": "comment", "text": name}])["id"]
             for name, trigger in (("Received", {"type": "contract_received"}),
                                   ("To approval", {"type": "stage_changed", "to": "approval"}),
                                   ("Signed", {"type": "signed"}))}
    seen: set[str] = set()
    seed_doc(ddb, "lic-1", value=200000)
    intake.handle_event(analysed_event("lic-1"))
    replay_new(gov, seen)
    for action, body in (("assign", {"owner": DANA}), ("approve", {}),
                         ("office_approve", {"office": "legal_affairs"}), ("mark_signed", {})):
        workflow.perform_action("lic-1", action, body, DANA)
        replay_new(gov, seen)
    texts = [e["detail"]["text"] for e in automated(gov, "lic-1")]
    assert texts == ["Received", "To approval", "Signed"]
    assert {r["id"]: r["runCount"] for r in automations.list_rules(TENANT)} == {i: 1 for i in rules.values()}


def test_blocker_added_matches_the_tier_sonar_found(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    create(name="Unacceptable", trigger={"type": "blocker_added", "tier": "unacceptable"},
           actions=[{"type": "comment", "text": "u"}])
    create(name="Any item", trigger={"type": "blocker_added"}, actions=[{"type": "comment", "text": "any"}])
    seen: set[str] = set()
    seed_doc(ddb, "lic-1", value=200000)
    intake.handle_event(analysed_event("lic-1"))                         # Sonar finds unacceptable terms
    replay_new(gov, seen)
    assert sorted(e["detail"]["text"] for e in automated(gov, "lic-1")) == ["any", "u"]
    workflow.add_blocker("lic-1", {"text": "Ask about the budget"}, DANA)  # a reviewer's item has no tier
    replay_new(gov, seen)
    assert sorted(e["detail"]["text"] for e in automated(gov, "lic-1")) == ["any", "any", "u"]


def test_entries_written_by_an_automation_never_trigger_one(lic):
    create(name="Every stage change", trigger={"type": "stage_changed"},
           actions=[{"type": "escalate", "office": "legal_affairs"}])
    tagged = {"id": "e1", "tenantId": TENANT, "contractId": "lic-1", "action": "escalated", "fromStage": "approval",
              "toStage": "review", "detail": {"automation": {"ruleId": "x", "ruleName": "y"}}}
    assert automations.on_entry(tagged) == 0


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_conditions_must_all_hold(lic):
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    trig = {"type": "in_stage_for", "stage": "review", "days": 10}
    create(name="NDAs", trigger=trig, conditions=[{"field": "agreementType", "op": "in", "value": ["nda"]}])
    create(name="Big", trigger=trig, conditions=[{"field": "value", "op": "gte", "value": 500000}])
    create(name="Unowned", trigger=trig, conditions=[{"field": "owner", "op": "unset"}])
    create(name="Lakeshore", trigger=trig, conditions=[{"field": "counterparty", "op": "contains", "value": "nowhere"}])
    assert sweeper.sweep(now)["automations"] == 0
    match = create(name="Match", trigger=trig, conditions=[
        {"field": "agreementType", "op": "in", "value": ["license", "option"]},
        {"field": "value", "op": "lte", "value": 250000},
        {"field": "value", "op": "gte", "value": 100000},
        {"field": "owner", "op": "set"},
    ])
    assert sweeper.sweep(now)["automations"] == 1
    assert [e["detail"]["automation"]["ruleId"] for e in automated(lic, "lic-1")] == [match["id"]]


def test_condition_helpers_read_the_contract():
    c = {"contractId": "c", "agreementType": "license", "direction": "incoming", "manualValue": 10,
         "department": "  Chemistry ", "owner": None, "counterparty": None, "sponsor": "Lakeshore BioSensors, Inc."}
    holds = automations.condition_holds
    assert holds({"field": "direction", "op": "is", "value": "incoming"}, c)
    assert holds({"field": "department", "op": "is", "value": "chemistry"}, c)
    assert holds({"field": "counterparty", "op": "contains", "value": "lakeshore"}, c)
    assert holds({"field": "owner", "op": "unset"}, c) and not holds({"field": "owner", "op": "set"}, c)
    assert not holds({"field": "value", "op": "gte", "value": 11}, c)
    assert not holds({"field": "value", "op": "gte", "value": 0}, {**c, "manualValue": None})


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def test_escalate_assign_and_comment_act_as_the_automation(lic):
    store.config.put_settings(TENANT, {"assignmentRules": [{"agreementType": "license", "department": "*", "reviewer": ELI}]})
    store.contracts.mutate("lic-1", lambda c: c.update(owner=None))
    rule = create(name="Triage", actions=[{"type": "assign"}, {"type": "escalate", "office": "export_control"},
                                          {"type": "comment", "text": "Owner is {owner}."}])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 1
    c = store.contracts.get("lic-1")
    assert c["owner"]["email"] == "eli@northfield.edu"
    assert c["state"] == "escalated" and "export_control" in workflow.pending_offices(c)
    made = automated(lic, "lic-1")
    assert [e["action"] for e in made] == ["assigned", "escalated", "comment"]
    assert made[0]["summary"] == "Automation: Triage assigned this to Eli Park."
    assert made[1]["summary"] == "Automation: Triage escalated this to Export Control."
    assert made[2]["detail"]["text"] == "Owner is Eli Park."
    assert all(e["detail"]["automation"]["ruleId"] == rule["id"] for e in made)


def test_an_action_the_state_forbids_is_skipped(lic):
    store.contracts.mutate("lic-1", lambda c: c.update(state="rejected"))
    create(actions=[{"type": "escalate", "office": "legal_affairs"}, {"type": "comment", "text": "Noted"}])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    store.contracts.mutate("lic-1", lambda c: c.update(state="signed", stage="review"))
    assert sweeper.sweep(now)["automations"] == 1
    assert [e["action"] for e in automated(lic, "lic-1")] == ["comment"]
    assert store.contracts.get("lic-1")["state"] == "signed"


def test_notify_goes_through_the_notifier_to_the_right_people(lic, monkeypatch):
    monkeypatch.setattr(settings, "notify_from_email", "govern@northfield.edu")
    store.config.put_settings(TENANT, {"reviewers": [{**ELI, "offices": ["legal_affairs"], "agreementTypes": []}],
                                       "notifications": {"email": True, "events": {"overdue": False}}})
    create(name="Tell people", actions=[
        {"type": "notify", "to": "owner"},
        {"type": "notify", "to": "office", "office": "legal_affairs", "message": "{contract} needs Legal ({days} days)."},
        {"type": "notify", "to": "emails", "emails": ["Dean@Northfield.edu"]},
    ])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 1
    asks = entries(lic, "lic-1", "automation_notify")
    assert [a["detail"]["recipients"] for a in asks] == [["dana@northfield.edu"], ["eli@northfield.edu"],
                                                         ["dean@northfield.edu"]]
    assert asks[1]["summary"] == ("Automation: Tell people raised an alert for Legal Affairs: "
                                  "“Exclusive License Agreement needs Legal (11 days).”")
    for a in asks:
        entry = {k: v for k, v in a.items() if k not in ("PK", "SK")}
        assert notifier.handle_event({"detail": entry}) == ["email"]     # the rule is the switch
        assert notifier.handle_event({"detail": entry}) == []            # re-delivery sends nothing
    sent = [m["Destination"]["ToAddresses"] for m in lic.emails]
    assert sent == [["dana@northfield.edu"], ["eli@northfield.edu"], ["dean@northfield.edu"]]
    subject = lic.emails[1]["Content"]["Simple"]["Subject"]["Data"]
    assert subject == "Govern: Exclusive License Agreement (Tell people)"
    assert "Sent by the automation “Tell people”." in lic.emails[1]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert entries(lic, "lic-1", "notification_sent")


def test_notify_with_nobody_to_tell_writes_nothing(lic):
    store.contracts.mutate("lic-1", lambda c: c.update(owner=None))
    create(actions=[{"type": "notify", "to": "owner"}])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 1
    assert entries(lic, "lic-1", "automation_notify") == []


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_reports_and_changes_nothing(lic):
    rule = create(conditions=[{"field": "owner", "op": "set"}],
                  actions=[{"type": "escalate", "office": "legal_affairs"}, {"type": "notify", "to": "owner"}])
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    before = snapshot(lic)
    status, out = call("POST", f"/automations/{rule['id']}/test", OWNER, {"contractId": "lic-1"})
    assert status == 200, out
    r = out["result"]
    assert r["wouldFire"] is True and r["alreadyRan"] is False and r["trigger"]["matched"] is True
    assert r["trigger"]["detail"] == "It has been in review for 11 days, and the rule waits 10 days."
    assert r["conditions"] == [{"field": "owner", "op": "set", "matched": True, "actual": "dana@northfield.edu"}]
    assert [(a["type"], a["wouldRun"]) for a in r["actions"]] == [("escalate", True), ("notify", True)]
    assert r["actions"][0]["summary"] == "Escalate to Legal Affairs"
    assert r["actions"][1]["recipients"] == ["dana@northfield.edu"]
    assert snapshot(lic) == before                                       # nothing written anywhere

    sweeper.sweep(now)
    again = call("POST", f"/automations/{rule['id']}/test", OWNER, {"contractId": "lic-1"})[1]["result"]
    assert again["alreadyRan"] is True and again["wouldFire"] is False


def test_the_dry_run_tests_unsaved_rules_and_event_triggers(lic):
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, DANA)
    draft = rule_body(trigger={"type": "sent_back"}, conditions=[{"field": "value", "op": "gte", "value": 1000000}])
    status, out = call("POST", "/automations/new/test", OWNER, {"contractId": "lic-1", "rule": draft})
    assert status == 200
    r = out["result"]
    assert r["trigger"]["matched"] is True and r["trigger"]["detail"].startswith("Matches “Dana Ruiz sent this back")
    assert r["conditions"][0]["matched"] is False and r["conditions"][0]["actual"] == 200000
    assert r["wouldFire"] is False
    assert call("POST", "/automations/new/test", OWNER, {"contractId": "lic-1"})[0] == 400
    assert call("POST", "/automations/new/test", OWNER, {"contractId": "lic-1", "rule": draft, "x": 1})[0] == 400
    assert call("POST", "/automations/new/test", OWNER, {"contractId": "nope", "rule": draft})[0] == 404


# ---------------------------------------------------------------------------
# Feature switch
# ---------------------------------------------------------------------------


def test_with_the_feature_off_nothing_runs(lic, monkeypatch):
    rule = create(trigger={"type": "in_stage_for", "stage": "review", "days": 10})
    sent = create(name="Sent back", trigger={"type": "sent_back"}, actions=[{"type": "comment", "text": "x"}])
    monkeypatch.setattr(settings, "govern_features", parse_govern_features("notifications,obligations"))
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 0
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, DANA)
    replay_new(lic, set())
    assert automated(lic, "lic-1") == []
    status, out = call("POST", "/automations", OWNER, rule_body())
    assert status == 409 and out["code"] == "feature_disabled"
    assert call("PATCH", f"/automations/{rule['id']}", OWNER, {"name": "x"})[0] == 409
    listed = call("GET", "/automations", OWNER)[1]
    assert listed["enabled"] is False and listed["count"] == 2
    assert call("DELETE", f"/automations/{sent['id']}", OWNER)[0] == 200


def test_an_organization_that_left_automations_out_of_its_modules_runs_none(lic):
    create()
    store.config.put_settings(TENANT, {"organization": {"enabledModules": ["obligations"]}})
    now = datetime.now(timezone.utc)
    age_stage("lic-1", 11, now)
    assert sweeper.sweep(now)["automations"] == 0
    assert json.loads(json.dumps(call("GET", "/automations", OWNER)[1]))["enabled"] is False
