"""Field-level diff + heuristic+LLM impact scoring."""
from __future__ import annotations

import uuid
from typing import Any

from shared.config import settings
from shared.logger import get_logger
from shared.openai_client import chat_json
from shared.text import normalize

log = get_logger("blue-iq.diff.engine")


HIGH_RISK_CATEGORIES = {"Liability", "IP", "Indemnity", "Termination"}


_IMPACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "rationale"],
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 100},
        "rationale": {"type": "string"},
    },
}


_IMPACT_SYSTEM = """You assess commercial risk impact of contract clause changes.
Given before/after text of a single clause, return a strict JSON object with:
  - score: integer 1..100 (higher = more commercial/legal risk)
  - rationale: one sentence explaining the score
Be conservative; small wording changes are usually low.
"""


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_clauses(
    *,
    current_clauses: list[dict[str, Any]],
    parent_clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return field-level changes (title/body/category) keyed by clauseNumber."""
    parent_by_num = {_norm_num(c.get("number", "")): c for c in parent_clauses}
    changes: list[dict[str, Any]] = []

    for cur in current_clauses:
        num_key = _norm_num(cur.get("number", ""))
        parent = parent_by_num.get(num_key)
        if not parent:
            # New clause in amendment — treat as an ADD.
            changes.append(
                _mk_change(
                    clause_number=cur.get("number", ""),
                    field="body",
                    before="",
                    after=cur.get("body", ""),
                )
            )
            continue
        for field in ("title", "body", "category"):
            before = parent.get(field, "") or ""
            after = cur.get(field, "") or ""
            if normalize(before) != normalize(after):
                changes.append(
                    _mk_change(
                        clause_number=cur.get("number", ""),
                        field=field,
                        before=before,
                        after=after,
                    )
                )

    # Detect deletions (clauses present in parent but missing in current).
    current_keys = {_norm_num(c.get("number", "")) for c in current_clauses}
    for k, parent_clause in parent_by_num.items():
        if k not in current_keys:
            changes.append(
                _mk_change(
                    clause_number=parent_clause.get("number", ""),
                    field="body",
                    before=parent_clause.get("body", ""),
                    after="",
                )
            )

    return changes


def _mk_change(
    *, clause_number: str, field: str, before: str, after: str
) -> dict[str, Any]:
    return {
        "changeId": uuid.uuid4().hex,
        "clauseNumber": clause_number,
        "field": field,
        "before": before,
        "after": after,
        "impactScore": 0,
        "impactRationale": "",
        "_length_delta_pct": _length_delta_pct(before, after),
        "_category": "",
    }


def _length_delta_pct(before: str, after: str) -> float:
    a, b = len(before or ""), len(after or "")
    if a == 0 and b == 0:
        return 0.0
    return abs(b - a) / max(a, b) * 100.0


def _norm_num(num: str) -> str:
    return "".join(ch for ch in (num or "").lower() if ch.isalnum() or ch == ".")


# ---------------------------------------------------------------------------
# Impact scoring
# ---------------------------------------------------------------------------


def score_impacts(
    changes: list[dict[str, Any]],
    *,
    current_clauses: list[dict[str, Any]],
) -> None:
    """Mutate `changes` in place to add impactScore + impactRationale.

    Rules:
      * heuristic base score is always applied
      * top N changes by length-delta also get an OpenAI call (N = cap)
    """
    cat_by_num = {
        _norm_num(c.get("number", "")): c.get("category", "Other") for c in current_clauses
    }

    # 1. Heuristic score for every change.
    for ch in changes:
        cat = cat_by_num.get(_norm_num(ch["clauseNumber"]), "Other")
        ch["_category"] = cat
        base = 60 if cat in HIGH_RISK_CATEGORIES else 30
        if ch["_length_delta_pct"] > 30:
            base += 20
        if ch["field"] == "category":
            base += 10
        ch["impactScore"] = min(100, base)
        ch["impactRationale"] = (
            f"Heuristic: {cat} field={ch['field']} Δlen={ch['_length_delta_pct']:.0f}%"
        )

    # 2. LLM refinement for top-N most-impactful candidates.
    cap = settings.diff_impact_call_cap
    candidates = sorted(changes, key=lambda c: c["_length_delta_pct"], reverse=True)[:cap]
    for ch in candidates:
        try:
            refined = _llm_impact(ch)
            ch["impactScore"] = int(refined["score"])
            ch["impactRationale"] = refined["rationale"]
        except Exception as e:
            log.warning(
                "diff.impact.llm_failed",
                changeId=ch.get("changeId"),
                error=str(e),
            )

    # Strip private fields.
    for ch in changes:
        ch.pop("_length_delta_pct", None)
        ch.pop("_category", None)


def _llm_impact(change: dict[str, Any]) -> dict[str, Any]:
    user = (
        f"Category: {change.get('_category', 'Other')}\n"
        f"Field changed: {change['field']}\n"
        f"BEFORE:\n{(change['before'] or '')[:3000]}\n\n"
        f"AFTER:\n{(change['after'] or '')[:3000]}\n"
    )
    return chat_json(
        system=_IMPACT_SYSTEM,
        user=user,
        json_schema=_IMPACT_SCHEMA,
        schema_name="ImpactScore",
        temperature=0.0,
    )


def build_impact_summary(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return "No changes detected vs. parent."
    high = [c for c in changes if c["impactScore"] >= 70]
    med = [c for c in changes if 40 <= c["impactScore"] < 70]
    return (
        f"{len(changes)} change(s): {len(high)} high-impact, {len(med)} medium, "
        f"{len(changes) - len(high) - len(med)} low."
    )
