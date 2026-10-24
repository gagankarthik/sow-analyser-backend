"""govern-signing Lambda — the signer's side of Govern e-signature (no JWT).

    GET  /sign/{token}             the signer's page (shared/govern/esign.py)
    POST /sign/{token}/complete    adopt a signature, fill the fields, sign
    POST /sign/{token}/decline     decline with a reason
    GET  /sign/{token}/download    the sealed copy, once everyone has signed

The token in the path IS the credential: it is hashed and looked up by key.
Unknown, malformed, revoked and expired tokens get one identical 404. Its own
Lambda so this unauthenticated surface holds only what signing needs
(terraform/govern_esign.tf). The signer's IP address and browser are recorded
for the certificate of completion; nothing here logs the token, the path or
the request body.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from aws_lambda_powertools import Tracer

from shared.govern import esign, workflow
from shared.govern.store import ContractConflict
from shared.logger import get_logger, safe_trace

log = get_logger("blue-iq.govern-signing")
tracer = Tracer(service="blue-iq.govern-signing")

# A drawn signature and initials as PNG data URLs, plus field values.
_MAX_BODY_BYTES = 900_000
_VIEW_RE = re.compile(r"/sign/([^/]+)")
_ACTION_RE = re.compile(r"/sign/([^/]+)/(complete|decline|download)")
_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"}
_GONE = {"error": esign.GONE_MESSAGE, "code": "not_found"}


@tracer.capture_lambda_handler(capture_response=False)
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return dispatch(event)


def _client(event: dict[str, Any]) -> tuple[str | None, str | None]:
    http = event.get("requestContext", {}).get("http", {})
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    ip = http.get("sourceIp") or None
    agent = http.get("userAgent") or headers.get("user-agent") or None
    return (str(ip)[:64] if ip else None), (str(agent)[:300] if agent else None)


def dispatch(event: dict[str, Any]) -> dict[str, Any]:
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    if method == "OPTIONS":
        return _reply(204, None)
    ip, agent = _client(event)
    try:
        action = _ACTION_RE.fullmatch(path)
        if action:
            token, verb = action.group(1), action.group(2)
            if verb == "download":
                if method != "GET":
                    return _method_not_allowed()
                return _reply(200, esign.signer_download(token))
            if method != "POST":
                return _method_not_allowed()
            if verb == "complete":
                out = esign.complete(token, _body(event), ip=ip, user_agent=agent)
                log.info("govern_signing.signed", envelopeStatus=out.get("envelopeStatus"))
            else:
                out = esign.decline(token, _body(event), ip=ip, user_agent=agent)
                log.info("govern_signing.declined")
            return _reply(200, out)
        view = _VIEW_RE.fullmatch(path)
        if view:
            if method != "GET":
                return _method_not_allowed()
            return _reply(200, esign.signer_view(view.group(1), ip=ip, user_agent=agent))
        return _reply(404, _GONE)
    except esign.LinkGone:
        return _reply(404, _GONE)
    except workflow.GovernError as exc:
        log.info("govern_signing.refused", code=exc.code)
        return _reply(exc.status, {"error": str(exc), "code": exc.code})
    except ContractConflict:
        return _reply(409, {"error": "Something changed at the same time. Please try again.", "code": "conflict"})
    except Exception as exc:  # noqa: BLE001
        log.error("govern_signing.unhandled_error", error_type=type(exc).__name__, trace=safe_trace(exc))
        return _reply(500, {"error": "Something went wrong. Please try again.", "code": "internal"})


def _method_not_allowed() -> dict[str, Any]:
    return _reply(405, {"error": "Method not allowed", "code": "method_not_allowed"})


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > _MAX_BODY_BYTES:
        raise workflow.BadRequest("Request body is too large")
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise workflow.BadRequest("Invalid JSON body")
    if not isinstance(body, dict):
        raise workflow.BadRequest("Body must be a JSON object")
    return body


def _reply(status: int, body: Any) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(_HEADERS), "body": json.dumps(body, default=str) if body is not None else ""}
