"""Document management API Lambda — backs API Gateway routes.

Routes
------
GET    /documents                         → list all documents for tenant
GET    /documents/upload-url              → generate a presigned S3 PUT URL
GET    /documents/{docId}                 → get document + all versions
DELETE /documents/{docId}                 → delete all document data
DELETE /documents/{docId}/versions/{n}   → delete version n, rollback to n-1

The tenantId is read from the Cognito authorizer context or a fallback
`x-tenant-id` header for dev/testing.

All responses are JSON with CORS headers.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any

from aws_lambda_powertools import Tracer
from shared.aws import s3_client
from shared.dynamodb import (
    delete_doc_entirely,
    delete_doc_version,
    get_doc_meta,
    list_tenant_docs,
    query_doc_versions,
)
from shared.logger import get_logger
from shared.s3 import object_exists

# The API Lambda role needs s3:PutObject on the raw bucket to sign presigned
# PUT URLs on behalf of callers.  See aws_iam_role_policy.api in lambda.tf.

log = get_logger("blue-iq.api")
tracer = Tracer(service="blue-iq.api")

_CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,x-tenant-id",
    "Access-Control-Allow-Methods": "GET,DELETE,OPTIONS",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")).upper()
    path   = event.get("path") or event.get("rawPath") or "/"

    # Preflight
    if method == "OPTIONS":
        return _ok({})

    tenant_id = _tenant(event)
    log.append_keys(tenantId=tenant_id, method=method, path=path)

    try:
        return _route(method, path, event, tenant_id)
    except Exception as exc:
        log.exception("api.unhandled_error", error=str(exc))
        return _err(500, "Internal server error")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _route(method: str, path: str, event: dict, tenant_id: str) -> dict[str, Any]:
    # GET /documents
    if method == "GET" and re.fullmatch(r"/documents/?", path):
        return _list_documents(tenant_id)

    # GET /documents/upload-url  — must come before the /{docId} wildcard
    if method == "GET" and re.fullmatch(r"/documents/upload-url/?", path):
        return _get_upload_url(event, tenant_id)

    # GET /documents/{docId}
    m = re.fullmatch(r"/documents/([^/]+)/?", path)
    if method == "GET" and m:
        return _get_document(m.group(1), tenant_id)

    # DELETE /documents/{docId}/versions/{n}
    m = re.fullmatch(r"/documents/([^/]+)/versions/(\d+)/?", path)
    if method == "DELETE" and m:
        return _delete_version(m.group(1), int(m.group(2)), tenant_id)

    # DELETE /documents/{docId}
    m = re.fullmatch(r"/documents/([^/]+)/?", path)
    if method == "DELETE" and m:
        return _delete_document(m.group(1), tenant_id)

    return _err(404, f"No route for {method} {path}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _list_documents(tenant_id: str) -> dict[str, Any]:
    docs = list_tenant_docs(tenant_id)
    return _ok({"documents": [_clean(d) for d in docs], "count": len(docs)})


_VALID_DOC_TYPES = {"SOW", "MSA", "AMENDMENT", "NDA", "OTHER"}


def _get_upload_url(event: dict, tenant_id: str) -> dict[str, Any]:
    """Return a presigned S3 PUT URL so the client can upload a document directly."""
    qs = event.get("queryStringParameters") or {}
    filename = qs.get("filename", "").strip()
    if not filename:
        return _err(400, "Missing required query parameter: filename")

    doc_type = qs.get("docType", "OTHER").strip().upper()
    if doc_type not in _VALID_DOC_TYPES:
        return _err(400, f"Invalid docType '{doc_type}'. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}")

    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        log.error("api.upload_url.missing_bucket")
        return _err(500, "Server misconfiguration: RAW_BUCKET not set")

    doc_id = str(uuid.uuid4())
    key = f"tenants/{tenant_id}/uploads/{doc_id}/{filename}"

    upload_url = s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": raw_bucket, "Key": key, "ContentType": "application/octet-stream"},
        ExpiresIn=300,
    )

    log.info("api.upload_url_generated", docId=doc_id, key=key, docType=doc_type)
    return _ok({"uploadUrl": upload_url, "key": key, "docId": doc_id})


def _get_document(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    versions_raw = query_doc_versions(doc_id)
    versions = sorted(
        [
            {
                "versionNumber":    int(v.get("versionNumber", 0)),
                "extractionMethod": v.get("extractionMethod", ""),
                "createdAt":        v.get("createdAt", ""),
                "parsedKey":        v.get("parsedKey", ""),
                "classificationKey": v.get("classificationKey", ""),
                "timelineKey":      v.get("timelineKey"),
                "diffKey":          v.get("diffKey"),
            }
            for v in versions_raw
        ],
        key=lambda v: v["versionNumber"],
    )
    return _ok({"document": _clean(meta), "versions": versions})


def _delete_version(doc_id: str, version_number: int, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    versions = query_doc_versions(doc_id)
    if len(versions) <= 1:
        # Only one version left — delete the whole document.
        delete_doc_entirely(doc_id)
        log.info("api.delete_last_version", docId=doc_id)
        return _ok({"deleted": True, "docId": doc_id, "versionDeleted": version_number,
                    "message": "Last version deleted — document removed."})

    new_meta = delete_doc_version(doc_id, version_number)
    log.info("api.version_deleted", docId=doc_id, deletedVersion=version_number,
             rolledBackTo=new_meta.get("latestVersion") if new_meta else None)
    return _ok({
        "deleted":         True,
        "docId":           doc_id,
        "versionDeleted":  version_number,
        "latestVersion":   new_meta.get("latestVersion") if new_meta else None,
        "document":        _clean(new_meta) if new_meta else None,
    })


def _delete_document(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    delete_doc_entirely(doc_id)
    log.info("api.document_deleted", docId=doc_id)
    return _ok({"deleted": True, "docId": doc_id})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(event: dict) -> str:
    ctx = event.get("requestContext", {})
    # Cognito authorizer injects tenantId into claims / resolver context.
    tenant = (
        ctx.get("authorizer", {}).get("claims", {}).get("custom:tenantId")
        or ctx.get("authorizer", {}).get("tenantId")
        or (event.get("headers") or {}).get("x-tenant-id")
        or "default"
    )
    return tenant


def _clean(item: dict | None) -> dict:
    """Strip DDB internals and convert Decimal → float/int for JSON."""
    if not item:
        return {}
    skip = {"PK", "SK", "GSI1PK", "GSI1SK", "entityType"}
    return {k: _conv(v) for k, v in item.items() if k not in skip}


def _conv(val: Any) -> Any:
    if isinstance(val, Decimal):
        return int(val) if val == val.to_integral_value() else float(val)
    if isinstance(val, dict):
        return {k: _conv(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_conv(v) for v in val]
    return val


def _ok(body: Any, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers":    {**_CORS, "Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }


def _err(status: int, message: str) -> dict[str, Any]:
    return _ok({"error": message}, status=status)
