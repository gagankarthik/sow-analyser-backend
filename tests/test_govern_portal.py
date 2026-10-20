"""Counterparty portal links: the reviewer routes on govern-api
(create / list / revoke, permissions, the 5-link cap) and the public
govern-portal Lambda (token model, identical 404s, the allow-listed view,
revision uploads, activity entries, per-link caps, no token in the logs).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from govern_intake import handler as intake
from govern_portal import handler as portal_api
from govern_support import (EDITOR, EMAIL, OUTSIDER, OWNER, VIEWER, analysed_event, call, install_analysis,
                            seed_doc, seed_project)
from shared.config import settings
from shared.govern import portal, store


class _S3:
    def __init__(self) -> None:
        self.signed: list[dict] = []

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803 — boto3 naming
        self.signed.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return "https://s3.invalid/put"


@pytest.fixture
def world(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    monkeypatch.setattr(settings, "raw_bucket", "raw")
    monkeypatch.setattr(settings, "app_base_url", "https://app.example")
    seed_doc(ddb, "lic-1", project_ids=["proj_osu"])
    seed_project(ddb, {EDITOR: "editor", VIEWER: "viewer"}, ["lic-1"])
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    s3 = _S3()
    import shared.aws

    monkeypatch.setattr(shared.aws, "s3_client", lambda: s3)
    gov.s3 = s3
    return gov


def create(sub=EDITOR, body=None, cid="lic-1"):
    status, out = call("POST", f"/contracts/{cid}/portal-links", sub, body if body is not None else {})
    assert status == 201, out
    return out


def token_of(url: str) -> str:
    assert url.startswith("https://app.example/portal/")
    return url.rsplit("/", 1)[1]


def public(method: str, token: str, body=None):
    path = f"/portal/{token}" + ("/upload-url" if method == "POST" else "")
    resp = portal_api.dispatch({"rawPath": path, "requestContext": {"http": {"method": method}},
                                "body": json.dumps(body) if body is not None else None})
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None), resp


def link_item(gov, token: str) -> dict:
    return gov.config.items[(f"PORTAL#{portal.token_hash(token)}", "LINK")]


# ---------------------------------------------------------------------------
# Reviewer side
# ---------------------------------------------------------------------------


def test_create_list_and_revoke(world):
    out = create(body={"expiresInDays": 7, "allowUpload": True, "note": "Please send the revised draft by Friday."})
    assert set(out) == {"id", "url", "expiresAt", "link"}
    token = token_of(out["url"])
    assert len(token) == 43
    expires = store.parse_iso(out["expiresAt"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)

    status, listed = call("GET", "/contracts/lic-1/portal-links", EDITOR)
    assert status == 200 and listed["activeCount"] == 1 and listed["maxActive"] == 5
    (link,) = listed["links"]
    assert set(link) == {"id", "status", "createdAt", "createdBy", "expiresAt", "allowUpload", "note", "opens",
                         "lastOpenedAt", "uploads", "revokedAt"}
    assert (link["id"], link["status"], link["opens"], link["allowUpload"]) == (out["id"], "active", 0, True)
    raw = json.dumps(listed)
    assert token not in raw and portal.token_hash(token) not in raw and "tokenHash" not in raw

    status, revoked = call("DELETE", f"/contracts/lic-1/portal-links/{out['id']}", OWNER)
    assert status == 200 and revoked["link"]["status"] == "revoked" and revoked["link"]["revokedAt"]
    # Revoking again changes nothing (and logs nothing new).
    assert call("DELETE", f"/contracts/lic-1/portal-links/{out['id']}", OWNER)[0] == 200
    assert call("DELETE", "/contracts/lic-1/portal-links/0123456789abcdef", OWNER)[0] == 404
    assert call("DELETE", "/contracts/lic-1/portal-links/nope", OWNER)[0] == 404
    assert call("GET", "/contracts/lic-1/portal-links", EDITOR)[1]["links"][0]["status"] == "revoked"
    actions = world.actions("lic-1")
    assert actions.count("portal_link_created") == 1 and actions.count("portal_link_revoked") == 1


def test_only_editors_manage_links(world):
    status, body = call("POST", "/contracts/lic-1/portal-links", VIEWER, {})
    assert (status, body["code"]) == (403, "forbidden")
    assert call("GET", "/contracts/lic-1/portal-links", VIEWER)[0] == 403
    assert call("POST", "/contracts/lic-1/portal-links", OUTSIDER, {})[0] == 404
    assert call("POST", "/contracts/lic-1/portal-links", EDITOR, {}, groups=["govern-leader"])[0] == 403
    link_id = create()["id"]
    assert call("DELETE", f"/contracts/lic-1/portal-links/{link_id}", VIEWER)[0] == 403
    assert call("DELETE", f"/contracts/lic-1/portal-links/{link_id}", OUTSIDER)[0] == 404


@pytest.mark.parametrize("body", [{"expiresInDays": 0}, {"expiresInDays": 61}, {"expiresInDays": "7"},
                                  {"expiresInDays": True}, {"allowUpload": "yes"}, {"note": 5},
                                  {"note": "x" * 1001}, {"token": "mine"}])
def test_create_validates_the_body(world, body):
    status, out = call("POST", "/contracts/lic-1/portal-links", EDITOR, body)
    assert (status, out["code"]) == (400, "bad_request")


def test_defaults_are_fourteen_days_with_uploads(world):
    out = create()
    days = (store.parse_iso(out["expiresAt"]) - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 13.9 < days <= 14 and out["link"]["allowUpload"] is True and out["link"]["note"] is None


def test_at_most_five_active_links(world):
    ids = [create()["id"] for _ in range(5)]
    status, out = call("POST", "/contracts/lic-1/portal-links", EDITOR, {})
    assert (status, out["code"]) == (409, "limit_reached")
    assert call("DELETE", f"/contracts/lic-1/portal-links/{ids[0]}", EDITOR)[0] == 200
    create()
    # An expired link does not count either.
    token = None
    for (pk, sk), item in world.config.items.items():
        if sk == "LINK" and item["linkId"] == ids[1]:
            item["expiresAt"] = "2020-01-01T00:00:00Z"
            token = pk
    assert token
    create()
    assert call("POST", "/contracts/lic-1/portal-links", EDITOR, {})[0] == 409


def test_the_raw_token_is_never_stored(world):
    out = create(body={"note": "Thanks"})
    token = token_of(out["url"])
    stored = json.dumps([*world.config.items.values(), *world.activity.items.values(),
                         *world.contracts.items.values()], default=str)
    assert token not in stored
    item = link_item(world, token)
    assert item["tokenHash"] == portal.token_hash(token) and len(item["tokenHash"]) == 64
    index = world.config.items[("CON#lic-1#PORTAL", f"L#{out['id']}")]
    assert index["tokenHash"] == item["tokenHash"]


# ---------------------------------------------------------------------------
# Public side
# ---------------------------------------------------------------------------


def test_unknown_expired_and_revoked_tokens_get_one_identical_404(world):
    expired = token_of(create()["url"])
    link_item(world, expired)["expiresAt"] = "2020-01-01T00:00:00Z"
    revoked_out = create()
    revoked = token_of(revoked_out["url"])
    call("DELETE", f"/contracts/lic-1/portal-links/{revoked_out['id']}", EDITOR)
    import secrets as _secrets

    unknown = _secrets.token_urlsafe(32)
    replies = []
    for token in (unknown, expired, revoked, "short", "x" * 43 + "!", "%2e%2e"):
        for method in ("GET", "POST"):
            status, body, resp = public(method, token, {"filename": "a.pdf", "size": 10} if method == "POST" else None)
            replies.append((status, json.dumps(body), json.dumps(resp["headers"], sort_keys=True)))
    assert len(set(replies)) == 1
    status, body, headers = replies[0]
    assert status == 404 and json.loads(body) == {"error": portal.GONE_MESSAGE, "code": "not_found"}
    # A deleted contract's link answers the same.
    live = token_of(create()["url"])
    world.contracts.items.pop(("CON#lic-1", "META"))
    status, body, resp = public("GET", live)
    assert (status, json.dumps(body), json.dumps(resp["headers"], sort_keys=True)) == replies[0]


def test_the_public_view_is_allow_listed(world):
    call("PATCH", "/contracts/lic-1", EDITOR, {"requestedDate": "2026-11-15"})
    call("PUT", "/workflow/settings", OWNER, {"organization": {"name": "Northfield University"}})
    call("POST", "/contracts/lic-1/blockers", EDITOR, {"text": "INTERNAL: legal thinks they will fold", "clauseType": "Indemnification",
                                                         "suggestedLanguage": "Each party shall indemnify the other."})
    call("POST", "/contracts/lic-1/blockers", EDITOR, {"text": "Internal only, no clause"})
    call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "comment", "text": "secret internal comment"})
    token = token_of(create(body={"note": "Please mark up the indemnity."})["url"])

    status, view, resp = public("GET", token)
    assert status == 200
    assert set(view) == set(portal.PUBLIC_VIEW_KEYS)
    assert view["contractTitle"] and view["organizationName"] == "Northfield University"
    assert view["requestedDate"] == "2026-11-15" and view["note"] == "Please mark up the indemnity."
    assert view["allowUpload"] is True and view["expiresAt"]
    assert view["needs"], "the sample licence has open items"
    for need in view["needs"]:
        assert set(need) == set(portal.NEED_KEYS)
        assert need["tier"] in {"Required change", "Change requested", "Missing clause", "Please confirm", "Requested"}
    assert any(n["suggestedLanguage"] == "Each party shall indemnify the other." and n["tier"] == "Requested"
               for n in view["needs"])

    raw = json.dumps(view)
    for secret in ("INTERNAL", "Internal only", "secret internal comment", EMAIL[OWNER], EMAIL[EDITOR], EMAIL[VIEWER],
                   "matrix", "lic-1", "tenant"):
        assert secret not in raw, secret
    blocker_texts = [b["text"] for b in store.contracts.blockers("lic-1")]
    assert blocker_texts and not any(t in raw for t in blocker_texts)
    assert resp["headers"]["Cache-Control"] == "no-store" and resp["headers"]["Referrer-Policy"] == "no-referrer"


def test_opening_counts_and_logs_once_a_day(world):
    out = create()
    token = token_of(out["url"])
    for _ in range(3):
        assert public("GET", token)[0] == 200
    assert world.actions("lic-1").count("portal_opened") == 1
    entry = next(e for e in world.activity_for("lic-1") if e["action"] == "portal_opened")
    assert entry["summary"] == "Counterparty opened the link." and entry["actor"]["name"] == "Counterparty via portal link"
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    portal.open_link(token, now=tomorrow)
    assert world.actions("lic-1").count("portal_opened") == 2
    (link,) = call("GET", "/contracts/lic-1/portal-links", EDITOR)[1]["links"]
    assert link["opens"] == 4 and link["lastOpenedAt"]


def test_views_are_capped_per_link(world):
    token = token_of(create()["url"])
    link_item(world, token)["opens"] = portal.MAX_OPENS - 1
    assert public("GET", token)[0] == 200
    status, body, _ = public("GET", token)
    assert (status, body["code"]) == (429, "rate_limited")


def test_upload_url_files_a_revision_for_the_contract(world):
    token = token_of(create()["url"])
    size = 1_234_567
    status, out, _ = public("POST", token, {"filename": "license v2.docx", "size": size})
    assert status == 200, out
    assert out["uploadUrl"] == "https://s3.invalid/put" and set(out) == {"uploadUrl", "headers", "expiresIn", "maxBytes"}
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert out["headers"] == {"Content-Type": ct}
    (signed,) = world.s3.signed
    assert (signed["op"], signed["Bucket"], signed["ContentType"], signed["ContentLength"]) == ("put_object", "raw", ct, size)
    c = world.contract("lic-1")
    new_id = c["pendingDocId"]
    # Filed exactly as the link's creator's own revision upload would be (their workspace).
    assert c["versionDocIds"][-1] == new_id and signed["Key"] == f"tenants/u-{EDITOR}/uploads/{new_id}/license v2.docx"
    meta = world.docs.doc(new_id)
    assert (meta["status"], meta["revisionOf"], meta["uploadedVia"], meta["ownerSub"]) == ("PENDING", "lic-1", "portal", EDITOR)
    assert meta["projectIds"] == ["proj_osu"] and new_id in world.docs.items[("PROJ#proj_osu", "META")]["docIds"]
    entry = next(e for e in world.activity_for("lic-1") if e["action"] == "portal_upload")
    assert entry["summary"].startswith("Counterparty uploaded a revision")
    assert entry["actor"]["name"] == "Counterparty via portal link"
    # The pipeline treats it as the next round: intake links and rescores it.
    meta.update(status="READY", sample="01-exclusive-license-v2-revised.txt", latestVersion=1)
    assert intake.handle_event(analysed_event(new_id)) == "revision"
    assert world.contract("lic-1")["currentDocId"] == new_id


@pytest.mark.parametrize("body,code", [({"filename": "notes.txt", "size": 10}, "bad_request"),
                                       ({"filename": "a.exe", "size": 10}, "bad_request"),
                                       ({"filename": "a.pdf", "size": 25 * 1024 * 1024 + 1}, "bad_request"),
                                       ({"filename": "a.pdf", "size": 0}, "bad_request"),
                                       ({"filename": "a.pdf"}, "bad_request"),
                                       ({"filename": "../../a.pdf", "size": 10, "extra": 1}, "bad_request")])
def test_upload_url_limits_type_and_size(world, body, code):
    token = token_of(create()["url"])
    status, out, _ = public("POST", token, body)
    assert (status, out["code"]) == (400, code)
    assert world.s3.signed == []


def test_upload_url_only_when_the_link_allows_it(world):
    token = token_of(create(body={"allowUpload": False})["url"])
    assert public("GET", token)[1]["allowUpload"] is False
    status, out, _ = public("POST", token, {"filename": "a.pdf", "size": 10})
    assert (status, out["code"]) == (403, "upload_not_allowed")
    assert world.s3.signed == [] and "portal_upload" not in world.actions("lic-1")


def test_uploads_are_capped_per_link(world):
    token = token_of(create()["url"])
    link_item(world, token)["uploads"] = portal.MAX_UPLOADS
    status, out, _ = public("POST", token, {"filename": "a.pdf", "size": 10})
    assert (status, out["code"]) == (429, "rate_limited")


def test_wrong_methods_and_paths(world):
    token = token_of(create()["url"])
    resp = portal_api.dispatch({"rawPath": f"/portal/{token}", "requestContext": {"http": {"method": "DELETE"}}})
    assert resp["statusCode"] == 405
    resp = portal_api.dispatch({"rawPath": "/portal", "requestContext": {"http": {"method": "GET"}}})
    assert resp["statusCode"] == 404


def test_no_token_in_the_logs(world, capsys):
    capsys.readouterr()
    token = token_of(create()["url"])
    public("GET", token)
    public("POST", token, {"filename": "a.pdf", "size": 10})
    public("GET", token[:-1] + ("A" if token[-1] != "A" else "B"))
    logged = capsys.readouterr()
    assert token not in logged.out + logged.err
    assert portal.token_hash(token) not in logged.out + logged.err
