"""Project membership — input validation for invites and roles.

The access rules themselves (who may invite, what a member can see) are covered
in tests/test_access.py; this file covers the deterministic input handling.
All storage is the in-memory table from tests/fakes.py.
"""
from __future__ import annotations

import json

import pytest

from api import handler
from shared.access import Caller, normalise_email, normalise_role

SUB = "11111111-1111-4111-8111-111111111111"


def _event(body) -> dict:
    return {"body": json.dumps(body)}


def _owner() -> Caller:
    return Caller.from_claims({"sub": SUB, "email": "owner@co.com", "email_verified": "true"})


@pytest.fixture
def project(ddb, monkeypatch):
    monkeypatch.setattr(handler.settings, "cognito_user_pool_id", "")
    owner = _owner()
    handler._put_project("proj_1", _event({"name": "P"}), owner)
    return owner


def test_email_regex():
    assert handler._EMAIL_RE.match("ada@blue-iq.ai")
    assert handler._EMAIL_RE.match("a.b+c@d.co.uk")
    assert not handler._EMAIL_RE.match("not-an-email")
    assert not handler._EMAIL_RE.match("a@b")


def test_emails_are_normalised_and_bad_ones_rejected():
    assert normalise_email("  Owner@Co.COM ") == "owner@co.com"
    assert normalise_email("no-at-sign") is None
    assert normalise_email("has space@co.com") is None
    assert normalise_email(None) is None
    assert normalise_email("x" * 250 + "@co.com") is None


def test_roles_are_normalised_to_the_three_real_ones():
    assert normalise_role("OWNER") == "owner"
    assert normalise_role("editor") == "editor"
    assert normalise_role("member") == "viewer"     # the legacy role gets the least privilege
    assert normalise_role("bogus") == "viewer"
    assert normalise_role(None) == "viewer"


def test_invite_rejects_bad_email(project):
    resp = handler._invite_member("proj_1", _event({"email": "nope"}), project)
    assert resp["statusCode"] == 400


def test_invite_rejects_missing_email(project):
    resp = handler._invite_member("proj_1", _event({"role": "editor"}), project)
    assert resp["statusCode"] == 400


def test_invite_rejects_non_object_body(project):
    assert handler._invite_member("proj_1", {"body": "[1,2]"}, project)["statusCode"] == 400
    assert handler._invite_member("proj_1", {"body": "{not json"}, project)["statusCode"] == 400


def test_invite_stores_a_lower_cased_member_with_a_real_role(project, ddb):
    resp = handler._invite_member("proj_1", _event({"email": "Ada@Blue-IQ.ai", "role": "member"}), project)
    assert resp["statusCode"] == 201
    row = ddb.items[("PROJ#proj_1", "MEMBER#ada@blue-iq.ai")]
    assert row["role"] == "viewer" and row["status"] == "invited"
    assert row["GSI1PK"] == "MEMBER#ada@blue-iq.ai" and row["invitedBy"] == SUB


def test_invite_to_a_project_the_caller_cannot_see_is_404(ddb):
    stranger = Caller.from_claims({"sub": "22222222-2222-4222-8222-222222222222"})
    assert handler._invite_member("proj_1", _event({"email": "a@b.co"}), stranger)["statusCode"] == 404
