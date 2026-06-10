"""Playbook — the firm's standard negotiation positions, and deviation detection.

The product promise ("Deviations from your standard positions surface instantly")
needs two things that did NOT previously exist anywhere in the backend:

  1. a SOURCE OF TRUTH for the firm's standard positions, keyed by clause type, and
  2. a deviation check that compares each extracted clause against those positions
     and produces a structured, persisted result.

This module owns (1) and the deterministic core of (2). The classify stage calls
``evaluate_clauses`` after extraction and writes the result into
classification.json as ``playbook`` so the existing
``GET /documents/{docId}/classification`` read path exposes it to the frontend
with no infra change.

Design choices
--------------
* DETERMINISTIC by default. Each standard position carries a small set of
  machine-checkable rules (numeric thresholds parsed from the clause text,
  required/forbidden keywords). A clause is only flagged when a rule actually
  fires, so the same document always yields the same deviations — critical for a
  money/correctness product. No LLM is required for the deviation check.
* OVERRIDABLE. The default positions can be replaced per deployment via the
  ``PLAYBOOK_JSON`` env var (a JSON object keyed by clause category) or per
  tenant via a DynamoDB row (``PK=TENANT#<id>``, ``SK=PLAYBOOK``). Env/DDB only
  override the fields they specify; everything else falls back to the defaults.
* HONEST. When a clause's text doesn't contain a value the rule needs (e.g. a
  liability clause that never states a cap), the position is reported as
  ``status="review"`` (needs human eyes) rather than silently "ok".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .logger import get_logger

log = get_logger("blue-iq.playbook")

# Severity ranking used to roll a document up to one headline severity.
_SEVERITY_RANK = {"none": 0, "minor": 1, "moderate": 2, "material": 3}


# ---------------------------------------------------------------------------
# Money / term parsing helpers (deterministic)
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"(?:USD|US\$|\$|€|£)\s?([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
_NET_DAYS_RE = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d{1,3})\s*(?:calendar\s*|business\s*)?days?", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d{1,3})\s*months?", re.IGNORECASE)
_HOURS_RE = re.compile(r"(\d{1,4})\s*hours?", re.IGNORECASE)


def parse_money(text: str) -> float | None:
    """Return the first monetary amount in ``text`` as a float, or None."""
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_net_days(text: str) -> int | None:
    """Return the payment window in days from a 'Net N' phrase, or None."""
    if not text:
        return None
    m = _NET_DAYS_RE.search(text)
    if m:
        return int(m.group(1))
    # "due within 30 days", "payable in 45 days"
    if re.search(r"\b(?:due|payable|within|paid)\b", text, re.IGNORECASE):
        d = _DAYS_RE.search(text)
        if d:
            return int(d.group(1))
    if re.search(r"\bdue on receipt\b", text, re.IGNORECASE):
        return 0
    return None


def parse_notice_days(text: str) -> int | None:
    """Return a notice/cure window in days (months converted), or None."""
    if not text:
        return None
    d = _DAYS_RE.search(text)
    if d:
        return int(d.group(1))
    mo = _MONTHS_RE.search(text)
    if mo:
        return int(mo.group(1)) * 30
    return None


def _has_any(text: str, phrases: list[str]) -> bool:
    low = (text or "").lower()
    return any(p.lower() in low for p in phrases)


# ---------------------------------------------------------------------------
# Standard positions
# ---------------------------------------------------------------------------


@dataclass
class StandardPosition:
    """One firm-standard rule for a clause category.

    ``check`` receives the clause body and returns (status, deviationText) where
    status is one of: "ok", "minor", "moderate", "material", "review".
    """

    category: str
    label: str
    standard: str            # human-readable standard position (shown in the UI)
    rationale: str           # why the firm holds this position
    check: Callable[[str], tuple[str, str]]
    extra: dict[str, Any] = field(default_factory=dict)


# --- individual deterministic checks ----------------------------------------


def _check_liability(body: str) -> tuple[str, str]:
    low = body.lower()
    if _has_any(body, ["uncapped", "unlimited liability", "without limitation as to amount", "no limitation of liability"]):
        return "material", "Liability appears UNCAPPED — the standard is a cap of 1x fees paid."
    if _has_any(body, ["limitation of liability", "liability cap", "aggregate liability", "total liability", "shall not exceed", "limited to"]):
        # Look for a multiplier of fees.
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:the\s*)?(?:annual\s*)?fees", low)
        if m:
            mult = float(m.group(1))
            if mult > 1.0:
                sev = "material" if mult >= 3 else "moderate"
                return sev, f"Liability cap is {mult:g}x fees; standard is 1x fees."
            return "ok", ""
        if "12 months" in low or "twelve months" in low or "preceding 12" in low:
            return "ok", ""
        # A cap exists but its size can't be read deterministically.
        return "review", "A liability cap is present but its magnitude could not be parsed; confirm it is ≤ 1x fees."
    return "review", "No clear limitation of liability found; standard requires a 1x-fees cap."


def _check_indemnity(body: str) -> tuple[str, str]:
    if _has_any(body, ["mutual indemn", "each party shall indemnify", "indemnify each other"]):
        return "ok", ""
    if _has_any(body, ["indemnify", "indemnification", "hold harmless"]):
        if _has_any(body, ["uncapped", "unlimited", "any and all", "without limitation"]):
            return "material", "Indemnity appears one-sided and uncapped; standard is mutual and capped."
        return "moderate", "Indemnity may be one-sided; standard is a mutual indemnity."
    return "ok", ""


def _check_payment(body: str) -> tuple[str, str]:
    days = parse_net_days(body)
    if days is None:
        return "review", "No payment term found; standard is Net 30."
    if days <= 30:
        return "ok", ""
    if days <= 45:
        return "minor", f"Payment term is Net {days}; standard is Net 30."
    if days <= 60:
        return "moderate", f"Payment term is Net {days}; standard is Net 30."
    return "material", f"Payment term is Net {days}; standard is Net 30 (long terms hurt cash flow)."


def _check_fees(body: str) -> tuple[str, str]:
    # Fees clauses are informational for the playbook; flag only an explicit
    # auto-escalation above the standard ceiling.
    m = re.search(r"increase[ds]?\s*(?:by\s*)?(?:up to\s*)?(\d+(?:\.\d+)?)\s*%", body, re.IGNORECASE)
    if m:
        pct = float(m.group(1))
        if pct > 5.0:
            return "moderate", f"Annual fee increase of {pct:g}% exceeds the standard 5% cap."
        return "ok", ""
    return "ok", ""


def _check_termination(body: str) -> tuple[str, str]:
    low = body.lower()
    if _has_any(body, ["for convenience", "without cause", "at any time"]):
        days = parse_notice_days(body)
        if days is not None and days < 30:
            return "moderate", f"Termination-for-convenience notice is {days} days; standard is ≥ 30 days."
        if days is None:
            return "review", "Termination for convenience present but the notice period could not be parsed; standard is ≥ 30 days."
        return "ok", ""
    if "terminate" in low:
        return "ok", ""
    return "review", "No termination terms found; confirm a ≥ 30-day notice / cure window."


def _check_autorenewal(body: str) -> tuple[str, str]:
    low = body.lower()
    if _has_any(body, ["automatically renew", "auto-renew", "auto renew", "evergreen", "renew for successive"]):
        days = parse_notice_days(body)
        if days is not None and days > 30:
            return "moderate", f"Auto-renewal opt-out window is {days} days; standard is ≤ 30 days notice."
        if days is None:
            return "moderate", "Auto-renewal present with no clear opt-out window; standard is a ≤ 30-day opt-out."
        return "ok", ""
    return "ok", ""


def _check_confidentiality(body: str) -> tuple[str, str]:
    if _has_any(body, ["confidential", "non-disclosure", "proprietary information"]):
        # Survival period: standard is <= 3 years (trade secrets perpetual is fine).
        m = re.search(r"(\d+)\s*years?", body, re.IGNORECASE)
        if m:
            yrs = int(m.group(1))
            if yrs > 5:
                return "moderate", f"Confidentiality survival is {yrs} years; standard is ≤ 3 years (trade secrets aside)."
        return "ok", ""
    return "review", "No confidentiality terms found."


def _check_ip(body: str) -> tuple[str, str]:
    low = body.lower()
    if _has_any(body, ["work made for hire", "work for hire", "assign all right", "assigns all right", "ownership of all"]):
        if not _has_any(body, ["background ip", "pre-existing", "retained", "carve-out", "carve out"]):
            return "moderate", "Full IP assignment with no background-IP carve-out; standard retains pre-existing IP."
        return "ok", ""
    if "intellectual property" in low or "ip" in low:
        return "ok", ""
    return "review", "No IP ownership terms found."


def _check_dataprotection(body: str) -> tuple[str, str]:
    if _has_any(body, ["personal data", "gdpr", "data protection", "data processing", "ccpa"]):
        if not _has_any(body, ["breach notification", "notify", "notification"]):
            return "minor", "Data-protection clause lacks an explicit breach-notification obligation."
        return "ok", ""
    return "ok", ""


def _check_breach_notification(body: str) -> tuple[str, str]:
    """Compliance docs (DPA/BAA): the breach-notification window. GDPR sets 72h."""
    if not _has_any(body, ["breach", "security incident", "notify", "notification"]):
        return "review", "No breach-notification timeframe found; standard is notice without undue delay and within 72 hours."
    hm = _HOURS_RE.search(body)
    if hm:
        hrs = int(hm.group(1))
        return ("ok", "") if hrs <= 72 else ("moderate", f"Breach notification window is {hrs} hours; standard is within 72 hours.")
    dm = _DAYS_RE.search(body)
    if dm:
        days = int(dm.group(1))
        return ("ok", "") if days * 24 <= 72 else ("moderate", f"Breach notification window is {days} day(s); standard is within 72 hours.")
    if _has_any(body, ["without undue delay", "promptly", "immediately"]):
        return "ok", ""
    return "review", "Breach-notification clause present but no clear timeframe; standard is within 72 hours."


def _check_data_retention(body: str) -> tuple[str, str]:
    """Compliance docs: data must be deleted or returned on termination."""
    if _has_any(body, ["delete", "deletion", "return or destroy", "destroy", "erasure", "purge"]):
        return "ok", ""
    if _has_any(body, ["retain", "retention", "kept for", "stored for"]):
        return "minor", "Retention stated but no deletion/return obligation on termination; standard requires data be deleted or returned."
    return "review", "No data-retention or deletion terms found; standard requires deletion or return of data on termination."


def _check_audit_rights(body: str) -> tuple[str, str]:
    """Licensing/compliance docs: audits need reasonable advance notice."""
    if not _has_any(body, ["audit", "inspect", "examine records", "right to verify"]):
        return "ok", ""
    if _has_any(body, ["at any time", "without notice", "unannounced"]):
        return "moderate", "Audit rights allow inspection without reasonable notice; standard requires advance written notice."
    days = parse_notice_days(body)
    if days is None:
        return "review", "Audit rights present but no notice period stated; standard requires reasonable advance notice (≥ 10 business days)."
    if days < 10:
        return "minor", f"Audit notice is {days} day(s); standard is ≥ 10 business days."
    return "ok", ""


def _check_license_grant(body: str) -> tuple[str, str]:
    """Licensing: the grant should be clear, and ideally non-exclusive & non-revocable."""
    if not _has_any(body, ["license", "licence", "grant", "right to use"]):
        return "review", "No clear licence grant found; confirm what is licensed and on what basis (term, exclusivity)."
    if _has_any(body, ["exclusive"]) and not _has_any(body, ["non-exclusive", "nonexclusive", "non exclusive"]):
        return "moderate", "Licence appears EXCLUSIVE; the standard position is a non-exclusive grant unless exclusivity is intended."
    if _has_any(body, ["revocable at", "may revoke", "terminate the licen", "at licensor's sole discretion"]):
        return "moderate", "Licence may be revocable at the licensor's discretion; standard is a non-revocable grant for the term."
    return "ok", ""


def _check_license_scope(body: str) -> tuple[str, str]:
    """Licensing: scope (territory / field of use / users) should be bounded."""
    if _has_any(body, ["territory", "field of use", "named user", "per seat", "per-seat", "environment",
                       "worldwide", "perpetual", "for use in", "internal business", "named"]):
        return "ok", ""
    return "review", "Licence scope (territory / field of use / users) is not clearly bounded; confirm the limits."


def _check_restrictions(body: str) -> tuple[str, str]:
    """Licensing: use restrictions are normal; flag only one-sided suspension powers."""
    if _has_any(body, ["sole discretion", "any reason", "without cause"]) and \
       _has_any(body, ["terminate", "suspend", "revoke", "disable"]):
        return "moderate", "Restrictions allow suspension/termination at the licensor's sole discretion; standard requires cause and notice."
    return "ok", ""


def _check_royalties(body: str) -> tuple[str, str]:
    """Licensing: licence fees / royalties — flag uncapped escalation or retroactive true-ups."""
    m = re.search(r"increase[ds]?\s*(?:by\s*)?(?:up to\s*)?(\d+(?:\.\d+)?)\s*%", body, re.IGNORECASE)
    if m and float(m.group(1)) > 5.0:
        return "moderate", f"Royalty/fee escalation of {float(m.group(1)):g}% exceeds the standard 5% annual cap."
    if _has_any(body, ["true-up", "true up"]) and _has_any(body, ["retroactive", "retrospective", "back-dated", "backdated"]):
        return "minor", "Royalty true-up may be applied retroactively; confirm the look-back period and cap."
    return "ok", ""


def _check_sublicensing(body: str) -> tuple[str, str]:
    """Licensing: sublicensing rights should be addressed explicitly."""
    if not _has_any(body, ["sublicense", "sub-license", "sublicence", "sub-licence"]):
        return "review", "Sublicensing is not addressed; confirm whether the rights may be passed on, and on what terms."
    return "ok", ""


def _check_open_source(body: str) -> tuple[str, str]:
    """Licensing: copyleft components without a carve-out are a compliance risk."""
    if _has_any(body, ["gpl", "agpl", "lgpl", "copyleft"]):
        if not _has_any(body, ["carve-out", "carve out", "excluded", "does not include", "no copyleft", "exclud"]):
            return "moderate", "Copyleft (GPL/AGPL) components are referenced without a carve-out; review licence-compatibility obligations."
        return "ok", ""
    return "ok", ""


def _check_data_residency(body: str) -> tuple[str, str]:
    """Compliance: where data is stored/processed should be stated."""
    if _has_any(body, ["data center", "data centre", "region", "located in", "stored in", "hosted in",
                       "residency", "within the eea", "within the eu", "united states", "processed in"]):
        return "ok", ""
    return "review", "No data-residency / location terms found; confirm where data is stored and processed."


def _check_subprocessors(body: str) -> tuple[str, str]:
    """Compliance: sub-processors should require notice and a right to object."""
    if not _has_any(body, ["sub-processor", "subprocessor", "sub processor", "sub-processors", "subprocessors"]):
        return "review", "Sub-processor terms not found; confirm approval / notice rights for any sub-processors."
    if _has_any(body, ["prior written consent", "advance notice", "right to object", "list of sub", "notify"]):
        return "ok", ""
    return "minor", "Sub-processors permitted with no clear notice / objection right; standard requires advance notice and a right to object."


def _noop_check(_body: str) -> tuple[str, str]:
    return "ok", ""


# --- default position registry (overridable) --------------------------------

_DEFAULT_POSITIONS: dict[str, StandardPosition] = {
    "Liability": StandardPosition(
        category="Liability", label="Limitation of Liability",
        standard="Aggregate liability capped at 1x fees paid in the prior 12 months; exclude consequential damages.",
        rationale="Caps downside exposure to the value of the engagement.",
        check=_check_liability,
        extra={"capMultipleOfFees": 1.0},
    ),
    "Indemnity": StandardPosition(
        category="Indemnity", label="Indemnification",
        standard="Mutual indemnification, capped, limited to third-party claims.",
        rationale="One-sided or uncapped indemnities transfer unbounded risk.",
        check=_check_indemnity,
    ),
    "Payment": StandardPosition(
        category="Payment", label="Payment Terms",
        standard="Net 30 from invoice date.",
        rationale="Protects cash flow; longer terms are a financing cost.",
        check=_check_payment,
        extra={"netDays": 30},
    ),
    "Fees": StandardPosition(
        category="Fees", label="Fees & Escalation",
        standard="Annual fee increases capped at 5%.",
        rationale="Prevents uncontrolled cost growth over the term.",
        check=_check_fees,
        extra={"maxAnnualIncreasePct": 5.0},
    ),
    "Termination": StandardPosition(
        category="Termination", label="Termination",
        standard="Termination for convenience with ≥ 30 days' notice; cure period for breach.",
        rationale="Preserves an orderly exit and a chance to cure.",
        check=_check_termination,
        extra={"minNoticeDays": 30},
    ),
    "Term": StandardPosition(
        category="Term", label="Term & Auto-Renewal",
        standard="Auto-renewal only with a ≤ 30-day opt-out notice window.",
        rationale="Avoids being locked into an unwanted renewal.",
        check=_check_autorenewal,
        extra={"maxOptOutDays": 30},
    ),
    "Confidentiality": StandardPosition(
        category="Confidentiality", label="Confidentiality",
        standard="Confidentiality survives ≤ 3 years post-termination (trade secrets indefinitely).",
        rationale="Bounded survival is standard; very long terms are unusual.",
        check=_check_confidentiality,
        extra={"maxSurvivalYears": 3},
    ),
    "IP": StandardPosition(
        category="IP", label="Intellectual Property",
        standard="Assign foreground IP to the client; retain background / pre-existing IP.",
        rationale="Keeps reusable, pre-existing assets out of a blanket assignment.",
        check=_check_ip,
    ),
    "DataProtection": StandardPosition(
        category="DataProtection", label="Data Protection",
        standard="Include breach-notification obligations and a data-processing addendum.",
        rationale="Regulatory exposure (GDPR/CCPA) requires explicit handling terms.",
        check=_check_dataprotection,
    ),
    # ── Compliance & licensing document positions ─────────────────────────
    "BreachNotification": StandardPosition(
        category="BreachNotification", label="Breach Notification",
        standard="Breaches notified without undue delay and within 72 hours.",
        rationale="GDPR expects a 72-hour window; longer delays increase exposure.",
        check=_check_breach_notification,
        extra={"maxHours": 72},
    ),
    "DataRetention": StandardPosition(
        category="DataRetention", label="Data Retention & Deletion",
        standard="Data deleted or returned on termination; no indefinite retention.",
        rationale="Holding data beyond its purpose raises breach and compliance risk.",
        check=_check_data_retention,
    ),
    "AuditRights": StandardPosition(
        category="AuditRights", label="Audit Rights",
        standard="Audits permitted with reasonable advance written notice (≥ 10 business days).",
        rationale="Unannounced or unlimited audits are disruptive and one-sided.",
        check=_check_audit_rights,
        extra={"minNoticeDays": 10},
    ),
    # ── Technology / software licensing positions ─────────────────────────
    "LicenseGrant": StandardPosition(
        category="LicenseGrant", label="License Grant",
        standard="A clear, non-exclusive, non-revocable grant for the term.",
        rationale="Exclusive or revocable grants reduce flexibility and create lock-in.",
        check=_check_license_grant,
    ),
    "LicenseScope": StandardPosition(
        category="LicenseScope", label="License Scope",
        standard="Scope bounded by territory, field of use, and/or named users.",
        rationale="An unbounded scope makes future use and pricing unpredictable.",
        check=_check_license_scope,
    ),
    "Restrictions": StandardPosition(
        category="Restrictions", label="Use Restrictions",
        standard="Use restrictions are reasonable; no suspension at the licensor's sole discretion.",
        rationale="One-sided suspension powers can disrupt the business without notice.",
        check=_check_restrictions,
    ),
    "Royalties": StandardPosition(
        category="Royalties", label="Royalties & License Fees",
        standard="Fee escalation capped at 5% annually; no retroactive true-ups.",
        rationale="Uncapped or retroactive fees create uncontrolled cost growth.",
        check=_check_royalties,
        extra={"maxAnnualIncreasePct": 5.0},
    ),
    "Sublicensing": StandardPosition(
        category="Sublicensing", label="Sublicensing",
        standard="Sublicensing rights addressed explicitly (permitted to affiliates / as needed).",
        rationale="Silence on sublicensing blocks legitimate downstream use.",
        check=_check_sublicensing,
    ),
    "OpenSource": StandardPosition(
        category="OpenSource", label="Open-Source Components",
        standard="Copyleft (GPL/AGPL) components disclosed and carved out.",
        rationale="Undisclosed copyleft can impose unwanted distribution obligations.",
        check=_check_open_source,
    ),
    # ── Compliance / data-protection positions ────────────────────────────
    "DataResidency": StandardPosition(
        category="DataResidency", label="Data Residency",
        standard="Storage and processing location is stated (and acceptable for the data).",
        rationale="Residency drives regulatory exposure and cross-border transfer risk.",
        check=_check_data_residency,
    ),
    "SubProcessors": StandardPosition(
        category="SubProcessors", label="Sub-processors",
        standard="Sub-processors require advance notice and a right to object.",
        rationale="Unchecked sub-processors extend the data supply chain without oversight.",
        check=_check_subprocessors,
    ),
}


# ---------------------------------------------------------------------------
# Position resolution (defaults + overrides)
# ---------------------------------------------------------------------------


def _apply_overrides(base: dict[str, StandardPosition], overrides: dict[str, Any]) -> dict[str, StandardPosition]:
    """Overlay a JSON override map onto the default positions.

    An override only replaces the human-readable ``standard``/``rationale``/
    ``label`` and the numeric thresholds in ``extra``; the deterministic check
    function is kept from the default for the category (or _noop for an unknown
    category) so behaviour stays predictable.
    """
    out = dict(base)
    if not isinstance(overrides, dict):
        return out
    for cat, ov in overrides.items():
        if not isinstance(ov, dict):
            continue
        cur = out.get(cat)
        if cur is None:
            cur = StandardPosition(
                category=cat, label=ov.get("label", cat),
                standard=ov.get("standard", ""), rationale=ov.get("rationale", ""),
                check=_noop_check, extra={},
            )
        merged_extra = {**cur.extra, **(ov.get("extra") or {})}
        out[cat] = StandardPosition(
            category=cat,
            label=ov.get("label", cur.label),
            standard=ov.get("standard", cur.standard),
            rationale=ov.get("rationale", cur.rationale),
            check=cur.check,
            extra=merged_extra,
        )
    return out


def _load_tenant_overrides(tenant_id: str | None) -> dict[str, Any]:
    """Per-tenant overrides from DynamoDB (best-effort; never raises)."""
    if not tenant_id:
        return {}
    try:
        from .aws import dynamodb_resource
        from .config import settings as _settings
        if not _settings.table_name:
            return {}
        tbl = dynamodb_resource().Table(_settings.table_name)
        item = tbl.get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PLAYBOOK"}).get("Item")
        if item and isinstance(item.get("positions"), dict):
            return item["positions"]
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("playbook.tenant_override_load_failed", tenantId=tenant_id, error=str(exc))
    return {}


def resolve_positions(tenant_id: str | None = None) -> dict[str, StandardPosition]:
    """Return the effective standard positions (defaults ← env ← tenant)."""
    positions = dict(_DEFAULT_POSITIONS)

    env_raw = os.environ.get("PLAYBOOK_JSON", "").strip()
    if env_raw:
        try:
            import orjson
            positions = _apply_overrides(positions, orjson.loads(env_raw))
        except Exception as exc:
            log.warning("playbook.env_override_invalid", error=str(exc))

    tenant_ov = _load_tenant_overrides(tenant_id)
    if tenant_ov:
        positions = _apply_overrides(positions, tenant_ov)

    return positions


# ---------------------------------------------------------------------------
# Deviation evaluation (the stage)
# ---------------------------------------------------------------------------


def evaluate_clauses(clauses: list[dict[str, Any]], tenant_id: str | None = None) -> dict[str, Any]:
    """Compare each extracted clause against the firm's standard positions.

    Returns a structured, deterministic ``playbook`` block:
      {
        "checked": <int>,           # clauses evaluated against a position
        "deviationCount": <int>,    # clauses that deviate (not ok / not review)
        "reviewCount": <int>,       # clauses needing human eyes (ambiguous)
        "overallSeverity": "none|minor|moderate|material",
        "deviations": [ {clauseNumber, category, title, standard, rationale,
                         severity, deviation, status, sourceQuote}, ... ],
        "coverage": [<category>, ...]   # categories with a defined standard
      }
    Only clauses whose category maps to a defined standard position are checked;
    everything else is left untouched.
    """
    positions = resolve_positions(tenant_id)
    deviations: list[dict[str, Any]] = []
    checked = 0
    review = 0
    worst = "none"

    for clause in clauses or []:
        category = (clause.get("category") or "Other")
        pos = positions.get(category)
        if pos is None:
            continue
        checked += 1
        body = clause.get("body") or ""
        try:
            status, deviation_text = pos.check(body)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("playbook.check_failed", category=category, error=str(exc))
            status, deviation_text = "review", "Could not evaluate this clause automatically."

        if status == "ok":
            continue

        if status == "review":
            review += 1
            severity = "minor"
        else:
            severity = status
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(worst, 0):
                worst = severity

        deviations.append({
            "clauseNumber": clause.get("number", ""),
            "category": category,
            "title": clause.get("title", "") or pos.label,
            "standard": pos.standard,
            "rationale": pos.rationale,
            "severity": severity,
            "deviation": deviation_text,
            "status": status,           # "minor" | "moderate" | "material" | "review"
            "sourceQuote": body[:600],  # verbatim snippet so the UI can show provenance
        })

    deviation_count = sum(1 for d in deviations if d["status"] != "review")

    return {
        "checked": checked,
        "deviationCount": deviation_count,
        "reviewCount": review,
        "overallSeverity": worst,
        "deviations": deviations,
        "coverage": sorted(positions.keys()),
    }
