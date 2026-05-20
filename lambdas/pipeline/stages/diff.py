"""Stage 05 — Diff: field-level change detection + LLM impact scoring."""
from __future__ import annotations

import uuid
from typing import Any

from shared.config import settings
from shared.dynamodb import get_doc_meta, query_doc_versions, update_status
from shared.logger import get_logger
from shared.openai_client import chat_json
from shared.s3 import get_json, processed_key, put_json
from shared.text import normalize

log = get_logger("blue-iq.diff")

HIGH_RISK_CATEGORIES = {"Liability", "IP", "Indemnity", "Termination"}

_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "rationale"],
    "properties": {
        "score":     {"type": "integer", "minimum": 1, "maximum": 100},
        "rationale": {"type": "string"},
    },
}

_IMPACT_SYSTEM = (
    "You assess commercial risk impact of contract clause changes. "
    "Given before/after text of a single clause, return a strict JSON object with: "
    "score (integer 1..100, higher = more risk) and rationale (one sentence). "
    "Be conservative; small wording changes are usually low."
)


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict) -> dict:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    lineage          = event.get("lineage") or {}
    parent_id        = lineage.get("parentDocId")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "DIFFING")

    if not parent_id:
        log.info("diff.skipped", reason="no parent")
        event["diffs"] = {"changes": [], "impactSummary": "No parent — nothing to diff."}
        return event

    current_clauses = (event.get("classification") or {}).get("clauses") or []
    parent_clauses  = _load_parent_clauses(processed_bucket, tenant_id, parent_id)
    if not parent_clauses:
        log.warning("diff.parent_classification_missing", parentDocId=parent_id)
        event["diffs"] = {"changes": [], "impactSummary": "Parent classification unavailable."}
        return event

    changes = _diff_clauses(current_clauses, parent_clauses)
    _score_impacts(changes, current_clauses)

    summary = _build_summary(changes)
    payload = {"changes": changes, "impactSummary": summary}

    out_key = processed_key(tenant_id, doc_id, "diff.json")
    put_json(processed_bucket, out_key, payload)
    log.info("diff.done", changes=len(changes), summary=summary)

    event["diffs"] = payload
    return event


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------


def _diff_clauses(current: list[dict], parent: list[dict]) -> list[dict[str, Any]]:
    parent_by_num = {_norm(c.get("number", "")): c for c in parent}
    changes: list[dict[str, Any]] = []

    for cur in current:
        key = _norm(cur.get("number", ""))
        par = parent_by_num.get(key)
        if not par:
            changes.append(_mk_change(cur.get("number", ""), "body", "", cur.get("body", "")))
            continue
        for field in ("title", "body", "category"):
            before = par.get(field, "") or ""
            after  = cur.get(field, "") or ""
            if normalize(before) != normalize(after):
                changes.append(_mk_change(cur.get("number", ""), field, before, after))

    current_keys = {_norm(c.get("number", "")) for c in current}
    for k, par_clause in parent_by_num.items():
        if k not in current_keys:
            changes.append(_mk_change(par_clause.get("number", ""), "body", par_clause.get("body", ""), ""))

    return changes


def _mk_change(clause_number: str, field: str, before: str, after: str) -> dict[str, Any]:
    a, b      = len(before), len(after)
    delta_pct = abs(b - a) / max(a, b, 1) * 100.0
    return {
        "changeId":         uuid.uuid4().hex,
        "clauseNumber":     clause_number,
        "field":            field,
        "before":           before,
        "after":            after,
        "impactScore":      0,
        "impactRationale":  "",
        "_delta_pct":       delta_pct,
        "_cat":             "",
    }


def _norm(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


# ---------------------------------------------------------------------------
# Impact scoring
# ---------------------------------------------------------------------------


def _score_impacts(changes: list[dict[str, Any]], current: list[dict]) -> None:
    cat_by_num = {_norm(c.get("number", "")): c.get("category", "Other") for c in current}

    for ch in changes:
        cat     = cat_by_num.get(_norm(ch["clauseNumber"]), "Other")
        ch["_cat"] = cat
        base    = 60 if cat in HIGH_RISK_CATEGORIES else 30
        if ch["_delta_pct"] > 30: base += 20
        if ch["field"] == "category": base += 10
        ch["impactScore"]     = min(100, base)
        ch["impactRationale"] = f"Heuristic: {cat} field={ch['field']} Δlen={ch['_delta_pct']:.0f}%"

    # LLM refinement for top-N most-changed clauses.
    cap        = settings.diff_impact_call_cap
    candidates = sorted(changes, key=lambda c: c["_delta_pct"], reverse=True)[:cap]
    for ch in candidates:
        try:
            result = chat_json(
                system=_IMPACT_SYSTEM,
                user=(
                    f"Category: {ch.get('_cat', 'Other')}\nField: {ch['field']}\n"
                    f"BEFORE:\n{(ch['before'] or '')[:3000]}\n\n"
                    f"AFTER:\n{(ch['after'] or '')[:3000]}"
                ),
                json_schema=_IMPACT_SCHEMA,
                schema_name="ImpactScore",
                temperature=0.0,
            )
            ch["impactScore"]     = int(result["score"])
            ch["impactRationale"] = result["rationale"]
        except Exception as exc:
            log.warning("diff.impact.llm_failed", changeId=ch.get("changeId"), error=str(exc))

    for ch in changes:
        ch.pop("_delta_pct", None)
        ch.pop("_cat", None)


def _build_summary(changes: list[dict]) -> str:
    if not changes:
        return "No changes detected vs. parent."
    high = sum(1 for c in changes if c["impactScore"] >= 70)
    med  = sum(1 for c in changes if 40 <= c["impactScore"] < 70)
    return f"{len(changes)} change(s): {high} high-impact, {med} medium, {len(changes) - high - med} low."


# ---------------------------------------------------------------------------
# Parent clause loader
# ---------------------------------------------------------------------------


def _load_parent_clauses(processed_bucket: str, tenant_id: str, parent_id: str) -> list[dict]:
    versions = query_doc_versions(parent_id)
    if versions:
        versions.sort(key=lambda v: v.get("SK", ""), reverse=True)
        for v in versions:
            key = v.get("classificationKey")
            if key:
                try:
                    return get_json(processed_bucket, key).get("clauses", [])
                except Exception as exc:
                    log.warning("diff.parent_version_load_failed", key=key, error=str(exc))
                    break

    meta   = get_doc_meta(parent_id)
    tenant = (meta or {}).get("tenantId", tenant_id)
    try:
        return get_json(processed_bucket, processed_key(tenant, parent_id, "classification.json")).get("clauses", [])
    except Exception as exc:
        log.warning("diff.parent_classification_fallback_failed", error=str(exc))
        return []
