"""Govern real-time invalidations: the stream Lambda's push to the tenant's
AppSync Events channel (shared/govern/realtime.py) and the channel
namespace's subscribe handler (terraform/appsync/govern_channel.js)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from shared.config import settings
from shared.govern import realtime
from test_govern_events import TENANT, lic, replay  # noqa: F401 — fixture reused

HANDLER_JS = Path(__file__).resolve().parents[1] / "terraform" / "appsync" / "govern_channel.js"
SEGMENT_RE = re.compile(r"^[A-Za-z0-9-]{1,50}$")


@pytest.fixture
def pushes(monkeypatch):
    """Turn real-time on and capture what would be published."""
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(settings, "govern_realtime_http_domain", "abc.appsync-api.us-east-2.amazonaws.com")
    monkeypatch.setattr(realtime, "_send", lambda channel, events: sent.append((channel, events)))
    return sent


# ── message shape ────────────────────────────────────────────────────────────

def test_message_carries_only_the_invalidation_fields():
    entry = {
        "id": "e1", "contractId": "c-1", "tenantId": "northfield", "action": "comment",
        "at": "2026-10-20T10:00:00Z", "actor": {"email": "dana@northfield.edu", "name": "Dana Reyes"},
        "summary": "Dana commented: the indemnity cap is $2M", "detail": {"text": "secret", "value": 2_000_000},
        "fromStage": "legal", "toStage": "approval", "automation": "a-1",
    }
    msg = realtime.message_for(entry)
    assert set(msg) == set(realtime.MESSAGE_KEYS) == {"type", "contractId", "action", "actor", "at"}
    assert msg == {"type": "contract.changed", "contractId": "c-1", "action": "comment",
                   "actor": "Dana Reyes", "at": "2026-10-20T10:00:00Z"}
    assert "@" not in json.dumps(msg) and "secret" not in json.dumps(msg) and "2000000" not in json.dumps(msg)


def test_actor_is_a_display_name_never_an_email():
    assert realtime.actor_name({"email": "dana@northfield.edu", "name": None}) is None
    assert realtime.actor_name({"email": "dana@northfield.edu", "name": "dana@northfield.edu"}) is None
    assert realtime.actor_name(None) == "Sonar"                     # a system entry
    assert realtime.actor_name("dana@northfield.edu") is None


def test_an_entry_without_a_contract_is_not_sent(pushes):
    assert realtime.message_for({"action": "intake"}) is None
    assert realtime.publish({"action": "intake", "tenantId": "northfield"}) is False
    assert pushes == []


# ── channels: one per tenant ─────────────────────────────────────────────────

def test_channel_is_scoped_to_the_tenant():
    assert realtime.channel_for("northfield") == "/govern/northfield"
    assert realtime.channel_for("u-1234-abcd") == "/govern/u-01234-0abcd"
    assert realtime.channel_for("north_field") != realtime.channel_for("north.field") != realtime.channel_for("north-field")
    assert realtime.channel_for("a-1") != realtime.channel_for("a_1")


@pytest.mark.parametrize("tenant", ["x" * 64, "a" + "_" * 63, "a.b-c_d" * 9, "u-" + "f" * 36])
def test_channel_segments_are_valid_for_appsync(tenant):
    path = realtime.channel_for(tenant)
    segments = path.split("/")[1:]
    assert segments[0] == "govern" and 2 <= len(segments) <= 4
    assert all(SEGMENT_RE.fullmatch(s) for s in segments[1:])


@pytest.mark.parametrize("bad", ["", "a/b", "*", "-lead", "a" * 65, "has space", None])
def test_a_tenant_auth_would_reject_has_no_channel(bad):
    with pytest.raises(ValueError):
        realtime.channel_for(bad)


# ── the stream Lambda ────────────────────────────────────────────────────────

def test_stream_pushes_one_minimal_message_per_entry_to_the_tenant_channel(lic, pushes):
    assert replay(lic) == {"batchItemFailures": []}
    assert [ch for ch, _ in pushes] == [realtime.channel_for(TENANT)] * 2
    messages = [json.loads(ev) for _, events in pushes for ev in events]
    assert [m["action"] for m in messages] == ["intake", "rescored"]
    for m in messages:
        assert set(m) == {"type", "contractId", "action", "actor", "at"}
        assert m["type"] == "contract.changed" and m["contractId"] == "lic-1"
    assert len(lic.events) == 2                                     # EventBridge publishing unchanged


def test_a_failed_push_never_fails_the_batch(lic, monkeypatch, capsys):
    monkeypatch.setattr(settings, "govern_realtime_http_domain", "abc.appsync-api.us-east-2.amazonaws.com")

    def boom(channel, events):
        raise ConnectionError("appsync down")

    monkeypatch.setattr(realtime, "_send", boom)
    assert replay(lic) == {"batchItemFailures": []}
    assert len(lic.events) == 2                                     # the rest of the work still ran
    metrics = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"PublishFailed"' in line]
    assert len(metrics) == 2 and all(m["PublishFailed"] == 1 for m in metrics)


def test_off_when_no_endpoint_is_configured(lic, monkeypatch):
    monkeypatch.setattr(settings, "govern_realtime_http_domain", "")
    monkeypatch.setattr(realtime, "_send", lambda *a: pytest.fail("must not publish"))
    assert replay(lic) == {"batchItemFailures": []}


class _Creds:
    def get_frozen_credentials(self):
        from botocore.credentials import ReadOnlyCredentials
        return ReadOnlyCredentials("AKIDEXAMPLE", "secret", None)


class _Session:
    def get_credentials(self):
        return _Creds()


def test_publish_is_sigv4_signed_and_a_rejected_event_is_a_failure(monkeypatch):
    from shared import aws

    monkeypatch.setattr(settings, "govern_realtime_http_domain", "abc.appsync-api.us-east-2.amazonaws.com")
    monkeypatch.setattr(aws, "session", lambda: _Session())
    calls = []

    def post(url, body, headers):
        calls.append((url, json.loads(body), headers))
        return (200, json.dumps({"successful": [], "failed": [{"index": 0}]}).encode())

    monkeypatch.setattr(realtime, "_post", post)
    entry = {"contractId": "c-1", "tenantId": "northfield", "action": "approve", "at": "t", "actor": None}
    assert realtime.publish(entry) is False                         # rejected → reported, not raised
    url, body, headers = calls[0]
    assert url == "https://abc.appsync-api.us-east-2.amazonaws.com/event"
    assert body["channel"] == "/govern/northfield" and len(body["events"]) == 1
    assert json.loads(body["events"][0])["actor"] == "Sonar"
    assert "/appsync/aws4_request" in headers["Authorization"]

    monkeypatch.setattr(realtime, "_post", lambda url, body, headers: (200, b'{"successful":[{"index":0}],"failed":[]}'))
    assert realtime.publish(entry) is True
    monkeypatch.setattr(realtime, "_post", lambda url, body, headers: (403, b"{}"))
    assert realtime.publish(entry) is False


# ── the subscribe handler (APPSYNC_JS), run under Node ───────────────────────

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*$/m, '')
  .replace(/^export function/mg, 'function');
const util = { unauthorized() { throw new Error('Unauthorized'); } };
const mod = new Function('util', src + '\nreturn { onSubscribe, channelFor };')(util);
const cases = JSON.parse(process.argv[2]);
const out = cases.map(([claims, path]) => {
  if (claims === null) return mod.channelFor(path);
  try { mod.onSubscribe({ identity: { claims }, info: { channel: { path } } }); return 'allowed'; }
  catch (e) { return e.message; }
});
process.stdout.write(JSON.stringify(out));
"""


def _run_handler(cases: list) -> list:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", _HARNESS, str(HANDLER_JS), json.dumps(cases)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_subscribe_handler_allows_only_the_callers_own_tenant():
    sub = "1b2c3d4e-0000-1111-2222-333344445555"
    cases = [
        [{"custom:tenantId": "northfield", "sub": sub}, "/govern/northfield"],
        [{"custom:tenantId": "northfield", "sub": sub}, "/govern/southfield"],      # another tenant
        [{"custom:tenantId": "northfield", "sub": sub}, "/govern/*"],               # wildcard
        [{"custom:tenantId": "northfield", "sub": sub}, "/govern/northfield/*"],
        [{"sub": sub}, realtime.channel_for(f"u-{sub}")],                           # private workspace
        [{"sub": sub}, "/govern/northfield"],
        [{"custom:tenantId": "  ", "sub": sub}, realtime.channel_for(f"u-{sub}")],  # blank claim → sub
        [{"custom:tenantId": "a/b", "sub": sub}, "/govern/a/b"],                    # malformed claim
        [{}, "/govern/northfield"],                                                 # no identity
    ]
    assert _run_handler(cases) == ["allowed", "Unauthorized", "Unauthorized", "Unauthorized", "allowed",
                                   "Unauthorized", "allowed", "Unauthorized", "Unauthorized"]


def test_subscribe_handler_encodes_channels_like_the_publisher():
    tenants = ["northfield", "north_field.edu", "u-1b2c-3d4e", "x" * 64, "a.b-c_d" * 9]
    assert _run_handler([[None, t] for t in tenants]) == [realtime.channel_for(t) for t in tenants]
