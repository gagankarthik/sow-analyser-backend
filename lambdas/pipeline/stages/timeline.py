"""Stage 06 — Timeline: replay amendment chain to produce state snapshots."""
from __future__ import annotations

from typing import Any

from shared.dynamodb import get_doc_meta, query_doc_changes, query_doc_children, query_doc_parents
from shared.logger import get_logger
from shared.s3 import get_json, processed_key, put_json
from shared.text import normalize

log = get_logger("blue-iq.timeline")


def run(event: dict) -> dict:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    classification   = event.get("classification") or {}
    doc_type         = classification.get("docType", "OTHER")
    parent_id        = (event.get("lineage") or {}).get("parentDocId")

    log.append_keys(docId=doc_id, tenantId=tenant_id)

    root_id, current_is_root = _find_root(doc_id=doc_id, doc_type=doc_type, parent_id=parent_id)

    root_cls      = _load_classification(root_id, processed_bucket, tenant_id, event, current_is_root)
    initial_state = _state_from_clauses(root_cls.get("clauses", []))

    chain_docs = _gather_chain(root_id=root_id, current_doc_id=doc_id, current_doc_type=doc_type, event=event)
    amendment_chain = [
        {"docId": d["docId"], "docType": d.get("docType"), "lifecycle": d.get("lifecycle"),
         "effectiveDate": d.get("effectiveDate"), "title": d.get("title")}
        for d in chain_docs
    ]

    current_state = _clone(initial_state)
    future_state  = _clone(initial_state)
    has_pending   = False

    for amd in chain_docs:
        is_active = (amd.get("lifecycle") or "").lower() == "active"
        changes   = _changes_for(amd["docId"], doc_id, event)
        if is_active:
            _apply(current_state, changes)
            _apply(future_state, changes)
        else:
            has_pending = True
            _apply(future_state, changes)

    timeline = {
        "initialState":   initial_state,
        "currentState":   current_state,
        "amendmentChain": amendment_chain,
        "futureState":    future_state if has_pending else None,
    }
    out_key = processed_key(tenant_id, doc_id, "timeline.json")
    put_json(processed_bucket, out_key, timeline)
    log.info("timeline.done", amendments=len(amendment_chain), pending=has_pending)

    event["timeline"] = timeline
    return event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_root(*, doc_id: str, doc_type: str, parent_id: str | None) -> tuple[str, bool]:
    if doc_type != "AMENDMENT" or not parent_id:
        return (doc_id, True)
    cur  = parent_id
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


def _state_from_clauses(clauses: list[dict]) -> dict[str, dict[str, Any]]:
    return {
        _norm(c.get("number", "")): {
            "number": c.get("number", ""), "title": c.get("title", ""),
            "body": c.get("body", ""), "category": c.get("category", "Other"),
        }
        for c in clauses
    }


def _clone(state: dict[str, dict]) -> dict[str, dict]:
    return {k: dict(v) for k, v in state.items()}


def _apply(state: dict[str, dict], changes: list[dict]) -> None:
    for ch in changes:
        key   = _norm(ch.get("clauseNumber", ""))
        field = ch.get("field", "body")
        after = ch.get("after", "")
        if not key:
            continue
        if after == "" and field == "body":
            state.pop(key, None)
            continue
        slot = state.setdefault(key, {"number": ch.get("clauseNumber", ""), "title": "", "body": "", "category": "Other"})
        slot[field] = after


def _norm(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


def _load_classification(doc_id: str, bucket: str, tenant_id: str, event: dict, is_root: bool) -> dict:
    if is_root and event.get("docId") == doc_id:
        return event.get("classification") or {}
    meta   = get_doc_meta(doc_id)
    tenant = (meta or {}).get("tenantId", tenant_id)
    try:
        return get_json(bucket, processed_key(tenant, doc_id, "classification.json"))
    except Exception as exc:
        log.warning("timeline.classification_load_failed", docId=doc_id, error=str(exc))
        return {"clauses": []}


def _gather_chain(*, root_id: str, current_doc_id: str, current_doc_type: str, event: dict) -> list[dict[str, Any]]:
    children = query_doc_children(root_id)
    doc_ids  = {c["childId"] for c in children if c.get("childId")}
    if current_doc_type == "AMENDMENT":
        doc_ids.add(current_doc_id)

    rows: list[dict[str, Any]] = []
    for did in doc_ids:
        if did == current_doc_id:
            cls = event.get("classification") or {}
            rows.append({"docId": did, "docType": cls.get("docType", "AMENDMENT"),
                         "lifecycle": cls.get("lifecycle", "draft"),
                         "effectiveDate": cls.get("effectiveDate"), "title": cls.get("title", "")})
        else:
            meta = get_doc_meta(did) or {}
            rows.append({"docId": did, "docType": meta.get("docType", "AMENDMENT"),
                         "lifecycle": meta.get("lifecycle", "draft"),
                         "effectiveDate": meta.get("effectiveDate"), "title": meta.get("title", "")})

    rows.sort(key=lambda r: (r.get("effectiveDate") or "", r["docId"]))
    return rows


def _changes_for(amd_doc_id: str, current_doc_id: str, event: dict) -> list[dict]:
    if amd_doc_id == current_doc_id:
        return (event.get("diffs") or {}).get("changes") or []
    return query_doc_changes(amd_doc_id) or []
