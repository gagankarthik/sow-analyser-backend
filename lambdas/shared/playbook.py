"""Playbook — standard negotiation positions, and per-clause deviation detection.

WHAT THE PLAYBOOK IS
--------------------
Three layers, later ones overriding earlier ones field by field:

  1. BUILT-IN DEFAULTS (``_DEFAULT_POSITIONS`` below) — a general-purpose set of
     standard positions for 21 clause types. They are the product's defaults,
     not a customer's own policy.
  2. DEPLOYMENT overrides — the ``PLAYBOOK_JSON`` env var.
  3. WORKSPACE rules — a DynamoDB row (``PK=TENANT#<id>``, ``SK=PLAYBOOK``) the
     workspace's user edits through ``/playbook`` in the API. A document is
     graded against the playbook of the workspace it was uploaded into.

A rule ("position") is keyed by a ``ruleId``: a clause category
(``"Payment"``) or, for clause types outside the fixed list, ``type.<key>``
(``"type.non-solicitation"``). It carries the standard position, the rationale,
an optional acceptable ``fallback``, numeric ``thresholds`` and optional
``requiredPhrases`` / ``forbiddenPhrases``.

HOW A CLAUSE IS GRADED (deterministic — no model call)
-----------------------------------------------------
Every clause gets exactly one outcome:

  within        checked against its rule and inside the standard position
  deviates      checked and outside it (severity minor / moderate / material)
  flagged       a rule applies but the text does not settle it — needs a human
  no_rule       the playbook has NO position for this clause type. This is not
                a pass: nothing was checked.
  unclassified  the clause has no type, so no rule could be chosen

Thresholds are read from the rule, so changing "Net 30" to "Net 45" in a
workspace rule changes what counts as a deviation. A rule with no automatic
check and no phrase lists is reported as ``flagged`` — never as ``within``.
The same clauses and the same rules always give the same result.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .clause_types import KNOWN_CATEGORIES, known_label
from .dates import offset_days, parse_quantity
from .logger import get_logger
from .money import parse_amount

log = get_logger("blue-iq.playbook")

# Severity ranking used to roll a document up to one headline severity.
_SEVERITY_RANK = {"none": 0, "minor": 1, "moderate": 2, "material": 3}
_OUTCOME = {"ok": "within", "minor": "deviates", "moderate": "deviates", "material": "deviates",
            "review": "flagged"}


# ---------------------------------------------------------------------------
# Money / term parsing helpers (deterministic)
# ---------------------------------------------------------------------------

_NET_DAYS_RE = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)
_HOURS_RE = re.compile(r"(\d{1,4})\s*hours?", re.IGNORECASE)
_INCREASE_RE = re.compile(r"increase[ds]?\s*(?:by\s*)?(?:up to\s*)?(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


def parse_money(text: str) -> float | None:
    """Return the first monetary amount in ``text`` as a float, or None.

    Understands multipliers and non-US notation ("USD 1.2 million", "Rs. 5,00,000",
    "€1.200,50") — see shared/money.py."""
    if not text:
        return None
    return parse_amount(text)


def _duration_days(text: str) -> int | None:
    """First duration in ``text`` in days: "thirty (30) days" → 30, "3 months" → 90,
    "two weeks" → 14. Written-out numbers and bracketed digits are both read."""
    qty = parse_quantity(text or "")
    if not qty:
        return None
    return offset_days(qty[0], qty[1])


def parse_net_days(text: str) -> int | None:
    """Return the payment window in days from a 'Net N' phrase, or None."""
    if not text:
        return None
    m = _NET_DAYS_RE.search(text)
    if m:
        return int(m.group(1))
    # "due within 30 days", "payable in forty-five (45) days"
    if re.search(r"\b(?:due|payable|within|paid)\b", text, re.IGNORECASE):
        days = _duration_days(text)
        if days is not None:
            return days
    if re.search(r"\bdue on receipt\b", text, re.IGNORECASE):
        return 0
    return None


def parse_notice_days(text: str) -> int | None:
    """Return a notice/cure window in days (weeks/months/years converted), or None."""
    if not text:
        return None
    return _duration_days(text)


def _has_any(text: str, phrases: list[str]) -> bool:
    low = (text or "").lower()
    return any(p.lower() in low for p in phrases)


def _num(extra: dict[str, Any] | None, key: str, default: float) -> float:
    try:
        return float((extra or {}).get(key, default))
    except (TypeError, ValueError):
        return default


class CheckResult(tuple):
    """``(status, reason)`` — unpacks like the 2-tuple it always was — plus
    ``.found``: the value read from the clause ("Net 45"), or None."""

    found: str | None

    def __new__(cls, status: str, reason: str = "", found: str | None = None) -> "CheckResult":
        obj = super().__new__(cls, (status, reason))
        obj.found = found
        return obj


_R = CheckResult


# ---------------------------------------------------------------------------
# Standard positions
# ---------------------------------------------------------------------------


@dataclass
class StandardPosition:
    """One standard rule for a clause type.

    ``check`` receives the clause body and the rule's thresholds and returns a
    CheckResult whose status is one of: "ok", "minor", "moderate", "material",
    "review". ``check`` is None for a rule with no automatic test.
    """

    category: str
    label: str
    standard: str            # human-readable standard position (shown in the UI)
    rationale: str           # why the position is held
    check: Callable[..., tuple[str, str]] | None
    extra: dict[str, Any] = field(default_factory=dict)      # numeric thresholds
    fallback: str | None = None                              # acceptable fallback position
    required_phrases: list[str] = field(default_factory=list)
    forbidden_phrases: list[str] = field(default_factory=list)
    phrase_severity: str = "moderate"
    source: str = "default"                                  # default | deployment | custom


# --- individual deterministic checks ----------------------------------------


def _check_liability(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    cap = _num(extra, "capMultipleOfFees", 1.0)
    low = body.lower()
    if _has_any(body, ["uncapped", "unlimited liability", "without limitation as to amount", "no limitation of liability"]):
        return _R("material", f"Liability appears UNCAPPED — the standard is a cap of {cap:g}x fees paid.", "uncapped")
    if _has_any(body, ["limitation of liability", "liability cap", "aggregate liability", "total liability", "shall not exceed", "limited to"]):
        # Look for a multiplier of fees.
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:the\s*)?(?:annual\s*)?fees", low)
        if m:
            mult = float(m.group(1))
            found = f"{mult:g}x fees"
            if mult > cap:
                sev = "material" if mult >= 3 * cap else "moderate"
                return _R(sev, f"Liability cap is {mult:g}x fees; standard is {cap:g}x fees.", found)
            return _R("ok", "", found)
        if "12 months" in low or "twelve months" in low or "preceding 12" in low:
            return _R("ok", "", "fees paid in the preceding 12 months")
        # A cap exists but its size can't be read deterministically.
        return _R("review", f"A liability cap is present but its magnitude could not be parsed; confirm it is ≤ {cap:g}x fees.")
    return _R("review", f"No clear limitation of liability found; standard requires a {cap:g}x-fees cap.")


def _check_indemnity(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    if _has_any(body, ["mutual indemn", "each party shall indemnify", "indemnify each other"]):
        return _R("ok", "", "mutual")
    if _has_any(body, ["indemnify", "indemnification", "hold harmless"]):
        if _has_any(body, ["uncapped", "unlimited", "any and all", "without limitation"]):
            return _R("material", "Indemnity appears one-sided and uncapped; standard is mutual and capped.", "one-sided, uncapped")
        return _R("moderate", "Indemnity may be one-sided; standard is a mutual indemnity.", "one-sided")
    return _R("ok", "")


def _check_payment(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    std = int(_num(extra, "netDays", 30))
    days = parse_net_days(body)
    if days is None:
        return _R("review", f"No payment term found; standard is Net {std}.")
    found = f"Net {days}"
    if days <= std:
        return _R("ok", "", found)
    if days <= std + 15:
        return _R("minor", f"Payment term is Net {days}; standard is Net {std}.", found)
    if days <= std + 30:
        return _R("moderate", f"Payment term is Net {days}; standard is Net {std}.", found)
    return _R("material", f"Payment term is Net {days}; standard is Net {std} (long terms hurt cash flow).", found)


def _check_fees(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    # Fees clauses are informational for the playbook; flag only an explicit
    # auto-escalation above the standard ceiling.
    cap = _num(extra, "maxAnnualIncreasePct", 5.0)
    m = _INCREASE_RE.search(body)
    if m:
        pct = float(m.group(1))
        if pct > cap:
            return _R("moderate", f"Annual fee increase of {pct:g}% exceeds the standard {cap:g}% cap.", f"{pct:g}% increase")
        return _R("ok", "", f"{pct:g}% increase")
    return _R("ok", "")


def _check_termination(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    minimum = int(_num(extra, "minNoticeDays", 30))
    low = body.lower()
    if _has_any(body, ["for convenience", "without cause", "at any time"]):
        days = parse_notice_days(body)
        if days is not None and days < minimum:
            return _R("moderate", f"Termination-for-convenience notice is {days} days; standard is ≥ {minimum} days.", f"{days} days' notice")
        if days is None:
            return _R("review", f"Termination for convenience present but the notice period could not be parsed; standard is ≥ {minimum} days.")
        return _R("ok", "", f"{days} days' notice")
    if "terminate" in low:
        return _R("ok", "")
    return _R("review", f"No termination terms found; confirm a ≥ {minimum}-day notice / cure window.")


def _check_autorenewal(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    maximum = int(_num(extra, "maxOptOutDays", 30))
    if _has_any(body, ["automatically renew", "auto-renew", "auto renew", "evergreen", "renew for successive"]):
        days = parse_notice_days(body)
        if days is not None and days > maximum:
            return _R("moderate", f"Auto-renewal opt-out window is {days} days; standard is ≤ {maximum} days notice.", f"{days} days' notice")
        if days is None:
            return _R("moderate", f"Auto-renewal present with no clear opt-out window; standard is a ≤ {maximum}-day opt-out.", "auto-renewal, no opt-out window")
        return _R("ok", "", f"{days} days' notice")
    return _R("ok", "")


def _check_confidentiality(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    maximum = int(_num(extra, "maxSurvivalYears", 3))
    if _has_any(body, ["confidential", "non-disclosure", "proprietary information"]):
        # Survival period: flag when well beyond the standard (trade secrets perpetual is fine).
        m = re.search(r"(\d+)\s*years?", body, re.IGNORECASE)
        if m:
            yrs = int(m.group(1))
            if yrs > maximum + 2:
                return _R("moderate", f"Confidentiality survival is {yrs} years; standard is ≤ {maximum} years (trade secrets aside).", f"{yrs} years")
            return _R("ok", "", f"{yrs} years")
        return _R("ok", "")
    return _R("review", "No confidentiality terms found.")


def _check_ip(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    low = body.lower()
    if _has_any(body, ["work made for hire", "work for hire", "assign all right", "assigns all right", "ownership of all"]):
        if not _has_any(body, ["background ip", "pre-existing", "retained", "carve-out", "carve out"]):
            return _R("moderate", "Full IP assignment with no background-IP carve-out; standard retains pre-existing IP.", "full assignment, no carve-out")
        return _R("ok", "", "assignment with background-IP carve-out")
    if "intellectual property" in low or "ip" in low:
        return _R("ok", "")
    return _R("review", "No IP ownership terms found.")


def _check_dataprotection(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    if _has_any(body, ["personal data", "gdpr", "data protection", "data processing", "ccpa"]):
        if not _has_any(body, ["breach notification", "notify", "notification"]):
            return _R("minor", "Data-protection clause lacks an explicit breach-notification obligation.", "no breach-notification duty")
        return _R("ok", "")
    return _R("ok", "")


def _check_breach_notification(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Compliance docs (DPA/BAA): the breach-notification window. GDPR sets 72h."""
    limit = int(_num(extra, "maxHours", 72))
    if not _has_any(body, ["breach", "security incident", "notify", "notification"]):
        return _R("review", f"No breach-notification timeframe found; standard is notice without undue delay and within {limit} hours.")
    hm = _HOURS_RE.search(body)
    if hm:
        hrs = int(hm.group(1))
        found = f"{hrs} hours"
        return _R("ok", "", found) if hrs <= limit else _R(
            "moderate", f"Breach notification window is {hrs} hours; standard is within {limit} hours.", found)
    days = _duration_days(body)
    if days is not None:
        found = f"{days} day(s)"
        return _R("ok", "", found) if days * 24 <= limit else _R(
            "moderate", f"Breach notification window is {days} day(s); standard is within {limit} hours.", found)
    if _has_any(body, ["without undue delay", "promptly", "immediately"]):
        return _R("ok", "", "without undue delay")
    return _R("review", f"Breach-notification clause present but no clear timeframe; standard is within {limit} hours.")


def _check_data_retention(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Compliance docs: data must be deleted or returned on termination."""
    if _has_any(body, ["delete", "deletion", "return or destroy", "destroy", "erasure", "purge"]):
        return _R("ok", "", "deletion / return on termination")
    if _has_any(body, ["retain", "retention", "kept for", "stored for"]):
        return _R("minor", "Retention stated but no deletion/return obligation on termination; standard requires data be deleted or returned.", "retention only")
    return _R("review", "No data-retention or deletion terms found; standard requires deletion or return of data on termination.")


def _check_audit_rights(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing/compliance docs: audits need reasonable advance notice."""
    minimum = int(_num(extra, "minNoticeDays", 10))
    if not _has_any(body, ["audit", "inspect", "examine records", "right to verify"]):
        return _R("ok", "")
    if _has_any(body, ["at any time", "without notice", "unannounced"]):
        return _R("moderate", "Audit rights allow inspection without reasonable notice; standard requires advance written notice.", "no notice required")
    days = parse_notice_days(body)
    if days is None:
        return _R("review", f"Audit rights present but no notice period stated; standard requires reasonable advance notice (≥ {minimum} business days).")
    if days < minimum:
        return _R("minor", f"Audit notice is {days} day(s); standard is ≥ {minimum} business days.", f"{days} days' notice")
    return _R("ok", "", f"{days} days' notice")


def _check_license_grant(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: the grant should be clear, and ideally non-exclusive & non-revocable."""
    if not _has_any(body, ["license", "licence", "grant", "right to use"]):
        return _R("review", "No clear licence grant found; confirm what is licensed and on what basis (term, exclusivity).")
    if _has_any(body, ["exclusive"]) and not _has_any(body, ["non-exclusive", "nonexclusive", "non exclusive"]):
        return _R("moderate", "Licence appears EXCLUSIVE; the standard position is a non-exclusive grant unless exclusivity is intended.", "exclusive")
    if _has_any(body, ["revocable at", "may revoke", "terminate the licen", "at licensor's sole discretion"]):
        return _R("moderate", "Licence may be revocable at the licensor's discretion; standard is a non-revocable grant for the term.", "revocable")
    return _R("ok", "")


def _check_license_scope(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: scope (territory / field of use / users) should be bounded."""
    if _has_any(body, ["territory", "field of use", "named user", "per seat", "per-seat", "environment",
                       "worldwide", "perpetual", "for use in", "internal business", "named"]):
        return _R("ok", "")
    return _R("review", "Licence scope (territory / field of use / users) is not clearly bounded; confirm the limits.")


def _check_restrictions(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: use restrictions are normal; flag only one-sided suspension powers."""
    if _has_any(body, ["sole discretion", "any reason", "without cause"]) and \
       _has_any(body, ["terminate", "suspend", "revoke", "disable"]):
        return _R("moderate", "Restrictions allow suspension/termination at the licensor's sole discretion; standard requires cause and notice.", "suspension at sole discretion")
    return _R("ok", "")


def _check_royalties(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: licence fees / royalties — flag uncapped escalation or retroactive true-ups."""
    cap = _num(extra, "maxAnnualIncreasePct", 5.0)
    m = _INCREASE_RE.search(body)
    if m and float(m.group(1)) > cap:
        return _R("moderate", f"Royalty/fee escalation of {float(m.group(1)):g}% exceeds the standard {cap:g}% annual cap.", f"{float(m.group(1)):g}% increase")
    if _has_any(body, ["true-up", "true up"]) and _has_any(body, ["retroactive", "retrospective", "back-dated", "backdated"]):
        return _R("minor", "Royalty true-up may be applied retroactively; confirm the look-back period and cap.", "retroactive true-up")
    return _R("ok", "")


def _check_sublicensing(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: sublicensing rights should be addressed explicitly."""
    if not _has_any(body, ["sublicense", "sub-license", "sublicence", "sub-licence"]):
        return _R("review", "Sublicensing is not addressed; confirm whether the rights may be passed on, and on what terms.")
    return _R("ok", "")


def _check_open_source(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Licensing: copyleft components without a carve-out are a compliance risk."""
    if _has_any(body, ["gpl", "agpl", "lgpl", "copyleft"]):
        if not _has_any(body, ["carve-out", "carve out", "excluded", "does not include", "no copyleft", "exclud"]):
            return _R("moderate", "Copyleft (GPL/AGPL) components are referenced without a carve-out; review licence-compatibility obligations.", "copyleft, no carve-out")
        return _R("ok", "")
    return _R("ok", "")


def _check_data_residency(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Compliance: where data is stored/processed should be stated."""
    if _has_any(body, ["data center", "data centre", "region", "located in", "stored in", "hosted in",
                       "residency", "within the eea", "within the eu", "united states", "processed in"]):
        return _R("ok", "")
    return _R("review", "No data-residency / location terms found; confirm where data is stored and processed.")


def _check_subprocessors(body: str, extra: dict[str, Any] | None = None) -> tuple[str, str]:
    """Compliance: sub-processors should require notice and a right to object."""
    if not _has_any(body, ["sub-processor", "subprocessor", "sub processor", "sub-processors", "subprocessors"]):
        return _R("review", "Sub-processor terms not found; confirm approval / notice rights for any sub-processors.")
    if _has_any(body, ["prior written consent", "advance notice", "right to object", "list of sub", "notify"]):
        return _R("ok", "")
    return _R("minor", "Sub-processors permitted with no clear notice / objection right; standard requires advance notice and a right to object.", "no notice / objection right")


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
# Rule ids and validation of workspace rules
# ---------------------------------------------------------------------------

_TYPE_RULE_RE = re.compile(r"^type\.[a-z0-9][a-z0-9-]{0,59}$")
_PHRASE_SEVERITIES = ("minor", "moderate", "material")
_MAX_PHRASES = 20


def valid_rule_id(rule_id: str) -> bool:
    """A known clause category ("Payment") or ``type.<specificTypeKey>``."""
    return (rule_id in KNOWN_CATEGORIES and rule_id != "Other") or bool(_TYPE_RULE_RE.fullmatch(rule_id or ""))


def rule_id_for_clause(clause: dict[str, Any]) -> str | None:
    """Which rule grades this clause: its category, or ``type.<key>`` when it is
    an "Other" clause with a specific type. None if the clause has no type."""
    category = clause.get("category")
    if not category or clause.get("classificationStatus") == "unclassified":
        return None
    if category != "Other":
        return category
    key = clause.get("specificTypeKey")
    return f"type.{key}" if key else "Other"


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_phrases(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_PHRASES:
        return None
    out = []
    for p in value:
        if not isinstance(p, str) or not p.strip() or len(p) > 120:
            return None
        out.append(p.strip())
    return list(dict.fromkeys(out))


def validate_rule(rule_id: str, body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a workspace rule sent by a client. Returns (clean rule, None) or
    (None, error message). Unknown fields are rejected, not silently dropped."""
    if not valid_rule_id(rule_id):
        return None, "ruleId must be a known clause category or type.<clause-type-key>"
    if not isinstance(body, dict):
        return None, "Body must be a JSON object"
    allowed = {"label", "standard", "rationale", "fallback", "thresholds", "requiredPhrases",
               "forbiddenPhrases", "severity"}
    unknown = set(body) - allowed
    if unknown:
        return None, f"Unknown field(s): {', '.join(sorted(unknown))}"
    default = _DEFAULT_POSITIONS.get(rule_id)
    standard = _clean_text(body.get("standard"), 600)
    if not standard and default is None:
        return None, "standard (the position text) is required for a new rule"
    required = _clean_phrases(body.get("requiredPhrases"))
    forbidden = _clean_phrases(body.get("forbiddenPhrases"))
    if required is None or forbidden is None:
        return None, f"requiredPhrases / forbiddenPhrases must be lists of up to {_MAX_PHRASES} short strings"
    severity = str(body.get("severity") or "moderate").lower()
    if severity not in _PHRASE_SEVERITIES:
        return None, "severity must be minor, moderate or material"
    thresholds = body.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return None, "thresholds must be an object"
    known_thresholds = default.extra if default else {}
    clean_thresholds: dict[str, float] = {}
    for key, value in thresholds.items():
        if key not in known_thresholds:
            return None, (f"Unknown threshold '{key}'. This rule accepts: "
                          f"{', '.join(sorted(known_thresholds)) or 'none'}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100_000:
            return None, f"Threshold '{key}' must be a number between 0 and 100000"
        clean_thresholds[key] = float(value)
    rule = {
        "label": _clean_text(body.get("label"), 120),
        "standard": standard,
        "rationale": _clean_text(body.get("rationale"), 600),
        "fallback": _clean_text(body.get("fallback"), 600),
        "extra": clean_thresholds,
        "requiredPhrases": required,
        "forbiddenPhrases": forbidden,
        "severity": severity,
    }
    return {k: v for k, v in rule.items() if v not in (None, [], {})} | {"severity": severity}, None


# ---------------------------------------------------------------------------
# Position resolution (defaults + overrides)
# ---------------------------------------------------------------------------


def _default_label(rule_id: str) -> str:
    if rule_id.startswith("type."):
        return rule_id.split(".", 1)[1].replace("-", " ").capitalize()
    return known_label(rule_id)


def _apply_overrides(base: dict[str, StandardPosition], overrides: dict[str, Any],
                     source: str = "deployment") -> dict[str, StandardPosition]:
    """Overlay a JSON override map onto the positions.

    An override replaces the texts (``label`` / ``standard`` / ``rationale`` /
    ``fallback``), the numeric thresholds in ``extra`` and the phrase lists. The
    deterministic check function is kept from the default for that clause type;
    a rule for a type with no built-in check has none (it is graded by its
    phrase lists, or flagged for manual review if it has none).
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
                category=cat, label=_default_label(cat),
                standard="", rationale="", check=None, extra={},
            )
        merged_extra = {**cur.extra, **(ov.get("extra") or {})}
        out[cat] = StandardPosition(
            category=cat,
            label=ov.get("label") or cur.label,
            standard=ov.get("standard") or cur.standard,
            rationale=ov.get("rationale") or cur.rationale,
            check=cur.check,
            extra=merged_extra,
            fallback=ov.get("fallback") or cur.fallback,
            required_phrases=list(ov.get("requiredPhrases") or cur.required_phrases),
            forbidden_phrases=list(ov.get("forbiddenPhrases") or cur.forbidden_phrases),
            phrase_severity=ov.get("severity") if ov.get("severity") in _PHRASE_SEVERITIES else cur.phrase_severity,
            source=source,
        )
    return out


def _load_tenant_overrides(tenant_id: str | None) -> dict[str, Any]:
    """Per-workspace rules from DynamoDB (best-effort; never raises)."""
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
            return _plain(item["positions"])
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("playbook.tenant_override_load_failed", error_type=type(exc).__name__)
    return {}


def _plain(value: Any) -> Any:
    """DynamoDB numbers (Decimal) → float, recursively."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def save_tenant_rule(tenant_id: str, rule_id: str, rule: dict[str, Any] | None) -> dict[str, Any]:
    """Create / replace (``rule``) or delete (``None``) one workspace rule.
    Returns the workspace's rule map after the change."""
    from decimal import Decimal

    from .aws import dynamodb_resource
    from .config import settings as _settings
    from .schema import now_iso

    if not _settings.table_name:
        raise RuntimeError("TABLE_NAME env var is not set")
    tbl = dynamodb_resource().Table(_settings.table_name)
    key = {"PK": f"TENANT#{tenant_id}", "SK": "PLAYBOOK"}
    item = tbl.get_item(Key=key).get("Item") or {}
    positions = _plain(item.get("positions")) if isinstance(item.get("positions"), dict) else {}
    if rule is None:
        positions.pop(rule_id, None)
    else:
        positions[rule_id] = rule

    def to_ddb(value: Any) -> Any:
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {k: to_ddb(v) for k, v in value.items()}
        if isinstance(value, list):
            return [to_ddb(v) for v in value]
        return value

    tbl.put_item(Item={**key, "entityType": "PlaybookConfig", "positions": to_ddb(positions),
                       "updatedAt": now_iso()})
    return positions


def resolve_positions(tenant_id: str | None = None) -> dict[str, StandardPosition]:
    """Return the effective standard positions (defaults ← env ← workspace)."""
    positions = dict(_DEFAULT_POSITIONS)

    env_raw = os.environ.get("PLAYBOOK_JSON", "").strip()
    if env_raw:
        try:
            import orjson
            positions = _apply_overrides(positions, orjson.loads(env_raw), "deployment")
        except Exception as exc:
            log.warning("playbook.env_override_invalid", error_type=type(exc).__name__)

    tenant_ov = _load_tenant_overrides(tenant_id)
    if tenant_ov:
        positions = _apply_overrides(positions, tenant_ov, "custom")

    return positions


def describe_positions(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """The effective playbook as data for the API / UI: one entry per rule."""
    out = []
    for rule_id, pos in sorted(resolve_positions(tenant_id).items()):
        out.append({
            "ruleId": rule_id,
            "clauseType": rule_id if "." not in rule_id else rule_id.split(".", 1)[1],
            "isCustomType": "." in rule_id,
            "label": pos.label,
            "standard": pos.standard,
            "rationale": pos.rationale or None,
            "fallback": pos.fallback,
            "thresholds": dict(pos.extra),
            "requiredPhrases": list(pos.required_phrases),
            "forbiddenPhrases": list(pos.forbidden_phrases),
            "phraseSeverity": pos.phrase_severity,
            # default = built-in; deployment = PLAYBOOK_JSON; custom = this workspace's rule
            "source": pos.source,
            "hasAutomaticCheck": pos.check is not None or bool(pos.required_phrases or pos.forbidden_phrases),
            "hasBuiltInDefault": rule_id in _DEFAULT_POSITIONS,
        })
    return out


# ---------------------------------------------------------------------------
# Deviation evaluation (the stage)
# ---------------------------------------------------------------------------


def _worse(a: str, b: str) -> str:
    """The more serious of two check statuses (review ranks between ok and minor)."""
    rank = {"ok": 0, "review": 1, "minor": 2, "moderate": 3, "material": 4}
    return a if rank.get(a, 0) >= rank.get(b, 0) else b


def _grade(pos: StandardPosition, body: str) -> CheckResult:
    """Run a rule against a clause: the built-in check (with the rule's
    thresholds), then the rule's phrase lists. The more serious result wins."""
    status, reason, found = "ok", "", None
    checked = False
    if pos.check is not None:
        checked = True
        try:
            result = pos.check(body, pos.extra)
        except TypeError:
            result = pos.check(body)                # a check that takes no thresholds
        status, reason = result[0], result[1]
        found = getattr(result, "found", None)
    low = (body or "").lower()
    if pos.forbidden_phrases or pos.required_phrases:
        checked = True
        hit = next((p for p in pos.forbidden_phrases if p.lower() in low), None)
        missing = [p for p in pos.required_phrases if p.lower() not in low]
        if hit:
            new = pos.phrase_severity
            if _worse(new, status) == new and new != status:
                status, reason, found = new, f"Contains \"{hit}\", which this rule does not allow.", f"\"{hit}\""
        elif missing:
            new = pos.phrase_severity
            if _worse(new, status) == new and new != status:
                status, reason = new, "Does not contain the required wording: " + ", ".join(f"\"{m}\"" for m in missing) + "."
    if not checked:
        return CheckResult("review", "No automatic check is defined for this rule — compare the clause "
                                     "with the standard position.")
    return CheckResult(status, reason, found)


def evaluate_clauses(clauses: list[dict[str, Any]], tenant_id: str | None = None) -> dict[str, Any]:
    """Compare each extracted clause against the effective playbook.

    Returns a structured, deterministic ``playbook`` block:
      {
        "checked": <int>,           # clauses evaluated against a rule
        "deviationCount": <int>,    # clauses that deviate
        "reviewCount": <int>,       # clauses flagged for a human (rule could not decide)
        "withinCount": <int>,       # clauses inside the standard position
        "noRuleCount": <int>,       # clauses whose type has NO rule — not checked
        "unclassifiedCount": <int>, # clauses with no type at all
        "overallSeverity": "none|minor|moderate|material",
        "deviations": [ {clauseNumber, clauseId, category, title, standard,
                         rationale, severity, deviation, status, sourceQuote}, ... ],
        "clauseResults": [ {clauseId, clauseNumber, category, specificType,
                            specificTypeKey, ruleId, ruleName, standard, fallback,
                            found, outcome, status, severity, reason}, ... ],
        "coverage": [<ruleId>, ...] # every rule in the effective playbook
      }

    EVERY clause appears once in ``clauseResults``. ``outcome`` is one of
    "within", "deviates", "flagged", "no_rule", "unclassified" (see the module
    docstring); ``status`` is the underlying check status ("ok", "minor",
    "moderate", "material", "review", "no_rule", "unclassified").
    """
    positions = resolve_positions(tenant_id)
    deviations: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    checked = review = within = no_rule = unclassified = 0
    worst = "none"

    for clause in clauses or []:
        category = clause.get("category")
        row: dict[str, Any] = {
            "clauseId": clause.get("id"),
            "clauseNumber": clause.get("number", ""),
            "category": category,
            "specificType": clause.get("specificType"),
            "specificTypeKey": clause.get("specificTypeKey"),
            "ruleId": None, "ruleName": None, "standard": None, "fallback": None,
            "found": None, "severity": None, "reason": None,
        }
        rule_id = rule_id_for_clause(clause)
        if rule_id is None:
            unclassified += 1
            results.append({**row, "outcome": "unclassified", "status": "unclassified",
                            "reason": "This clause could not be typed, so no rule was applied."})
            continue
        pos = positions.get(rule_id)
        if pos is None:
            no_rule += 1
            results.append({**row, "outcome": "no_rule", "status": "no_rule",
                            "reason": "The playbook has no position for this clause type; nothing was checked."})
            continue
        checked += 1
        body = clause.get("body") or ""
        try:
            graded = _grade(pos, body)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("playbook.check_failed", category=category, error_type=type(exc).__name__)
            graded = CheckResult("review", "Could not evaluate this clause automatically.")
        status, deviation_text = graded[0], graded[1]

        row.update(ruleId=rule_id, ruleName=pos.label, standard=pos.standard or None,
                   fallback=pos.fallback, found=graded.found)
        if status == "ok":
            within += 1
            results.append({**row, "outcome": "within", "status": "ok"})
            continue

        if status == "review":
            review += 1
            severity = "minor"
        else:
            severity = status
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(worst, 0):
                worst = severity
        results.append({**row, "outcome": _OUTCOME[status], "status": status,
                        "severity": severity if status != "review" else None, "reason": deviation_text})

        deviations.append({
            "clauseNumber": clause.get("number", ""),
            "clauseId": clause.get("id"),
            "category": category,
            "ruleId": rule_id,
            "title": clause.get("title", "") or pos.label,
            "standard": pos.standard,
            "fallback": pos.fallback,
            "rationale": pos.rationale,
            "severity": severity,
            "deviation": deviation_text,
            "found": graded.found,
            "status": status,           # "minor" | "moderate" | "material" | "review"
            "sourceQuote": body[:600],  # verbatim snippet so the UI can show provenance
        })

    deviation_count = sum(1 for d in deviations if d["status"] != "review")

    return {
        "checked": checked,
        "deviationCount": deviation_count,
        "reviewCount": review,
        "withinCount": within,
        "noRuleCount": no_rule,
        "unclassifiedCount": unclassified,
        "overallSeverity": worst,
        "deviations": deviations,
        "clauseResults": results,
        "coverage": sorted(positions.keys()),
        # Which playbook graded this: any workspace rule present, else the defaults.
        "source": "custom" if any(p.source == "custom" for p in positions.values()) else (
            "deployment" if any(p.source == "deployment" for p in positions.values()) else "default"),
    }
