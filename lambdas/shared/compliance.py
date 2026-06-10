"""Compliance packs — grade a document against enabled regulatory frameworks.

The frontend lets a tenant enable compliance packs (GDPR, HIPAA, SOC 2, …); this
module is the SERVER-SIDE authority that turns that choice into a graded result.
Each pack maps to the clause categories the pipeline already classifies, so
"coverage" is simply: of a framework's required obligations, how many are present
in the document — and of those, how many are *weak* (a high-risk clause or a
playbook deviation).

Like ``playbook``, this is:

* DETERMINISTIC — coverage is computed from extracted clause categories + the
  deterministic playbook result, never an LLM call. Same document → same grade.
* OVERRIDABLE — the set of enabled packs comes from defaults, then the
  ``COMPLIANCE_PACKS_JSON`` env var (a JSON list of pack ids), then a per-tenant
  DynamoDB row (``PK=TENANT#<id>``, ``SK=COMPLIANCE`` with ``packs``: [...]).
* PERSISTED — the classify stage writes the result into classification.json as
  ``compliance`` so the existing read API exposes it with no infra change.

Keep the pack → category mapping in lock-step with the frontend
``lib/compliance-packs.ts``.
"""
from __future__ import annotations

import os
from typing import Any

from .logger import get_logger

log = get_logger("blue-iq.compliance")

_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# Pack registry (mirror of the frontend lib/compliance-packs.ts)
# ---------------------------------------------------------------------------

class Pack:
    __slots__ = ("id", "name", "region", "categories", "default_on")

    def __init__(self, id: str, name: str, region: str, categories: list[str], default_on: bool):
        self.id = id
        self.name = name
        self.region = region
        self.categories = categories
        self.default_on = default_on


_PACKS: dict[str, Pack] = {
    "gdpr": Pack(
        "gdpr", "GDPR", "European Union",
        ["DataProcessing", "DataResidency", "SubProcessors", "BreachNotification",
         "DataRetention", "DataProtection", "AuditRights"],
        True,
    ),
    "hipaa": Pack(
        "hipaa", "HIPAA", "United States · Health",
        ["BreachNotification", "SecurityControls", "DataRetention", "DataProtection",
         "SubProcessors", "AuditRights"],
        True,
    ),
    "soc2": Pack(
        "soc2", "SOC 2", "AICPA Trust Services",
        ["SecurityControls", "AuditRights", "DataRetention", "BreachNotification", "DataProcessing"],
        True,
    ),
    "ccpa": Pack(
        "ccpa", "CCPA / CPRA", "California",
        ["DataProcessing", "DataResidency", "DataRetention", "DataProtection"],
        False,
    ),
    "iso27001": Pack(
        "iso27001", "ISO 27001", "International",
        ["SecurityControls", "AuditRights", "DataRetention", "BreachNotification"],
        False,
    ),
}

_DEFAULT_ENABLED = [p.id for p in _PACKS.values() if p.default_on]

# Public: the full set of known pack ids, in registry order (used by the API to
# validate a tenant's selection).
KNOWN_PACK_IDS = list(_PACKS.keys())

# Playbook deviation statuses that count a present obligation as "weak".
_WEAK_DEVIATION_STATUSES = {"moderate", "material"}


# ---------------------------------------------------------------------------
# Enabled-pack resolution (defaults ← env ← tenant)
# ---------------------------------------------------------------------------

def _load_tenant_packs(tenant_id: str | None) -> list[str] | None:
    """Per-tenant enabled packs from DynamoDB (best-effort; never raises)."""
    if not tenant_id:
        return None
    try:
        from .dynamodb import get_compliance_packs
        return get_compliance_packs(tenant_id)
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("compliance.tenant_packs_load_failed", tenantId=tenant_id, error=str(exc))
        return None


def resolve_enabled_packs(tenant_id: str | None = None) -> list[str]:
    """Return the effective list of enabled pack ids (defaults ← env ← tenant)."""
    enabled = list(_DEFAULT_ENABLED)

    env_raw = os.environ.get("COMPLIANCE_PACKS_JSON", "").strip()
    if env_raw:
        try:
            import orjson
            parsed = orjson.loads(env_raw)
            if isinstance(parsed, list):
                enabled = [str(p) for p in parsed]
        except Exception as exc:
            log.warning("compliance.env_packs_invalid", error=str(exc))

    tenant_packs = _load_tenant_packs(tenant_id)
    if tenant_packs is not None:
        enabled = tenant_packs

    # Keep only ids we actually know about, preserving registry order.
    known = [pid for pid in _PACKS if pid in set(enabled)]
    return known


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _category_signals(clauses: list[dict[str, Any]], playbook: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    """Return (present categories, weak categories).

    A category is *present* if any clause carries it. It is *weak* if a clause in
    that category is high/critical risk, or the playbook flagged a
    moderate/material deviation for it.
    """
    present: set[str] = set()
    weak: set[str] = set()

    for clause in clauses or []:
        cat = clause.get("category")
        if not cat:
            continue
        present.add(cat)
        if _RISK_RANK.get((clause.get("riskLevel") or "low").lower(), 0) >= _RISK_RANK["high"]:
            weak.add(cat)

    for dev in (playbook or {}).get("deviations", []) or []:
        if dev.get("status") in _WEAK_DEVIATION_STATUSES and dev.get("category"):
            weak.add(dev["category"])

    return present, weak


def _status_for(pct: int) -> str:
    if pct >= 80:
        return "strong"
    if pct >= 50:
        return "partial"
    return "weak"


def evaluate_compliance(
    clauses: list[dict[str, Any]],
    playbook: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    enabled_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Grade a document against the tenant's enabled compliance packs.

    Returns a structured, deterministic ``compliance`` block:
      {
        "evaluated":  [<pack id>, ...],
        "frameworks": [ {id, name, region, required, covered, gaps:[...],
                         weak:[...], coveragePct, status}, ... ],
        "overallCoveragePct": <int>,   # mean coverage across enabled packs
        "totalGaps": <int>,            # distinct (pack, category) gaps
      }
    """
    enabled = enabled_ids if enabled_ids is not None else resolve_enabled_packs(tenant_id)
    present, weak = _category_signals(clauses, playbook)

    frameworks: list[dict[str, Any]] = []
    total_gaps = 0
    pct_sum = 0

    for pid in enabled:
        pack = _PACKS.get(pid)
        if pack is None:
            continue
        required = pack.categories
        covered = [c for c in required if c in present]
        gaps = [c for c in required if c not in present]
        weak_here = [c for c in covered if c in weak]
        pct = round((len(covered) / len(required)) * 100) if required else 0
        total_gaps += len(gaps)
        pct_sum += pct
        frameworks.append({
            "id": pack.id,
            "name": pack.name,
            "region": pack.region,
            "required": len(required),
            "covered": len(covered),
            "gaps": gaps,
            "weak": weak_here,
            "coveragePct": pct,
            "status": _status_for(pct),
        })

    overall = round(pct_sum / len(frameworks)) if frameworks else 0

    return {
        "evaluated": [f["id"] for f in frameworks],
        "frameworks": frameworks,
        "overallCoveragePct": overall,
        "totalGaps": total_gaps,
    }
