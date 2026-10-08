"""Upload bookkeeping shared by every route that hands out an upload URL
(``GET /documents/upload-url`` and Govern's revision upload).

The pipeline only processes an object whose key is
``tenants/<tenantId>/uploads/<docId>/<filename>`` AND that has a PENDING META
row owned by that tenant, so both are built here, once.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Only formats the parse stage can extract (legacy binary .doc has no parser).
_ALLOWED_EXT = re.compile(r"\.(pdf|docx|txt)$", re.IGNORECASE)
_SAFE_NAME = re.compile(r"[A-Za-z0-9._ -]{1,200}")


def clean_upload_filename(raw: Any) -> tuple[str | None, str | None]:
    """(filename, None) for an acceptable upload name, else (None, problem)."""
    raw_name = str(raw or "").strip()
    if not raw_name:
        return None, "Missing required query parameter: filename"
    filename = os.path.basename(raw_name.replace("\\", "/"))
    if filename in ("", ".", "..") or not _SAFE_NAME.fullmatch(filename):
        return None, "Invalid filename"
    if not _ALLOWED_EXT.search(filename):
        return None, "Unsupported file type. Allowed: pdf, docx, txt"
    return filename, None


def upload_key(tenant_id: str, doc_id: str, filename: str) -> str:
    return f"tenants/{tenant_id}/uploads/{doc_id}/{filename}"


def pending_document_meta(*, doc_id: str, tenant_id: str, owner_sub: str, owner_email: str | None,
                          filename: str, doc_type: str, project_ids: list[str],
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The PENDING META row written before the browser uploads. The persist
    stage later fills in the analysis fields (and keeps everything else)."""
    return {
        "docId":           doc_id,
        "tenantId":        tenant_id,
        "ownerSub":        owner_sub,
        "ownerEmail":      owner_email,
        "projectIds":      list(project_ids),
        "title":           filename.rsplit(".", 1)[0] or filename,
        "docType":         doc_type,
        "lifecycle":       "draft",
        "status":          "PENDING",
        "parties":         [],
        "effectiveDate":   None,
        "parentDocId":     None,
        "rawKey":          upload_key(tenant_id, doc_id, filename),
        "processedPrefix": "",
        "structuralHash":  "",
        "checksum":        "",
        "latestVersion":   0,
        **(extra or {}),
    }
