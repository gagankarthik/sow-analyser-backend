"""The organization admin model (shared/govern/org.py, GET/POST/DELETE /govern/org*):
the creator of a tenant is its admin, admins are per tenant, and the pool-wide
Cognito govern-admin group grants nothing."""
from __future__ import annotations

import pytest

from govern_support import EDITOR, EMAIL, NAME, OUTSIDER, OWNER, VIEWER, call
from shared.govern import org, store

ACME, GLOBEX = "acme", "globex"


@pytest.fixture
def acme(gov):
    """OWNER opens Govern first in acme (creator), then EDITOR and VIEWER sign in."""
    for sub in (OWNER, EDITOR, VIEWER):
        assert call("GET", "/govern/me", sub, tenant=ACME)[0] == 200
    return gov


def org_item(gov, tenant):
    return gov.config.items.get((f"T#{tenant}#ORG", "ORG"))


def admins(tenant=ACME, sub=OWNER):
    return [a["email"] for a in call("GET", "/govern/org", sub, tenant=tenant)[1]["admins"]]


# ---------------------------------------------------------------------------
# Who is admin
# ---------------------------------------------------------------------------


def test_the_personal_workspace_user_is_its_creator_and_admin(gov):
    me = call("GET", "/govern/me", OWNER)[1]
    assert (me["role"], me["isAdmin"], me["isCreator"]) == ("admin", True, True)
    item = org_item(gov, f"u-{OWNER}")
    assert item["createdBy"] == {"sub": OWNER, "email": EMAIL[OWNER], "name": NAME[OWNER]}
    assert [a["sub"] for a in item["admins"]] == [OWNER] and item["rev"] == 1
    out = call("GET", "/govern/org", OWNER)[1]
    assert out["personal"] is True and out["you"] == {"isAdmin": True, "isCreator": True}
    assert out["admins"][0]["isCreator"] is True
    # A second /govern/me does not rewrite the record.
    call("GET", "/govern/me", OWNER)
    assert org_item(gov, f"u-{OWNER}")["rev"] == 1


def test_a_personal_workspace_cannot_have_more_admins(gov):
    call("GET", "/govern/me", OWNER)
    status, out = call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR]})
    assert status == 400 and "personal workspace" in out["error"]
    assert call("DELETE", f"/govern/org/admins/{OWNER}", OWNER)[0] == 409


def test_someone_else_signed_in_to_a_personal_workspace_is_not_its_admin(gov):
    me = call("GET", "/govern/me", EDITOR, tenant=f"u-{OWNER}")[1]
    assert (me["isAdmin"], me["isCreator"]) == (False, False)
    assert org_item(gov, f"u-{OWNER}") is None                 # only the owner creates it


def test_the_first_member_of_a_shared_tenant_creates_it_and_later_members_are_not_admins(acme):
    assert org_item(acme, ACME)["createdBy"]["sub"] == OWNER
    me = call("GET", "/govern/me", EDITOR, tenant=ACME)[1]
    assert (me["role"], me["isAdmin"], me["isCreator"]) == ("reviewer", False, False)
    out = call("GET", "/govern/org", EDITOR, tenant=ACME)[1]
    assert out["you"] == {"isAdmin": False, "isCreator": False}
    assert [a["email"] for a in out["admins"]] == [EMAIL[OWNER]]
    assert "members" not in out and "activity" not in out          # admins only
    owner_view = call("GET", "/govern/org", OWNER, tenant=ACME)[1]
    assert {m["email"] for m in owner_view["members"]} == {EMAIL[OWNER], EMAIL[EDITOR], EMAIL[VIEWER]}
    assert owner_view["activity"][0]["action"] == "org_created"


def test_the_creator_claim_happens_exactly_once_under_a_race(gov, monkeypatch):
    """EDITOR and OWNER both read "no organization"; OWNER's conditional put
    wins, EDITOR's fails and EDITOR reads OWNER's record instead."""
    real_get = org.get
    reads = {"n": 0}

    def racing_get(tenant_id):
        reads["n"] += 1
        if reads["n"] == 1:
            # OWNER commits between EDITOR's read and EDITOR's put.
            assert store.config.create_org(tenant_id, {
                "tenantId": tenant_id, "createdAt": "2026-10-20T00:00:00Z",
                "createdBy": {"sub": OWNER, "email": EMAIL[OWNER], "name": NAME[OWNER]},
                "admins": [{"sub": OWNER, "email": EMAIL[OWNER], "name": NAME[OWNER]}]})
            return None
        return real_get(tenant_id)

    monkeypatch.setattr(org, "get", racing_get)
    me = call("GET", "/govern/me", EDITOR, tenant=ACME)[1]
    assert (me["isAdmin"], me["isCreator"]) == (False, False)
    assert org_item(gov, ACME)["createdBy"]["sub"] == OWNER and org_item(gov, ACME)["rev"] == 1
    assert len([k for k in gov.config.items if k == (f"T#{ACME}#ORG", "ORG")]) == 1
    # The losing request wrote no audit entry claiming it created the organization.
    assert not [k for k in gov.config.items if k[0] == f"T#{ACME}#ORG" and k[1].startswith("AUDIT#")]


def test_an_admin_of_one_tenant_has_no_admin_rights_in_another_even_with_the_group(gov):
    call("GET", "/govern/me", OUTSIDER, tenant=GLOBEX)                 # OUTSIDER creates globex
    call("GET", "/govern/me", OWNER, tenant=ACME)                      # OWNER creates acme
    me = call("GET", "/govern/me", OUTSIDER, groups=["govern-admin"], tenant=ACME)[1]
    assert (me["role"], me["isAdmin"]) == ("reviewer", False)
    for method, path, body in ADMIN_ROUTES:
        status, out = call(method, path, OUTSIDER, body, groups=["govern-admin"], tenant=ACME)
        assert (status, out["code"]) == (403, "forbidden"), path
    assert call("GET", "/integrations", OUTSIDER, tenant=GLOBEX)[0] == 200


def test_no_organization_yet_means_no_admin_in_a_shared_tenant(gov):
    """Admin checks outside /govern/me and /govern/org never create the
    organization, so probing an admin route does not make anyone its creator."""
    assert call("PUT", "/workflow/settings", EDITOR, {}, tenant=ACME)[0] == 403
    assert org_item(gov, ACME) is None


# ---------------------------------------------------------------------------
# Adding and removing admins
# ---------------------------------------------------------------------------


def test_an_admin_adds_a_member_as_admin_and_it_is_audited(acme):
    rev = call("GET", "/govern/org", OWNER, tenant=ACME)[1]["rev"]
    status, out = call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR].upper(), "rev": rev}, tenant=ACME)
    assert status == 200, out
    assert [a["email"] for a in out["admins"]] == [EMAIL[OWNER], EMAIL[EDITOR]] and out["rev"] == rev + 1
    assert out["admins"][1]["addedBy"] == {"email": EMAIL[OWNER], "name": NAME[OWNER]}
    assert out["activity"][0]["action"] == "admin_added" and out["activity"][0]["target"]["email"] == EMAIL[EDITOR]
    me = call("GET", "/govern/me", EDITOR, tenant=ACME)[1]
    assert (me["isAdmin"], me["isCreator"], me["role"]) == (True, False, "admin")
    assert call("PUT", "/workflow/settings", EDITOR, {}, tenant=ACME)[0] == 200
    # Adding again changes nothing (no new rev, no new audit entry).
    again = call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR]}, tenant=ACME)[1]
    assert again["rev"] == out["rev"] and len(again["activity"]) == len(out["activity"])


def test_only_people_who_signed_in_to_this_tenant_can_become_admins(acme):
    call("GET", "/govern/me", OUTSIDER, tenant=GLOBEX)
    for email in (EMAIL[OUTSIDER], "nobody@acme.edu"):
        status, out = call("POST", "/govern/org/admins", OWNER, {"email": email}, tenant=ACME)
        assert (status, out["code"]) == (404, "not_found"), email
        assert "signed in to this organization" in out["error"]
    assert call("POST", "/govern/org/admins", OWNER, {"email": "not-an-email"}, tenant=ACME)[0] == 400
    assert call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR], "role": "x"}, tenant=ACME)[0] == 400
    assert admins() == [EMAIL[OWNER]]


def test_non_admins_cannot_change_admins(acme):
    status, out = call("POST", "/govern/org/admins", EDITOR, {"email": EMAIL[EDITOR]}, tenant=ACME)
    assert (status, out["code"]) == (403, "forbidden")
    status, out = call("POST", "/govern/org/admins", EDITOR, {"email": EMAIL[EDITOR]}, groups=["govern-admin"],
                       tenant=ACME)
    assert (status, out["code"]) == (403, "forbidden")
    assert call("DELETE", f"/govern/org/admins/{OWNER}", VIEWER, tenant=ACME)[0] == 403
    assert admins() == [EMAIL[OWNER]]


def test_the_creator_cannot_be_removed(acme):
    call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR]}, tenant=ACME)
    for actor in (OWNER, EDITOR):
        status, out = call("DELETE", f"/govern/org/admins/{OWNER}", actor, tenant=ACME)
        assert (status, out["code"]) == (409, "conflict") and "created the organization" in out["error"]
    assert admins() == [EMAIL[OWNER], EMAIL[EDITOR]]


def test_an_admin_removes_another_admin_and_it_is_audited(acme):
    call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR]}, tenant=ACME)
    call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[VIEWER]}, tenant=ACME)
    status, out = call("DELETE", f"/govern/org/admins/{VIEWER}", EDITOR, tenant=ACME)
    assert status == 200 and [a["email"] for a in out["admins"]] == [EMAIL[OWNER], EMAIL[EDITOR]]
    assert out["activity"][0]["action"] == "admin_removed" and out["activity"][0]["actor"]["email"] == EMAIL[EDITOR]
    assert call("PUT", "/workflow/settings", VIEWER, {}, tenant=ACME)[0] == 403
    # An admin who is not the creator may step down.
    assert call("DELETE", f"/govern/org/admins/{EDITOR}", EDITOR, tenant=ACME)[0] == 200
    assert admins() == [EMAIL[OWNER]]
    # Someone who is not an admin, or an id that is not one at all.
    assert call("DELETE", f"/govern/org/admins/{VIEWER}", OWNER, tenant=ACME)[0] == 404
    assert call("DELETE", "/govern/org/admins/bad id!", OWNER, tenant=ACME)[0] == 404


def test_the_last_admin_cannot_be_removed(gov):
    """A record without a creator (e.g. provisioned by hand) still keeps one admin."""
    call("GET", "/govern/me", EDITOR, tenant=ACME)
    gov.config.items.pop((f"T#{ACME}#ORG", "ORG"))
    assert store.config.create_org(ACME, {"tenantId": ACME, "createdBy": None, "createdAt": "2026-10-20T00:00:00Z",
                                          "admins": [{"sub": EDITOR, "email": EMAIL[EDITOR], "name": NAME[EDITOR]}]})
    status, out = call("DELETE", f"/govern/org/admins/{EDITOR}", EDITOR, tenant=ACME)
    assert (status, out["code"]) == (409, "conflict") and "at least one admin" in out["error"]


def test_a_stale_rev_is_a_conflict(acme):
    rev = call("GET", "/govern/org", OWNER, tenant=ACME)[1]["rev"]
    call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR], "rev": rev}, tenant=ACME)
    status, out = call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[VIEWER], "rev": rev}, tenant=ACME)
    assert (status, out["code"]) == (409, "conflict")
    status, out = call("DELETE", f"/govern/org/admins/{EDITOR}", OWNER, qs={"rev": str(rev)}, tenant=ACME)
    assert (status, out["code"]) == (409, "conflict")
    assert call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[VIEWER], "rev": "x"}, tenant=ACME)[0] == 400
    assert admins() == [EMAIL[OWNER], EMAIL[EDITOR]]


def test_a_concurrent_write_is_retried_against_the_new_record(acme, monkeypatch):
    """The lock loses once (someone else added an admin meanwhile): the add is
    re-applied on top, so neither change is lost."""
    real_put = store.config.put_org
    calls = {"n": 0}

    def racing_put(tenant_id, record, expected_rev):
        calls["n"] += 1
        if calls["n"] == 1:
            current = org.get(tenant_id)
            assert real_put(tenant_id, {**current, "admins": current["admins"] + [
                {"sub": VIEWER, "email": EMAIL[VIEWER], "name": NAME[VIEWER]}]}, expected_rev)
        return real_put(tenant_id, record, expected_rev)

    monkeypatch.setattr(store.config, "put_org", racing_put)
    status, out = call("POST", "/govern/org/admins", OWNER, {"email": EMAIL[EDITOR]}, tenant=ACME)
    assert status == 200
    assert {a["email"] for a in out["admins"]} == {EMAIL[OWNER], EMAIL[VIEWER], EMAIL[EDITOR]}


# ---------------------------------------------------------------------------
# Every admin-only route, server-side
# ---------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("PUT", "/matrix", {"playbooks": {}}), ("POST", "/matrix/import", {}),
    ("PUT", "/workflow/settings", {}),
    ("GET", "/integrations", None), ("GET", "/integrations/sync-log", None), ("GET", "/integrations/unmatched", None),
    ("PUT", "/integrations/huron", {"enabled": True}), ("POST", "/integrations/workday/sync", {}),
    ("POST", "/automations", {}), ("PATCH", "/automations/abc", {"name": "x"}), ("DELETE", "/automations/abc", None),
    ("GET", "/reports/trends", None),
    ("POST", "/govern/org/admins", {"email": "x@y.z"}), ("DELETE", f"/govern/org/admins/{OWNER}", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
@pytest.mark.parametrize("groups", [None, ["govern-admin"], ["govern-reviewer"]])
def test_every_admin_only_route_rejects_a_non_admin(acme, method, path, body, groups):
    status, out = call(method, path, EDITOR, body, groups=groups, tenant=ACME)
    assert (status, out["code"]) == (403, "forbidden"), (path, out)


@pytest.mark.parametrize("method,path,body", [("GET", "/integrations", None), ("GET", "/integrations/sync-log", None),
                                              ("PUT", "/workflow/settings", {}), ("GET", "/reports/trends", None)])
def test_the_creator_passes_the_same_routes(acme, method, path, body):
    assert call(method, path, OWNER, body, tenant=ACME)[0] == 200
