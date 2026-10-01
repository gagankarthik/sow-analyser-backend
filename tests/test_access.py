"""Access control — a user sees a project only as owner/member, and a document
only if they uploaded it or it is in a project they can see.

Runs against the real handlers and the real key schema on an in-memory table
(tests/fakes.py). S3, OpenSearch, Cognito and OpenAI are faked; nothing here
reaches a network.
"""
from __future__ import annotations

import json

import pytest

import migrate_access
from api import handler as api
from rag import handler as rag
from shared import access, dynamodb, opensearch
from shared.access import Caller

OWNER, EDITOR, VIEWER, OUTSIDER = (
    "aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002",
    "aaaaaaaa-0000-4000-8000-000000000003", "aaaaaaaa-0000-4000-8000-000000000004",
)
EMAIL = {OWNER: "owner@acme.com", EDITOR: "editor@acme.com", VIEWER: "viewer@acme.com",
         OUTSIDER: "outsider@else.com"}
ROLE_OF = {"owner": OWNER, "editor": EDITOR, "viewer": VIEWER, "outsider": OUTSIDER}
PROJ = "proj_1"
D1 = "d1d1d1d1-0000-4000-8000-000000000001"          # owner's document, filed in PROJ
D_OUT = "d2d2d2d2-0000-4000-8000-000000000002"       # the outsider's private document


def caller(sub: str, verified: bool = True, email: str | None = None) -> Caller:
    return Caller.from_claims({"sub": sub, "email": email or EMAIL.get(sub),
                               "email_verified": "true" if verified else "false"})


def seed_doc(ddb, doc_id: str, owner_sub: str, **extra) -> None:
    ddb.items[(f"DOC#{doc_id}", "META")] = {
        "PK": f"DOC#{doc_id}", "SK": "META", "GSI1PK": f"TENANT#u-{owner_sub}", "GSI1SK": f"DOC#{doc_id}",
        "entityType": "DOCUMENT", "docId": doc_id, "tenantId": f"u-{owner_sub}", "ownerSub": owner_sub,
        "ownerEmail": EMAIL.get(owner_sub), "title": f"Contract {doc_id[:2]}", "status": "READY",
        "rawKey": f"tenants/u-{owner_sub}/uploads/{doc_id}/c.pdf", "updatedAt": "2020-01-01T00:00:00+00:00",
        "projectIds": [], **extra,
    }


def call(method: str, path: str, who: Caller, body=None, qs=None):
    ev = {"rawPath": path, "queryStringParameters": qs,
          "body": json.dumps(body) if body is not None else None}
    resp = api._route(method, path, ev, who)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def world(monkeypatch, ddb):
    """A project owned by OWNER holding D1, with an editor and a viewer; and an
    outsider with a private document of their own."""
    monkeypatch.setenv("RAW_BUCKET", "raw")
    monkeypatch.setenv("PROCESSED_BUCKET", "processed")
    monkeypatch.setattr(api.settings, "cognito_user_pool_id", "")       # no Cognito calls
    artefacts = {"classificationKey": {"clauses": []}, "diffKey": {"changes": []},
                 "timelineKey": {"initialState": {"1": {"body": "x"}}, "currentState": {}, "futureState": None,
                                 "amendmentChain": [], "keyDates": []}}
    monkeypatch.setattr(api, "query_doc_versions", lambda _id: [
        {"versionNumber": 1, **{k: k for k in artefacts}}, {"versionNumber": 2, **{k: k for k in artefacts}}])
    from shared import s3 as shared_s3
    monkeypatch.setattr(shared_s3, "get_json", lambda bucket, key: artefacts[key])
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

    seed_doc(ddb, D1, OWNER)
    seed_doc(ddb, D_OUT, OUTSIDER)
    for role in ("editor", "viewer"):                       # one private document each
        seed_doc(ddb, f"own-{role}", ROLE_OF[role])
    seed_doc(ddb, "own-owner", OWNER)

    owner = caller(OWNER)
    assert call("PUT", f"/projects/{PROJ}", owner, {"name": "Acme renewal"})[0] == 201
    assert call("PUT", f"/projects/{PROJ}/documents/{D1}", owner)[0] == 200
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": EMAIL[EDITOR], "role": "editor"})[0] == 201
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": EMAIL[VIEWER], "role": "viewer"})[0] == 201
    return {"ddb": ddb, "searches": searches}


# ── The matrix itself ────────────────────────────────────────────────────────


def test_permission_matrix_is_exactly_as_documented():
    owner, editor, viewer = (access.PERMISSIONS[r] for r in ("owner", "editor", "viewer"))
    assert owner == set(access.CAPABILITIES)
    assert editor == {"view", "upload", "edit", "reprocess", "manage_documents"}
    assert "share_document" in owner and "share_document" not in editor
    assert viewer == {"view"}
    for capability in access.CAPABILITIES:
        assert access.can("owner", capability)
        assert not access.can(None, capability)            # no role → nothing
        assert not access.can("stranger", capability)
    assert access.normalise_role("member") == "viewer"      # legacy role → least privilege
    assert access.normalise_role("ADMIN") == "viewer"


# (method, path template, body, query, capability needed, success status)
ROUTES = [
    ("GET",    "/documents/{doc}",                None, None, "view", 200),
    ("GET",    "/documents/{doc}/classification", None, None, "view", 200),
    ("GET",    "/documents/{doc}/diff",           None, None, "view", 200),
    ("GET",    "/documents/{doc}/timeline",       None, None, "view", 200),
    ("GET",    "/documents/{doc}/file",           None, None, "view", 200),
    ("GET",    "/documents/{doc}/similar",        None, {"clause": "1"}, "view", 200),
    ("PATCH",  "/documents/{doc}",                {"title": "Renamed"}, None, "edit", 200),
    ("POST",   "/documents/{doc}/reprocess",      None, None, "reprocess", 200),
    ("DELETE", "/documents/{doc}/versions/2",     None, None, "delete_document", 200),
    ("DELETE", "/documents/{doc}",                None, None, "delete_document", 200),
    ("GET",    "/projects/{proj}",                None, None, "view", 200),
    ("PUT",    "/projects/{proj}",                {"name": "Renamed"}, None, "rename_project", 200),
    ("PUT",    "/projects/{proj}/documents/{mine}", None, None, "manage_documents", 200),
    ("DELETE", "/projects/{proj}/documents/{doc}",  None, None, "manage_documents", 200),
    ("GET",    "/documents/upload-url",           None, {"filename": "a.pdf", "projectId": PROJ}, "upload", 200),
    ("POST",   "/projects/{proj}/invite",         {"email": "new@x.com", "role": "viewer"}, None, "invite", 201),
    ("PATCH",  "/projects/{proj}/members/{viewer_email}", {"role": "editor"}, None, "set_role", 200),
    ("DELETE", "/projects/{proj}/members/{viewer_email}", None, None, "remove_member", 200),
    ("DELETE", "/projects/{proj}",                None, None, "delete_project", 200),
]


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", "outsider"])
@pytest.mark.parametrize("method,template,body,qs,capability,ok", ROUTES,
                         ids=[f"{r[0]} {r[1]}" for r in ROUTES])
def test_every_route_enforces_the_matrix(world, role, method, template, body, qs, capability, ok):
    """Each cell: a non-member gets 404 (cannot even learn the id exists), a
    member whose role lacks the capability gets 403, the rest succeed."""
    who = caller(ROLE_OF[role])
    path = template.format(doc=D1, proj=PROJ, mine=f"own-{role}", viewer_email=EMAIL[VIEWER])
    if role == "viewer" and capability == "remove_member":
        # (a member may always remove THEMSELF — tested separately; aim at someone else)
        path = template.format(doc=D1, proj=PROJ, mine="", viewer_email=EMAIL[EDITOR])
    status, out = call(method, path, who, body, qs)
    if role == "outsider":
        assert status == 404 and out["code"] == "not_found"
    elif access.can(role, capability):
        assert status == ok, out
    else:
        assert status == 403 and out["code"] == "forbidden"
        # ...and nothing changed
        assert world["ddb"].doc(D1)["title"] == f"Contract {D1[:2]}"
        assert world["ddb"].items[(f"PROJ#{PROJ}", "META")]["name"] == "Acme renewal"


# ── Listing ──────────────────────────────────────────────────────────────────


def test_lists_only_show_what_the_caller_may_see(world):
    def docs(sub):
        _, out = call("GET", "/documents", caller(sub))
        return {d["docId"]: d["role"] for d in out["documents"]}

    assert docs(OWNER) == {D1: "owner", "own-owner": "owner"}
    assert docs(EDITOR) == {D1: "editor", "own-editor": "owner"}
    assert docs(VIEWER) == {D1: "viewer", "own-viewer": "owner"}
    assert docs(OUTSIDER) == {D_OUT: "owner"}

    def projects(sub):
        _, out = call("GET", "/projects", caller(sub))
        return {p["id"]: p["role"] for p in out["projects"]}

    assert projects(OWNER) == {PROJ: "owner"}
    assert projects(EDITOR) == {PROJ: "editor"} and projects(VIEWER) == {PROJ: "viewer"}
    assert projects(OUTSIDER) == {}


def test_document_list_carries_compact_key_dates_and_detail_carries_them_in_full(world):
    full = {"id": "kd-1", "kind": "term_end", "label": "Term ends", "date": "2027-03-01", "rawText": "x" * 150,
            "precision": "day", "isEstimated": False, "isDerived": True, "ambiguous": False, "anchor": "effective",
            "offsetValue": 12, "offsetUnit": "months", "offsetDays": 360, "recurring": None, "amount": None,
            "currency": None, "clauseId": "c003", "clauseNumber": "4", "sectionRef": "4 Term",
            "confidence": "medium", "issues": [], "origin": "extracted", "periodStart": None}
    world["ddb"].doc(D1)["keyDates"] = [full]
    listed = next(d for d in call("GET", "/documents", caller(VIEWER))[1]["documents"] if d["docId"] == D1)
    assert listed["keyDates"] == [{"id": "kd-1", "kind": "term_end", "label": "Term ends", "date": "2027-03-01",
                                   "precision": "day", "isEstimated": False, "isDerived": True, "ambiguous": False,
                                   "amount": None, "currency": None, "clauseNumber": "4", "confidence": "medium"}]
    assert call("GET", f"/documents/{D1}", caller(VIEWER))[1]["document"]["keyDates"] == [full]


def test_project_listing_keeps_the_shape_the_frontend_reads(world):
    _, out = call("GET", "/projects", caller(OWNER))
    p = out["projects"][0]
    assert {"id", "name", "createdAt", "docIds", "members", "ownerEmail", "role"} <= set(p)
    assert p["docIds"] == [D1] and p["ownerEmail"] == EMAIL[OWNER]
    assert {m["email"]: m["role"] for m in p["members"]} == {
        EMAIL[OWNER]: "owner", EMAIL[EDITOR]: "editor", EMAIL[VIEWER]: "viewer"}
    assert all({"email", "role", "status"} <= set(m) for m in p["members"])


def test_a_document_only_reports_projects_the_caller_can_see(world):
    owner = caller(OWNER)
    call("PUT", "/projects/proj_private", owner, {"name": "Private"})
    call("PUT", f"/projects/proj_private/documents/{D1}", owner)
    assert set(call("GET", f"/documents/{D1}", owner)[1]["document"]["projectIds"]) == {PROJ, "proj_private"}
    assert call("GET", f"/documents/{D1}", caller(EDITOR))[1]["document"]["projectIds"] == [PROJ]


# ── Membership changes take effect at once ───────────────────────────────────


def test_removing_a_member_removes_access_immediately(world):
    editor = caller(EDITOR)
    assert call("GET", f"/documents/{D1}", editor)[0] == 200
    assert call("DELETE", f"/projects/{PROJ}/members/{EMAIL[EDITOR]}", caller(OWNER))[0] == 200
    editor = caller(EDITOR)                         # the next request
    assert call("GET", f"/documents/{D1}", editor)[0] == 404
    assert call("GET", f"/projects/{PROJ}", editor)[0] == 404
    assert call("GET", "/documents", editor)[1]["documents"][0]["docId"] == "own-editor"


def test_removing_a_document_from_the_project_ends_members_access(world):
    assert call("DELETE", f"/projects/{PROJ}/documents/{D1}", caller(OWNER))[0] == 200
    assert call("GET", f"/documents/{D1}", caller(VIEWER))[0] == 404
    assert call("GET", f"/documents/{D1}", caller(OWNER))[0] == 200      # still the uploader's


def test_a_stale_project_hint_on_the_document_grants_nothing(world):
    """The document says it is in a project, the project does not list it: the
    project record decides."""
    world["ddb"].doc(D_OUT)["projectIds"] = [PROJ]
    assert call("GET", f"/documents/{D_OUT}", caller(EDITOR))[0] == 404


def test_a_member_can_leave_but_not_remove_others(world):
    viewer = caller(VIEWER)
    assert call("DELETE", f"/projects/{PROJ}/members/{EMAIL[EDITOR]}", viewer)[0] == 403
    assert call("DELETE", f"/projects/{PROJ}/members/{EMAIL[VIEWER]}", viewer)[0] == 200
    assert call("GET", f"/projects/{PROJ}", caller(VIEWER))[0] == 404


def test_role_change_applies_and_owner_is_protected(world):
    owner = caller(OWNER)
    assert call("PATCH", f"/projects/{PROJ}/members/{EMAIL[VIEWER]}", owner, {"role": "editor"})[0] == 200
    assert call("PATCH", f"/documents/{D1}", caller(VIEWER), {"title": "By the promoted viewer"})[0] == 200
    assert call("PATCH", f"/projects/{PROJ}/members/{EMAIL[VIEWER]}", owner, {"role": "owner"})[0] == 400
    assert call("PATCH", f"/projects/{PROJ}/members/{EMAIL[OWNER]}", owner, {"role": "viewer"})[0] == 400
    assert call("DELETE", f"/projects/{PROJ}/members/{EMAIL[OWNER]}", owner)[0] == 400
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": "x@y.com", "role": "owner"})[0] == 400


# ── Invitations ──────────────────────────────────────────────────────────────


def test_invite_to_an_unknown_email_grants_access_once_that_email_signs_in(world):
    new_sub, new_email = "bbbbbbbb-0000-4000-8000-000000000009", "Newcomer@Partner.com"
    status, out = call("POST", f"/projects/{PROJ}/invite", caller(OWNER), {"email": new_email, "role": "viewer"})
    assert status == 201 and out["member"] == {
        "email": "newcomer@partner.com", "role": "viewer", "status": "invited", "sub": None,
        "invitedAt": out["member"]["invitedAt"]}
    # Signed up, but the address is not verified yet → no access.
    assert call("GET", f"/documents/{D1}", caller(new_sub, verified=False, email=new_email))[0] == 404
    # Verified → access to this project and nothing else, in their own workspace.
    newcomer = caller(new_sub, email=new_email)
    assert newcomer.tenant_id == f"u-{new_sub}"
    assert call("GET", f"/documents/{D1}", newcomer)[0] == 200
    assert call("GET", f"/documents/own-owner", newcomer)[0] == 404
    assert call("PATCH", f"/documents/{D1}", newcomer, {"title": "x"})[0] == 403
    # First visit marks the invitation accepted.
    members = {m["email"]: m for m in call("GET", "/projects", caller(new_sub, email=new_email))[1]["projects"][0]["members"]}
    assert members["newcomer@partner.com"]["status"] == "active"
    assert members["newcomer@partner.com"]["sub"] == new_sub


def test_email_in_the_request_is_never_identity(world):
    """Claiming to be the owner in the body, a header or an unverified claim
    gets nowhere."""
    ev = {"rawPath": f"/documents/{D1}", "headers": {"x-user-email": EMAIL[OWNER], "x-tenant-id": f"u-{OWNER}"},
          "body": json.dumps({"email": EMAIL[OWNER], "ownerEmail": EMAIL[OWNER]}), "queryStringParameters": None}
    assert api._route("GET", ev["rawPath"], ev, caller(OUTSIDER))["statusCode"] == 404
    impostor = caller(OUTSIDER, verified=False, email=EMAIL[OWNER])
    assert impostor.email is None
    assert call("GET", f"/projects/{PROJ}", impostor)[0] == 404


def test_duplicate_invite_and_bad_bodies(world):
    owner = caller(OWNER)
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": EMAIL[EDITOR]})[0] == 409
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": EMAIL[OWNER]})[0] == 409
    assert call("POST", f"/projects/{PROJ}/invite", owner, {"email": "not-an-email"})[0] == 400
    assert call("POST", f"/projects/{PROJ}/invite", owner, [1, 2])[0] == 400
    assert call("POST", "/projects/proj_missing/invite", owner, {"email": "a@b.co"})[0] == 404


def test_invite_no_longer_writes_a_tenant_attribute(world, monkeypatch):
    """The pool has no custom attributes; membership alone grants access."""
    from shared import aws as shared_aws

    created = []

    class _Cognito:
        def admin_create_user(self, **kw):
            created.append(kw)
            return {"User": {"Attributes": [{"Name": "sub", "Value": "new-sub"}]}}
    monkeypatch.setattr(api.settings, "cognito_user_pool_id", "pool")
    monkeypatch.setattr(shared_aws, "cognito_idp_client", lambda: _Cognito())
    status, out = call("POST", f"/projects/{PROJ}/invite", caller(OWNER), {"email": "fresh@x.com"})
    assert status == 201 and out["invitationEmailSent"] is True
    assert {a["Name"] for a in created[0]["UserAttributes"]} == {"email", "email_verified"}


# ── Project writes ───────────────────────────────────────────────────────────


def test_project_id_cannot_be_taken_over(world):
    status, _ = call("PUT", f"/projects/{PROJ}", caller(OUTSIDER), {"name": "Mine now"})
    assert status == 404
    stored = world["ddb"].items[(f"PROJ#{PROJ}", "META")]
    assert stored["name"] == "Acme renewal" and stored["ownerSub"] == OWNER


def test_whole_list_save_cannot_touch_other_users_projects(world):
    ddb = world["ddb"]
    before = json.dumps(ddb.items[(f"PROJ#{PROJ}", "META")], sort_keys=True, default=str)
    members_before = ddb.keys_with_prefix(f"PROJ#{PROJ}")
    # An outsider sends the victim's project id with a new name, an empty document
    # list, themself as owner and member — and omits nothing of their own.
    status, out = call("POST", "/projects", caller(OUTSIDER), {"projects": [
        {"id": PROJ, "name": "Hacked", "docIds": [D_OUT], "ownerEmail": EMAIL[OUTSIDER],
         "members": [{"email": EMAIL[OUTSIDER], "role": "owner"}]},
        {"id": "proj_out", "name": "Outsider's own", "docIds": [D_OUT, D1]},
    ]})
    assert status == 200 and out["ignored"] == [PROJ]
    assert json.dumps(ddb.items[(f"PROJ#{PROJ}", "META")], sort_keys=True, default=str) == before
    assert ddb.keys_with_prefix(f"PROJ#{PROJ}") == members_before
    # Their own new project was created — owned by them, holding only THEIR document.
    mine = ddb.items[("PROJ#proj_out", "META")]
    assert mine["ownerSub"] == OUTSIDER and mine["docIds"] == [D_OUT]
    assert call("GET", f"/documents/{D1}", caller(OUTSIDER))[0] == 404
    # An empty save (stale client) deletes none of anyone else's projects.
    assert call("POST", "/projects", caller(VIEWER), {"projects": []})[0] == 200
    assert (f"PROJ#{PROJ}", "META") in ddb.items


def test_whole_list_save_respects_roles(world):
    ddb = world["ddb"]
    # Viewer: nothing in their save is applied.
    call("POST", "/projects", caller(VIEWER), {"projects": [{"id": PROJ, "name": "V", "docIds": []}]})
    assert ddb.items[(f"PROJ#{PROJ}", "META")]["name"] == "Acme renewal"
    assert ddb.items[(f"PROJ#{PROJ}", "META")]["docIds"] == [D1]
    # Editor: may file their own document, may not rename, may not file one they cannot edit.
    call("POST", "/projects", caller(EDITOR), {"projects": [
        {"id": PROJ, "name": "E", "docIds": [D1, "own-editor", D_OUT]}]})
    stored = ddb.items[(f"PROJ#{PROJ}", "META")]
    assert stored["name"] == "Acme renewal" and stored["docIds"] == [D1, "own-editor"]
    assert call("GET", "/documents/own-editor", caller(VIEWER))[0] == 200     # now shared
    # Owner: rename works; omitting the project deletes it — and only the project.
    call("POST", "/projects", caller(OWNER), {"projects": [{"id": PROJ, "name": "Renamed", "docIds": [D1]}]})
    assert ddb.items[(f"PROJ#{PROJ}", "META")]["name"] == "Renamed"
    assert call("GET", "/documents/own-editor", caller(VIEWER))[0] == 404     # unfiled again
    call("POST", "/projects", caller(OWNER), {"projects": []})
    assert ddb.keys_with_prefix(f"PROJ#{PROJ}") == []
    assert ddb.doc(D1) is not None and ddb.doc(D1)["projectIds"] == []       # documents are kept
    assert call("GET", f"/documents/{D1}", caller(EDITOR))[0] == 404


def test_filing_a_document_needs_rights_on_the_document_too(world):
    """An editor cannot share someone else's private document by filing it."""
    assert call("PUT", f"/projects/{PROJ}/documents/{D_OUT}", caller(EDITOR))[0] == 404
    assert world["ddb"].items[(f"PROJ#{PROJ}", "META")]["docIds"] == [D1]


def test_an_editor_cannot_pass_on_a_document_they_were_only_given_access_to(world):
    """D1 was shared WITH the editor through PROJ. They may read and edit it, but
    not file it into a project of their own and so share it with other people."""
    editor = caller(EDITOR)
    assert call("PUT", "/projects/proj_editors_own", editor, {"name": "Side project"})[0] == 201
    status, out = call("PUT", f"/projects/proj_editors_own/documents/{D1}", editor)
    assert status == 403 and out["code"] == "forbidden"
    call("POST", "/projects", editor, {"projects": [
        {"id": PROJ, "name": "x", "docIds": [D1]},
        {"id": "proj_editors_own", "name": "Side project", "docIds": [D1, "own-editor"]}]})
    assert world["ddb"].items[("PROJ#proj_editors_own", "META")]["docIds"] == ["own-editor"]
    # the uploader can
    assert call("PUT", "/projects/proj_owner2", caller(OWNER), {"name": "Second"})[0] == 201
    assert call("PUT", f"/projects/proj_owner2/documents/{D1}", caller(OWNER))[0] == 200


def test_upload_into_a_project_files_and_owns_the_document(world):
    status, out = call("GET", "/documents/upload-url", caller(EDITOR),
                       qs={"filename": "amendment.pdf", "projectId": PROJ})
    assert status == 200 and out["projectId"] == PROJ
    meta = world["ddb"].doc(out["docId"])
    assert meta["ownerSub"] == EDITOR and meta["tenantId"] == f"u-{EDITOR}"
    assert out["docId"] in world["ddb"].items[(f"PROJ#{PROJ}", "META")]["docIds"]
    assert call("GET", f"/documents/{out['docId']}", caller(VIEWER))[0] == 200
    assert call("GET", "/documents/upload-url", caller(OUTSIDER),
                qs={"filename": "x.pdf", "projectId": PROJ})[0] == 404


def test_deleting_a_document_unfiles_it(world):
    assert call("DELETE", f"/documents/{D1}", caller(OWNER))[0] == 200
    assert world["ddb"].items[(f"PROJ#{PROJ}", "META")]["docIds"] == []


def test_concurrent_project_edits_do_not_overwrite_each_other(world, monkeypatch):
    """Optimistic locking: a write based on a stale read is retried on fresh data."""
    ddb = world["ddb"]
    real_put = ddb.put_item
    raced = {"done": False}

    def racing_put(Item, **kw):
        if Item.get("SK") == "META" and Item.get("PK") == f"PROJ#{PROJ}" and not raced["done"]:
            raced["done"] = True                    # someone else files a document first
            other = dict(ddb.items[(f"PROJ#{PROJ}", "META")])
            other["docIds"] = other["docIds"] + ["own-owner"]
            other["rev"] += 1
            ddb.items[(f"PROJ#{PROJ}", "META")] = other
        return real_put(Item=Item, **kw)

    monkeypatch.setattr(ddb, "put_item", racing_put)
    assert call("PUT", f"/projects/{PROJ}/documents/own-editor", caller(EDITOR))[0] == 200
    assert ddb.items[(f"PROJ#{PROJ}", "META")]["docIds"] == [D1, "own-owner", "own-editor"]


# ── Derived artefacts must not leak another document's text ─────────────────


def test_diff_is_withheld_when_the_parent_is_not_visible(world):
    world["ddb"].doc(D1)["parentDocId"] = D_OUT          # parent belongs to the outsider
    status, out = call("GET", f"/documents/{D1}/diff", caller(EDITOR))
    assert status == 404 and out["code"] == "not_available"


def test_timeline_is_restricted_when_part_of_the_chain_is_not_visible(world):
    world["ddb"].doc(D1)["parentDocId"] = D_OUT
    status, out = call("GET", f"/documents/{D1}/timeline", caller(EDITOR))
    assert status == 200 and out["restricted"] is True
    assert out["initialState"] == {} and out["currentState"] == {}
    world["ddb"].doc(D1)["parentDocId"] = None
    assert "restricted" not in call("GET", f"/documents/{D1}/timeline", caller(EDITOR))[1]


# ── Search and chat are scoped inside the query ─────────────────────────────


def test_similar_clause_search_is_limited_to_visible_documents(world):
    call("GET", f"/documents/{D1}/similar", caller(EDITOR), qs={"clause": "1"})
    search = world["searches"][-1]
    assert set(search["doc_ids"]) == {D1, "own-editor"}
    assert "tenant_id" not in search


def test_scope_is_applied_as_a_query_filter_never_afterwards(monkeypatch):
    sent = []

    class _OS:
        def search(self, index, body):
            sent.append(body)
            return {"hits": {"hits": []}}
    monkeypatch.setattr(opensearch, "client", lambda: _OS())
    opensearch.clause_search(text="fees", vector=[0.0], doc_ids=[D1, "own-editor"], k=5)
    assert len(sent) == 2
    for body in sent:
        text = json.dumps(body)
        assert '"terms": {"docId": ["%s", "own-editor"]}' % D1 in text
        assert '"filter"' in text
    # Nothing permitted → nothing is sent to the cluster at all.
    sent.clear()
    assert opensearch.clause_search(text="fees", vector=[0.0], doc_ids=[], k=5) == []
    assert opensearch.knn_search(vector=[0.0], doc_ids=[]) == []
    assert opensearch.bm25_search(text="x") == []           # an unscoped search is refused
    assert sent == []


@pytest.fixture
def chat(monkeypatch, world):
    seen = {"search": [], "chat": 0}
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [[0.0]])

    def search(**kw):
        seen["search"].append(kw)
        return [{"_id": f"{D1}::4", "_source": {"docId": D1, "clauseNumber": "4", "category": "Fees",
                                               "title": "Fees", "text": "The fee is $7,500."}}]

    def answer(**kw):
        seen["chat"] += 1
        return "The fee is $7,500 [§4]."
    monkeypatch.setattr(rag, "clause_search", search)
    monkeypatch.setattr(rag, "chat_text", answer)
    return seen


def _ask(target: str, sub: str, verified: bool = True):
    ev = {"rawPath": f"/documents/{target}/chat", "pathParameters": {"docId": target},
          "body": json.dumps({"question": "What is the fee?", "docIds": [D1], "tenantId": f"u-{OWNER}"}),
          "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {
              "sub": sub, "email": EMAIL[sub], "email_verified": "true" if verified else "false"}}}}}
    resp = rag._http_handle(ev)
    return resp["statusCode"], json.loads(resp["body"])


def test_chat_never_returns_another_users_clauses(chat):
    status, out = _ask(D1, OUTSIDER)
    assert status == 200 and out["citations"] == [] and out["grounded"] is False
    assert chat["search"] == [] and chat["chat"] == 0          # nothing searched, nothing asked
    # A project id the caller is not in behaves identically.
    assert _ask(PROJ, OUTSIDER)[1]["citations"] == []
    assert chat["search"] == []


def test_chat_works_for_members_and_is_scoped_to_what_they_asked_about(chat):
    status, out = _ask(D1, VIEWER)
    assert status == 200 and out["citations"][0]["docId"] == D1
    assert chat["search"][-1]["doc_id"] == D1 and chat["search"][-1]["tenant_id"] == f"u-{OWNER}"
    # Asking about the project searches exactly the project's documents.
    _ask(PROJ, VIEWER)
    assert chat["search"][-1] == {"text": "What is the fee?", "vector": [0.0], "k": 16, "doc_ids": [D1]}
    # A removed member is refused on their very next question.
    call("DELETE", f"/projects/{PROJ}/members/{EMAIL[VIEWER]}", caller(OWNER))
    before = len(chat["search"])
    assert _ask(D1, VIEWER)[1]["citations"] == []
    assert len(chat["search"]) == before


def test_appsync_search_passes_only_the_callers_visible_documents(chat, monkeypatch):
    monkeypatch.setattr(rag, "_push_token", lambda *a, **k: None)

    class _Stream(list):
        pass
    monkeypatch.setattr(rag, "openai_client", lambda: type("C", (), {"chat": type("X", (), {
        "completions": type("Y", (), {"create": staticmethod(lambda **kw: _Stream())})})})())
    ev = {"arguments": {"input": {"question": "fees?", "docIds": [D_OUT], "tenantId": f"u-{OUTSIDER}"}},
          "identity": {"claims": {"sub": EDITOR, "email": EMAIL[EDITOR], "email_verified": True}}}
    rag._handle(ev)
    assert set(chat["search"][-1]["doc_ids"]) == {D1, "own-editor"}
    # A user with nothing visible is answered without any search.
    chat["search"].clear()
    nobody = {"arguments": {"input": {"question": "fees?"}},
              "identity": {"claims": {"sub": "cccccccc-0000-4000-8000-00000000000c"}}}
    assert "couldn't find" in rag._handle(nobody)["answer"]
    assert chat["search"] == []


def test_pipeline_parent_search_reaches_shared_project_documents_only(world):
    assert dynamodb.related_doc_ids(D1) == []
    call("PUT", f"/projects/{PROJ}/documents/own-editor", caller(EDITOR))
    assert dynamodb.related_doc_ids("own-editor") == [D1]
    assert dynamodb.related_doc_ids(D_OUT) == []


# ── Migration of the legacy shared tenant ───────────────────────────────────


@pytest.fixture
def legacy(ddb):
    """The pre-migration world: everything under the shared "default" tenant,
    projects in one blob with client-supplied ownerEmail / members, documents
    with no owner."""
    for doc_id in ("L1", "L2", "L3"):
        ddb.items[(f"DOC#{doc_id}", "META")] = {
            "PK": f"DOC#{doc_id}", "SK": "META", "GSI1PK": "TENANT#default", "GSI1SK": f"DOC#{doc_id}",
            "docId": doc_id, "tenantId": "default", "title": f"Legacy {doc_id}", "status": "READY",
            "rawKey": f"tenants/default/uploads/{doc_id}/c.pdf",
        }
    ddb.items[("TENANT#default", "PROJECTS")] = {
        "PK": "TENANT#default", "SK": "PROJECTS", "projects": json.dumps([
            {"id": "proj_a", "name": "Alpha", "createdAt": "2025-01-01", "docIds": ["L1", "GONE"],
             "ownerEmail": "Owner@Acme.com",
             "members": [{"email": EMAIL[EDITOR], "role": "member", "status": "invited"}]},
            {"id": "proj_b", "name": "Beta", "createdAt": "2025-02-01", "docIds": ["L2"],
             "ownerEmail": "nobody-yet@acme.com", "members": []},
            {"id": "proj_c", "name": "No owner", "createdAt": "2025-03-01", "docIds": []},
        ])}
    subs = {EMAIL[OWNER]: OWNER, EMAIL[EDITOR]: EDITOR, EMAIL[OUTSIDER]: OUTSIDER}
    return ddb, subs.get


def test_migration_dry_run_reports_and_writes_nothing(legacy):
    ddb, resolve = legacy
    before = json.dumps(sorted(map(str, ddb.items.items())))
    plan = migrate_access.build_plan(ddb, ["default"], resolve)
    assert json.dumps(sorted(map(str, ddb.items.items()))) == before
    assert not any(op in ("put_item", "update_item", "delete_item") for op, _ in ddb.calls)
    assert [(p["projectId"], p["ownerEmail"], p["ownerSub"]) for p in plan["projects"]] == [
        ("proj_a", EMAIL[OWNER], OWNER), ("proj_b", "nobody-yet@acme.com", None)]
    assert plan["projects"][0]["docIds"] == ["L1"] and plan["projects"][0]["droppedDocIds"] == ["GONE"]
    assert plan["projects"][0]["members"][0]["role"] == "viewer"          # legacy "member"
    assert {d["docId"]: d["ownerEmail"] for d in plan["documents"]} == {
        "L1": EMAIL[OWNER], "L2": "nobody-yet@acme.com"}
    # No guessing: the unfiled document and the ownerless project are listed, not assigned.
    assert [d["docId"] for d in plan["orphanDocuments"]] == ["L3"]
    assert [p["projectId"] for p in plan["ownerlessProjects"]] == ["proj_c"]
    report = migrate_access.format_report(plan, None)
    assert "DRY RUN" in report and "L3" in report and "--orphans-to" in report


def test_migration_apply_is_idempotent_and_never_deletes(legacy):
    ddb, resolve = legacy
    plan = migrate_access.build_plan(ddb, ["default"], resolve, orphans_to=EMAIL[OUTSIDER])
    assert plan["errors"] == [] and plan["orphanDocuments"] == []
    done = migrate_access.apply_plan(ddb, plan)
    assert done["projectsCreated"] == 3 and done["documentsUpdated"] == 3
    assert not any(op == "delete_item" for op, _ in ddb.calls)
    assert ("TENANT#default", "PROJECTS") in ddb.items                 # the old blob is kept

    l1 = ddb.doc("L1")
    assert (l1["ownerSub"], l1["projectIds"], l1["GSI1PK"]) == (OWNER, ["proj_a"], f"TENANT#u-{OWNER}")
    assert l1["tenantId"] == "default" and l1["rawKey"].startswith("tenants/default/")   # storage untouched
    assert ddb.doc("L3")["ownerSub"] == OUTSIDER                       # the orphan went where it was told
    assert ddb.items[("PROJ#proj_c", "META")]["ownerSub"] == OUTSIDER

    # Second run: nothing left to do, and applying again changes nothing.
    snapshot = json.dumps(sorted(map(str, ddb.items.items())))
    again = migrate_access.build_plan(ddb, ["default"], resolve, orphans_to=EMAIL[OUTSIDER])
    # (L2's owner has no account yet, so it is re-offered until they do — harmlessly)
    assert [d["docId"] for d in again["documents"]] == ["L2"] and all(p["exists"] for p in again["projects"])
    done = migrate_access.apply_plan(ddb, again)
    assert done["projectsCreated"] == 0 and done["members"] == 0 and done["ownerPointers"] == 0
    strip = lambda text: __import__("re").sub(r"'(updatedAt|accessMigratedAt)': '[^']*'", "", text)
    assert strip(json.dumps(sorted(map(str, ddb.items.items())))) == strip(snapshot)


def test_rerunning_the_migration_does_not_give_back_removed_access(legacy):
    ddb, resolve = legacy
    migrate_access.apply_plan(ddb, migrate_access.build_plan(ddb, ["default"], resolve))
    # after go-live the owner removes the carried-over member
    assert call("DELETE", f"/projects/proj_a/members/{EMAIL[EDITOR]}", caller(OWNER))[0] == 200
    migrate_access.apply_plan(ddb, migrate_access.build_plan(ddb, ["default"], resolve))
    assert ("PROJ#proj_a", f"MEMBER#{EMAIL[EDITOR]}") not in ddb.items
    assert call("GET", "/documents/L1", caller(EDITOR))[0] == 404


def test_an_accepted_membership_is_bound_to_the_account_that_accepted_it(world):
    """The address is later held by a different account: it does not inherit."""
    call("GET", "/projects", caller(EDITOR))                     # accepts: records the sub
    other = caller("eeeeeeee-0000-4000-8000-00000000000e", email=EMAIL[EDITOR])
    assert call("GET", f"/documents/{D1}", other)[0] == 404
    assert call("GET", f"/documents/{D1}", caller(EDITOR))[0] == 200


def test_a_role_change_racing_with_a_removal_does_not_recreate_the_member(world):
    ddb = world["ddb"]
    del ddb.items[(f"PROJ#{PROJ}", f"MEMBER#{EMAIL[VIEWER]}")]
    assert dynamodb.update_project_member(PROJ, EMAIL[VIEWER], {"role": "editor"}) is False
    assert (f"PROJ#{PROJ}", f"MEMBER#{EMAIL[VIEWER]}") not in ddb.items


def test_a_document_whose_project_hint_was_lost_is_still_reachable(world):
    world["ddb"].doc(D1)["projectIds"] = []
    assert call("GET", f"/documents/{D1}", caller(VIEWER))[0] == 200


def test_migrated_data_is_reachable_by_the_right_people_only(legacy, monkeypatch):
    ddb, resolve = legacy
    migrate_access.apply_plan(ddb, migrate_access.build_plan(ddb, ["default"], resolve))
    monkeypatch.setattr(api, "query_doc_versions", lambda _id: [])
    # The recorded owner: by their sub.
    assert call("GET", "/documents/L1", caller(OWNER))[0] == 200
    assert [d["docId"] for d in call("GET", "/documents", caller(OWNER))[1]["documents"]] == ["L1"]
    assert call("GET", "/projects", caller(OWNER))[1]["projects"][0]["role"] == "owner"
    # The carried-over member: by verified email, read-only.
    assert call("GET", "/documents/L1", caller(EDITOR))[0] == 200
    assert call("PATCH", "/documents/L1", caller(EDITOR), {"title": "x"})[0] == 403
    # Everyone else — including someone in the old shared tenant — sees nothing.
    assert call("GET", "/documents/L1", caller(OUTSIDER))[0] == 404
    assert call("GET", "/documents/L3", caller(OWNER))[0] == 404        # unassigned orphan stays hidden
    # An owner with no account yet owns the project the moment they sign in verified.
    late = caller("dddddddd-0000-4000-8000-00000000000d", email="nobody-yet@acme.com")
    assert call("GET", "/projects", late)[1]["projects"][0]["role"] == "owner"
    assert call("GET", "/documents/L2", late)[0] == 200


def test_migration_refuses_an_orphan_recipient_who_has_no_account(legacy):
    ddb, resolve = legacy
    plan = migrate_access.build_plan(ddb, ["default"], resolve, orphans_to="ghost@nowhere.com")
    assert plan["errors"] and "no Cognito user" in plan["errors"][0]
    assert [d["docId"] for d in plan["orphanDocuments"]] == ["L3"]
