"""Stage 7 — Persist.

Writes the final DynamoDB records (Document META, Version, Changes) and
returns the terminal event for the state machine.

AppSync notification: the brief says the subscription resolver reads from DDB,
so for v1 we just write the status update.  Updating META.status to READY is
sufficient to trigger any DynamoDB Streams → AppSync subscription pipeline
configured by the infra agent.
"""
from __future__ import annotations

from aws_lambda_powertools import Tracer

from shared.dynamodb import (
    get_doc_meta,
    put_change,
    put_doc_meta,
    put_version,
    query_doc_versions,
)
from shared.logger import get_logger
from shared.s3 import processed_key
from shared.schema import ProcessingStatus, now_iso

log = get_logger("blue-iq.persist")
tracer = Tracer(service="blue-iq.persist")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("persist.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    raw_key: str = event.get("rawKey", "")
    processed_bucket: str = event.get("processedBucket", "")

    log.append_keys(docId=doc_id, tenantId=tenant_id)

    classification = event.get("classification") or {}
    parsed = event.get("parsed") or {}
    lineage = event.get("lineage") or {}
    diffs = event.get("diffs") or {}
    timeline = event.get("timeline") or {}

    # 1. Determine version number.
    existing_versions = query_doc_versions(doc_id)
    version_n = (len(existing_versions) or 0) + 1

    # 2. Document META (idempotent upsert).
    processed_prefix = f"{tenant_id}/{doc_id}/"
    existing_meta = get_doc_meta(doc_id) or {}
    created_at = existing_meta.get("createdAt") or now_iso()

    doc_meta = {
        "docId": doc_id,
        "tenantId": tenant_id,
        "title": classification.get("title", existing_meta.get("title", "Untitled")),
        "docType": classification.get("docType", existing_meta.get("docType", "OTHER")),
        "lifecycle": classification.get("lifecycle", existing_meta.get("lifecycle", "draft")),
        "status": ProcessingStatus.READY.value,
        "parties": classification.get("parties", []),
        "effectiveDate": classification.get("effectiveDate"),
        "parentDocId": lineage.get("parentDocId"),
        "rawKey": raw_key,
        "processedPrefix": processed_prefix,
        "structuralHash": classification.get("structuralHash", ""),
        "checksum": parsed.get("checksum", ""),
        "latestVersion": version_n,
        "createdAt": created_at,
    }
    put_doc_meta(doc_meta)

    # 3. Version record.
    version = {
        "docId": doc_id,
        "versionNumber": version_n,
        "extractionMethod": parsed.get("extraction_method", "pdfplumber"),
        "parsedKey": processed_key(tenant_id, doc_id, "parsed.json"),
        "classificationKey": processed_key(tenant_id, doc_id, "classification.json"),
        "timelineKey": (
            processed_key(tenant_id, doc_id, "timeline.json") if timeline else None
        ),
        "diffKey": (
            processed_key(tenant_id, doc_id, "diff.json") if diffs.get("changes") else None
        ),
        "createdAt": now_iso(),
    }
    put_version(version)

    # 4. Change records.
    n_changes = 0
    for ch in diffs.get("changes", []):
        put_change(
            {
                "docId": doc_id,
                "changeId": ch["changeId"],
                "clauseNumber": ch.get("clauseNumber", ""),
                "field": ch.get("field", "body"),
                "before": ch.get("before", ""),
                "after": ch.get("after", ""),
                "impactScore": int(ch.get("impactScore", 0)),
                "impactRationale": ch.get("impactRationale", ""),
                "versionNumber": version_n,
            }
        )
        n_changes += 1

    log.info(
        "persist.done",
        version=version_n,
        changes=n_changes,
        bucket=processed_bucket,
    )

    return {"status": ProcessingStatus.READY.value, "docId": doc_id}
