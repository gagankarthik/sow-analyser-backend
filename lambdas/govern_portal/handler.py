"""govern-portal Lambda — the counterparty's side of a portal link (no JWT).

    GET  /portal/{token}              the counterparty view (shared/govern/portal.py)
    POST /portal/{token}/upload-url   {filename, size} → a presigned PUT for a revised version

The token in the path IS the credential: it is hashed and looked up by key.
Unknown, malformed, expired and revoked tokens get one identical 404. Its own
Lambda (not govern-api, not govern-webhooks) so this unauthenticated surface
holds only what it needs: read the link and its contract, count views, write
a revision's PENDING document and sign one raw-bucket PUT (terraform/govern.tf).

Nothing here logs the token, its hash, the request path or the event.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.govern import portal, workflow
from shared.govern.store import ContractConflict
from shared.logger import get_logger, safe_trace

log = get_logger("blue-iq.govern-portal")
tracer = Tracer(service="blue-iq.govern-portal")

_MAX_BODY_BYTES = 4_096
_VIEW_RE = re.compile(r"/portal/([^/]+)")
_UPLOAD_RE = re.compile(r"/portal/([^/]+)/upload-url")
# No caching anywhere (the body is per-link), no referrer leak, no indexing.
_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"}
_GONE = {"error": portal.GONE_MESSAGE, "code": "not_found"}


@tracer.capture_lambda_handler(capture_response=False)
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return dispatch(event)


def dispatch(event: dict[str, Any]) -> dict[str, Any]:
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    if method == "OPTIONS":
        return _reply(204, None)
    try:
        upload = _UPLOAD_RE.fullmatch(path)
        if upload:
            if method != "POST":
                return _reply(405, {"error": "Method not allowed", "code": "method_not_allowed"})
            raw_bucket = settings.raw_bucket or os.environ.get("RAW_BUCKET", "")
            out = portal.start_upload(upload.group(1), _body(event), raw_bucket=raw_bucket)
            log.info("govern_portal.upload_url")
            return _reply(200, out)
        view = _VIEW_RE.fullmatch(path)
        if view:
            if method != "GET":
                return _reply(405, {"error": "Method not allowed", "code": "method_not_allowed"})
            return _reply(200, portal.open_link(view.group(1)))
        return _reply(404, _GONE)
    except portal.LinkGone:
        return _reply(404, _GONE)
    except workflow.GovernError as exc:
        log.info("govern_portal.refused", code=exc.code)
        return _reply(exc.status, {"error": str(exc), "code": exc.code})
    except ContractConflict:
        return _reply(409, {"error": "Something changed at the same time. Please try again.", "code": "conflict"})
    except Exception as exc:  # noqa: BLE001
        log.error("govern_portal.unhandled_error", error_type=type(exc).__name__, trace=safe_trace(exc))
        return _reply(500, {"error": "Something went wrong. Please try again.", "code": "internal"})


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
