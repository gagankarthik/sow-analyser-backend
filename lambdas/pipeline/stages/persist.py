"""Stage 07 — Persist: write DynamoDB records and mark the pipeline complete.

Upserts the document META row (preserving createdAt and any manually-set title
from the upload form), writes a VERSION record with S3 key pointers for all
generated artefacts, and writes one CHANGE row per diff entry so that the
timeline stage (in future runs) can replay them.

This is the terminal stage — status is set to READY on success.
"""
from __future__ import annotations

from typing import Any

from shared.dynamodb import (
    get_doc_meta,
    put_change,
    put_doc_meta,
    put_version,
    query_doc_versions,
    update_status,
)
from shared.logger import get_logger
from shared.s3 import processed_key
from shared.schema import ProcessingStatus, now_iso

log = get_logger("blue-iq.persist")


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    raw_key          = event.get("rawKey", "")
    processed_bucket = event.get("processedBucket", "")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "PERSISTING")

    classification = event.get("classification") or {}
    parsed         = event.get("parsed") or {}
    lineage        = event.get("lineage") or {}
    diffs          = event.get("diffs") or {}
    timeline       = event.get("timeline") or {}

    existing_versions = query_doc_versions(doc_id)
    version_n         = len(existing_versions) + 1

    existing_meta = get_doc_meta(doc_id) or {}
    created_at    = existing_meta.get("createdAt") or now_iso()

    # Preserve a user-supplied title from the upload form (written by the API
    # during presigned-URL generation) over the LLM-extracted title when the
    # user has intentionally overridden it.
    title = classification.get("title") or existing_meta.get("title") or "Untitled"

    put_doc_meta({
        "docId":           doc_id,
        "tenantId":        tenant_id,
        "title":           title,
        "docType":         classification.get("docType",   existing_meta.get("docType",   "OTHER")),
        "lifecycle":       classification.get("lifecycle", existing_meta.get("lifecycle", "draft")),
        "status":          ProcessingStatus.READY.value,
        "parties":         classification.get("parties", []),
        "effectiveDate":   classification.get("effectiveDate"),
        "parentDocId":     lineage.get("parentDocId"),
        "rawKey":          raw_key,
        "processedPrefix": f"{tenant_id}/{doc_id}/",
        "structuralHash":  classification.get("structuralHash", ""),
        "checksum":        parsed.get("checksum", ""),
        "latestVersion":   version_n,
        "createdAt":       created_at,
    })

    put_version({
        "docId":             doc_id,
        "versionNumber":     version_n,
        "extractionMethod":  parsed.get("extraction_method", "pdfplumber"),
        "parsedKey":         processed_key(tenant_id, doc_id, "parsed.json"),
        "classificationKey": processed_key(tenant_id, doc_id, "classification.json"),
        "timelineKey":       processed_key(tenant_id, doc_id, "timeline.json") if timeline else None,
        "diffKey":           processed_key(tenant_id, doc_id, "diff.json") if diffs.get("changes") else None,
        "createdAt":         now_iso(),
    })

    n_changes = 0
    for ch in diffs.get("changes", []):
        put_change({
            "docId":           doc_id,
            "changeId":        ch["changeId"],
            "clauseNumber":    ch.get("clauseNumber", ""),
            "field":           ch.get("field", "body"),
            "before":          ch.get("before", ""),
            "after":           ch.get("after", ""),
            "impactScore":     int(ch.get("impactScore", 0)),
            "impactRationale": ch.get("impactRationale", ""),
            "versionNumber":   version_n,
        })
        n_changes += 1

    log.info("persist.done", version=version_n, changes=n_changes)
    return {"status": ProcessingStatus.READY.value, "docId": doc_id}
