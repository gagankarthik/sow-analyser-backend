"""Real-time "contract changed" pushes to open Govern pages (AppSync Events).

The activity stream Lambda calls :func:`publish` for every new activity entry.
It sends a SMALL invalidation message to the tenant's channel, and the browser
re-reads the contract list / record / bell through the normal REST API:

    {"type": "contract.changed", "contractId": "...", "action": "approve",
     "actor": "Dana Reyes", "at": "2026-10-20T10:00:00Z"}

Nothing else from the entry travels: no contract contents, values, comments,
details or email addresses. ``actor`` is the person's display name only (or
"Sonar" for a system entry, or null when only an email is known).

Channels are per tenant, ``/govern/<encoded tenant>``. Channel segments allow
only letters, digits and "-", and at most 50 characters, so the tenant id is
escaped (``-`` → ``-0``, ``_`` → ``-1``, ``.`` → ``-2``; one-to-one) and cut
into 50-character segments. The same encoding is enforced on subscribe by the
channel namespace handler (terraform/appsync/govern_channel.js) and computed by
the web app (lib/govern/realtime-logic.ts).

Publishing is best effort: a push that fails is logged with a CloudWatch
metric and dropped. It never raises, so it can never fail the stream batch —
open pages fall back to their polling safety net.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse
from typing import Any

from shared.config import settings
from shared.logger import get_logger

log = get_logger("blue-iq.govern-realtime")

NAMESPACE = "govern"
MESSAGE_TYPE = "contract.changed"
MESSAGE_KEYS = ("type", "contractId", "action", "actor", "at")
SEGMENT_MAX = 50
METRIC_NAMESPACE = "BlueIQ/GovernRealtime"

# The tenant ids shared/auth.py accepts.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ESCAPES = (("-", "-0"), ("_", "-1"), (".", "-2"))   # "-" first: the escape character


def channel_for(tenant_id: str) -> str:
    """The tenant's channel path. Raises ValueError for an id auth would reject."""
    if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
        raise ValueError("not a valid tenant id")
    encoded = tenant_id
    for raw, escaped in _ESCAPES:
        encoded = encoded.replace(raw, escaped)
    segments = [encoded[i:i + SEGMENT_MAX] for i in range(0, len(encoded), SEGMENT_MAX)]
    return "/" + "/".join([NAMESPACE, *segments])


def actor_name(actor: Any) -> str | None:
    """Display name only — never an email address."""
    if actor is None:
        return "Sonar"
    if isinstance(actor, dict):
        name = str(actor.get("name") or "").strip()
        return name[:200] if name and "@" not in name else None
    return None


def message_for(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The invalidation message for an activity entry (None: nothing to send)."""
    contract_id = entry.get("contractId")
    if not isinstance(contract_id, str) or not contract_id:
        return None
    return {
        "type": MESSAGE_TYPE,
        "contractId": contract_id,
        "action": str(entry.get("action") or ""),
        "actor": actor_name(entry.get("actor")),
        "at": str(entry.get("at") or ""),
    }


def publish(entry: dict[str, Any]) -> bool:
    """Push the entry's invalidation to its tenant's channel. True when sent.

    Never raises: a failure is logged, counted and dropped."""
    if not settings.govern_realtime_http_domain:
        return False
    try:
        message = message_for(entry)
        if message is None:
            return False
        channel = channel_for(str(entry.get("tenantId") or ""))
        _send(channel, [json.dumps(message, separators=(",", ":"))])
        return True
    except Exception as exc:  # noqa: BLE001 — best effort by design
        log.warning("govern_realtime.publish_failed", error_type=type(exc).__name__,
                    action=entry.get("action"))
        _count_failure()
        return False


# ---------------------------------------------------------------------------
# Transport — SigV4-signed HTTP publish (POST https://<domain>/event)
# ---------------------------------------------------------------------------

# One kept-alive connection per warm Lambda; the stream handler is single-threaded.
_conn: http.client.HTTPSConnection | None = None
_TIMEOUT_S = 2.0


def _send(channel: str, events: list[str]) -> None:
    """Publish ``events`` (JSON strings) to ``channel``; raises on any failure."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    from shared import aws

    url = f"https://{settings.govern_realtime_http_domain}/event"
    body = json.dumps({"channel": channel, "events": events}).encode("utf-8")
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    creds = aws.session().get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials")
    SigV4Auth(creds.get_frozen_credentials(), "appsync", settings.aws_region).add_auth(req)
    status, payload = _post(url, body, dict(req.headers.items()))
    if status >= 300:
        raise RuntimeError(f"publish answered HTTP {status}")
    failed = (json.loads(payload or b"{}") or {}).get("failed") or []
    if failed:
        raise RuntimeError(f"publish rejected {len(failed)} event(s)")


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    """POST over the shared connection; a connection closed while idle is
    reopened and the request sent once more."""
    global _conn
    parts = urllib.parse.urlsplit(url)
    for attempt in (1, 2):
        if _conn is None:
            _conn = http.client.HTTPSConnection(parts.netloc, timeout=_TIMEOUT_S)
        try:
            _conn.request("POST", parts.path or "/", body=body, headers=headers)
            resp = _conn.getresponse()
            return resp.status, resp.read()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            _conn.close()
            _conn = None
            if attempt == 2:
                raise
        except Exception:
            _conn.close()
            _conn = None
            raise
    raise RuntimeError("unreachable")


def _count_failure() -> None:
    """One CloudWatch Embedded Metric Format record: PublishFailed = 1."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": METRIC_NAMESPACE, "Dimensions": [["Stage"]],
            "Metrics": [{"Name": "PublishFailed", "Unit": "Count"}]}]},
        "Stage": settings.stage, "PublishFailed": 1,
    }))
