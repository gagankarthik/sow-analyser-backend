"""govern-api routes (lambdas/govern_api/handler.py) end to end on the fakes:
access (404 / 403), Govern roles and the admin gate, intake at upload, lazy
contracts for library documents, the Oct 9 "done when" (an admin edits one
matrix position and rescoring reflects it), the demo story, settings,
integrations and reports.
"""
from __future__ import annotations

import copy
import json

import pytest

from govern_intake import handler as intake
from govern_support import (DANA, EDITOR, EMAIL, LICENSE_V2, OUTSIDER, OWNER, SRA, VIEWER,
                            analysed_event, call, install_analysis, seed_doc, seed_project)
from shared.config import settings
from shared.govern import store


@pytest.fixture
def world(gov, ddb, monkeypatch):
    """OWNER's licence (in a project with an editor and a viewer) through intake."""
    install_analysis(monkeypatch, ddb)
    monkeypatch.setattr(settings, "raw_bucket", "raw")
    seed_doc(ddb, "lic-1", project_ids=["proj_osu"])
    seed_project(ddb, {EDITOR: "editor", VIEWER: "viewer"}, ["lic-1"])
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    return gov


def contract(sub=OWNER, cid="lic-1", **kw):
    status, body = call("GET", f"/contracts/{cid}", sub, **kw)
    assert status == 200, body
    return body["contract"]


# ---------------------------------------------------------------------------
# Identity, access and roles
# ---------------------------------------------------------------------------


def test_me_reports_the_govern_role(world, monkeypatch):
    me = call("GET", "/govern/me", OWNER)[1]
    assert {k: v for k, v in me.items() if k != "features"} == {"email": EMAIL[OWNER], "name": "Dana Ruiz",
                                                                "role": "admin", "tenantId": f"u-{OWNER}"}
    assert set(me["features"]) == {"routingRules", "docusign", "notifications", "obligations", "exports", "integrations"}
    assert call("GET", "/govern/me", OWNER, groups=["govern-leader"])[1]["role"] == "leader"
    assert call("GET", "/govern/me", OWNER, groups="[govern-reviewer other]")[1]["role"] == "reviewer"
    monkeypatch.setattr(settings, "govern_open_admin", False)
    assert call("GET", "/govern/me", OWNER)[1]["role"] == "reviewer"


def test_contract_visibility_follows_the_document(world):
    assert call("GET", "/contracts/lic-1", OUTSIDER)[0] == 404
    assert call("GET", "/contracts/nope", OWNER)[0] == 404
    assert call("GET", "/contracts/bad id!", OWNER)[0] == 404
    assert contract(VIEWER)["contractId"] == "lic-1"
    status, body = call("PATCH", "/contracts/lic-1", VIEWER, {"department": "X"})
    assert (status, body["code"]) == (403, "forbidden")
    assert call("PATCH", "/contracts/lic-1", OUTSIDER, {"department": "X"})[0] == 404
    status, body = call("PATCH", "/contracts/lic-1", EDITOR, {"department": "Biomedical Engineering"})
    assert status == 200 and body["contract"]["department"] == "Biomedical Engineering"
    assert [c["contractId"] for c in call("GET", "/contracts", VIEWER)[1]["contracts"]] == ["lic-1"]
    assert call("GET", "/contracts", OUTSIDER)[1]["contracts"] == []


def test_actions_follow_allowed_actions(world):
    viewer_view = contract(VIEWER)
    assert viewer_view["allowedActions"] == ["comment"]
    status, body = call("POST", "/contracts/lic-1/actions", VIEWER, {"action": "assign", "owner": DANA})
    assert (status, body["code"]) == (403, "forbidden")
    assert call("POST", "/contracts/lic-1/actions", VIEWER, {"action": "comment", "text": "Looks fine"})[0] == 200
    status, body = call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "activate"})
    assert (status, body["code"]) == (409, "invalid_transition")
    status, body = call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "assign", "owner": DANA})
    assert status == 200 and body["contract"]["state"] == "in_review"
    assert body["contract"]["allowedActions"] == contract(EDITOR)["allowedActions"]
    status, body = call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "dance"})
    assert (status, body["code"]) == (400, "bad_request")


def test_a_routed_office_approver_may_approve_with_view_access(world):
    call("PUT", "/workflow/settings", OWNER, {"reviewers": [
        {"email": EMAIL[VIEWER], "name": "Avery Chen", "offices": ["legal_affairs"], "agreementTypes": []}]})
    call("POST", "/contracts/lic-1/actions", OWNER, {"action": "assign", "owner": DANA})
    call("POST", "/contracts/lic-1/actions", OWNER, {"action": "approve"})
    view = contract(VIEWER, groups=["govern-leader"])
    assert view["state"] == "escalated" and view["allowedActions"] == ["office_approve", "comment"]
    status, body = call("POST", "/contracts/lic-1/actions", VIEWER, {"action": "office_approve", "office": "export_control"},
                        groups=["govern-leader"])
    assert status == 403
    status, body = call("POST", "/contracts/lic-1/actions", VIEWER, {"action": "office_approve", "office": "legal_affairs"},
                        groups=["govern-leader"])
    assert status == 200 and body["contract"]["state"] == "ready_to_sign"


def test_admin_routes_need_the_admin_role(world, monkeypatch):
    for method, path, body in (("PUT", "/matrix", {"playbooks": {}}), ("PUT", "/workflow/settings", {}),
                               ("GET", "/integrations", None), ("PUT", "/integrations/huron", {"enabled": True}),
                               ("POST", "/integrations/workday/sync", {}), ("POST", "/matrix/import", {})):
        status, out = call(method, path, OWNER, body, groups=["govern-reviewer"])
        assert (status, out["code"]) == (403, "forbidden"), path
    monkeypatch.setattr(settings, "govern_open_admin", False)
    assert call("GET", "/integrations", OWNER)[0] == 403
    assert call("GET", "/integrations", OWNER, groups=["govern-admin"])[0] == 200
    assert call("GET", "/matrix", OWNER)[0] == 200                    # reading is open to everyone
    assert call("GET", "/reports/trends", OWNER)[0] == 403
    assert call("GET", "/reports/trends", OWNER, groups=["govern-leader"])[0] == 200


# ---------------------------------------------------------------------------
# Intake at upload, lazy contracts, revisions
# ---------------------------------------------------------------------------


def test_post_contracts_creates_a_draft_and_intake_keeps_user_fields(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "sra-1", sample=SRA, status="CLASSIFYING", doc_type="OTHER",
             parties=["Northfield University", "Midwest Advanced Materials Corp."], value=425000)
    status, body = call("POST", "/contracts", OWNER, {"docId": "sra-1", "sponsor": "Midwest Materials (MAMC)",
                                                       "expectedValue": 400000})
    c = body["contract"]
    assert status == 201
    assert (c["stage"], c["state"], c["analysisStatus"]) == ("draft", "intake", "CLASSIFYING")
    assert c["nextStep"]["action"] == "wait" and c["waitingOn"]["label"] == "Sonar is still reading this agreement"
    assert [a["action"] for a in c["activity"]] == ["intake"]
    assert [x["contractId"] for x in call("GET", "/contracts", OWNER)[1]["contracts"]] == ["sra-1"]
    status, body = call("POST", "/contracts", OWNER, {"docId": "sra-1", "piName": "Dr. P. Raman"})
    assert status == 200 and body["contract"]["piName"] == "Dr. P. Raman"          # idempotent → patched

    ddb.items[("DOC#sra-1", "META")]["status"] = "READY"
    assert intake.handle_event(analysed_event("sra-1")) == "completed"
    c = contract(cid="sra-1")
    assert (c["stage"], c["agreementType"], c["analysisStatus"]) == ("review", "sponsored_research", "READY")
    assert c["sponsor"] == "Midwest Materials (MAMC)" and c["piName"] == "Dr. P. Raman"     # people win
    assert c["extractedValue"] == 425000 and c["value"] == 425000
    assert (c["huronRecordId"], c["workdayRef"], c["college"]) == ("AGR00012402", "WD-GR-220871", "College of Engineering")
    assert c["matrix"]["version"] == 1
    assert [a["action"] for a in c["activity"]][:2] == ["rescored", "stage_changed"]
    assert call("POST", "/contracts", OUTSIDER, {"docId": "sra-1"})[0] == 404
    assert call("POST", "/contracts", OWNER, {"docId": "sra-1", "colour": "red"})[0] == 400


def test_get_contracts_turns_library_documents_into_contracts(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "old-1", value=90000)
    seed_doc(ddb, "running-1", status="EMBEDDING")
    seed_doc(ddb, "rev-x", revisionOf="old-1")
    status, body = call("GET", "/contracts", OWNER)
    assert status == 200 and [c["contractId"] for c in body["contracts"]] == ["old-1"]
    assert body["count"] == 1 and body["generatedAt"].endswith("Z")
    c = body["contracts"][0]
    assert (c["state"], c["stage"], c["value"], c["matrix"]) == ("intake", "review", 90000, None)
    assert gov.actions("old-1") == ["intake"]
    assert gov.activity_for("old-1")[0]["summary"] == "Sonar added this agreement to Govern from the document library."
    call("GET", "/contracts", OWNER)
    assert gov.actions("old-1") == ["intake"]                       # created once
    assert store.config.tenants() == [f"u-{OWNER}"]


def test_closed_contracts_are_hidden_unless_asked_for(world):
    call("POST", "/contracts/lic-1/actions", OWNER, {"action": "reject", "reasonCode": "duplicate"})
    assert call("GET", "/contracts", OWNER)[1]["count"] == 0
    assert call("GET", "/contracts", OWNER, qs={"includeClosed": "true"})[1]["count"] == 1


def test_revision_upload_creates_a_linked_pending_document(world, ddb, monkeypatch):
    class _S3:
        def generate_presigned_url(self, *a, **k):
            return "https://s3.invalid/put"

    import shared.aws
    monkeypatch.setattr(shared.aws, "s3_client", lambda: _S3())
    status, body = call("GET", "/contracts/lic-1/revision-upload-url", EDITOR, qs={"filename": "license v2.docx"})
    assert status == 200 and body["uploadUrl"] == "https://s3.invalid/put"
    new_id = body["docId"]
    meta = ddb.doc(new_id)
    assert (meta["status"], meta["revisionOf"], meta["parentDocId"], meta["projectIds"]) == ("PENDING", "lic-1", "lic-1", ["proj_osu"])
    assert meta["ownerSub"] == EDITOR and meta["rawKey"].endswith(f"/{new_id}/license v2.docx")
    assert new_id in ddb.items[("PROJ#proj_osu", "META")]["docIds"]
    c = contract()
    assert c["versionDocIds"] == ["lic-1", new_id] and c["analysisStatus"] == "PENDING"
    assert call("GET", "/contracts/lic-1/revision-upload-url", VIEWER, qs={"filename": "a.pdf"})[0] == 403
    assert call("GET", "/contracts/lic-1/revision-upload-url", EDITOR, qs={"filename": "a.exe"})[0] == 400


# ---------------------------------------------------------------------------
# Oct 9 "done when": edit one matrix position, rescoring reflects it
# ---------------------------------------------------------------------------


def test_admin_edits_a_matrix_position_and_rescoring_reflects_it(world):
    before = contract()
    assert before["matrix"]["counts"]["deviates"] == 2
    tier = {c["clauseType"]: c["tier"] for c in before["review"]["clauses"]}
    assert tier["Confidentiality"] == "within"

    status, body = call("GET", "/matrix", OWNER)
    assert status == 200 and body["current"]["version"] == 1 and len(body["versions"]) == 1
    playbooks = copy.deepcopy(body["current"]["playbooks"])
    conf = next(c for c in playbooks["license"]["clauses"] if c["clauseType"] == "Confidentiality")
    conf["thresholds"].update(maxConfidentialityYears=1, fallbackConfidentialityYears=2)
    status, body = call("PUT", "/matrix", OWNER, {"playbooks": playbooks, "note": "Shorter confidentiality"})
    assert status == 200 and body["matrix"]["version"] == 2

    status, body = call("POST", "/contracts/lic-1/rescore", OWNER, {})
    after = body["contract"]
    assert status == 200 and after["matrix"]["version"] == 2 and after["matrix"]["counts"]["deviates"] == 3
    assert {c["clauseType"]: c["tier"] for c in after["review"]["clauses"]}["Confidentiality"] == "deviates"
    assert after["openBlockers"] == before["openBlockers"] + 1
    assert after["activity"][0]["action"] == "rescored"
    assert after["activity"][0]["detail"]["matrixVersion"] == 2
    assert "Confidentiality" in after["activity"][0]["detail"]["deviatingClauseTypes"]
    versions = call("GET", "/matrix", OWNER)[1]["versions"]
    assert [v["version"] for v in versions] == [2, 1] and versions[0]["note"] == "Shorter confidentiality"
    assert call("GET", "/matrix/versions/1", OWNER)[1]["matrix"]["version"] == 1
    assert call("GET", "/matrix/versions/9", OWNER)[0] == 404


def test_matrix_import_saves_a_new_version(world):
    rows = [{"clauseType": "Royalties", "standard": "At least 4% running royalty", "fallback": "3%",
             "escalationOffice": "Technology Commercialization"},
            {"clauseType": "Not a clause", "standard": "x"}]
    status, body = call("POST", "/matrix/import", OWNER, {"agreementType": "license", "rows": rows, "mode": "merge"})
    assert status == 200 and body["imported"] == 1 and body["matrix"]["version"] == 2
    assert body["skipped"][0]["row"] == 2
    assert call("POST", "/matrix/import", OWNER, {"agreementType": "boat", "rows": rows})[0] == 400
    assert call("PUT", "/matrix", OWNER, {"playbooks": "nope"})[0] == 400


# ---------------------------------------------------------------------------
# Demo story
# ---------------------------------------------------------------------------


def test_demo_story_licence_sent_back_revised_approved_and_signed(world, ddb):
    call("POST", "/contracts/lic-1/actions", OWNER, {"action": "assign", "owner": DANA})
    c = contract()
    assert c["openBlockers"] == 3 and c["valueBucket"] == "potential"
    step = c["nextStep"]
    assert step["action"] == "escalate" and step["office"] == "legal_affairs"           # Delaware law, no fallback
    clauses = [{"clauseType": b["clauseType"], "label": b["clauseType"], "suggestedLanguage": b["suggestedLanguage"]}
               for b in c["blockers"]]
    status, body = call("POST", "/contracts/lic-1/actions", OWNER, {"action": "send_back", "clauses": clauses})
    assert body["contract"]["activity"][0]["summary"] == "Dana Ruiz sent this back to the licensee with 3 clauses to change."
    assert body["contract"]["activity"][0]["detail"]["clauses"] == clauses

    seed_doc(ddb, "lic-2", sample=LICENSE_V2, revisionOf="lic-1", project_ids=["proj_osu"])
    assert intake.handle_event(analysed_event("lic-2")) == "revision"
    assert intake.handle_event(analysed_event("lic-2")) == "unchanged"                 # re-delivery
    c = contract()
    assert (c["currentDocId"], c["state"], c["rounds"]) == ("lic-2", "in_review", 1)
    assert c["versionDocIds"] == ["lic-1", "lic-2"] and c["versions"][1]["round"] == 2
    assert [a["action"] for a in c["activity"]][:2] == ["rescored", "revision_received"]
    assert c["openBlockers"] == 0 and c["nextStep"]["action"] in ("approve", "add_value")

    call("PATCH", "/contracts/lic-1", OWNER, {"manualValue": 150000})
    for body in ({"action": "approve"}, {"action": "send_for_signature", "provider": "manual"},
                 {"action": "mark_signed", "signedAt": "2026-10-14"}):
        status, out = call("POST", "/contracts/lic-1/actions", OWNER, body)
        assert status == 200, out
    c = out["contract"]
    assert (c["state"], c["valueBucket"], c["value"], c["fiscalYear"]) == ("signed", "current", 150000, 2027)


# ---------------------------------------------------------------------------
# Blockers, obligations, income
# ---------------------------------------------------------------------------


def test_reviewer_blockers_survive_rescoring(world):
    status, body = call("POST", "/contracts/lic-1/blockers", OWNER,
                        {"text": "Confirm the field of use with the PI", "office": "tech_commercialization"})
    assert status == 200 and body["contract"]["openBlockers"] == 4
    sonar = next(b for b in body["contract"]["blockers"] if b["source"] == "sonar")
    status, body = call("PATCH", f"/contracts/lic-1/blockers/{sonar['id']}", OWNER, {"status": "closed"})
    assert body["contract"]["openBlockers"] == 3 and body["contract"]["activity"][0]["action"] == "blocker_closed"
    body = call("POST", "/contracts/lic-1/rescore", OWNER, {})[1]["contract"]
    by_id = {b["id"]: b for b in body["blockers"]}
    assert by_id[sonar["id"]]["status"] == "closed"                    # closed Sonar blocker stays closed
    assert sum(b["source"] == "reviewer" for b in body["blockers"]) == 1
    assert call("PATCH", "/contracts/lic-1/blockers/nope", OWNER, {"status": "closed"})[0] == 404
    assert call("POST", "/contracts/lic-1/blockers", OWNER, {"text": ""})[0] == 400


def test_obligations_and_income(world):
    status, body = call("POST", "/contracts/lic-1/obligations", OWNER,
                        {"kind": "royalty_report", "title": "Q4 royalty report", "dueDate": "2026-10-20", "amount": 0})
    c = body["contract"]
    assert status == 200 and c["obligationsDue"] == 1 and c["obligations"][0]["source"] == "manual"
    obl = c["obligations"][0]["id"]
    body = call("PATCH", f"/contracts/lic-1/obligations/{obl}", OWNER, {"status": "done"})[1]["contract"]
    assert body["obligationsDue"] == 0 and body["obligations"][0]["completedAt"]
    assert body["activity"][0]["action"] == "obligation_done"
    items = [{"kind": "upfront", "description": "Licence issue fee", "amount": 25000, "expectedDate": "2026-11-01"}]
    body = call("PUT", "/contracts/lic-1/income", OWNER, {"items": items})[1]["contract"]
    assert [(i["kind"], i["amount"], i["source"]) for i in body["licensingIncome"]] == [("upfront", 25000, "manual")]
    assert call("PUT", "/contracts/lic-1/income", OWNER, {"items": [{"kind": "gold"}]})[0] == 400


# ---------------------------------------------------------------------------
# Settings, integrations, reports
# ---------------------------------------------------------------------------


def test_workflow_settings_round_trip_and_teams_secret(world, gov, monkeypatch):
    monkeypatch.setattr(settings, "teams_secret_arn", "arn:teams")
    gov.secrets["arn:teams"] = ""
    status, body = call("PUT", "/workflow/settings", OWNER, {
        "stageTargetDays": {"review": 3}, "redAfterMultiple": 1.5,
        "teamsWebhookUrl": "https://example.webhook.office.com/webhookb2/abc"})
    s = body["settings"]
    assert status == 200 and s["stageTargetDays"]["review"] == 3 and s["stageTargetDays"]["negotiation"] == 10
    assert s["redAfterMultiple"] == 1.5 and s["teamsWebhookConfigured"] is True and "teamsWebhookUrl" not in s
    assert json.loads(gov.secrets["arn:teams"])["tenants"][f"u-{OWNER}"].startswith("https://example.webhook")
    assert call("GET", "/workflow/settings", OWNER)[1]["settings"]["redAfterMultiple"] == 1.5
    assert call("PUT", "/workflow/settings", OWNER, {"teamsWebhookUrl": "http://evil.example"})[0] == 400
    assert call("PUT", "/workflow/settings", OWNER, {"routingRules": [{"when": {}, "route": ["the_dean"]}]})[0] == 400


def test_integrations_list_update_and_dry_run_sync(world, gov, monkeypatch):
    monkeypatch.setattr(settings, "huron_secret_arn", "arn:huron")
    gov.secrets["arn:huron"] = ""
    connectors = call("GET", "/integrations", OWNER)[1]["connectors"]
    assert [c["id"] for c in connectors] == ["huron", "workday", "m365", "docusign"]
    assert all(c["status"] == "not_connected" and not c["credentialsConfigured"] for c in connectors)
    status, body = call("PUT", "/integrations/huron", OWNER, {"credentials": {"clientId": "a"}})
    assert status == 400 and "clientId, clientSecret" in body["error"]
    status, body = call("PUT", "/integrations/huron", OWNER, {"enabled": True,
                                                               "credentials": {"clientId": "a", "clientSecret": "s"}})
    assert status == 200 and body["connector"]["credentialsConfigured"] is True
    assert "clientSecret" not in json.dumps(body) and "\"s\"" not in json.dumps(body)
    assert json.loads(gov.secrets["arn:huron"])["tenants"][f"u-{OWNER}"] == {"clientId": "a", "clientSecret": "s"}
    status, body = call("POST", "/integrations/workday/sync", OWNER, {})
    assert status == 200 and body["run"]["status"] == "dry_run" and body["run"]["dryRun"] is True
    runs = call("GET", "/integrations/sync-log", OWNER)[1]["runs"]
    assert runs[0]["connectorId"] == "workday"
    unmatched = call("GET", "/integrations/unmatched", OWNER)[1]["contracts"]
    assert unmatched == []                                         # lic-1 carries a Workday ref → auto
    assert call("PUT", "/integrations/salesforce", OWNER, {})[0] == 404


def test_capture_report_lists_gaps_and_missed_documents(world, ddb):
    seed_doc(ddb, "failed-1", status="FAILED", errorMessage='States.TaskFailed: {"errorMessage": "The PDF is encrypted"}')
    status, body = call("GET", "/reports/capture", OWNER)
    assert status == 200
    assert {g["gap"]: g["contractIds"] for g in body["gaps"]} == {"value": ["lic-1"]}
    assert body["missedDocuments"] == [{"docId": "failed-1", "title": "Exclusive License Agreement", "status": "FAILED",
                                        "reason": "The analysis failed: The PDF is encrypted"}]
    assert body["lastReconciledAt"] is None


def test_unknown_routes_and_methods(world):
    assert call("GET", "/nowhere", OWNER)[0] == 404
    assert call("DELETE", "/contracts", OWNER)[0] == 405
    status, body = call("POST", "/contracts/lic-1/actions", OWNER, None)
    assert status == 400


def test_open_admin_is_off_unless_the_stage_turns_it_on(monkeypatch):
    from shared.config import Settings

    monkeypatch.delenv("GOVERN_OPEN_ADMIN", raising=False)
    assert Settings().govern_open_admin is False
    monkeypatch.setenv("GOVERN_OPEN_ADMIN", "true")
    assert Settings().govern_open_admin is True


def test_portfolio_obligations_list(world):
    call("POST", "/contracts/lic-1/obligations", OWNER,
         {"kind": "royalty_report", "title": "Q4 royalty report", "dueDate": "2026-10-20"})
    call("POST", "/contracts/lic-1/obligations", OWNER,
         {"kind": "royalty_report", "title": "Undated note"})                      # undated: not listed
    status, body = call("GET", "/obligations", OWNER)
    assert status == 200 and body["count"] == 1
    o = body["obligations"][0]
    assert o["title"] == "Q4 royalty report" and o["contractId"] == "lic-1" and o["contractTitle"]
    # A project viewer in another workspace sees the shared contract's obligations too.
    assert call("GET", "/obligations", VIEWER)[1]["count"] == 1
    # Someone with no access sees none.
    assert call("GET", "/obligations", OUTSIDER)[1]["count"] == 0
    # Done drops out.
    obl = call("GET", "/contracts/lic-1", OWNER)[1]["contract"]["obligations"]
    first = next(x for x in obl if x["title"] == "Q4 royalty report")["id"]
    call("PATCH", f"/contracts/lic-1/obligations/{first}", OWNER, {"status": "done"})
    assert call("GET", "/obligations", OWNER)[1]["count"] == 0


def test_organization_settings(world):
    org = call("GET", "/workflow/settings", OWNER)[1]["settings"]["organization"]
    assert org == {"name": None, "defaultCurrency": "USD", "fiscalYearStartMonth": 1, "confirmedSteps": [],
                   "setupCompletedAt": None}
    status, body = call("PUT", "/workflow/settings", OWNER, {"organization": {
        "name": "  Acme Research  ", "defaultCurrency": "eur", "fiscalYearStartMonth": 7,
        "setupCompletedAt": "2026-10-08T12:00:00Z"}})
    assert status == 200
    assert body["settings"]["organization"] == {"name": "Acme Research", "defaultCurrency": "EUR",
                                                 "fiscalYearStartMonth": 7, "confirmedSteps": [],
                                                 "setupCompletedAt": "2026-10-08T12:00:00Z"}
    body = call("PUT", "/workflow/settings", OWNER, {"organization": {"confirmedSteps": ["workflow", "matrix"]}})[1]
    assert body["settings"]["organization"]["confirmedSteps"] == ["matrix", "workflow"]
    # A partial update keeps the rest.
    body = call("PUT", "/workflow/settings", OWNER, {"organization": {"name": "Acme"}})[1]
    assert body["settings"]["organization"]["defaultCurrency"] == "EUR"
    for bad in ({"defaultCurrency": "euro"}, {"fiscalYearStartMonth": 13}, {"name": ""}, {"setupCompletedAt": "soon"},
                {"confirmedSteps": ["everything"]}):
        assert call("PUT", "/workflow/settings", OWNER, {"organization": bad})[0] == 400


def test_obligation_verification(world):
    c = call("POST", "/contracts/lic-1/obligations", OWNER,
             {"kind": "royalty_report", "title": "Annual report", "dueDate": "2026-12-01"})[1]["contract"]
    obl = c["obligations"][0]
    assert obl["verified"] is True and obl["verifiedAt"]          # a person entered it
    oid = obl["id"]
    body = call("PATCH", f"/contracts/lic-1/obligations/{oid}", OWNER, {"verified": False})[1]["contract"]
    assert body["obligations"][0]["verified"] is False and body["obligations"][0]["verifiedAt"] is None
    body = call("PATCH", f"/contracts/lic-1/obligations/{oid}", OWNER, {"verified": True})[1]["contract"]
    assert body["obligations"][0]["verified"] is True
    assert body["activity"][0]["action"] == "obligation_verified"
    assert call("PATCH", f"/contracts/lic-1/obligations/{oid}", OWNER, {"verified": "yes"})[0] == 400
    listed = call("GET", "/obligations", OWNER)[1]["obligations"][0]
    assert listed["verified"] is True
