"""DocuSign Connect webhook (lambdas/govern_webhooks/handler.py): HMAC
verification (good / bad / missing / rotated key), stale and replayed
deliveries, and envelope-completed → mark_signed through the workflow."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from govern_intake import handler as intake
from govern_support import analysed_event, install_analysis, seed_doc
from govern_webhooks import handler as webhooks
from shared.config import settings
from shared.govern import store, workflow

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
KEY = "tenant-connect-key"
TENANT = "u-aaaaaaaa-0000-4000-8000-0000000000a1"


@pytest.fixture
def ready(gov, ddb, monkeypatch):
    """lic-1 out for signature; the DocuSign secret holds the tenant's key."""
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))
    actor = {"email": "dana@osu.edu", "name": "Dana Ruiz"}
    store.contracts.mutate("lic-1", lambda c: c.update(state="ready_to_sign", stage="approval"))
    workflow.perform_action("lic-1", "send_for_signature", {"provider": "docusign"}, actor)
    monkeypatch.setattr(settings, "docusign_secret_arn", "arn:docusign")
    gov.secrets["arn:docusign"] = json.dumps({"tenants": {TENANT: {"integrationKey": "i", "userId": "u",
                                                                   "privateKey": "p", "connectHmacKey": KEY}}})
    return gov


def payload(event="envelope-completed", contract_id="lic-1", generated=NOW - timedelta(seconds=30)):
    return json.dumps({
        "event": event, "generatedDateTime": generated.strftime("%Y-%m-%dT%H:%M:%S.1234567Z"),
        "data": {"envelopeId": "env-42", "envelopeSummary": {
            "status": "completed", "completedDateTime": "2026-10-14T14:58:00.0000000Z",
            "customFields": {"textCustomFields": [{"name": "contractId", "value": contract_id}]}}},
    })


def sign(body: str, key: str = KEY) -> str:
    return base64.b64encode(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()).decode()


def deliver(body: str, signatures: list[str] | None = None, b64: bool = False):
    headers = {f"X-DocuSign-Signature-{i + 1}": s for i, s in enumerate(signatures if signatures is not None else [sign(body)])}
    event = {"rawPath": "/webhooks/docusign", "requestContext": {"http": {"method": "POST"}}, "headers": headers,
             "body": base64.b64encode(body.encode()).decode() if b64 else body, "isBase64Encoded": b64}
    resp = webhooks.docusign(event, NOW)
    return resp["statusCode"], json.loads(resp["body"])


def test_a_signed_envelope_marks_the_contract_signed(ready):
    status, body = deliver(payload(), b64=True)
    assert (status, body) == (200, {"status": "signed", "contractId": "lic-1"})
    c = store.contracts.get("lic-1")
    assert c["state"] == "signed" and c["signedAt"] == "2026-10-14T14:58:00Z"
    assert c["signature"]["envelopeId"] == "env-42" and c["signature"]["provider"] == "docusign"
    last = ready.activity_for("lic-1")[-1]
    assert last["action"] == "signed" and last["actor"] is None
    assert last["summary"] == "DocuSign reported this agreement signed."


def test_bad_or_missing_signatures_are_rejected(ready):
    body = payload()
    assert deliver(body, [sign(body, "wrong-key")])[0] == 401
    assert deliver(body, [])[0] == 401
    assert deliver(body.replace("env-42", "env-43"), [sign(body)])[0] == 401        # body tampered
    assert store.contracts.get("lic-1")["state"] == "out_for_signature"


def test_any_header_and_an_account_wide_key_verify(ready):
    ready.secrets["arn:docusign"] = json.dumps({"hmacKey": "account-key"})
    from shared.govern import secrets
    secrets.clear_cache()
    body = payload()
    status, _ = deliver(body, ["bogus", sign(body, "account-key")])
    assert status == 200 and store.contracts.get("lic-1")["state"] == "signed"


def test_stale_future_and_undated_deliveries_are_rejected(ready):
    for generated in (NOW - timedelta(minutes=6), NOW + timedelta(minutes=6)):
        body = payload(generated=generated)
        assert deliver(body) == (401, {"error": "Stale or undated delivery", "code": "stale"})
    body = json.dumps({"event": "envelope-completed"})
    assert deliver(body)[0] == 401


def test_a_replayed_delivery_does_nothing_twice(ready):
    body = payload()
    assert deliver(body)[1]["status"] == "signed"
    assert deliver(body) == (200, {"status": "duplicate"})
    assert [a for a in ready.actions("lic-1") if a == "signed"] == ["signed"]


def test_other_events_and_unknown_contracts_are_acknowledged(ready):
    assert deliver(payload(event="envelope-sent"))[1] == {"status": "ignored", "reason": "event"}
    ready.secrets["arn:docusign"] = json.dumps({"hmacKey": KEY})
    from shared.govern import secrets
    secrets.clear_cache()
    assert deliver(payload(contract_id="nope", generated=NOW - timedelta(seconds=5)))[1] == {"status": "ignored", "reason": "contract"}


def test_signed_twice_or_in_the_wrong_state_is_safe(ready):
    store.contracts.mutate("lic-1", lambda c: c.update(state="in_review", stage="review"))
    assert deliver(payload())[1] == {"status": "ignored", "reason": "state"}


def test_no_secret_configured_fails_closed(ready, monkeypatch):
    monkeypatch.setattr(settings, "docusign_secret_arn", "")
    assert deliver(payload())[0] == 401


def test_routing_of_the_lambda_entry(ready):
    from govern_support import lambda_context

    event = {"rawPath": "/webhooks/acme", "requestContext": {"http": {"method": "POST"}}, "body": "{}"}
    assert webhooks.handler(event, lambda_context())["statusCode"] == 404
    event = {"rawPath": "/webhooks/docusign", "requestContext": {"http": {"method": "GET"}}}
    assert webhooks.handler(event, lambda_context())["statusCode"] == 405
