"""webhooks Lambda — ``POST /webhooks/{provider}`` (no JWT; signed instead).

DocuSign Connect (``provider = docusign``):

1. the raw body is authenticated with HMAC-SHA256 (base64) against every
   ``X-DocuSign-Signature-<n>`` header, using the tenant's ``connectHmacKey``
   (Secrets Manager, docusign secret) — a top-level ``hmacKey`` / ``hmacKeys``
   in that secret is accepted too, for account-wide keys and rotation;
   comparison is constant time;
2. a delivery whose ``generatedDateTime`` is older than ``WEBHOOK_MAX_AGE_S``
   (default 5 minutes) — or in the future beyond that — is rejected, and an
   identical body seen before is acknowledged without acting (replay guard);
3. ``envelope-completed`` with the envelope custom field ``contractId`` →
   the ``mark_signed`` action through shared/govern/workflow.py, exactly the
   path of a reviewer's click (system actor "DocuSign").

Nothing in the body is trusted before the signature checks out: the body is
only parsed first to know WHICH tenant's key to verify with.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.govern import secrets, store, workflow
from shared.logger import get_logger

log = get_logger("blue-iq.govern-webhooks")
tracer = Tracer(service="blue-iq.govern-webhooks")

_MAX_BODY_BYTES = 1_000_000
_FUTURE_SKEW = timedelta(minutes=5)
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    path = event.get("rawPath") or event.get("path") or ""
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "").upper()
    if method != "POST":
        return _reply(405, {"error": "Method not allowed", "code": "method_not_allowed"})
    if path.rstrip("/") != "/webhooks/docusign":
        return _reply(404, {"error": "Not found", "code": "not_found"})
    if not settings.feature_enabled("docusign"):
        # DocuSign is a "Later" feature: acknowledge (so Connect does not retry)
        # and act on nothing.
        log.info("govern_webhooks.feature_disabled", feature="docusign")
        return _reply(200, {"status": "disabled"})
    try:
        return docusign(event, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — DocuSign retries a 5xx
        log.exception("govern_webhooks.unhandled", error_type=type(exc).__name__)
        return _reply(500, {"error": "Internal server error", "code": "internal"})


def raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")


def signature(key: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")


def verify(body: bytes, signatures: list[str], keys: list[str]) -> bool:
    """True when any signature header matches any key (constant-time)."""
    expected = [signature(k, body) for k in keys]
    ok = False
    for sig in signatures:
        for exp in expected:
            ok |= hmac.compare_digest(sig.strip().encode("ascii", "ignore"), exp.encode("ascii"))
    return ok


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def _custom_field(payload: dict[str, Any], name: str) -> str | None:
    summary = ((payload.get("data") or {}).get("envelopeSummary") or {})
    fields = (summary.get("customFields") or {}).get("textCustomFields") or []
    for f in fields:
        if isinstance(f, dict) and f.get("name") == name and isinstance(f.get("value"), str):
            return f["value"].strip()
    return None


def _timestamp(value: Any) -> datetime | None:
    """DocuSign writes 7 fractional digits; Python reads at most 6."""
    if not isinstance(value, str):
        return None
    return store.parse_iso(re.sub(r"(\.\d{6})\d+", r"\1", value.strip()))


def _keys_for(contract: dict[str, Any] | None) -> list[str]:
    arn = settings.docusign_secret_arn
    try:
        keys = secrets.hmac_keys(arn)
    except secrets.SecretUnavailable:
        return []
    if contract:
        tenant = secrets.tenant_value(arn, contract["tenantId"]) or {}
        if isinstance(tenant, dict) and isinstance(tenant.get("connectHmacKey"), str) and tenant["connectHmacKey"]:
            keys = [tenant["connectHmacKey"]] + keys
    return list(dict.fromkeys(keys))


def docusign(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = raw_body(event)
    if len(body) > _MAX_BODY_BYTES:
        return _reply(413, {"error": "Payload too large", "code": "too_large"})
    headers = _headers(event)
    signatures = [v for k, v in sorted(headers.items()) if k.startswith("x-docusign-signature-")]
    if not signatures:
        return _reply(401, {"error": "Missing signature", "code": "unauthenticated"})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return _reply(400, {"error": "Invalid JSON body", "code": "bad_request"})

    contract_id = _custom_field(payload, "contractId")
    contract = store.contracts.get(contract_id) if contract_id and _ID_RE.fullmatch(contract_id) else None
    keys = _keys_for(contract)
    if not keys or not verify(body, signatures, keys):
        log.warning("govern_webhooks.bad_signature", keys=len(keys))
        return _reply(401, {"error": "Invalid signature", "code": "unauthenticated"})

    sent = _timestamp(payload.get("generatedDateTime"))
    max_age = timedelta(seconds=settings.webhook_max_age_s)
    if sent is None or now - sent > max_age or sent - now > _FUTURE_SKEW:
        log.warning("govern_webhooks.stale")
        return _reply(401, {"error": "Stale or undated delivery", "code": "stale"})
    digest = hashlib.sha256(body).hexdigest()
    if not store.metrics.claim("WEBHOOK#docusign", digest, days=2):
        return _reply(200, {"status": "duplicate"})
    try:
        return _apply(payload, contract, sent, now)
    except Exception:
        store.metrics.release("WEBHOOK#docusign", digest)     # let DocuSign's retry through
        raise


def _apply(payload: dict[str, Any], contract: dict[str, Any] | None, sent: datetime, now: datetime) -> dict[str, Any]:
    event_name = str(payload.get("event") or "")
    if event_name != "envelope-completed":
        return _reply(200, {"status": "ignored", "reason": "event"})
    if contract is None:
        log.warning("govern_webhooks.unknown_contract")
        return _reply(200, {"status": "ignored", "reason": "contract"})
    data = payload.get("data") or {}
    summary = data.get("envelopeSummary") or {}
    completed = _timestamp(summary.get("completedDateTime")) or sent
    try:
        workflow.perform_action(contract["contractId"], "mark_signed",
                                {"signedAt": store.iso(completed), "envelopeId": data.get("envelopeId"),
                                 "provider": "docusign"},
                                None, now=now, system_name="DocuSign")
    except workflow.InvalidTransition:
        log.info("govern_webhooks.signed_ignored", state=contract.get("state"))
        return _reply(200, {"status": "ignored", "reason": "state"})
    log.info("govern_webhooks.signed", contractId=contract["contractId"])
    return _reply(200, {"status": "signed", "contractId": contract["contractId"]})


def _reply(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}
