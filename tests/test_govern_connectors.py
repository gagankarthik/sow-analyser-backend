"""Connector framework (shared/govern/connectors.py + lambdas/govern_connectors):
dry runs are logged and change nothing, system of record wins with a logged
conflict, a live Workday pull matches by reference and flags misses, Huron
push-back on Govern events."""
from __future__ import annotations

import json

import pytest

from govern_connectors import handler as connectors_lambda
from govern_intake import handler as intake
from govern_support import SRA, analysed_event, install_analysis, lambda_context, seed_doc
from shared.config import settings
from shared.govern import connectors, store

TENANT = "u-aaaaaaaa-0000-4000-8000-0000000000a1"


@pytest.fixture
def sra(gov, ddb, monkeypatch):
    """A sponsored research contract carrying Huron AGR00012402 / Workday WD-GR-220871."""
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "sra-1", sample=SRA, doc_type="OTHER", value=425000,
             parties=["The Ohio State University", "Midwest Advanced Materials Corp."])
    intake.handle_event(analysed_event("sra-1"))
    return gov


def test_without_credentials_every_run_is_a_logged_dry_run(sra):
    before = dict(store.contracts.get("sra-1"))
    run = connectors.run_connector(TENANT, "workday", "manual")
    assert (run["status"], run["dryRun"], run["recordsIn"]) == ("dry_run", True, 1)
    assert "would pull Workday awards" in run["summary"]
    huron = connectors.run_connector(TENANT, "huron", "schedule")
    assert huron["status"] == "dry_run" and huron["recordsOut"] == 1
    assert "would push status, findings, blockers and next step to 1 Huron record" in huron["summary"]
    assert [r["connectorId"] for r in store.sync.runs(TENANT)] == ["huron", "workday"]
    after = store.contracts.get("sra-1")
    assert after["rev"] == before["rev"], "a dry run changes nothing"


def test_system_of_record_wins_and_the_conflict_is_logged(sra):
    workflow_owned = {"piName": "Dr. Priya Raman"}
    assert store.contracts.get("sra-1")["piName"] == workflow_owned["piName"]
    adapter = connectors.ADAPTERS["huron"]
    mapped = connectors.map_record(
        {"ID": "AGR00012402", "PrincipalInvestigator": {"Name": "Dr. P. Raman-Singh"}, "Department": {"Name": None},
         "Sponsor": {"Name": "Midwest Advanced Materials Corp."}, "DateReceived": "2026-09-30"},
        adapter.default_mapping)
    assert connectors.apply_record(TENANT, adapter, mapped) == "conflict"
    c = store.contracts.get("sra-1")
    assert c["piName"] == "Dr. P. Raman-Singh"                          # the record's value is kept
    assert c["requestedDate"] == "2026-09-30"                           # gap filled (not owned)
    assert c["department"] == "Department of Materials Science & Engineering"   # empty record value ignored
    conflict = c["syncConflicts"][-1]
    assert (conflict["field"], conflict["govern"], conflict["recordValue"], conflict["system"]) == (
        "piName", "Dr. Priya Raman", "Dr. P. Raman-Singh", "huron")
    actions = sra.actions("sra-1")[-2:]
    assert actions == ["sync", "conflict"]
    entry = sra.activity_for("sra-1")[-1]
    assert entry["summary"] == ("Huron Research Suite and Govern disagreed on the PI; Huron Research Suite's value "
                                "(Dr. P. Raman-Singh) was kept (Govern had Dr. Priya Raman).")
    assert connectors.apply_record(TENANT, adapter, {**mapped, "huronRecordId": "AGR-UNKNOWN"}) == "unmatched"


def test_a_live_workday_pull_matches_by_reference_and_flags_misses(sra, gov, ddb, monkeypatch):
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))                       # WD-CC-104233, not in Workday
    monkeypatch.setattr(settings, "workday_secret_arn", "arn:workday")
    gov.secrets["arn:workday"] = ""
    connectors.update_connector(TENANT, "workday", {
        "enabled": True, "config": {"baseUrl": "https://wd.example", "tokenUrl": "https://wd.example/token"},
        "credentials": {"clientId": "c", "clientSecret": "s", "refreshToken": "r"}})
    calls = []

    def fake_request(method, url, *, token=None, body=None, form=None):
        calls.append((method, url, form))
        if url.endswith("/token"):
            assert form["grant_type"] == "refresh_token"
            return {"access_token": "tok"}
        assert token == "tok"
        return {"items": [{"Award_Reference_ID": "WD-GR-220871", "Award_Amount": "430000", "Cost_Center": "CC-77"}]}

    monkeypatch.setattr(connectors, "_request", fake_request)
    run = connectors.run_connector(TENANT, "workday", "manual")
    assert (run["status"], run["dryRun"], run["recordsIn"]) == ("ok", False, 1)
    sra_c = store.contracts.get("sra-1")
    assert sra_c["expectedValue"] == 430000 and sra_c["external"]["workday"]["costCenter"] == "CC-77"
    assert sra_c["workdayMatch"] == "auto"
    assert store.contracts.get("lic-1")["workdayMatch"] == "unmatched"
    assert store.config.connector(TENANT, "workday")["lastSyncAt"] == run["startedAt"]


def test_a_failing_endpoint_is_a_failed_run_not_an_exception(sra, gov, monkeypatch):
    monkeypatch.setattr(settings, "huron_secret_arn", "arn:huron")
    gov.secrets["arn:huron"] = ""
    connectors.update_connector(TENANT, "huron", {"enabled": True, "credentials": {"clientId": "c", "clientSecret": "s"}})
    run = connectors.run_connector(TENANT, "huron", "manual")             # no baseUrl / tokenUrl configured
    assert run["status"] == "failed" and run["errors"][0]["message"] == "endpoint is not configured"
    view = next(c for c in connectors.list_connectors(TENANT) if c["id"] == "huron")
    assert view["status"] == "error" and view["credentialsConfigured"] is True


def test_govern_events_push_back_to_huron(sra):
    entry = sra.activity_for("sra-1")[-1]
    event = {"Records": [{"messageId": "m1", "body": json.dumps({"detail": {**entry, "contractId": "sra-1"}})},
                         {"messageId": "m2", "body": json.dumps({"detail": {"action": "sync", "contractId": "sra-1"}})}]}
    assert connectors_lambda.handler(event, lambda_context()) == {"batchItemFailures": []}
    runs = store.sync.runs(TENANT)
    assert [(r["connectorId"], r["trigger"], r["status"], r["recordsOut"]) for r in runs] == [("huron", "event", "dry_run", 1)]
    payload = connectors.push_payload(store.contracts.get("sra-1"), connectors.datetime.now(connectors.timezone.utc))
    assert payload["huronRecordId"] == "AGR00012402" and payload["nextStep"] and payload["status"] == "intake"


def test_the_daily_schedule_runs_every_tenant(sra):
    assert connectors_lambda.handler({"trigger": "schedule"}, lambda_context()) == {"runs": 2}


def test_field_mapping_and_credentials_are_validated(sra):
    from shared.govern.workflow import BadRequest

    with pytest.raises(BadRequest, match="not a Govern field"):
        connectors.update_connector(TENANT, "huron", {"fieldMapping": {"salary": "X"}})
    with pytest.raises(BadRequest, match="https"):
        connectors.update_connector(TENANT, "huron", {"config": {"baseUrl": "http://x"}})
    with pytest.raises(BadRequest, match="integrationKey, userId, privateKey, connectHmacKey"):
        connectors.update_connector(TENANT, "docusign", {"credentials": {"integrationKey": "k"}})
    view = connectors.update_connector(TENANT, "huron", {"fieldMapping": {"piName": "PI.FullName"}})
    assert view["fieldMapping"]["piName"] == "PI.FullName" and view["fieldMapping"]["huronRecordId"] == "ID"
