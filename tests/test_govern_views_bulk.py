"""govern-api: saved list views (GET/POST /views, PATCH/DELETE /views/{id}),
bulk contract updates (POST /contracts/bulk) and the people directory
(GET /govern/people), end to end on the fakes."""
from __future__ import annotations

import pytest

from govern_intake import handler as intake
from govern_support import (DANA, EDITOR, EMAIL, OUTSIDER, OWNER, SRA, VIEWER,
                            analysed_event, call, install_analysis, seed_doc, seed_project)
from shared.config import settings
from shared.govern import views

T = "northfield"            # one workspace shared by every caller in the views tests


def config(**over):
    base = {"base": "all", "filters": {"match": "all", "rules": [{"field": "stage", "op": "in", "value": ["review", "approval"]}]},
            "columns": {"order": ["title", "stage"], "hidden": ["college"]}, "groupBy": "stage",
            "sort": {"key": "daysInStage", "dir": "desc"}, "density": "compact"}
    return {**base, **over}


def new_view(sub=OWNER, name="My review queue", visibility="private", shared_with=None, module="contracts", **kw):
    body = {"module": module, "name": name, "visibility": visibility, "config": kw.pop("cfg", config())}
    if shared_with is not None:
        body["sharedWith"] = shared_with
    status, out = call("POST", "/views", sub, body, tenant=T, **kw)
    assert status == 201, out
    return out["view"]


def listed(sub, module="contracts", **kw):
    status, out = call("GET", "/views", sub, qs={"module": module}, tenant=T, **kw)
    assert status == 200, out
    return [v["name"] for v in out["views"]]


# ---------------------------------------------------------------------------
# Saved views
# ---------------------------------------------------------------------------


def test_view_crud_round_trip(gov):
    v = new_view(name="  My review queue  ")
    assert len(v["id"]) == 32 and v["module"] == "contracts" and v["name"] == "My review queue"
    assert v["createdBy"] == DANA and v["canEdit"] is True and v["sharedWith"] == []
    assert v["config"] == config() and v["createdAt"] == v["updatedAt"]
    item = gov.config.items[(f"T#{T}#VIEWS", f"V#contracts#{v['id']}")]
    assert item["rev"] == 1 and item["createdBySub"] == OWNER

    status, out = call("PATCH", f"/views/{v['id']}", OWNER, {"name": "Renamed", "config": {"density": None}}, tenant=T)
    assert status == 200 and out["view"]["name"] == "Renamed"
    assert out["view"]["config"] == {"base": None, "filters": {"match": "all", "rules": []},
                                     "columns": {"order": [], "hidden": []}, "groupBy": None, "sort": None,
                                     "density": None}
    assert gov.config.items[(f"T#{T}#VIEWS", f"V#contracts#{v['id']}")]["rev"] == 2

    assert listed(OWNER) == ["Renamed"] and listed(OWNER, "obligations") == []
    status, out = call("DELETE", f"/views/{v['id']}", OWNER, tenant=T)
    assert (status, out) == (200, {"deleted": True, "id": v["id"]})
    assert listed(OWNER) == []
    assert call("DELETE", f"/views/{v['id']}", OWNER, tenant=T)[0] == 404


def test_view_visibility_matrix(gov):
    mine = new_view(OWNER, "b private")
    everyone = new_view(OWNER, "c everyone", "everyone")
    specific = new_view(OWNER, "a for avery", "specific", [EMAIL[VIEWER].upper()])
    eli = new_view(EDITOR, "z eli's own")
    assert specific["sharedWith"] == [EMAIL[VIEWER]]

    assert listed(OWNER) == ["a for avery", "b private", "c everyone"]        # own only: eli's is private
    assert listed(EDITOR) == ["z eli's own", "c everyone"]                    # own first, then by name
    assert listed(VIEWER) == ["a for avery", "c everyone"]
    assert listed(VIEWER, groups=["govern-leader"]) == ["a for avery", "c everyone"]
    out = call("GET", "/views", VIEWER, qs={"module": "contracts"}, tenant=T)[1]["views"]
    assert [v["canEdit"] for v in out] == [False, False]
    # Another workspace sees none of them, even an "everyone" view.
    assert call("GET", "/views", OUTSIDER, qs={"module": "contracts"})[1]["views"] == []

    # Visible but not the creator → 403; invisible → 404 (cannot be probed).
    for method, body in (("PATCH", {"name": "x"}), ("DELETE", None)):
        status, out = call(method, f"/views/{everyone['id']}", EDITOR, body, tenant=T)
        assert (status, out["code"]) == (403, "forbidden")
        status, out = call(method, f"/views/{specific['id']}", VIEWER, body, tenant=T)
        assert (status, out["code"]) == (403, "forbidden")
        for vid in (mine["id"], specific["id"]):
            status, out = call(method, f"/views/{vid}", EDITOR, body, tenant=T)
            assert (status, out["code"]) == (404, "not_found")
        assert call(method, f"/views/{everyone['id']}", OUTSIDER, body)[0] == 404
        assert call(method, "/views/not-a-view", OWNER, body, tenant=T)[0] == 404
    assert listed(OWNER) == ["a for avery", "b private", "c everyone"]       # nothing changed
    assert call("DELETE", f"/views/{eli['id']}", EDITOR, tenant=T)[0] == 200

    # Narrowing a view hides it again; sharedWith is dropped unless "specific".
    status, out = call("PATCH", f"/views/{specific['id']}", OWNER, {"visibility": "private"}, tenant=T)
    assert status == 200 and out["view"]["sharedWith"] == []
    assert listed(VIEWER) == ["c everyone"]
    status, out = call("PATCH", f"/views/{specific['id']}", OWNER, {"visibility": "specific"}, tenant=T)
    assert (status, out["code"]) == (400, "bad_request")                       # needs sharedWith again


@pytest.mark.parametrize("body, needle", [
    ({"module": "deals"}, "module"),
    ({"name": "   "}, "name"),
    ({"name": "x" * 81}, "name"),
    ({"visibility": "team"}, "visibility"),
    ({"visibility": "specific"}, "sharedWith"),
    ({"visibility": "specific", "sharedWith": []}, "sharedWith"),
    ({"visibility": "specific", "sharedWith": ["not-an-email"]}, "sharedWith[0]"),
    ({"visibility": "specific", "sharedWith": [f"p{i}@x.edu" for i in range(51)]}, "sharedWith"),
    ({"colour": "red"}, "Unknown"),
    ({"config": None}, "config"),
    ({"config": "all"}, "config"),
    ({"config": config(extra=1)}, "Unknown"),
    ({"config": config(filters={"match": "some", "rules": []})}, "match"),
    ({"config": config(filters={"match": "all", "rules": [{"field": "stage", "op": "eq", "value": 1}] * 26})}, "rules"),
    ({"config": config(filters={"rules": [{"field": "stage", "op": "eq", "value": 1, "x": 2}]})}, "Unknown"),
    ({"config": config(filters={"rules": [{"field": "bad field!", "op": "eq"}]})}, "field"),
    ({"config": config(filters={"rules": [{"field": "a", "op": "o" * 65}]})}, "op"),
    ({"config": config(filters={"rules": [{"field": "a", "op": "eq", "value": {"a": 1}}]})}, "value"),
    ({"config": config(filters={"rules": [{"field": "a", "op": "eq", "value": "x" * 201}]})}, "value"),
    ({"config": config(filters={"rules": [{"field": "a", "op": "eq", "value": list(range(51))}]})}, "value"),
    ({"config": config(filters={"rules": [{"field": "a", "op": "eq", "value": [[1]]}]})}, "value[0]"),
    ({"config": config(columns={"order": [f"c{i}" for i in range(41)], "hidden": []})}, "order"),
    ({"config": config(columns={"order": [], "hidden": [7]})}, "hidden[0]"),
    ({"config": config(sort={"key": "title", "dir": "up"})}, "dir"),
    ({"config": config(sort={"key": "title"})}, "dir"),
    ({"config": config(density="cozy")}, "density"),
    ({"config": config(groupBy=3)}, "groupBy"),
    ({"config": config(base="x" * 300 * 60)}, "too large"),
])
def test_view_validation(gov, body, needle):
    full = {"module": "contracts", "name": "Queue", "visibility": "private", "config": config(), **body}
    full = {k: v for k, v in full.items() if not (k == "config" and v is None)}
    status, out = call("POST", "/views", OWNER, full, tenant=T)
    assert (status, out["code"]) == (400, "bad_request"), out
    assert needle in out["error"], out["error"]
    assert listed(OWNER) == []


def test_view_queries_and_patches_are_validated(gov):
    assert call("GET", "/views", OWNER, tenant=T)[0] == 400
    assert call("GET", "/views", OWNER, qs={"module": "deals"}, tenant=T)[0] == 400
    v = new_view()
    for body in ({}, {"module": "obligations"}, {"name": ""}, {"config": config(sort={"key": "x", "dir": 1})},
                 {"visibility": "specific", "sharedWith": ["nope"]}):
        status, out = call("PATCH", f"/views/{v['id']}", OWNER, body, tenant=T)
        assert (status, out["code"]) == (400, "bad_request"), body
    assert listed(OWNER) == ["My review queue"]
    status, out = call("PATCH", f"/views/{v['id']}", OWNER,
                       {"visibility": "specific", "sharedWith": [EMAIL[EDITOR], EMAIL[EDITOR].title()]}, tenant=T)
    assert status == 200 and out["view"]["sharedWith"] == [EMAIL[EDITOR]]
    assert listed(EDITOR) == ["My review queue"]


def test_views_per_creator_limit(gov, monkeypatch):
    monkeypatch.setattr(views, "MAX_VIEWS_PER_MODULE", 3)
    for i in range(3):
        new_view(name=f"v{i}")
    status, out = call("POST", "/views", OWNER, {"module": "contracts", "name": "v4", "visibility": "private",
                                                 "config": {}}, tenant=T)
    assert (status, out["code"]) == (400, "limit")
    new_view(name="other module", module="obligations")                 # per module
    new_view(EDITOR, name="other creator")                               # per creator
    assert len(listed(OWNER)) == 3


def test_leaders_and_reviewers_may_save_views(gov):
    assert new_view(VIEWER, groups=["govern-leader"])["canEdit"] is True
    assert new_view(EDITOR, groups=["govern-reviewer"])["canEdit"] is True


# ---------------------------------------------------------------------------
# Bulk update
# ---------------------------------------------------------------------------


@pytest.fixture
def portfolio(gov, ddb, monkeypatch):
    """lic-1 in OWNER's project (EDITOR edits, VIEWER views); lic-2 OWNER's
    alone; sra-1 in a second project where EDITOR is only a viewer."""
    install_analysis(monkeypatch, ddb)
    monkeypatch.setattr(settings, "raw_bucket", "raw")
    seed_doc(ddb, "lic-1", project_ids=["proj_osu"])
    seed_doc(ddb, "lic-2")
    seed_doc(ddb, "sra-1", sample=SRA, doc_type="OTHER", project_ids=["proj_b"],
             parties=["Northfield University", "Midwest Advanced Materials Corp."])
    seed_project(ddb, {EDITOR: "editor", VIEWER: "viewer"}, ["lic-1"])
    seed_project(ddb, {EDITOR: "viewer"}, ["sra-1"], project_id="proj_b")
    for doc_id in ("lic-1", "lic-2", "sra-1"):
        assert intake.handle_event(analysed_event(doc_id)) == "created"
    return gov


def bulk(sub, body, **kw):
    return call("POST", "/contracts/bulk", sub, body, **kw)


def test_bulk_reports_each_contract_without_leaking(portfolio):
    gov = portfolio
    status, out = bulk(EDITOR, {"contractIds": ["lic-1", "lic-2", "sra-1", "nope", "bad id!"],
                                "fields": {"department": "Chemistry", "expectedValue": 1000}})
    assert status == 200
    assert out["updated"] == 1
    codes = {r["contractId"]: (r["ok"], (r.get("error") or {}).get("code")) for r in out["results"]}
    assert codes == {"lic-1": (True, None), "lic-2": (False, "not_found"), "sra-1": (False, "forbidden"),
                     "nope": (False, "not_found"), "bad id!": (False, "not_found")}
    assert [r["contractId"] for r in out["results"]] == ["lic-1", "lic-2", "sra-1", "nope", "bad id!"]
    assert all(r["error"]["message"] for r in out["results"] if not r["ok"])
    assert gov.contract("lic-1")["department"] == "Chemistry" and gov.contract("lic-1")["expectedValue"] == 1000
    assert gov.contract("lic-2").get("department") != "Chemistry"
    assert gov.contract("sra-1").get("department") != "Chemistry"
    assert "field_updated" in gov.actions("lic-1") and "field_updated" not in gov.actions("lic-2")


def test_bulk_bad_body_changes_nothing(portfolio):
    gov = portfolio
    before = {cid: gov.actions(cid) for cid in ("lic-1", "lic-2")}
    for body in ({"contractIds": ["lic-1", "lic-2"], "fields": {"colour": "red"}},
                 {"contractIds": ["lic-1", "lic-2"], "fields": {"department": "X", "expectedValue": -5}},
                 {"contractIds": ["lic-1"], "owner": {"email": "not-an-email"}},
                 {"contractIds": ["lic-1"]},
                 {"contractIds": ["lic-1"], "fields": {}},
                 {"contractIds": [], "fields": {"department": "X"}},
                 {"contractIds": ["lic-1", "lic-1"], "fields": {"department": "X"}},
                 {"contractIds": [f"c{i}" for i in range(201)], "fields": {"department": "X"}},
                 {"contractIds": "lic-1", "fields": {"department": "X"}},
                 {"contractIds": [1], "fields": {"department": "X"}},
                 {"contractIds": ["lic-1"], "fields": ["department"]},
                 {"contractIds": ["lic-1"], "fields": {"department": "X"}, "stage": "signed"}):
        status, out = bulk(OWNER, body)
        assert (status, out["code"]) == (400, "bad_request"), body
    assert {cid: gov.actions(cid) for cid in ("lic-1", "lic-2")} == before
    assert gov.contract("lic-1").get("department") != "X"


def test_bulk_owner_assigns_through_the_assign_action(portfolio):
    gov = portfolio
    call("POST", "/contracts/lic-2/actions", OWNER, {"action": "reject", "reasonCode": "duplicate"})
    eli = {"email": EMAIL[EDITOR].upper(), "name": "Eli Park"}
    status, out = bulk(OWNER, {"contractIds": ["lic-1", "lic-2", "sra-1"], "owner": eli,
                               "fields": {"college": "College of Science"}})
    assert status == 200 and out["updated"] == 2
    by_id = {r["contractId"]: r for r in out["results"]}
    assert by_id["lic-2"]["error"]["code"] == "invalid_transition"
    for cid in ("lic-1", "sra-1"):
        c = gov.contract(cid)
        assert c["owner"] == {"email": EMAIL[EDITOR], "name": "Eli Park"} and c["college"] == "College of Science"
        assert c["state"] == "in_review" and "assigned" in gov.actions(cid)
    # The rejected contract is left exactly as it was: no fields, no owner.
    assert gov.contract("lic-2").get("college") != "College of Science" and not gov.contract("lic-2").get("owner")
    # Assigning the same owner again changes nothing and still succeeds.
    status, out = bulk(OWNER, {"contractIds": ["lic-1"], "owner": {"email": EMAIL[EDITOR], "name": "Eli Park"}})
    assert out["updated"] == 1 and gov.actions("lic-1").count("assigned") == 1


def test_bulk_leaders_are_read_only(portfolio):
    gov = portfolio
    status, out = bulk(OWNER, {"contractIds": ["lic-1", "nope"], "owner": DANA}, groups=["govern-leader"])
    assert status == 200 and out["updated"] == 0
    assert [r["error"]["code"] for r in out["results"]] == ["forbidden", "not_found"]
    assert not gov.contract("lic-1").get("owner")


# ---------------------------------------------------------------------------
# People directory
# ---------------------------------------------------------------------------


def test_people_is_the_union_of_projects_reviewers_and_owners(portfolio):
    call("PUT", "/workflow/settings", OWNER, {"reviewers": [
        {"email": EMAIL[VIEWER].upper(), "name": "Avery Chen", "offices": [], "agreementTypes": []},
        {"email": "rita@northfield.edu", "name": "Rita Ortiz", "offices": [], "agreementTypes": []}]})
    call("POST", "/contracts/lic-2/actions", OWNER, {"action": "assign",
                                                     "owner": {"email": "sam@northfield.edu", "name": "Sam Lee"}})
    call("POST", "/contracts/lic-1/actions", OWNER, {"action": "assign", "owner": DANA})
    status, out = call("GET", "/govern/people", OWNER)
    assert status == 200
    assert out["people"] == [
        {"email": EMAIL[VIEWER], "name": "Avery Chen", "sources": ["project", "reviewer"]},
        {"email": EMAIL[OWNER], "name": "Dana Ruiz", "sources": ["project", "owner"]},
        {"email": "rita@northfield.edu", "name": "Rita Ortiz", "sources": ["reviewer"]},
        {"email": "sam@northfield.edu", "name": "Sam Lee", "sources": ["owner"]},
        {"email": EMAIL[EDITOR], "name": None, "sources": ["project"]},
    ]
    # EDITOR sees the people of both their projects and of the contracts they
    # can see (not lic-2's owner), and themself by name; not OWNER's reviewers.
    people = {p["email"]: p for p in call("GET", "/govern/people", EDITOR)[1]["people"]}
    assert set(people) == {EMAIL[OWNER], EMAIL[EDITOR], EMAIL[VIEWER]}
    assert people[EMAIL[EDITOR]] == {"email": EMAIL[EDITOR], "name": "Eli Park", "sources": ["project"]}
    assert people[EMAIL[OWNER]]["sources"] == ["project", "owner"]


def test_people_never_shows_another_workspace(portfolio):
    call("PUT", "/workflow/settings", OWNER, {"reviewers": [
        {"email": "rita@northfield.edu", "name": "Rita Ortiz", "offices": [], "agreementTypes": []}]})
    status, out = call("GET", "/govern/people", OUTSIDER)
    assert status == 200
    assert out["people"] == [{"email": EMAIL[OUTSIDER], "name": "Out Sider", "sources": []}]
