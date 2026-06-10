"""Stage 05 — Diff: field-level change detection + LLM impact scoring.

Two diff modes, picked by document type:

  AMENDMENT — an amendment is a DELTA document: classify only extracts the
    clauses the amendment itself introduces, NOT the whole parent contract. So a
    naive clause-by-clause comparison against the parent would report every
    parent clause as a deletion and every amendment clause as a brand-new
    addition. Instead we drive the diff from the structured
    `classification.amendment.changes[]` array (changeType / category /
    targetSection / before → after), mapping each declared change onto the
    parent clause it targets (by number or title) so the timeline stage can
    replay it correctly.

  RE-VERSION — a full re-upload of the same document. Here both clause lists are
    complete, so a field-level clause-number comparison is correct, including
    detecting genuinely removed clauses.

For each change, heuristic scoring assigns an initial impact score; then the
top-N most-changed clauses are refined via GPT for a more accurate rationale.

If no parent exists (graph stage found none), the stage exits immediately with
an empty diff — this is the happy path for first-version documents.
"""
from __future__ import annotations

import uuid
from typing import Any

from shared.config import settings
from shared.dynamodb import get_doc_meta, query_doc_versions, update_status
from shared.logger import get_logger
from shared.openai_client import chat_json
from shared.s3 import get_json, processed_key, put_json
from shared.text import normalize, title_similarity

log = get_logger("blue-iq.diff")

_HIGH_RISK = {"Liability", "IP", "Indemnity", "Termination"}

# NOTE: OpenAI strict Structured Outputs rejects numeric `minimum`/`maximum`
# keywords (the request 400s). The [1, 100] bound is enforced in the prompt and
# clamped in code (see `_score_impacts`).
_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "rationale"],
    "properties": {
        "score":     {"type": "integer"},
        "rationale": {"type": "string"},
    },
}

_IMPACT_SYSTEM = (
    "You assess commercial risk of contract clause changes. "
    "Given before/after text, return JSON with: "
    "score (integer from 1 to 100, higher = more risk) and rationale (one sentence). "
    "Be conservative — minor wording tweaks are usually low risk."
)


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    parent_id        = (event.get("lineage") or {}).get("parentDocId")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "DIFFING")

    if not parent_id:
        log.info("diff.skipped", reason="no parent document")
        event["diffs"] = {"changes": [], "impactSummary": "First version — no diff."}
        return event

    classification  = event.get("classification") or {}
    current_clauses = classification.get("clauses") or []
    parent_clauses  = _load_parent_clauses(processed_bucket, tenant_id, parent_id)

    if not parent_clauses:
        log.warning("diff.parent_unavailable", parentDocId=parent_id)
        event["diffs"] = {"changes": [], "impactSummary": "Parent classification unavailable."}
        return event

    amendment = classification.get("amendment") or {}
    is_delta  = (
        classification.get("docType") == "AMENDMENT"
        and (amendment.get("amendmentType") or "none") != "none"
        and amendment.get("changes")
    )
    if is_delta:
        # Delta document: drive the diff from the amendment's declared changes so
        # we don't fabricate deletions for every parent clause the amendment
        # simply leaves untouched.
        changes = _diff_amendment(amendment.get("changes") or [], parent_clauses)
    else:
        changes = _diff(current_clauses, parent_clauses)
    _score_impacts(changes, current_clauses)

    summary = _summarise(changes)
    payload = {"changes": changes, "impactSummary": summary}
    put_json(processed_bucket, processed_key(tenant_id, doc_id, "diff.json"), payload)
    log.info("diff.done", changes=len(changes), summary=summary)

    event["diffs"] = payload
    return event


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------


def _diff(current: list[dict[str, Any]], parent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Field-level comparison for a full re-upload of the same document.

    Both clause lists are assumed complete, so a clause present in the parent but
    absent from the current document is a genuine deletion. As a safety net
    against a delta document that slipped past the AMENDMENT check, deletions are
    only emitted when the current clause set is reasonably complete relative to
    the parent — otherwise a half-extracted re-upload would wipe the contract.
    """
    parent_map = {_norm_num(c.get("number", "")): c for c in parent}
    changes: list[dict[str, Any]] = []

    for cur in current:
        key = _norm_num(cur.get("number", ""))
        par = parent_map.get(key)
        if not par:
            changes.append(_mk_change(cur.get("number", ""), "body", "", cur.get("body", "")))
            continue
        for field in ("title", "body", "category"):
            before = par.get(field, "") or ""
            after  = cur.get(field, "") or ""
            if normalize(before) != normalize(after):
                changes.append(_mk_change(cur.get("number", ""), field, before, after))

    current_keys = {_norm_num(c.get("number", "")) for c in current}
    # Only treat missing clauses as deletions when the current document looks like
    # a complete re-version (not a sparse/partial extraction).
    looks_complete = len(current) >= max(1, len(parent) // 2)
    if looks_complete:
        for key, par_clause in parent_map.items():
            if key not in current_keys:
                changes.append(_mk_change(par_clause.get("number", ""), "body",
                                           par_clause.get("body", ""), ""))
    return changes


def _diff_amendment(
    amendment_changes: list[dict[str, Any]], parent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build changes from an amendment's declared delta, mapped onto parent clauses.

    Each amendment change targets a section described narratively
    (`targetSection`), not by the parent's clause numbering. We resolve it to a
    parent clause by exact number match first, then best title-similarity match.
    Unmatched changes become additions keyed by a stable synthetic id so replay
    never clobbers an unrelated clause.
    """
    parent_by_num = {_norm_num(c.get("number", "")): c for c in parent}
    changes: list[dict[str, Any]] = []

    for i, ac in enumerate(amendment_changes):
        target      = (ac.get("targetSection") or "").strip()
        change_type = (ac.get("changeType") or "modification").lower()
        before      = ac.get("before") or ""
        after       = ac.get("after") or ""
        summary     = ac.get("summary") or ""

        par = _match_parent_clause(target, parent, parent_by_num)
        if par is not None:
            clause_number = par.get("number", "") or target or f"amendment.{i + 1}"
            # Pull a verbatim before from the parent when the amendment didn't
            # restate it (the prompt notes `before` often lives only in the parent).
            if not before:
                before = par.get("body", "") or ""
        else:
            clause_number = target or f"amendment.{i + 1}"

        if change_type == "deletion":
            # Only a matched parent clause can actually be removed from state.
            after = "" if par is not None else after

        change = _mk_change(clause_number, "body", before, after)
        change["_cat"] = _category_hint(ac.get("category"), par)
        change["_summary"] = summary
        changes.append(change)

    return changes


def _match_parent_clause(
    target: str, parent: list[dict[str, Any]], parent_by_num: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if not target:
        return None
    # Exact clause-number match (e.g. targetSection "Section 4.2").
    key = _norm_num(target)
    if key and key in parent_by_num:
        return parent_by_num[key]
    # Best title-similarity match above a confidence floor.
    best, best_sim = None, 0.0
    for c in parent:
        sim = title_similarity(target, c.get("title", "") or "")
        if sim > best_sim:
            best, best_sim = c, sim
    return best if best_sim >= 0.5 else None


# Categories the amendment classifier uses → clause categories used for risk.
_CATEGORY_MAP = {
    "value": "Fees", "payment": "Payment", "scope": "ScopeOfWork",
    "timeline": "Term", "term": "Term", "sla": "Other", "personnel": "Other",
}


def _category_hint(amendment_category: str | None, par: dict[str, Any] | None) -> str:
    if par and par.get("category"):
        return par["category"]
    return _CATEGORY_MAP.get((amendment_category or "").lower(), "Other")


def _mk_change(num: str, field: str, before: str, after: str) -> dict[str, Any]:
    a, b      = len(before), len(after)
    delta_pct = abs(b - a) / max(a, b, 1) * 100.0
    return {
        "changeId":        uuid.uuid4().hex,
        "clauseNumber":    num,
        "field":           field,
        "before":          before,
        "after":           after,
        "impactScore":     0,
        "impactRationale": "",
        "_deltaPct":       delta_pct,
        "_cat":            "",
    }


def _norm_num(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


# ---------------------------------------------------------------------------
# Impact scoring
# ---------------------------------------------------------------------------


def _score_impacts(changes: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    cat_map = {_norm_num(c.get("number", "")): c.get("category", "Other") for c in current}

    for ch in changes:
        # Amendment-mode changes pre-set `_cat` from the matched parent clause;
        # only fall back to the current-doc clause map when it wasn't set.
        cat = ch.get("_cat") or cat_map.get(_norm_num(ch["clauseNumber"]), "Other")
        ch["_cat"] = cat
        base = 60 if cat in _HIGH_RISK else 30
        if ch["_deltaPct"] > 30: base += 20
        if ch["field"] == "category": base += 10
        ch["impactScore"]     = min(100, base)
        ch["impactRationale"] = ch.get("_summary") or (
            f"Heuristic: {cat}, field={ch['field']}, Δ={ch['_deltaPct']:.0f}%"
        )

    # LLM refinement for top-N most-changed clauses.
    top = sorted(changes, key=lambda c: c["_deltaPct"], reverse=True)[:settings.diff_impact_call_cap]
    for ch in top:
        try:
            result = chat_json(
                system=_IMPACT_SYSTEM,
                user=(
                    f"Category: {ch['_cat']}\nField: {ch['field']}\n"
                    f"BEFORE:\n{(ch['before'] or '')[:3000]}\n\n"
                    f"AFTER:\n{(ch['after'] or '')[:3000]}"
                ),
                json_schema=_IMPACT_SCHEMA,
                schema_name="ImpactScore",
                temperature=0.0,
            )
            # Clamp defensively to the documented [1, 100] range.
            ch["impactScore"]     = max(1, min(100, int(result["score"])))
            ch["impactRationale"] = result["rationale"]
        except Exception as exc:
            log.warning("diff.impact_llm_failed",
                        changeId=ch.get("changeId"), error=str(exc))

    for ch in changes:
        ch.pop("_deltaPct", None)
        ch.pop("_cat", None)
        ch.pop("_summary", None)


def _summarise(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return "No changes detected vs. parent."
    high = sum(1 for c in changes if c["impactScore"] >= 70)
    med  = sum(1 for c in changes if 40 <= c["impactScore"] < 70)
    return f"{len(changes)} change(s): {high} high, {med} medium, {len(changes)-high-med} low impact."


# ---------------------------------------------------------------------------
# Parent clause loader
# ---------------------------------------------------------------------------


def _load_parent_clauses(
    bucket: str, tenant_id: str, parent_id: str
) -> list[dict[str, Any]]:
    versions = sorted(
        query_doc_versions(parent_id),
        key=lambda v: v.get("SK", ""),
        reverse=True,
    )
    for v in versions:
        key = v.get("classificationKey")
        if key:
            try:
                return get_json(bucket, key).get("clauses", [])
            except Exception as exc:
                log.warning("diff.parent_version_load_failed", key=key, error=str(exc))
                break

    meta   = get_doc_meta(parent_id) or {}
    tenant = meta.get("tenantId", tenant_id)
    try:
        return get_json(bucket, processed_key(tenant, parent_id, "classification.json")).get("clauses", [])
    except Exception as exc:
        log.warning("diff.parent_fallback_failed", parentDocId=parent_id, error=str(exc))
        return []
