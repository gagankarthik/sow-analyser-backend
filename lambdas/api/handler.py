"""Document management API Lambda — backs API Gateway HTTP API routes.

Routes
------
GET    /documents                       list all documents for the tenant
GET    /documents/upload-url            generate a presigned S3 PUT URL
GET    /documents/{docId}               get document metadata + version list
PATCH  /documents/{docId}               update title / lifecycle / docType
DELETE /documents/{docId}               delete all document data
DELETE /documents/{docId}/versions/{n}  delete version n, rollback to n-1

tenantId is read from the Cognito JWT claim first; the x-tenant-id header is
accepted only as a fallback for local development.

CORS is owned entirely by the API Gateway cors_configuration — this Lambda emits
no CORS headers. Emitting them would create a duplicate/conflicting
Access-Control-Allow-Origin header and break browser requests.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from shared.config import settings

from aws_lambda_powertools import Tracer
from shared.aws import s3_client
from shared.dynamodb import (
    delete_doc_entirely,
    delete_doc_version,
    get_doc_meta,
    get_projects_state,
    list_tenant_docs,
    put_doc_meta,
    put_projects_state,
    query_doc_versions,
    update_doc_fields,
)
from shared.logger import get_logger
from shared.opensearch import get_clause_vector, knn_search
from shared.s3 import presign_get
from shared.schema import now_iso

log = get_logger("blue-iq.api")
tracer = Tracer(service="blue-iq.api")

_VALID_DOC_TYPES  = frozenset({
    "SOW", "MSA", "AMENDMENT", "NDA",
    "LICENSE", "DPA", "BAA", "COMPLIANCE",
    "OTHER",
})
_VALID_LIFECYCLES = frozenset({
    "draft", "review", "negotiation", "approval",
    "signed", "active", "renewal", "expired",
})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    ).upper()
    path = event.get("path") or event.get("rawPath") or "/"

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


def _route(method: str, path: str, event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    # Per-tenant project groupings (cloud storage; replaces browser localStorage).
    if re.fullmatch(r"/projects/?", path):
        if method == "GET":  return _get_projects(tenant_id)
        if method == "POST": return _save_projects(event, tenant_id)
        return _err(405, "Method not allowed")

    # POST /projects/{id}/invite — invite a user (Cognito) to a project
    m = re.fullmatch(r"/projects/([^/]+)/invite/?", path)
    if method == "POST" and m:
        return _invite_member(m.group(1), event, tenant_id)

    # DELETE /projects/{id}/members/{email} — remove a member from a project
    m = re.fullmatch(r"/projects/([^/]+)/members/([^/]+)/?", path)
    if method == "DELETE" and m:
        return _remove_member(m.group(1), unquote(m.group(2)), tenant_id)

    if method == "GET" and re.fullmatch(r"/documents/?", path):
        return _list_documents(tenant_id)

    # upload-url must be matched before the /{docId} wildcard
    if method == "GET" and re.fullmatch(r"/documents/upload-url/?", path):
        return _get_upload_url(event, tenant_id)

    m = re.fullmatch(r"/documents/([^/]+)/versions/(\d+)/?", path)
    if method == "DELETE" and m:
        return _delete_version(m.group(1), int(m.group(2)), tenant_id)

    # GET /documents/{docId}/classification
    m = re.fullmatch(r"/documents/([^/]+)/classification/?", path)
    if method == "GET" and m:
        return _get_doc_classification(m.group(1), tenant_id)

    # GET /documents/{docId}/file → presigned URL to the original upload
    m = re.fullmatch(r"/documents/([^/]+)/file/?", path)
    if method == "GET" and m:
        return _get_doc_file(m.group(1), tenant_id)

    # POST /documents/{docId}/reprocess → re-run the pipeline on the stored upload
    m = re.fullmatch(r"/documents/([^/]+)/reprocess/?", path)
    if method == "POST" and m:
        return _reprocess_document(m.group(1), tenant_id)

    # GET /documents/{docId}/diff
    m = re.fullmatch(r"/documents/([^/]+)/diff/?", path)
    if method == "GET" and m:
        return _get_doc_diff(m.group(1), tenant_id)

    # GET /documents/{docId}/timeline
    m = re.fullmatch(r"/documents/([^/]+)/timeline/?", path)
    if method == "GET" and m:
        return _get_doc_timeline(m.group(1), tenant_id)

    # GET /documents/{docId}/similar?clause=<n>&k=<k> — top-KNN similar clauses
    m = re.fullmatch(r"/documents/([^/]+)/similar/?", path)
    if method == "GET" and m:
        return _get_similar_clauses(m.group(1), event, tenant_id)

    m = re.fullmatch(r"/documents/([^/]+)/?", path)
    if m:
        doc_id = m.group(1)
        if method == "GET":    return _get_document(doc_id, tenant_id)
        if method == "PATCH":  return _update_document(doc_id, tenant_id, event)
        if method == "DELETE": return _delete_document(doc_id, tenant_id)

    return _err(404, f"No route for {method} {path}")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _list_documents(tenant_id: str) -> dict[str, Any]:
    docs = list_tenant_docs(tenant_id)
    return _ok({"documents": [_clean(d) for d in docs], "count": len(docs)})


def _get_projects(tenant_id: str) -> dict[str, Any]:
    return _ok({"projects": get_projects_state(tenant_id)})


def _save_projects(event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    raw_body = event.get("body") or ""
    if len(raw_body) > 1_000_000:
        return _err(413, "Projects payload too large")
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    projects = body.get("projects") if isinstance(body, dict) else None
    if not isinstance(projects, list):
        return _err(400, "Body must be {\"projects\": [...] }")
    if len(projects) > 500:
        return _err(400, "Too many projects (max 500)")
    # Light shape guard — keep only the fields we expect, so a malformed client
    # can't write arbitrary data into the tenant record.
    clean: list[dict[str, Any]] = []
    for p in projects:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            return _err(400, "Each project needs a string id")
        doc_ids = p.get("docIds") or []
        if not isinstance(doc_ids, list):
            return _err(400, "docIds must be a list")
        clean.append({
            "id":        p["id"],
            "name":      str(p.get("name", "Untitled project"))[:200],
            "client":    (str(p["client"])[:200] if isinstance(p.get("client"), str) and p.get("client") else None),
            "createdAt": str(p.get("createdAt", "")),
            "docIds":    [str(d) for d in doc_ids if isinstance(d, str)],
            # Preserve project membership written by the invite endpoint so a
            # client's whole-list save doesn't wipe invited users.
            "members":   _clean_members(p.get("members")),
            "ownerEmail": (str(p["ownerEmail"])[:200].lower() if isinstance(p.get("ownerEmail"), str) and p.get("ownerEmail") else None),
        })
    put_projects_state(tenant_id, clean)
    return _ok({"projects": clean})


# ---------------------------------------------------------------------------
# Project membership — invite users via Cognito
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VALID_ROLES = frozenset({"owner", "editor", "viewer", "member"})


def _clean_members(raw: Any) -> list[dict[str, Any]]:
    """Shape-guard the per-project members array (invited users)."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for m in raw[:200]:
        if not isinstance(m, dict) or not isinstance(m.get("email"), str):
            continue
        role = str(m.get("role", "member")).lower()
        out.append({
            "email":     m["email"][:200],
            "role":      role if role in _VALID_ROLES else "member",
            "status":    str(m.get("status", "invited"))[:20],
            "sub":       (str(m["sub"])[:64] if isinstance(m.get("sub"), str) else None),
            "invitedAt": str(m.get("invitedAt", "")),
        })
    return out


def _invite_member(project_id: str, event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Invite a user to a project. Creates the user in the Cognito pool (tagged
    to this tenant) and emails them a temporary password, then records them as a
    member of the project. An existing pool user is added without re-creating."""
    raw_body = event.get("body") or ""
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    email = str(body.get("email", "")).strip().lower()
    role = str(body.get("role", "member")).strip().lower()
    if not _EMAIL_RE.match(email):
        return _err(400, "A valid email address is required")
    if role not in _VALID_ROLES:
        role = "member"

    projects = get_projects_state(tenant_id)
    proj = next((p for p in projects if p.get("id") == project_id), None)
    if proj is None:
        return _err(404, "Project not found")

    members = _clean_members(proj.get("members"))
    if any(m["email"] == email for m in members):
        return _err(409, "That user is already a member of this project")

    pool_id = settings.cognito_user_pool_id
    if not pool_id:
        return _err(500, "Server misconfiguration: COGNITO_USER_POOL_ID not set")

    sub: str | None = None
    status = "invited"
    try:
        from shared.aws import cognito_idp_client
        resp = cognito_idp_client().admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:tenantId", "Value": tenant_id},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        for a in resp.get("User", {}).get("Attributes", []):
            if a.get("Name") == "sub":
                sub = a.get("Value")
    except Exception as exc:  # noqa: BLE001
        # The user already exists in the pool → still add them to the project.
        if "UsernameExists" in type(exc).__name__ or "UsernameExistsException" in str(exc):
            status = "active"
        else:
            log.exception("api.invite_failed", projectId=project_id, error=str(exc))
            return _err(502, "Could not send the invitation. Check the email and try again.")

    member = {"email": email, "role": role, "status": status, "sub": sub, "invitedAt": now_iso()}
    proj["members"] = members + [member]
    put_projects_state(tenant_id, projects)
    log.info("api.member_invited", projectId=project_id, email=email, role=role, status=status)
    return _ok({"member": member, "projectId": project_id}, status=201)


def _remove_member(project_id: str, email: str, tenant_id: str) -> dict[str, Any]:
    """Remove a member from a project. Does NOT delete the Cognito user (they may
    belong to other projects); it only drops the project membership."""
    email = email.strip().lower()
    projects = get_projects_state(tenant_id)
    proj = next((p for p in projects if p.get("id") == project_id), None)
    if proj is None:
        return _err(404, "Project not found")
    members = _clean_members(proj.get("members"))
    kept = [m for m in members if m["email"] != email]
    if len(kept) == len(members):
        return _err(404, "Member not found on this project")
    proj["members"] = kept
    put_projects_state(tenant_id, projects)
    log.info("api.member_removed", projectId=project_id, email=email)
    return _ok({"removed": True, "email": email, "projectId": project_id})


def _get_similar_clauses(doc_id: str, event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Top-KNN: clauses across the tenant's documents most similar to a given
    clause, using its stored embedding vector (cosine). Returns [] when the
    clause isn't embedded yet — never a heuristic guess."""
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    qs = event.get("queryStringParameters") or {}
    clause = (qs.get("clause") or "").strip()
    if not clause:
        return _err(400, "clause query parameter is required")
    try:
        k = max(1, min(10, int(qs.get("k", "5"))))
    except (TypeError, ValueError):
        k = 5

    vector = get_clause_vector(doc_id, clause)
    if not vector:
        return _ok({"similar": []})

    # Pull a few extra so we can drop the clause itself and still return k.
    hits = knn_search(vector=vector, tenant_id=tenant_id, k=k + 6)
    titles: dict[str, str] = {}
    def _title(did: str) -> str:
        if did not in titles:
            m = get_doc_meta(did)
            titles[did] = (m or {}).get("title") or "Untitled document"
        return titles[did]

    out: list[dict[str, Any]] = []
    for h in hits:
        src = h.get("_source") or {}
        hid, hclause = src.get("docId"), src.get("clauseNumber")
        if hid == doc_id and hclause == clause:
            continue  # the clause itself
        out.append({
            "docId":        hid,
            "docTitle":     _title(hid) if hid else "Untitled document",
            "docType":      src.get("docType"),
            "clauseNumber": hclause,
            "category":     src.get("category"),
            "score":        round(float(h.get("_score", 0)), 4),
            "text":         (src.get("text") or "")[:280],
        })
        if len(out) >= k:
            break
    return _ok({"similar": out})


def _get_upload_url(event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    qs           = event.get("queryStringParameters") or {}
    raw_filename = qs.get("filename", "").strip()
    if not raw_filename:
        return _err(400, "Missing required query parameter: filename")

    # Sanitize: collapse path separators, restrict to a safe character set.
    filename = os.path.basename(raw_filename.replace("\\", "/"))
    if filename in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9._ -]{1,200}", filename):
        return _err(400, "Invalid filename")
    # Only formats the parse stage can actually extract. Legacy binary .doc has
    # no parser in the Lambda layer, so we don't advertise it.
    if not re.search(r"\.(pdf|docx|txt)$", filename, re.IGNORECASE):
        return _err(400, "Unsupported file type. Allowed: pdf, docx, txt")

    doc_type = qs.get("docType", "OTHER").strip().upper()
    if doc_type not in _VALID_DOC_TYPES:
        return _err(400, f"Invalid docType '{doc_type}'. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}")

    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        log.error("api.upload_url.missing_bucket")
        return _err(500, "Server misconfiguration: RAW_BUCKET not set")

    doc_id = str(uuid.uuid4())
    key    = f"tenants/{tenant_id}/uploads/{doc_id}/{filename}"

    # ContentType is intentionally NOT signed — the browser can send the real
    # MIME type without breaking the signature (SignedHeaders = "host" only).
    upload_url = s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": raw_bucket, "Key": key},
        ExpiresIn=300,
    )

    # Write a PENDING row immediately so the document appears in the UI during
    # processing. The persist stage upserts this row with the full extracted data.
    title = filename.rsplit(".", 1)[0] or filename
    try:
        put_doc_meta({
            "docId":           doc_id,
            "tenantId":        tenant_id,
            "title":           title,
            "docType":         doc_type,
            "lifecycle":       "draft",
            "status":          "PENDING",
            "parties":         [],
            "effectiveDate":   None,
            "parentDocId":     None,
            "rawKey":          key,
            "processedPrefix": "",
            "structuralHash":  "",
            "checksum":        "",
            "latestVersion":   0,
        })
    except Exception:
        log.exception("api.upload_url.meta_write_failed", docId=doc_id)

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
                "versionNumber":     int(v.get("versionNumber", 0)),
                "extractionMethod":  v.get("extractionMethod", ""),
                "createdAt":         v.get("createdAt", ""),
                "parsedKey":         v.get("parsedKey", ""),
                "classificationKey": v.get("classificationKey", ""),
                "timelineKey":       v.get("timelineKey"),
                "diffKey":           v.get("diffKey"),
            }
            for v in versions_raw
        ],
        key=lambda v: v["versionNumber"],
    )
    return _ok({"document": _clean(meta), "versions": versions})


def _content_type(name: str) -> str:
    n = name.lower()
    if n.endswith(".pdf"):  return "application/pdf"
    if n.endswith(".docx"): return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if n.endswith(".doc"):  return "application/msword"
    if n.endswith(".txt"):  return "text/plain"
    return "application/octet-stream"


def _get_doc_file(doc_id: str, tenant_id: str) -> dict[str, Any]:
    """Return a short-lived presigned URL to the original uploaded file so the
    UI can render it (PDF inline, DOCX via client-side conversion)."""
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")
    raw_key = meta.get("rawKey")
    if not raw_key:
        return _err(404, "Original file is not available for this document")
    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        return _err(500, "Server misconfiguration: RAW_BUCKET not set")
    filename = raw_key.rsplit("/", 1)[-1]
    url = presign_get(raw_bucket, raw_key, expires_seconds=900)
    return _ok({"url": url, "filename": filename, "contentType": _content_type(filename)})


def _reprocess_document(doc_id: str, tenant_id: str) -> dict[str, Any]:
    """Re-run the ingestion pipeline on the stored upload.

    Re-writes the raw object in place (MetadataDirective=REPLACE), which fires the
    same S3 'Object Created' EventBridge rule that starts a Step Functions
    execution for a fresh upload — no separate pipeline-invoke permission needed.
    """
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")
    raw_key    = meta.get("rawKey")
    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_key or not raw_bucket:
        return _err(409, "Original upload is no longer available to re-analyze")

    try:
        s3_client().copy_object(
            Bucket=raw_bucket,
            Key=raw_key,
            CopySource={"Bucket": raw_bucket, "Key": raw_key},
            MetadataDirective="REPLACE",
            Metadata={"reprocessedat": now_iso()},
            ContentType=_content_type(raw_key),
        )
    except Exception as exc:
        log.exception("api.reprocess_failed", docId=doc_id, error=str(exc))
        return _err(500, "Failed to re-trigger analysis")

    update_doc_fields(doc_id, {"status": "PENDING"})
    log.info("api.reprocess_triggered", docId=doc_id)
    return _ok({"reprocessing": True, "docId": doc_id})


def _update_document(doc_id: str, tenant_id: str, event: dict[str, Any]) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    raw_body = event.get("body") or ""
    if not raw_body:
        return _err(400, "Request body is required")
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object")
    if not body:
        return _err(400, "No fields to update")

    allowed = {"title", "lifecycle", "docType"}
    unknown = set(body.keys()) - allowed
    if unknown:
        return _err(400, f"Unknown field(s): {', '.join(sorted(unknown))}")

    if "lifecycle" in body and body["lifecycle"] not in _VALID_LIFECYCLES:
        return _err(400, f"Invalid lifecycle. Must be one of: {', '.join(sorted(_VALID_LIFECYCLES))}")

    if "docType" in body and body["docType"] not in _VALID_DOC_TYPES:
        return _err(400, f"Invalid docType. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}")

    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            return _err(400, "title cannot be empty")
        if len(title) > 500:
            return _err(400, "title is too long (max 500 characters)")
        body["title"] = title

    update_doc_fields(doc_id, body)
    updated = get_doc_meta(doc_id)
    log.info("api.document_updated", docId=doc_id, fields=list(body.keys()))
    return _ok({"document": _clean(updated)})


def _delete_version(doc_id: str, version_number: int, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    versions = query_doc_versions(doc_id)
    if len(versions) <= 1:
        delete_doc_entirely(doc_id)
        log.info("api.delete_last_version", docId=doc_id)
        return _ok({
            "deleted":        True,
            "docId":          doc_id,
            "versionDeleted": version_number,
            "message":        "Last version deleted — document removed.",
        })

    new_meta = delete_doc_version(doc_id, version_number)
    log.info("api.version_deleted", docId=doc_id, deletedVersion=version_number,
             rolledBackTo=(new_meta or {}).get("latestVersion"))
    return _ok({
        "deleted":        True,
        "docId":          doc_id,
        "versionDeleted": version_number,
        "latestVersion":  (new_meta or {}).get("latestVersion"),
        "document":       _clean(new_meta) if new_meta else None,
    })


def _delete_document(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")

    # Tear down ALL storage for this document, not just the DynamoDB rows:
    #   - the original upload in the raw bucket
    #   - every processed artefact (parsed/classification/diff/timeline JSON)
    #   - the clause vectors + text in the OpenSearch index
    # Each is best-effort so a single failure can't strip-mine the others or
    # leave the document un-deletable; the DynamoDB rows are removed last.
    _purge_storage(doc_id, meta)

    delete_doc_entirely(doc_id)
    log.info("api.document_deleted", docId=doc_id)
    return _ok({"deleted": True, "docId": doc_id})


def _purge_storage(doc_id: str, meta: dict[str, Any]) -> None:
    raw_bucket       = os.environ.get("RAW_BUCKET", "")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    raw_key          = meta.get("rawKey")
    processed_prefix = meta.get("processedPrefix") or f"{meta.get('tenantId', '')}/{doc_id}/"

    from shared.s3 import delete_object, delete_prefix

    if raw_bucket and raw_key:
        try:
            delete_object(raw_bucket, raw_key)
        except Exception as exc:
            log.warning("api.delete.raw_failed", docId=doc_id, error=str(exc))

    if processed_bucket and processed_prefix:
        try:
            delete_prefix(processed_bucket, processed_prefix)
        except Exception as exc:
            log.warning("api.delete.processed_failed", docId=doc_id, error=str(exc))

    try:
        from shared.opensearch import delete_doc as os_delete_doc
        os_delete_doc(doc_id)
    except Exception as exc:
        log.warning("api.delete.opensearch_failed", docId=doc_id, error=str(exc))


def _get_doc_classification(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")
    versions = query_doc_versions(doc_id)
    if not versions:
        return _err(404, "No processed versions found")
    latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
    key = latest.get("classificationKey")
    if not key:
        return _err(404, "Classification not yet available")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    if not processed_bucket:
        return _err(500, "Server misconfiguration: PROCESSED_BUCKET not set")
    try:
        from shared.s3 import get_json
        data = get_json(processed_bucket, key)
        return _ok(data)
    except Exception as exc:
        log.warning("api.classification_read_failed", docId=doc_id, error=str(exc))
        return _err(404, "Classification data not found in storage")


def _get_doc_diff(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")
    versions = query_doc_versions(doc_id)
    if not versions:
        return _err(404, "No processed versions found")
    latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
    key = latest.get("diffKey")
    if not key:
        return _err(404, "Diff not available — this is the first version or no parent was found")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    if not processed_bucket:
        return _err(500, "Server misconfiguration: PROCESSED_BUCKET not set")
    try:
        from shared.s3 import get_json
        data = get_json(processed_bucket, key)
        return _ok(data)
    except Exception as exc:
        log.warning("api.diff_read_failed", docId=doc_id, error=str(exc))
        return _err(404, "Diff data not found in storage")


def _get_doc_timeline(doc_id: str, tenant_id: str) -> dict[str, Any]:
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id:
        return _err(404, "Document not found")
    versions = query_doc_versions(doc_id)
    if not versions:
        return _err(404, "No processed versions found")
    latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
    key = latest.get("timelineKey")
    if not key:
        return _err(404, "Timeline not yet available")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    if not processed_bucket:
        return _err(500, "Server misconfiguration: PROCESSED_BUCKET not set")
    try:
        from shared.s3 import get_json
        data = get_json(processed_bucket, key)
        return _ok(data)
    except Exception as exc:
        log.warning("api.timeline_read_failed", docId=doc_id, error=str(exc))
        return _err(404, "Timeline data not found in storage")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(event: dict[str, Any]) -> str:
    ctx        = event.get("requestContext", {}) or {}
    authorizer = ctx.get("authorizer", {}) or {}
    jwt_claims = (authorizer.get("jwt") or {}).get("claims") or {}
    return (
        jwt_claims.get("custom:tenantId")
        or (authorizer.get("claims") or {}).get("custom:tenantId")  # REST v1 compat
        or (event.get("headers") or {}).get("x-tenant-id")
        or "default"
    )


def _clean(item: dict[str, Any] | None) -> dict[str, Any]:
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
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }


def _err(status: int, message: str) -> dict[str, Any]:
    return _ok({"error": message}, status=status)
