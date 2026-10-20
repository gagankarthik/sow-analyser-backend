"""Cross-tenant / cross-user isolation regression suite.

One world, two workspaces:

* tenant ``acme``   — OWNER (owns every acme document and the project),
                      EDITOR (editor on the project, nothing else),
                      VIEWER (same tenant, but shares NOTHING with OWNER: the
                      "unshared colleague").
* tenant ``globex`` — OUTSIDER (another organisation, with a document of its own).

Every probe below is made by VIEWER (same tenant, unshared) and by OUTSIDER
(other tenant) against OWNER's data. Each must be denied with the same 404 an
unknown id gets (so ids cannot be probed), and nothing of OWNER's may appear in
any list, count, search result, people directory or dry run. Runs against the
real handlers on the in-memory tables (tests/fakes.py); nothing leaves the box.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api import handler as api
from govern_api import handler as gapi
from govern_intake import handler as intake
from govern_support import (EDITOR, EMAIL, LICENSE_V2, OUTSIDER, OWNER, VIEWER, analysed_event, call as gcall,
                            install_analysis, seed_doc, seed_project)
from rag import handler as rag
from shared.access import Caller
from shared.config import settings

ACME, GLOBEX = "acme", "globex"
TENANT_OF = {OWNER: ACME, EDITOR: ACME, VIEWER: ACME, OUTSIDER: GLOBEX}
PROJ = "proj_acme"
SHARED_DOC, PRIVATE_DOC, GLOBEX_DOC = "acme-shared", "acme-private", "globex-own"
ACME_DOCS = (SHARED_DOC, PRIVATE_DOC)
INTRUDERS = (VIEWER, OUTSIDER)


def _claims(sub: str, groups: list[str] | None = None) -> dict:
    out = {"sub": sub, "email": EMAIL[sub], "email_verified": "true", "custom:tenantId": TENANT_OF[sub]}
    if groups is not None:
        out["cognito:groups"] = groups
    return out


def acall(method: str, path: str, sub: str, body=None, qs=None, groups=None):
    """The documents API, with identity from verified JWT claims only."""
    event = {"rawPath": path, "queryStringParameters": qs, "body": json.dumps(body) if body is not None else None,
             "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": _claims(sub, groups)}}}}
    resp = api._route(method, path, event, Caller.from_event(event))
    return resp["statusCode"], json.loads(resp["body"])


def gov_call(method: str, path: str, sub: str, body=None, qs=None, groups=None):
    """govern-api, with each user in their own tenant."""
    return gcall(method, path, sub, body, qs, groups=groups, tenant=TENANT_OF[sub])


def _tenant_doc(ddb, doc_id: str, owner: str, **extra):
    tenant = TENANT_OF[owner]
    return seed_doc(ddb, doc_id, owner=owner, tenantId=tenant, GSI1PK=f"TENANT#{tenant}",
                    rawKey=f"tenants/{tenant}/uploads/{doc_id}/c.pdf", **extra)


@pytest.fixture
def world(monkeypatch, gov, ddb):
    install_analysis(monkeypatch, ddb)
    monkeypatch.setattr(settings, "raw_bucket", "raw")
    monkeypatch.setenv("RAW_BUCKET", "raw")
    monkeypatch.setenv("PROCESSED_BUCKET", "processed")
    monkeypatch.setattr(api.settings, "cognito_user_pool_id", "")          # no Cognito calls

    # Storage / search fakes for the documents API (record what was asked for).
    versions = [{"versionNumber": 1, "classificationKey": "c", "diffKey": "d", "timelineKey": "t"}]
    monkeypatch.setattr(api, "query_doc_versions", lambda _id: versions)
    from shared import s3 as shared_s3
    monkeypatch.setattr(shared_s3, "get_json", lambda b, k: {"clauses": [], "changes": [], "amendmentChain": []})
    monkeypatch.setattr(api, "presign_get", lambda *a, **k: "https://s3.invalid/file")
    monkeypatch.setattr(api, "_purge_storage", lambda *a, **k: None)
    monkeypatch.setattr(api, "delete_doc_version", lambda doc_id, n: dict(ddb.doc(doc_id) or {}))
    monkeypatch.setattr(api, "get_clause_vector", lambda *a: [0.1])
    searches: list[dict] = []
    monkeypatch.setattr(api, "knn_search", lambda **kw: searches.append(kw) or [])

    class _S3:
        def copy_object(self, **_k):
            return {}

        def generate_presigned_url(self, *a, **k):
            return "https://s3.invalid/put"
    monkeypatch.setattr(api, "s3_client", lambda: _S3())
    import shared.aws
    monkeypatch.setattr(shared.aws, "s3_client", lambda: _S3())

    _tenant_doc(ddb, SHARED_DOC, OWNER, project_ids=[PROJ])
    _tenant_doc(ddb, PRIVATE_DOC, OWNER)
    _tenant_doc(ddb, GLOBEX_DOC, OUTSIDER)
    seed_project(ddb, {EDITOR: "editor"}, [SHARED_DOC], project_id=PROJ)
    for doc_id, tenant in ((SHARED_DOC, ACME), (PRIVATE_DOC, ACME), (GLOBEX_DOC, GLOBEX)):
        assert intake.handle_event(analysed_event(doc_id, tenant)) == "created"
    return {"ddb": ddb, "gov": gov, "searches": searches}


def _no_trace_of_acme(payload) -> None:
    """Nothing identifying OWNER's data in a response body."""
    text = json.dumps(payload)
    for needle in (*ACME_DOCS, PROJ, EMAIL[OWNER], EMAIL[EDITOR]):
        assert needle not in text, f"{needle} leaked: {text[:300]}"


# ---------------------------------------------------------------------------
# Documents API
# ---------------------------------------------------------------------------

DOC_PROBES = [
    ("GET", "", None), ("PATCH", "", {"title": "pwned"}), ("DELETE", "", None),
    ("DELETE", "/versions/1", None), ("GET", "/classification", None), ("GET", "/diff", None),
    ("GET", "/timeline", None), ("GET", "/file", None), ("POST", "/reprocess", {}),
]


@pytest.mark.parametrize("intruder", INTRUDERS)
@pytest.mark.parametrize("doc_id", ACME_DOCS)
@pytest.mark.parametrize("method,suffix,body", DOC_PROBES)
def test_document_routes_answer_404_to_other_tenants_and_unshared_users(world, intruder, doc_id, method, suffix, body):
    before = dict(world["ddb"].doc(doc_id))
    status, out = acall(method, f"/documents/{doc_id}{suffix}", intruder, body)
    assert (status, out.get("code")) == (404, "not_found")
    _no_trace_of_acme(out)
    assert world["ddb"].doc(doc_id) == before                     # nothing changed or deleted


@pytest.mark.parametrize("intruder", INTRUDERS)
def test_similar_clauses_of_a_foreign_document_is_404_and_search_never_runs(world, intruder):
    status, _ = acall("GET", f"/documents/{SHARED_DOC}/similar", intruder, qs={"clause": "1"})
    assert status == 404 and world["searches"] == []


def test_document_lists_and_counts_show_only_what_was_granted(world):
    def ids(sub):
        status, out = acall("GET", "/documents", sub)
        assert status == 200 and out["count"] == len(out["documents"])
        return sorted(d["docId"] for d in out["documents"])

    assert ids(OWNER) == sorted(ACME_DOCS)
    assert ids(EDITOR) == [SHARED_DOC]               # the project's document, not OWNER's private one
    assert ids(VIEWER) == []                          # same tenant, nothing shared
    assert ids(OUTSIDER) == [GLOBEX_DOC]


def test_search_scope_is_only_the_callers_visible_documents(world):
    assert acall("GET", f"/documents/{SHARED_DOC}/similar", EDITOR, qs={"clause": "1"})[0] == 200
    assert world["searches"][-1]["doc_ids"] == [SHARED_DOC]
    assert acall("GET", f"/documents/{GLOBEX_DOC}/similar", OUTSIDER, qs={"clause": "1"})[0] == 200
    assert world["searches"][-1]["doc_ids"] == [GLOBEX_DOC]
    for sub in INTRUDERS:
        visible = Caller.from_claims(_claims(sub)).visible_doc_ids()
        assert not set(visible) & set(ACME_DOCS)


@pytest.mark.parametrize("intruder", INTRUDERS)
def test_projects_are_invisible_and_cannot_be_taken_over(world, intruder):
    before = dict(world["ddb"].items[(f"PROJ#{PROJ}", "META")])
    assert acall("GET", "/projects", intruder)[1]["projects"] == []
    for method, path, body in (
        ("GET", f"/projects/{PROJ}", None),
        ("PUT", f"/projects/{PROJ}", {"name": "mine now"}),                    # id takeover
        ("DELETE", f"/projects/{PROJ}", None),
        ("POST", f"/projects/{PROJ}/invite", {"email": "spy@evil.com", "role": "editor"}),
        ("PATCH", f"/projects/{PROJ}/members/{EMAIL[EDITOR]}", {"role": "viewer"}),
        ("DELETE", f"/projects/{PROJ}/members/{EMAIL[EDITOR]}", None),
        ("PUT", f"/projects/{PROJ}/documents/{GLOBEX_DOC}", None),
        ("DELETE", f"/projects/{PROJ}/documents/{SHARED_DOC}", None),
    ):
        status, out = acall(method, path, intruder, body)
        assert status == 404, (method, path, status, out)
        _no_trace_of_acme(out)
    status, _ = acall("GET", "/documents/upload-url", intruder, qs={"filename": "a.pdf", "projectId": PROJ})
    assert status == 404
    # The legacy whole-list save cannot touch it either.
    acall("POST", "/projects", intruder, {"projects": [{"id": PROJ, "name": "x", "docIds": [GLOBEX_DOC]}]})
    assert world["ddb"].items[(f"PROJ#{PROJ}", "META")] == before
    assert world["ddb"].items.get((f"PROJ#{PROJ}", "MEMBER#spy@evil.com")) is None


def test_a_cross_tenant_share_is_explicit_scoped_and_revocable(world):
    assert acall("POST", f"/projects/{PROJ}/invite", OWNER, {"email": EMAIL[OUTSIDER], "role": "viewer"})[0] == 201
    assert acall("GET", f"/documents/{SHARED_DOC}", OUTSIDER)[0] == 200          # granted
    assert acall("GET", f"/documents/{PRIVATE_DOC}", OUTSIDER)[0] == 404         # not granted
    assert acall("PATCH", f"/documents/{SHARED_DOC}", OUTSIDER, {"title": "x"})[0] == 403   # viewer
    assert gov_call("GET", f"/contracts/{SHARED_DOC}", OUTSIDER)[0] == 200
    assert gov_call("PATCH", f"/contracts/{SHARED_DOC}", OUTSIDER, {"department": "x"})[0] == 403
    assert acall("DELETE", f"/projects/{PROJ}/members/{EMAIL[OUTSIDER]}", OWNER)[0] == 200
    assert acall("GET", f"/documents/{SHARED_DOC}", OUTSIDER)[0] == 404          # revoked at once
    assert gov_call("GET", f"/contracts/{SHARED_DOC}", OUTSIDER)[0] == 404


def test_workspace_settings_need_an_admin_of_that_same_tenant(world):
    ddb = world["ddb"]
    # OWNER opens Govern first and so creates (and administers) acme; OUTSIDER globex.
    assert gov_call("GET", "/govern/me", OWNER)[1]["isCreator"] is True
    assert gov_call("GET", "/govern/me", OUTSIDER)[1]["isCreator"] is True
    # Not an admin of acme (a later member; the pool-wide group grants nothing): refused server-side.
    for groups in (None, ["govern-admin"]):
        assert acall("POST", "/tenant/compliance", VIEWER, {"packs": []}, groups=groups)[0] == 403
        assert acall("PUT", "/playbook/rules/termination", VIEWER, {}, groups=groups)[0] == 403
        assert acall("DELETE", "/playbook/rules/termination", VIEWER, groups=groups)[0] == 403
    # An admin of ANOTHER tenant writes only their own tenant's row.
    assert acall("POST", "/tenant/compliance", OUTSIDER, {"packs": []})[0] == 200
    assert ddb.items[(f"TENANT#{GLOBEX}", "COMPLIANCE")]["packs"] == []
    assert (f"TENANT#{ACME}", "COMPLIANCE") not in ddb.items
    assert acall("GET", "/tenant/compliance", OWNER)[1]["explicit"] is False


# ---------------------------------------------------------------------------
# Sonar (RAG)
# ---------------------------------------------------------------------------


@pytest.fixture
def sonar(world, monkeypatch):
    calls = {"search": [], "chat": [], "embed": 0}

    def embed(texts, model=None):
        calls["embed"] += 1
        return [[0.0]]

    monkeypatch.setattr(rag, "embed_texts", embed)
    monkeypatch.setattr(rag, "clause_search", lambda **kw: calls["search"].append(kw) or [])
    monkeypatch.setattr(rag, "chat_text", lambda **kw: calls["chat"].append(kw) or "")
    return calls


def _chat(sub: str, target: str) -> dict:
    ev = {"rawPath": f"/documents/{target}/chat", "pathParameters": {"docId": target},
          "body": json.dumps({"question": "what are the fees?"}),
          "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": _claims(sub)}}}}
    resp = rag._http_handle(ev)
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


@pytest.mark.parametrize("intruder", INTRUDERS)
@pytest.mark.parametrize("target", [*ACME_DOCS, PROJ])
def test_sonar_never_searches_or_answers_about_foreign_documents_or_projects(sonar, intruder, target):
    out = _chat(intruder, target)
    assert out["citations"] == [] and out["grounded"] is False
    assert sonar == {"search": [], "chat": [], "embed": 0}        # nothing embedded, searched or sent
    _no_trace_of_acme(out)


def test_sonar_scope_for_a_granted_project_is_its_documents_only(sonar):
    _chat(EDITOR, PROJ)
    assert sonar["search"][0]["doc_ids"] == [SHARED_DOC] and "tenant_id" not in sonar["search"][0]


def test_streamed_sonar_searches_only_the_callers_documents(sonar, monkeypatch):
    monkeypatch.setattr(rag, "_push_token", lambda *a, **k: None)
    for sub in INTRUDERS:
        rag._handle({"arguments": {"input": {"question": "fees?", "tenantId": ACME, "docIds": list(ACME_DOCS)}},
                     "identity": {"claims": _claims(sub)}})
    scopes = [set(s.get("doc_ids") or []) for s in sonar["search"]]
    assert all(not s & set(ACME_DOCS) for s in scopes)


# ---------------------------------------------------------------------------
# Govern: contracts and everything hanging off them
# ---------------------------------------------------------------------------

CONTRACT_PROBES = [
    ("GET", "", None, None), ("PATCH", "", {"department": "x"}, None), ("DELETE", "", None, None),
    ("POST", "/actions", {"action": "comment", "text": "hi"}, None), ("POST", "/rescore", {}, None),
    ("POST", "/blockers", {"text": "x"}, None), ("PATCH", "/blockers/b1", {"status": "resolved"}, None),
    ("POST", "/obligations", {"title": "x"}, None), ("PATCH", "/obligations/o1", {"status": "done"}, None),
    ("PUT", "/income", {"items": []}, None), ("GET", "/activity", None, None),
    ("GET", "/revision-upload-url", None, {"filename": "v2.pdf"}),
    ("GET", "/portal-links", None, None), ("POST", "/portal-links", {}, None),
    ("DELETE", "/portal-links/0123456789abcdef", None, None),
]


@pytest.mark.parametrize("intruder", INTRUDERS)
@pytest.mark.parametrize("contract_id", ACME_DOCS)
@pytest.mark.parametrize("method,suffix,body,qs", CONTRACT_PROBES)
def test_contract_routes_answer_404_to_other_tenants_and_unshared_users(world, intruder, contract_id,
                                                                        method, suffix, body, qs):
    gov = world["gov"]
    before = (dict(gov.contract(contract_id)), list(gov.actions(contract_id)))
    status, out = gov_call(method, f"/contracts/{contract_id}{suffix}", intruder, body, qs)
    assert (status, out.get("code")) == (404, "not_found"), out
    _no_trace_of_acme(out)
    assert (dict(gov.contract(contract_id)), list(gov.actions(contract_id))) == before


@pytest.mark.parametrize("intruder", INTRUDERS)
def test_bulk_and_dry_run_cannot_reach_foreign_contracts(world, intruder):
    gov = world["gov"]
    status, out = gov_call("POST", "/contracts/bulk", intruder,
                           {"contractIds": [*ACME_DOCS, "nope"], "fields": {"department": "Pwned"}})
    assert status == 200 and out["updated"] == 0
    assert {r["contractId"]: r["error"]["code"] for r in out["results"]} == {
        SHARED_DOC: "not_found", PRIVATE_DOC: "not_found", "nope": "not_found"}
    assert all(gov.contract(c).get("department") != "Pwned" for c in ACME_DOCS)
    rule = {"name": "probe", "enabled": True, "trigger": {"type": "in_stage_for", "stage": "review", "days": 1},
            "conditions": [], "actions": [{"type": "comment", "text": "x"}]}
    for contract_id in ACME_DOCS:
        status, out = gov_call("POST", "/automations/new/test", intruder, {"contractId": contract_id, "rule": rule})
        assert status == 404, out
        _no_trace_of_acme(out)


@pytest.mark.parametrize("intruder", INTRUDERS)
def test_govern_lists_reports_and_people_hold_nothing_foreign(world, intruder):
    for path in ("/contracts", "/obligations", "/reports/capture", "/govern/people", "/govern/me",
                 "/matrix", "/workflow/settings", "/automations"):
        status, out = gov_call("GET", path, intruder, qs={"includeClosed": "true"} if path == "/contracts" else None)
        assert status == 200, (path, out)
        _no_trace_of_acme(out)
    status, out = gov_call("GET", "/views", intruder, qs={"module": "contracts"})
    assert status == 200 and out["views"] == []
    people = gov_call("GET", "/govern/people", intruder)[1]["people"]
    assert [p["email"] for p in people] == [EMAIL[intruder]]           # only themself
    ids = [c["contractId"] for c in gov_call("GET", "/contracts", intruder)[1]["contracts"]]
    assert ids == ([GLOBEX_DOC] if intruder == OUTSIDER else [])


def test_people_directory_lists_only_people_the_caller_shares_work_with(world):
    people = {p["email"] for p in gov_call("GET", "/govern/people", EDITOR)[1]["people"]}
    assert people == {EMAIL[OWNER], EMAIL[EDITOR]}


def test_saved_views_stay_in_their_tenant_and_with_their_audience(world):
    def make(visibility):
        status, out = gov_call("POST", "/views", OWNER, {"module": "contracts", "name": f"v-{visibility}",
                                                          "visibility": visibility, "config": {}})
        assert status == 201, out
        return out["view"]["id"]

    private, everyone = make("private"), make("everyone")
    names = lambda sub: [v["name"] for v in gov_call("GET", "/views", sub, qs={"module": "contracts"})[1]["views"]]
    assert names(VIEWER) == ["v-everyone"]                 # same workspace: only the shared one
    assert names(OUTSIDER) == []                           # another workspace: none
    for view_id in (private, everyone):
        assert gov_call("PATCH", f"/views/{view_id}", OUTSIDER, {"name": "x"})[0] == 404
        assert gov_call("DELETE", f"/views/{view_id}", OUTSIDER)[0] == 404
    assert gov_call("PATCH", f"/views/{private}", VIEWER, {"name": "x"})[0] == 404
    assert gov_call("PATCH", f"/views/{everyone}", VIEWER, {"name": "x"})[0] == 403      # visible, not theirs


def test_admin_settings_are_per_tenant_and_need_an_admin_of_that_organization(world):
    # Before anyone has opened Govern, acme has no organization and no admin.
    assert gov_call("PUT", "/workflow/settings", OWNER, {"reviewers": []}, groups=["govern-admin"])[0] == 403
    assert gov_call("GET", "/govern/me", OWNER)[1]["isAdmin"] is True        # OWNER creates acme
    assert gov_call("GET", "/govern/me", VIEWER)[1]["isAdmin"] is False      # a later member
    for groups in (None, ["govern-admin"]):
        assert gov_call("PUT", "/workflow/settings", VIEWER, {"reviewers": []}, groups=groups)[0] == 403
        assert gov_call("POST", "/automations", VIEWER, {}, groups=groups)[0] == 403
        assert gov_call("GET", "/integrations", VIEWER, groups=groups)[0] == 403
        assert gov_call("GET", "/reports/trends", VIEWER, groups=groups)[0] == 403
        assert gov_call("POST", "/govern/org/admins", VIEWER, {"email": EMAIL[VIEWER]}, groups=groups)[0] == 403
    rita = {"email": "rita@acme.com", "name": "Rita", "offices": [], "agreementTypes": []}
    assert gov_call("PUT", "/workflow/settings", OWNER, {"reviewers": [rita]})[0] == 200
    assert "rita@acme.com" not in json.dumps(gov_call("GET", "/workflow/settings", OUTSIDER)[1])
    assert "rita@acme.com" not in json.dumps(gov_call("GET", "/govern/people", OUTSIDER)[1])


def test_organization_admins_never_cross_tenants(world):
    gov_call("GET", "/govern/me", OWNER)
    gov_call("GET", "/govern/me", VIEWER)
    gov_call("GET", "/govern/me", OUTSIDER)
    # OUTSIDER (admin of globex) sees only globex, and cannot reach acme's people or admins.
    status, out = gov_call("GET", "/govern/org", OUTSIDER)
    assert status == 200 and out["tenantId"] == GLOBEX and out["you"]["isAdmin"] is True
    _no_trace_of_acme(out)
    assert EMAIL[VIEWER] not in json.dumps(out)
    status, out = gov_call("POST", "/govern/org/admins", OUTSIDER, {"email": EMAIL[VIEWER]})
    assert (status, out["code"]) == (404, "not_found")                  # not a member of globex
    status, out = gov_call("DELETE", f"/govern/org/admins/{OWNER}", OUTSIDER)
    assert (status, out["code"]) == (404, "not_found")                  # not an admin of globex
    acme = gov_call("GET", "/govern/org", OWNER)[1]
    assert [a["email"] for a in acme["admins"]] == [EMAIL[OWNER]]
    assert EMAIL[OUTSIDER] not in json.dumps(acme)


def test_a_revision_uploader_does_not_become_owner_of_the_contract(world):
    """Regression: an editor who uploads a revised version owns that document;
    once it became the contract's current version that ownership made them
    "owner" of the whole contract, so they could delete it."""
    ddb, gov = world["ddb"], world["gov"]
    status, out = gov_call("GET", f"/contracts/{SHARED_DOC}/revision-upload-url", EDITOR, qs={"filename": "v2.txt"})
    assert status == 200, out
    rev = out["docId"]
    ddb.items[(f"DOC#{rev}", "META")].update(status="READY", sample=LICENSE_V2, latestVersion=1)
    assert intake.handle_event(analysed_event(rev, ACME)) == "revision"
    assert gov.contract(SHARED_DOC)["currentDocId"] == rev

    c = gov.contract(SHARED_DOC)
    editor = gapi.GovernUser(Caller.from_claims(_claims(EDITOR)), _claims(EDITOR))
    assert gapi._contract_role(c, editor) == "editor"
    status, out = gov_call("DELETE", f"/contracts/{SHARED_DOC}", EDITOR)
    assert (status, out["code"]) == (403, "forbidden")
    assert gov.contract(SHARED_DOC) is not None
    listed = next(x for x in gov_call("GET", "/contracts", EDITOR)[1]["contracts"] if x["contractId"] == SHARED_DOC)
    assert listed["contractId"] == SHARED_DOC
    # The real owner keeps full rights.
    owner = gapi.GovernUser(Caller.from_claims(_claims(OWNER)), _claims(OWNER))
    assert gapi._contract_role(c, owner) == "owner"


# ---------------------------------------------------------------------------
# Observability: no response bodies in traces
# ---------------------------------------------------------------------------


def test_no_handler_lets_xray_record_its_response():
    """Powertools' ``capture_lambda_handler`` stores the handler's return value
    (documents, contracts, presigned URLs, Sonar answers) as X-Ray trace
    metadata unless ``capture_response=False``."""
    lambdas = Path(__file__).resolve().parents[1] / "lambdas"
    offenders = []
    for path in sorted(lambdas.glob("*/handler.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"@tracer\.capture_lambda_handler(\([^)]*\))?", text):
            if "capture_response=False" not in (m.group(1) or ""):
                offenders.append(path.parent.name)
    assert offenders == []
