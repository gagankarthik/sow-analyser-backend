"""Pillar (domain) tagging — classify a document into a Blue-IQ Campus pillar.

Blue-IQ Campus is one platform with four entry points (see the Campus GTM deck):

  * software_license — Software License Intelligence (EULAs, SaaS, POs)
  * contract         — Contract Governance (MSAs, SOWs, vendor agreements)
  * ip_venture       — Venture Formation & Tech Transfer (IP licenses, royalties)
  * grants           — Grants & Research Compliance (award terms, sub-awards)

Like ``playbook`` and ``compliance`` this is:

* DETERMINISTIC — the pillar and its risk flags are derived from the structured
  fields the classify stage already extracted (docType, clause categories +
  risk levels, renewal terms, compliance gaps). No extra LLM call; same
  classification → same domain.
* PERSISTED — the classify stage writes the result into classification.json as
  ``domain`` so the existing read API exposes it with no infra change.
* HONEST — every risk flag is backed by a signal actually present in the
  extraction. We never assert shelfware/usage facts a document can't evidence.

The per-domain risk flags mirror the "Key Risk Flags / Action It Enables" matrix
on slide 6 of the deck, but only ever fire on evidence in the document.
"""
from __future__ import annotations

from typing import Any

from .logger import get_logger

log = get_logger("blue-iq.domains")

_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# Pillar registry
# ---------------------------------------------------------------------------

class Pillar:
    __slots__ = ("id", "label")

    def __init__(self, id: str, label: str):
        self.id = id
        self.label = label


_PILLARS: dict[str, Pillar] = {
    "software_license": Pillar("software_license", "Software License Intelligence"),
    "contract":         Pillar("contract", "Contract Governance"),
    "ip_venture":       Pillar("ip_venture", "Venture Formation & Tech Transfer"),
    "grants":           Pillar("grants", "Grants & Research Compliance"),
}

# Public: pillar ids in registry order.
KNOWN_PILLAR_IDS = list(_PILLARS.keys())

# Contract Governance is the base pillar — it gets a small floor so a generic
# vendor agreement with no distinguishing signal lands here rather than nowhere.
_CONTRACT_FLOOR = 0.5


# ---------------------------------------------------------------------------
# Scoring inputs
# ---------------------------------------------------------------------------

# Clause categories that point at a pillar (category -> {pillar: weight}).
_CATEGORY_WEIGHTS: dict[str, dict[str, float]] = {
    "LicenseGrant":     {"software_license": 2, "ip_venture": 1},
    "LicenseScope":     {"software_license": 2},
    "Restrictions":     {"software_license": 1},
    "SourceCodeEscrow": {"software_license": 2},
    "OpenSource":       {"software_license": 2},
    "Sublicensing":     {"ip_venture": 2, "software_license": 1},
    "Royalties":        {"ip_venture": 2, "software_license": 1},
    "IP":               {"ip_venture": 1},
    "Subcontracting":   {"grants": 1},
    "Compliance":       {"grants": 1},
}

# docType nudges.
_DOCTYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "LICENSE": {"software_license": 2, "ip_venture": 1},
    "SOW":     {"contract": 1},
    "MSA":     {"contract": 1},
    "NDA":     {"contract": 0.5},
}

# Distinctive phrases per pillar. Substrings are matched against a lowercased
# haystack built from title/summary/parties/scope/findings/clause titles. Phrases
# are chosen to avoid cross-firing (e.g. "sub-award" not bare "grant", which would
# match "hereby granted" in a license clause).
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "software_license": (
        "software", "saas", "eula", "end user license", "end-user license",
        "subscription", "named user", "per-seat", "per seat", "seats",
        "license key", "deployment", "entitlement",
    ),
    "ip_venture": (
        "royalt", "inventor", "patent", "technology transfer", "tech transfer",
        "faculty disclosure", "invention disclosure", "spin-out", "spinout",
        "revenue share", "revenue-share", "exclusive license", "licensee",
    ),
    "grants": (
        "sub-award", "subaward", "sub award", "sponsored research", "sponsor",
        "2 cfr", "uniform guidance", "federal award", "indirect cost",
        "effort report", "f&a", "cost share", "cost-share", "nih", "nsf",
    ),
}


def _haystack(result: dict[str, Any]) -> str:
    parts: list[str] = [
        str(result.get("title") or ""),
        str(result.get("summary") or ""),
    ]
    parts.extend(str(p) for p in (result.get("parties") or []))
    scope = result.get("scope") or {}
    for key in ("inScope", "outOfScope", "assumptions", "dependencies"):
        parts.extend(str(s) for s in (scope.get(key) or []))
    for f in result.get("keyFindings") or []:
        parts.append(str(f.get("label") or ""))
        parts.append(str(f.get("detail") or ""))
    for c in result.get("clauses") or []:
        parts.append(str(c.get("title") or ""))
    return " ".join(parts).lower()


def _category_signals(clauses: list[dict[str, Any]]) -> tuple[set[str], dict[str, int]]:
    """Return (present categories, category -> max risk rank seen)."""
    present: set[str] = set()
    max_risk: dict[str, int] = {}
    for clause in clauses or []:
        cat = clause.get("category")
        if not cat:
            continue
        present.add(cat)
        rank = _RISK_RANK.get((clause.get("riskLevel") or "low").lower(), 0)
        if rank > max_risk.get(cat, -1):
            max_risk[cat] = rank
    return present, max_risk


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(result: dict[str, Any], present: set[str]) -> dict[str, float]:
    scores: dict[str, float] = {pid: 0.0 for pid in _PILLARS}
    scores["contract"] = _CONTRACT_FLOOR

    for cat in present:
        for pid, w in _CATEGORY_WEIGHTS.get(cat, {}).items():
            scores[pid] += w

    for pid, w in _DOCTYPE_WEIGHTS.get(str(result.get("docType") or ""), {}).items():
        scores[pid] += w

    haystack = _haystack(result)
    for pid, phrases in _KEYWORDS.items():
        hits = sum(1 for p in phrases if p in haystack)
        scores[pid] += min(hits, 4)  # cap so keyword spam can't dominate

    return {k: round(v, 2) for k, v in scores.items()}


def _confidence(scores: dict[str, float], winner: str) -> str:
    top = scores[winner]
    rest = sorted((v for k, v in scores.items() if k != winner), reverse=True)
    runner_up = rest[0] if rest else 0.0
    if top >= 3 and (top - runner_up) >= 2:
        return "high"
    if top > _CONTRACT_FLOOR:
        return "medium"
    return "low"  # nothing distinctive — defaulted to Contract Governance


# ---------------------------------------------------------------------------
# Risk flags (slide 6 — only ever fire on real evidence)
# ---------------------------------------------------------------------------

def _flag(id: str, label: str, detail: str, severity: str) -> dict[str, str]:
    return {"id": id, "label": label, "detail": detail, "severity": severity}


def _is_high(max_risk: dict[str, int], category: str) -> bool:
    return max_risk.get(category, -1) >= _RISK_RANK["high"]


def _risk_flags(
    pillar: str,
    result: dict[str, Any],
    present: set[str],
    max_risk: dict[str, int],
) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    timeline = result.get("timeline") or {}
    auto_renews = bool(timeline.get("autoRenews"))
    notice = timeline.get("renewalNoticeDays")
    long_or_no_notice = notice is None or (isinstance(notice, (int, float)) and notice >= 60)

    if pillar == "software_license":
        if _is_high(max_risk, "AuditRights"):
            flags.append(_flag(
                "broad_audit_rights", "Broad software audit rights",
                "The licensor's audit/inspection rights are high-risk — a common "
                "trigger for true-up audit exposure.", "high"))
        if _is_high(max_risk, "Restrictions"):
            flags.append(_flag(
                "restrictive_use", "Restrictive use terms",
                "Use restrictions are broad enough to constrain deployment — "
                "review against actual usage.", "medium"))
        if auto_renews:
            flags.append(_flag(
                "auto_renewal_overspend", "Auto-renewal may lock in unused licenses",
                "The license auto-renews; without a usage check this risks paying "
                "for shelfware. Reconcile entitlements vs. actual usage before renewal.",
                "medium" if long_or_no_notice else "low"))

    elif pillar == "ip_venture":
        if "Royalties" in present and _is_high(max_risk, "Royalties"):
            flags.append(_flag(
                "royalty_leakage", "Royalty terms pose leakage risk",
                "Royalty/revenue-share terms are high-risk as written — a source of "
                "missed IP revenue if unmonitored.", "high"))
        elif ("LicenseGrant" in present or "IP" in present) and "Royalties" not in present:
            flags.append(_flag(
                "missing_royalty_terms", "No royalty / revenue-share terms found",
                "IP appears to be licensed but no royalty or revenue-share clause was "
                "extracted — confirm the institution is capturing value.", "medium"))
        if _is_high(max_risk, "Sublicensing"):
            flags.append(_flag(
                "sublicensing_risk", "Sublicensing rights may dilute IP",
                "Sublicensing terms are high-risk — they can pass institutional IP "
                "downstream beyond intended control.", "high"))
        if _is_high(max_risk, "IP"):
            flags.append(_flag(
                "ip_ownership_risk", "IP ownership terms unfavorable",
                "IP ownership/assignment is high-risk from the institution's side.",
                "high"))

    elif pillar == "grants":
        if "Subcontracting" not in present:
            flags.append(_flag(
                "subaward_flowdown_missing", "Sub-award flow-down terms missing",
                "No subcontracting/sub-award clause was extracted. 2 CFR 200.332 "
                "requires flow-down of federal terms to sub-recipients.", "high"))
        if "DataRetention" not in present:
            flags.append(_flag(
                "retention_missing", "Record-retention terms missing",
                "No record-retention clause was found. 2 CFR 200.334 requires "
                "financial records be retained (generally 3 years).", "medium"))
        compliance = result.get("compliance") or {}
        weak_frameworks = [
            f["name"] for f in (compliance.get("frameworks") or [])
            if f.get("status") in ("weak", "partial")
        ]
        if weak_frameworks:
            flags.append(_flag(
                "compliance_gaps", "Grant-compliance coverage gaps",
                "Enabled frameworks with gaps: " + ", ".join(weak_frameworks) +
                ". Close these before audit to protect continued funding.", "high"))

    else:  # contract (governance) — the base pillar
        if auto_renews and long_or_no_notice:
            flags.append(_flag(
                "auto_renewal_trap", "Auto-renewal trap",
                "The term auto-renews with a long or unspecified opt-out window — "
                "set a renewal alert ahead of the notice deadline.", "high"))
        if _is_high(max_risk, "Indemnity"):
            flags.append(_flag(
                "indemnity_overreach", "Indemnity overreach",
                "Indemnification is one-sided / high-risk as written.", "high"))
        if max_risk.get("Liability", -1) >= _RISK_RANK["critical"]:
            flags.append(_flag(
                "uncapped_liability", "Uncapped liability exposure",
                "A liability clause carries critical exposure — check for a missing "
                "or one-sided cap.", "critical"))

    return flags


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def classify_domain(result: dict[str, Any]) -> dict[str, Any]:
    """Tag a classified document with its Blue-IQ Campus pillar + risk flags.

    Pass the (post-classify) result dict; reads docType, clauses, timeline,
    scope, summary and the already-computed ``compliance`` block. Returns:
      {
        "pillar":     <pillar id>,
        "label":      <human label>,
        "confidence": "high" | "medium" | "low",
        "scores":     {<pillar id>: <score>, ...},
        "riskFlags":  [ {id, label, detail, severity}, ... ],
      }
    """
    clauses = result.get("clauses") or []
    present, max_risk = _category_signals(clauses)
    scores = _score(result, present)

    # Highest score wins; ties break toward Contract Governance (the base pillar).
    winner = max(_PILLARS, key=lambda pid: (scores[pid], pid == "contract"))
    confidence = _confidence(scores, winner)
    flags = _risk_flags(winner, result, present, max_risk)

    return {
        "pillar":     winner,
        "label":      _PILLARS[winner].label,
        "confidence": confidence,
        "scores":     scores,
        "riskFlags":  flags,
    }
