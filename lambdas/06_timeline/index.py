"""Stage 6 — Timeline.

Replays the document chain to produce:
  * initialState  — parent at v1 (or this doc if it IS the root)
  * currentState  — initialState with every active amendment applied in order
  * futureState   — currentState with pending (non-active) amendments applied
  * amendmentChain — ordered list of {docId, docType, lifecycle, effectiveDate}

The clause state is represented as a dict keyed by normalised clause number;
each amendment's Change records are folded in.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.dynamodb import (
    get_doc_meta,
    query_doc_changes,
    query_doc_children,
    query_doc_parents,
)
from shared.logger import get_logger
from shared.s3 import get_json, processed_key, put_json
from shared.text import normalize

log = get_logger("blue-iq.timeline")
tracer = Tracer(service="blue-iq.timeline")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("timeline.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    processed_bucket: str = event["processedBucket"]
    log.append_keys(docId=doc_id, tenantId=tenant_id)

    classification = event.get("classification") or {}
    doc_type = classification.get("docType", "OTHER")
    lineage = event.get("lineage") or {}
    parent_id = lineage.get("parentDocId")

    # Root identification: if this is an AMENDMENT with a parent, the parent is
    # the root.  Otherwise this doc is the root.
    root_id, current_is_root = _find_root(
        doc_id=doc_id, doc_type=doc_type, parent_id=parent_id
    )

    root_classification = _load_classification_for(
        doc_id=root_id,
        processed_bucket=processed_bucket,
        tenant_id=tenant_id,
        current_event=event,
        current_is_root=current_is_root,
    )
    initial_state = _state_from_clauses(root_classification.get("clauses", []))

    # Gather every amendment in chain (siblings + this one).  Order chronologically.
    chain_docs = _gather_chain(
        root_id=root_id,
        current_doc_id=doc_id,
        current_doc_type=doc_type,
        current_event=event,
    )
    amendment_chain_meta = [
        {
            "docId": d["docId"],
            "docType": d.get("docType"),
            "lifecycle": d.get("lifecycle"),
            "effectiveDate": d.get("effectiveDate"),
            "title": d.get("title"),
        }
        for d in chain_docs
    ]

    # Apply changes.  We split active from pending so we can produce
    # currentState (active only) and futureState (active + pending).
    current_state = _clone_state(initial_state)
    future_state = _clone_state(initial_state)
    has_pending = False
    for amd in chain_docs:
        is_active = (amd.get("lifecycle") or "").lower() == "active"
        changes = _changes_for(
            amd_doc_id=amd["docId"],
            current_doc_id=doc_id,
            current_event=event,
        )
        if is_active:
            _apply_changes(current_state, changes)
            _apply_changes(future_state, changes)
        else:
            has_pending = True
            _apply_changes(future_state, changes)

    timeline = {
        "initialState": initial_state,
        "currentState": current_state,
        "amendmentChain": amendment_chain_meta,
        "futureState": future_state if has_pending else None,
    }

    out_key = processed_key(tenant_id, doc_id, "timeline.json")
    put_json(processed_bucket, out_key, timeline)
    log.info(
        "timeline.done",
        amendments=len(amendment_chain_meta),
        pending=has_pending,
        s3=f"s3://{processed_bucket}/{out_key}",
    )

    event["timeline"] = timeline
    return event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_root(
    *, doc_id: str, doc_type: str, parent_id: str | None
) -> tuple[str, bool]:
    """Walk up the lineage chain to find the root SOW/MSA."""
    if doc_type != "AMENDMENT" or not parent_id:
        return (doc_id, True)
    # Walk up via DDB LINK# records.
    cur = parent_id
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        links = query_doc_parents(cur)
        if not links:
            return (cur, False)
        nxt = links[0].get("parentId")
        if not nxt or nxt == cur:
            return (cur, False)
        cur = nxt
    return (cur, False)


def _state_from_clauses(clauses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in clauses:
        k = _norm_num(c.get("number", ""))
        out[k] = {
            "number": c.get("number", ""),
            "title": c.get("title", ""),
            "body": c.get("body", ""),
            "category": c.get("category", "Other"),
        }
    return out


def _clone_state(state: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in state.items()}


def _apply_changes(
    state: dict[str, dict[str, Any]], changes: list[dict[str, Any]]
) -> None:
    for ch in changes:
        key = _norm_num(ch.get("clauseNumber", ""))
        field = ch.get("field", "body")
        after = ch.get("after", "")
        if not key:
            continue
        if after == "" and field == "body" and key in state:
            # Deletion (parent had it, amendment cleared it)
            state.pop(key, None)
            continue
        slot = state.setdefault(
            key,
            {"number": ch.get("clauseNumber", ""), "title": "", "body": "", "category": "Other"},
        )
        slot[field] = after


def _norm_num(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


# ---------------------------------------------------------------------------
# DDB / S3 fetchers
# ---------------------------------------------------------------------------


def _load_classification_for(
    *,
    doc_id: str,
    processed_bucket: str,
    tenant_id: str,
    current_event: dict,
    current_is_root: bool,
) -> dict[str, Any]:
    if current_is_root and current_event.get("docId") == doc_id:
        return current_event.get("classification") or {}
    meta = get_doc_meta(doc_id)
    tenant = (meta or {}).get("tenantId", tenant_id)
    try:
        return get_json(
            processed_bucket, processed_key(tenant, doc_id, "classification.json")
        )
    except Exception as e:
        log.warning(
            "timeline.classification_load_failed", docId=doc_id, error=str(e)
        )
        return {"clauses": []}


def _gather_chain(
    *,
    root_id: str,
    current_doc_id: str,
    current_doc_type: str,
    current_event: dict,
) -> list[dict[str, Any]]:
    """Return amendments in chronological order, with this doc included even
    when it isn't yet persisted to DDB."""
    children = query_doc_children(root_id)
    doc_ids = {c["childId"] for c in children if c.get("childId")}
    if current_doc_type == "AMENDMENT":
        doc_ids.add(current_doc_id)

    rows: list[dict[str, Any]] = []
    for did in doc_ids:
        if did == current_doc_id:
            cls = current_event.get("classification") or {}
            rows.append(
                {
                    "docId": did,
                    "docType": cls.get("docType", "AMENDMENT"),
                    "lifecycle": cls.get("lifecycle", "draft"),
                    "effectiveDate": cls.get("effectiveDate"),
                    "title": cls.get("title", ""),
                }
            )
            continue
        meta = get_doc_meta(did) or {}
        rows.append(
            {
                "docId": did,
                "docType": meta.get("docType", "AMENDMENT"),
                "lifecycle": meta.get("lifecycle", "draft"),
                "effectiveDate": meta.get("effectiveDate"),
                "title": meta.get("title", ""),
            }
        )

    rows.sort(key=lambda r: (r.get("effectiveDate") or "", r["docId"]))
    return rows


def _changes_for(
    *, amd_doc_id: str, current_doc_id: str, current_event: dict
) -> list[dict[str, Any]]:
    if amd_doc_id == current_doc_id:
        return (current_event.get("diffs") or {}).get("changes") or []
    return query_doc_changes(amd_doc_id) or []
