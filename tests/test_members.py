"""Project membership — invite users (Cognito) to a project.

Covers the deterministic parts (email/role validation, member shape-guard)
without hitting AWS. The Cognito call itself is exercised in deploy/integration.
"""
from __future__ import annotations

import json

from api import handler


def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


def test_email_regex():
    assert handler._EMAIL_RE.match("ada@blue-iq.ai")
    assert handler._EMAIL_RE.match("a.b+c@d.co.uk")
    assert not handler._EMAIL_RE.match("not-an-email")
    assert not handler._EMAIL_RE.match("a@b")


def test_clean_members_normalizes_and_filters():
    raw = [
        {"email": "Owner@Co.com", "role": "OWNER", "status": "invited"},
        {"role": "member"},                       # no email → dropped
        {"email": "c@d.com", "role": "bogus"},    # bad role → member
        "garbage",                                # not a dict → dropped
    ]
    out = handler._clean_members(raw)
    assert [m["email"] for m in out] == ["Owner@Co.com", "c@d.com"]
    assert out[0]["role"] == "owner"
    assert out[1]["role"] == "member"


def test_clean_members_handles_non_list():
    assert handler._clean_members(None) == []
    assert handler._clean_members("x") == []


def test_invite_rejects_bad_email():
    resp = handler._invite_member("proj_1", _event({"email": "nope"}), "tenant-1")
    assert resp["statusCode"] == 400


def test_invite_rejects_missing_email():
    resp = handler._invite_member("proj_1", _event({"role": "editor"}), "tenant-1")
    assert resp["statusCode"] == 400
