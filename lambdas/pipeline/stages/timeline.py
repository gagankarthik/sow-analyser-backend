"""Stage 06 — Timeline: replay the amendment chain to produce clause-state snapshots.

Walks from the root SOW/MSA up through all amendments (active and pending) and
builds three views of the contract:
  - initialState  — clauses as they appeared in the root document
  - currentState  — initialState with all *active* amendments applied
  - futureState   — currentState with pending amendments also applied (or None
                    if there are no pending amendments)

If the current document is not an amendment (or has no parent), it is treated as
the root and the chain contains only itself.
"""
from __future__ import annotations

from typing import Any

from shared.dynamodb import get_doc_meta, query_doc_changes, query_doc_children, query_doc_parents, update_status
from shared.logger import get_logger
from shared.s3 import get_json, processed_key, put_json

log = get_logger("blue-iq.timeline")

# Lifecycles that mean the amendment has been executed and is in force — it
# belongs in `currentState`. Everything else (draft/review/negotiation/approval)
# is still pending and only contributes to `futureState`.
_IN_FORCE_LIFECYCLES = {"signed", "active", "renewal", "expired"}


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    classification   = event.get("classification") or {}
    doc_type         = classification.get("docType", "OTHER")
    parent_id        = (event.get("lineage") or {}).get("parentDocId")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "TIMELINING")

    root_id, is_root = _find_root(doc_id=doc_id, doc_type=doc_type, parent_id=parent_id)

    root_cls      = _load_classification(root_id, processed_bucket, tenant_id, event, is_root)
    initial_state = _state_from_clauses(root_cls.get("clauses", []))

    chain_docs = _gather_chain(
        root_id=root_id,
        current_doc_id=doc_id,
        current_doc_type=doc_type,
        event=event,
    )
    amendment_chain = [
        {
            "docId":         d["docId"],
            "docType":       d.get("docType"),
            "lifecycle":     d.get("lifecycle"),
            "effectiveDate": d.get("effectiveDate"),
            "title":         d.get("title"),
            # Signed change in contract value (negative = reduction); null if the
            # amendment does not change the value or it is not known.
            "valueDelta":    d.get("valueDelta"),
            "inForce":       (d.get("lifecycle") or "").lower() in _IN_FORCE_LIFECYCLES,
        }
        for d in chain_docs
    ]

    current_state = _clone(initial_state)
    future_state  = _clone(initial_state)
    has_pending   = False

    for amd in chain_docs:
        in_force = (amd.get("lifecycle") or "").lower() in _IN_FORCE_LIFECYCLES
        changes  = _changes_for(amd["docId"], doc_id, event)
        if in_force:
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
        # Every dated event / obligation of THIS document (see shared/keydates.py).
        # No "past / upcoming" status is stored — the reader derives it from today.
        "keyDates":       classification.get("keyDates") or [],
    }
    put_json(processed_bucket, processed_key(tenant_id, doc_id, "timeline.json"), timeline)
    log.info("timeline.done", amendments=len(amendment_chain), pending=has_pending)

    event["timeline"] = timeline
    return event


# ---------------------------------------------------------------------------
# Chain traversal
# ---------------------------------------------------------------------------


def _find_root(
    *, doc_id: str, doc_type: str, parent_id: str | None
) -> tuple[str, bool]:
    """Walk parent links to find the root SOW/MSA. Returns (root_id, is_current_doc_root)."""
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


def _gather_chain(
    *, root_id: str, current_doc_id: str, current_doc_type: str, event: dict[str, Any]
) -> list[dict[str, Any]]:
    """Collect all amendment doc rows sorted by effectiveDate then docId."""
    children = query_doc_children(root_id)
    doc_ids  = {c["childId"] for c in children if c.get("childId")}
    if current_doc_type == "AMENDMENT":
        doc_ids.add(current_doc_id)

    rows: list[dict[str, Any]] = []
    for did in doc_ids:
        if did == current_doc_id:
            cls = event.get("classification") or {}
            rows.append({
                "docId":         did,
                "docType":       cls.get("docType"),
                "lifecycle":     cls.get("lifecycle"),
                "effectiveDate": cls.get("effectiveDate"),
                "title":         cls.get("title"),
                "valueDelta":    (cls.get("amendment") or {}).get("valueDelta"),
                "createdAt":     "",
            })
        else:
            meta = get_doc_meta(did)
            if not meta:
                continue            # deleted since it was linked: not part of the chain
            delta = meta.get("valueDelta")
            rows.append({
                "docId":         did,
                # What the record says — never a made-up type or lifecycle. A
                # document with no lifecycle is simply not "in force".
                "docType":       meta.get("docType"),
                "lifecycle":     meta.get("lifecycle"),
                "effectiveDate": meta.get("effectiveDate"),
                "title":         meta.get("title"),
                "valueDelta":    float(delta) if delta is not None else None,
                "createdAt":     meta.get("createdAt") or "",
            })

    # Deterministic order: effective date, then upload time, then id. Undated
    # amendments sort last (they cannot be placed before a dated one).
    rows.sort(key=lambda r: (r.get("effectiveDate") is None, str(r.get("effectiveDate") or ""),
                             r.get("createdAt") or "~", r["docId"]))
    return rows


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _state_from_clauses(clauses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        _norm(c.get("number", "")): {
            "number":   c.get("number", ""),
            "title":    c.get("title", ""),
            "body":     c.get("body", ""),
            "category": c.get("category") or "Other",
        }
        for c in clauses
    }


def _clone(state: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in state.items()}


def _apply(state: dict[str, dict[str, Any]], changes: list[dict[str, Any]]) -> None:
    for ch in changes:
        key   = _norm(ch.get("clauseNumber", ""))
        field = ch.get("field", "body")
        after = ch.get("after", "")
        if not key:
            continue
        if field == "body" and after == "":
            state.pop(key, None)
            continue
        slot = state.setdefault(
            key,
            {"number": ch.get("clauseNumber", ""), "title": "", "body": "", "category": "Other"},
        )
        slot[field] = after


def _norm(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def _load_classification(
    doc_id: str, bucket: str, tenant_id: str, event: dict[str, Any], is_root: bool
) -> dict[str, Any]:
    if is_root and event.get("docId") == doc_id:
        return event.get("classification") or {}
    meta   = get_doc_meta(doc_id)
    tenant = (meta or {}).get("tenantId", tenant_id)
    try:
        return get_json(bucket, processed_key(tenant, doc_id, "classification.json"))
    except Exception as exc:
        log.warning("timeline.classification_load_failed", docId=doc_id, error_type=type(exc).__name__)
        return {"clauses": []}


def _changes_for(
    amd_doc_id: str, current_doc_id: str, event: dict[str, Any]
) -> list[dict[str, Any]]:
    if amd_doc_id == current_doc_id:
        return (event.get("diffs") or {}).get("changes") or []
    return latest_version_changes(query_doc_changes(amd_doc_id) or [])


def latest_version_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the changes written by the amendment's most recent analysis.

    Every re-analysis writes a fresh set of change rows (new ids) and the old
    ones stay in the table, so replaying ALL rows would apply an amendment's
    changes once per time it was analysed — stale wording included.
    """
    if not rows:
        return []
    latest = max(int(r.get("versionNumber") or 0) for r in rows)
    return [r for r in rows if int(r.get("versionNumber") or 0) == latest]
