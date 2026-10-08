"""Govern "Later" features (GOVERN_FEATURES in shared/config.py): parsing, the
``features`` object on GET /govern/me, and what each path does while its
feature is switched off. conftest.py turns every feature ON for the rest of
the suite; each test here switches off what it checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from govern_connectors import handler as connectors_lambda
from govern_intake import handler as intake
from govern_notifier import handler as notifier
from govern_support import DANA, OWNER, SRA, analysed_event, call, install_analysis, lambda_context, seed_doc
from govern_sweeper import handler as sweeper
from govern_webhooks import handler as webhooks
from shared.config import GOVERN_FEATURES, Settings, parse_govern_features, settings
from shared.govern import store, workflow

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def only(monkeypatch, features: str = "") -> None:
    """Switch on exactly the features named (comma list); everything else off."""
    monkeypatch.setattr(settings, "govern_features", parse_govern_features(features))


@pytest.fixture
def lic(gov, ddb, monkeypatch):
    """A licence through intake, in review, with an unacceptable term (→ Legal Affairs when routing is on)."""
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    workflow.perform_action("lic-1", "assign", {"owner": DANA}, DANA)
    return gov


# ---------------------------------------------------------------------------
# Parsing and GET /govern/me
# ---------------------------------------------------------------------------


def test_the_env_list_takes_snake_or_camel_case_and_ignores_unknown_names(monkeypatch):
    assert parse_govern_features("exports, routingRules,Routing_Rules,bogus,") == {"exports", "routingrules"}
    monkeypatch.setenv("GOVERN_FEATURES", "docusign,integrations")
    s = Settings()
    assert s.feature_enabled("docusign") and s.feature_enabled("integrations")
    assert not s.feature_enabled("routing_rules") and not s.feature_enabled("routingRules")


def test_everything_is_off_by_default(monkeypatch):
    monkeypatch.delenv("GOVERN_FEATURES", raising=False)
    flags = Settings().govern_feature_flags()
    assert flags == {camel: False for camel in GOVERN_FEATURES.values()}


def test_me_reports_the_features_this_deployment_has_on(gov, monkeypatch):
    only(monkeypatch, "exports,routing_rules")
    status, me = call("GET", "/govern/me", OWNER)
    assert status == 200
    assert me["features"] == {"routingRules": True, "docusign": False, "notifications": False,
                              "obligations": False, "exports": True, "integrations": False}


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------


def test_with_routing_off_approval_goes_straight_to_ready_to_sign(lic, monkeypatch):
    only(monkeypatch)
    c = workflow.perform_action("lic-1", "approve", {}, DANA)
    assert c["state"] == "ready_to_sign"                       # no Legal Affairs detour
    assert workflow.evaluate_routing(store.contracts.get("lic-1"), workflow.default_settings()) == ([], [])


def test_with_routing_on_the_same_approval_goes_to_the_office(lic):
    c = workflow.perform_action("lic-1", "approve", {}, DANA)
    assert c["state"] == "escalated"


def test_a_hand_escalation_still_applies_with_routing_off(lic, monkeypatch):
    only(monkeypatch)
    workflow.perform_action("lic-1", "escalate", {"office": "export_control"}, DANA)
    assert workflow.pending_offices(store.contracts.get("lic-1")) == ["export_control"]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_with_notifications_off_the_notifier_sends_and_claims_nothing(lic, monkeypatch):
    monkeypatch.setattr(settings, "notify_from_email", "govern@northfield.edu")
    only(monkeypatch)
    workflow.perform_action("lic-1", "assign", {"owner": {"email": "eli@northfield.edu", "name": "Eli Park"}}, DANA)
    entry = {k: v for k, v in lic.activity_for("lic-1")[-1].items() if k not in ("PK", "SK")}
    assert notifier.handle_event({"detail": entry}) == []
    assert lic.emails == [] and "notification_sent" not in lic.actions("lic-1")
    only(monkeypatch, "notifications")                         # switched on later: the event is still sendable
    assert notifier.handle_event({"detail": entry}) == ["email"]


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


@pytest.fixture
def sra(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "sra-1", sample=SRA, doc_type="OTHER", value=425000,
             parties=["Northfield University", "Midwest Advanced Materials Corp."])
    intake.handle_event(analysed_event("sra-1"))
    return gov


def test_with_integrations_off_scheduled_and_event_runs_are_skipped(sra, monkeypatch):
    only(monkeypatch)
    assert connectors_lambda.handler({"trigger": "schedule"}, lambda_context()) == {"runs": 0, "status": "disabled"}
    entry = sra.activity_for("sra-1")[-1]
    assert connectors_lambda.handle_govern_event({"detail": {**entry, "contractId": "sra-1"}}) == "disabled"
    assert store.sync.runs(store.contracts.get("sra-1")["tenantId"]) == []


def test_with_integrations_off_a_manual_sync_is_refused(gov, monkeypatch):
    only(monkeypatch)
    status, body = call("POST", "/integrations/workday/sync", OWNER, {})
    assert (status, body["code"]) == (409, "feature_disabled")


# ---------------------------------------------------------------------------
# DocuSign
# ---------------------------------------------------------------------------


def test_with_docusign_off_the_webhook_acknowledges_and_does_nothing(monkeypatch):
    only(monkeypatch)
    event = {"rawPath": "/webhooks/docusign", "requestContext": {"http": {"method": "POST"}},
             "headers": {}, "body": "{}", "isBase64Encoded": False}
    resp = webhooks.handler(event, lambda_context())
    assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"status": "disabled"}


def test_with_docusign_off_only_manual_signature_is_accepted(lic, monkeypatch):
    only(monkeypatch)
    store.contracts.mutate("lic-1", lambda c: c.update(state="ready_to_sign", stage="approval"))
    with pytest.raises(workflow.FeatureDisabled):
        workflow.perform_action("lic-1", "send_for_signature", {"provider": "docusign"}, DANA)
    c = workflow.perform_action("lic-1", "send_for_signature", {"provider": "manual"}, DANA)
    assert c["state"] == "out_for_signature"


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


def _signed_with_due_obligations() -> dict:
    return {"contractId": "c1", "tenantId": "t1", "state": "signed", "stage": "signed",
            "stageEnteredAt": "2026-10-01T00:00:00Z", "createdAt": "2026-09-01T00:00:00Z",
            "owner": DANA, "agreementType": "license", "direction": "incoming", "openBlockerRefs": [],
            "routing": {"required": [], "approvals": []}, "reviewIndex": {}, "manualValue": 1000,
            "obligationDueDates": ["2026-10-01", "2026-10-20"]}


def test_with_obligations_off_none_count_as_due(monkeypatch):
    c = _signed_with_due_obligations()
    assert workflow.to_api(c, now=NOW)["obligationsDue"] == 2
    only(monkeypatch)
    view = workflow.to_api(c, now=NOW)
    assert view["obligationsDue"] == 0
    assert "obligation" not in view["nextStep"]["headline"]


def test_with_obligations_off_the_sweeper_writes_no_due_entries(monkeypatch):
    only(monkeypatch)

    def no_reads(*_a, **_k):
        raise AssertionError("obligations were read while the feature is off")

    monkeypatch.setattr(store.contracts, "obligations_due", no_reads)
    assert sweeper._sweep_obligations("t1", NOW) == 0
