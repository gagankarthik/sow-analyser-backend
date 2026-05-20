"""Stage 5 — Diff.

Skips if no parent.  Otherwise loads the parent's latest classification.json
from the processed bucket, computes field-level diffs against the current
classification, scores impact, and writes diff.json.
"""
from __future__ import annotations

from aws_lambda_powertools import Tracer

from shared.dynamodb import get_doc_meta, query_doc_versions
from shared.logger import get_logger
from shared.s3 import get_json, processed_key, put_json

from .diff_engine import build_impact_summary, diff_clauses, score_impacts

log = get_logger("blue-iq.diff")
tracer = Tracer(service="blue-iq.diff")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("diff.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    processed_bucket: str = event["processedBucket"]
    log.append_keys(docId=doc_id, tenantId=tenant_id)

    lineage = event.get("lineage") or {}
    parent_id = lineage.get("parentDocId")
    if not parent_id:
        log.info("diff.skipped", reason="no parent")
        event["diffs"] = {"changes": [], "impactSummary": "No parent — nothing to diff."}
        return event

    current_clauses = (event.get("classification") or {}).get("clauses") or []
    parent_clauses = _load_parent_clauses(processed_bucket, tenant_id, parent_id)
    if not parent_clauses:
        log.warning("diff.parent_classification_missing", parentDocId=parent_id)
        event["diffs"] = {"changes": [], "impactSummary": "Parent classification unavailable."}
        return event

    changes = diff_clauses(
        current_clauses=current_clauses,
        parent_clauses=parent_clauses,
    )
    score_impacts(changes, current_clauses=current_clauses)
    summary = build_impact_summary(changes)

    payload = {"changes": changes, "impactSummary": summary}
    out_key = processed_key(tenant_id, doc_id, "diff.json")
    put_json(processed_bucket, out_key, payload)
    log.info(
        "diff.done",
        changes=len(changes),
        summary=summary,
        s3=f"s3://{processed_bucket}/{out_key}",
    )

    event["diffs"] = payload
    return event


def _load_parent_clauses(
    processed_bucket: str, tenant_id: str, parent_id: str
) -> list[dict]:
    """Find the parent's latest classification.json.

    Strategy:
      1. Look up DDB versions for the parent and use latest classificationKey.
      2. Fall back to the canonical processed-key layout.
    """
    versions = query_doc_versions(parent_id)
    if versions:
        # SK = V#NNNNNN, sort lexicographically descending
        versions.sort(key=lambda v: v.get("SK", ""), reverse=True)
        for v in versions:
            key = v.get("classificationKey")
            if key:
                try:
                    payload = get_json(processed_bucket, key)
                    return payload.get("clauses", [])
                except Exception as e:
                    log.warning(
                        "diff.parent_version_load_failed",
                        parentDocId=parent_id,
                        key=key,
                        error=str(e),
                    )
                    break

    # Fallback: parent's META should tell us where its classification lives.
    meta = get_doc_meta(parent_id)
    tenant = (meta or {}).get("tenantId", tenant_id)
    try:
        payload = get_json(
            processed_bucket, processed_key(tenant, parent_id, "classification.json")
        )
        return payload.get("clauses", [])
    except Exception as e:
        log.warning(
            "diff.parent_classification_fallback_failed",
            parentDocId=parent_id,
            error=str(e),
        )
        return []
