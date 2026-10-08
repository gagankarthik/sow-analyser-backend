"""Govern workflow — THE single place a contract changes state.

Used by govern-api (a reviewer's click), govern-intake (a document finished
analysis), webhooks (DocuSign says "signed") and the sweeper (SLA overdue), so
every path validates and records a change in exactly the same way.

What lives here
---------------
* the state machine: ``State`` → ``Stage`` mapping, which action is allowed
  from which state (anything else raises ``InvalidTransition`` → HTTP 409);
* every action of ``POST /contracts/{id}/actions`` (docs/GOVERN_API.md);
* routing (which internal offices must approve), auto-assignment, "waiting on" in
  plain words, SLA colours, value fields, fiscal year and the ONE
  recommended next step;
* matrix (re)scoring of a contract's current document, Sonar blockers,
  licensing income and obligations;
* ``to_api`` / ``detail`` — the Contract and ContractDetail JSON.

Every change appends an activity entry with a plain-language ``summary``
("Dana Ruiz sent this back to the sponsor with 3 clauses to change."). The
activity log's stream is the only source of domain events (notifications,
Huron push-back), so a change that is not logged did not happen.

Read-time values (days in stage, SLA colour, next step, waiting on) are never
stored: they are computed from the item on every read, so they are always
current.
"""
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import settings as app_settings
from ..logger import get_logger
from . import store
from .store import iso, parse_iso

log = get_logger("blue-iq.govern.workflow")

# ---------------------------------------------------------------------------
# Enumerations (GOVERN_API.md)
# ---------------------------------------------------------------------------

STAGES = ("draft", "review", "negotiation", "approval", "signed", "active", "renewal", "expired")
STATES = ("intake", "in_review", "sent_back", "escalated", "ready_to_sign",
          "out_for_signature", "signed", "active", "rejected", "closed")
DIRECTIONS = ("incoming", "outgoing")
REJECT_REASONS = ("unacceptable_terms", "sponsor_withdrew", "pi_withdrew", "duplicate", "out_of_scope", "other")
OBLIGATION_KINDS = ("sponsor_report", "milestone_payment", "royalty_report", "diligence_milestone",
                    "publication_review", "term_end", "closeout", "other")
INCOME_KINDS = ("upfront", "milestone", "royalty", "equity", "sublicense", "sponsor_funding", "subaward", "other")
TIERS = ("within", "fallback", "deviates", "unacceptable", "review", "missing")
RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
WORKDAY_MATCH = ("auto", "manual", "unmatched")

_STATE_STAGE = {
    "intake": "review", "in_review": "review", "escalated": "review",
    "sent_back": "negotiation",
    "ready_to_sign": "approval", "out_for_signature": "approval",
    "signed": "signed", "active": "active", "closed": "expired",
}
OPEN_STATES = frozenset({"intake", "in_review", "sent_back", "escalated", "ready_to_sign", "out_for_signature"})
TERMINAL_STATES = frozenset({"rejected", "closed"})
_CURRENT_STAGES = frozenset({"signed", "active", "renewal"})
_POTENTIAL_STAGES = frozenset({"draft", "review", "negotiation", "approval"})

# Which states each action may start from.
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    "assign": frozenset(OPEN_STATES | {"signed", "active"}),
    "approve": frozenset({"intake", "in_review", "sent_back", "escalated"}),
    "office_approve": frozenset({"intake", "in_review", "sent_back", "escalated"}),
    "send_back": frozenset({"intake", "in_review", "escalated", "ready_to_sign"}),
    "escalate": frozenset({"intake", "in_review", "sent_back", "escalated", "ready_to_sign"}),
    "reject": frozenset(OPEN_STATES),
    "send_for_signature": frozenset({"ready_to_sign"}),
    "mark_signed": frozenset({"ready_to_sign", "out_for_signature"}),
    "activate": frozenset({"signed"}),
    "close": frozenset({"signed", "active"}),
    "reopen": frozenset({"rejected", "closed", "sent_back", "escalated", "ready_to_sign", "out_for_signature"}),
    # Requirement 3.1: the PI or department owes an answer (a budget, a
    # signature on a form, a decision on terms). The state does not change;
    # ``piRequest`` on the contract makes "waiting on" say who holds it.
    "ask_pi": frozenset({"intake", "in_review", "escalated"}),
    "pi_answered": frozenset({"intake", "in_review", "escalated"}),
    "comment": frozenset(STATES),
}
ACTIONS = tuple(_ALLOWED_FROM)
_CLEARS_PI_REQUEST = frozenset({"approve", "office_approve", "send_back", "escalate", "reject",
                                "send_for_signature", "reopen"})

_STATE_WORDS = {
    "intake": "just arrived", "in_review": "in review", "sent_back": "sent back", "escalated": "escalated",
    "ready_to_sign": "ready to sign", "out_for_signature": "out for signature", "signed": "signed",
    "active": "active", "rejected": "rejected", "closed": "closed",
}

# Fields a person may set (PATCH /contracts/{id}, POST /contracts). A field set
# by a person is listed in ``userFields`` and never overwritten by inference.
PATCH_FIELDS = ("agreementType", "direction", "counterparty", "sponsor", "piName", "department", "college",
                "expectedValue", "manualValue", "currency", "requestedDate", "effectiveDate", "termEndDate",
                "huronRecordId", "workdayRef", "workdayMatch")
_DATE_FIELDS = ("requestedDate", "effectiveDate", "termEndDate")
_TEXT_FIELDS = ("counterparty", "sponsor", "piName", "department", "college")
_FIELD_WORDS = {
    "agreementType": "agreement type", "direction": "direction of money", "counterparty": "counterparty",
    "sponsor": "sponsor", "piName": "PI", "department": "department", "college": "college",
    "expectedValue": "expected value", "manualValue": "contract value", "currency": "currency",
    "requestedDate": "requested date", "effectiveDate": "effective date", "termEndDate": "term end date",
    "huronRecordId": "Huron record ID", "workdayRef": "Workday reference",
    "workdayMatch": "Workday match",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GovernError(Exception):
    status = 400
    code = "bad_request"


class BadRequest(GovernError):
    pass


class NotFound(GovernError):
    status = 404
    code = "not_found"


class InvalidTransition(GovernError):
    """The action is not allowed from the contract's current state (HTTP 409)."""
    status = 409
    code = "invalid_transition"


class NotReady(GovernError):
    status = 409
    code = "not_ready"


class FeatureDisabled(GovernError):
    """A "Later" feature (GOVERN_FEATURES) is switched off on this deployment (HTTP 409)."""
    status = 409
    code = "feature_disabled"


def require_feature(feature: str, what: str) -> None:
    """Raise FeatureDisabled unless ``feature`` is on; ``what`` names it in plain words."""
    if not app_settings.feature_enabled(feature):
        raise FeatureDisabled(f"{what} is not available yet on this deployment.")


# ---------------------------------------------------------------------------
# Matrix module (written alongside; imported lazily so this module loads even
# in contexts that never grade anything)
# ---------------------------------------------------------------------------


def _m() -> Any:
    from . import matrix
    return matrix


def office_label(office: str | None) -> str:
    if not office:
        return "an internal office"
    try:
        return _m().OFFICE_LABELS.get(office) or office.replace("_", " ").title()
    except Exception:  # noqa: BLE001
        return office.replace("_", " ").title()


def agreement_label(agreement_type: str | None) -> str:
    try:
        return _m().AGREEMENT_TYPE_LABELS.get(agreement_type or "other") or "agreement"
    except Exception:  # noqa: BLE001
        return (agreement_type or "agreement").replace("_", " ")


def offices() -> tuple[str, ...]:
    return tuple(_m().OFFICES)


def agreement_types() -> tuple[str, ...]:
    return tuple(_m().AGREEMENT_TYPES)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def person(email: Any, name: Any = None) -> dict[str, Any] | None:
    email = str(email or "").strip().lower()
    if not email:
        return None
    name = str(name).strip()[:200] if name else None
    return {"email": email[:200], "name": name or None}


def display(p: dict[str, Any] | None, system: str = "Sonar") -> str:
    """How a person is named in a sentence."""
    if not p:
        return system
    return p.get("name") or p.get("email") or system


def parse_person(value: Any, field: str = "person") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BadRequest(f"{field} must be an object {{email, name}}")
    email = str(value.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 200:
        raise BadRequest(f"{field}.email must be a valid email address")
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        raise BadRequest(f"{field}.name must be a string")
    return {"email": email, "name": (name or "").strip()[:200] or None}


def _note(body: dict[str, Any]) -> str | None:
    note = body.get("note")
    if note is None:
        return None
    if not isinstance(note, str):
        raise BadRequest("note must be a string")
    return note.strip()[:2000] or None


def stage_for(state: str, current_stage: str | None, analysis_ready: bool = True) -> str:
    """Stage shown on the board for a state. ``rejected`` keeps the stage it
    was rejected in; a contract still being read is ``draft``."""
    if state == "rejected":
        return current_stage or "review"
    if state == "intake" and not analysis_ready:
        return "draft"
    return _STATE_STAGE.get(state, current_stage or "review")


def money(amount: Any, currency: str | None = None) -> str:
    if amount is None:
        return "an unknown amount"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "USD").upper(), "")
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def counterparty_noun(c: dict[str, Any]) -> str:
    """Plain word for the other side: sponsor, licensee, or other party."""
    t = c.get("agreementType")
    if t in ("sponsored_research", "grant"):
        return "sponsor"
    if t in ("license", "option"):
        return "licensee"
    if t == "collaboration":
        return "collaborator"
    return "other party"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def fiscal_year(value: Any) -> int | None:
    """fiscal year: FY2027 runs 1 July 2026 – 30 June 2027."""
    dt = parse_iso(value) if not isinstance(value, datetime) else value
    if dt is None:
        return None
    return dt.year + 1 if dt.month >= 7 else dt.year


def _whole_days(start: Any, end: datetime) -> int:
    dt = parse_iso(start)
    if dt is None:
        return 0
    return max(0, math.floor((end - dt).total_seconds() / 86400))


# ---------------------------------------------------------------------------
# Workflow settings
# ---------------------------------------------------------------------------


# Organisation setup steps an admin confirms by hand (the others complete
# themselves from the data: a name, a home state, a reviewer, a contract).
SETUP_STEPS = ("matrix", "workflow")


def default_settings() -> dict[str, Any]:
    return {
        "stageTargetDays": {"draft": 2, "review": 5, "negotiation": 10, "approval": 3,
                            "signed": None, "active": None, "renewal": None, "expired": None},
        "redAfterMultiple": 2,
        "reviewers": [],
        "assignmentRules": [],
        "routingRules": [
            {"id": "default-legal", "name": "Unacceptable terms or $500,000 and more go to Legal Affairs",
             "enabled": True, "when": {"anyUnacceptable": True, "minValue": 500000}, "route": ["legal_affairs"]},
        ],
        "notifications": {"email": True, "teams": False,
                          "events": {"assigned": True, "sent_back": True, "approved": True,
                                     "overdue": True, "escalated": True}},
        "teamsWebhookConfigured": False,
        # The organisation, set during organisation setup (Settings → Overview).
        "organization": {"name": None, "defaultCurrency": "USD", "fiscalYearStartMonth": 1,
                         "confirmedSteps": [], "setupCompletedAt": None},
    }


def get_settings(tenant_id: str) -> dict[str, Any]:
    """Stored settings over the defaults (a missing key keeps its default)."""
    out = default_settings()
    stored = store.config.settings(tenant_id) or {}
    for key, value in stored.items():
        if key == "stageTargetDays" and isinstance(value, dict):
            out["stageTargetDays"].update({k: v for k, v in value.items() if k in STAGES})
        elif key == "organization" and isinstance(value, dict):
            out["organization"].update({k: v for k, v in value.items() if k in out["organization"]})
        elif key == "notifications" and isinstance(value, dict):
            out["notifications"].update({k: v for k, v in value.items() if k != "events"})
            if isinstance(value.get("events"), dict):
                out["notifications"]["events"].update(value["events"])
        elif key in out:
            out[key] = value
    return out


def validate_settings(body: Any, current: dict[str, Any]) -> dict[str, Any]:
    """A PUT body (whole or partial) → the complete, clean settings. Raises
    BadRequest. ``teamsWebhookUrl`` is handled by the caller (it is a secret)."""
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    known = set(default_settings()) | {"teamsWebhookUrl"}
    unknown = set(body) - known
    if unknown:
        raise BadRequest(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    out = {k: v for k, v in current.items()}
    office_set, type_set = set(offices()), set(agreement_types())

    if "stageTargetDays" in body:
        std = body["stageTargetDays"]
        if not isinstance(std, dict) or set(std) - set(STAGES):
            raise BadRequest("stageTargetDays must map stages to a number of days or null")
        merged = dict(current.get("stageTargetDays") or {})
        for k, v in std.items():
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 3650):
                raise BadRequest(f"stageTargetDays.{k} must be a number of days (0–3650) or null")
            merged[k] = v
        out["stageTargetDays"] = merged
    if "redAfterMultiple" in body:
        v = body["redAfterMultiple"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 20:
            raise BadRequest("redAfterMultiple must be a number between 1 and 20")
        out["redAfterMultiple"] = v
    if "reviewers" in body:
        revs = body["reviewers"]
        if not isinstance(revs, list) or len(revs) > 500:
            raise BadRequest("reviewers must be a list (max 500)")
        clean = []
        for i, r in enumerate(revs):
            p = parse_person(r, f"reviewers[{i}]")
            offs = r.get("offices") or []
            types = r.get("agreementTypes") or []
            if not isinstance(offs, list) or set(offs) - office_set:
                raise BadRequest(f"reviewers[{i}].offices has an unknown office")
            if not isinstance(types, list) or set(types) - type_set:
                raise BadRequest(f"reviewers[{i}].agreementTypes has an unknown agreement type")
            clean.append({**p, "offices": list(dict.fromkeys(offs)), "agreementTypes": list(dict.fromkeys(types))})
        out["reviewers"] = clean
    if "assignmentRules" in body:
        rules = body["assignmentRules"]
        if not isinstance(rules, list) or len(rules) > 500:
            raise BadRequest("assignmentRules must be a list (max 500)")
        clean = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                raise BadRequest(f"assignmentRules[{i}] must be an object")
            at = r.get("agreementType") or "*"
            if at != "*" and at not in type_set:
                raise BadRequest(f"assignmentRules[{i}].agreementType is unknown")
            dept = str(r.get("department") or "*").strip()[:200] or "*"
            clean.append({"id": str(r.get("id") or uuid.uuid4().hex[:12])[:64], "agreementType": at,
                          "department": dept, "reviewer": parse_person(r.get("reviewer"), f"assignmentRules[{i}].reviewer")})
        out["assignmentRules"] = clean
    if "routingRules" in body:
        rules = body["routingRules"]
        if not isinstance(rules, list) or len(rules) > 200:
            raise BadRequest("routingRules must be a list (max 200)")
        clean = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                raise BadRequest(f"routingRules[{i}] must be an object")
            when = r.get("when") or {}
            if not isinstance(when, dict) or set(when) - {"agreementTypes", "minValue", "anyUnacceptable", "minRisk", "direction"}:
                raise BadRequest(f"routingRules[{i}].when has an unknown condition")
            w: dict[str, Any] = {}
            if when.get("agreementTypes") is not None:
                if not isinstance(when["agreementTypes"], list) or set(when["agreementTypes"]) - type_set:
                    raise BadRequest(f"routingRules[{i}].when.agreementTypes has an unknown type")
                w["agreementTypes"] = list(when["agreementTypes"])
            if when.get("minValue") is not None:
                mv = when["minValue"]
                if isinstance(mv, bool) or not isinstance(mv, (int, float)) or mv < 0:
                    raise BadRequest(f"routingRules[{i}].when.minValue must be a non-negative number")
                w["minValue"] = mv
            if when.get("anyUnacceptable") is not None:
                w["anyUnacceptable"] = bool(when["anyUnacceptable"])
            if when.get("minRisk") is not None:
                if when["minRisk"] not in ("high", "critical"):
                    raise BadRequest(f"routingRules[{i}].when.minRisk must be high or critical")
                w["minRisk"] = when["minRisk"]
            if when.get("direction") is not None:
                if when["direction"] not in DIRECTIONS:
                    raise BadRequest(f"routingRules[{i}].when.direction must be incoming or outgoing")
                w["direction"] = when["direction"]
            route = r.get("route") or []
            if not isinstance(route, list) or not route or set(route) - office_set:
                raise BadRequest(f"routingRules[{i}].route must list at least one known office")
            clean.append({"id": str(r.get("id") or uuid.uuid4().hex[:12])[:64],
                          "name": str(r.get("name") or "Routing rule").strip()[:200],
                          "enabled": bool(r.get("enabled", True)), "when": w, "route": list(dict.fromkeys(route))})
        out["routingRules"] = clean
    if "notifications" in body:
        n = body["notifications"]
        if not isinstance(n, dict):
            raise BadRequest("notifications must be an object")
        merged = {**(current.get("notifications") or {})}
        merged["events"] = dict(merged.get("events") or {})
        for key in ("email", "teams"):
            if key in n:
                merged[key] = bool(n[key])
        if "events" in n:
            if not isinstance(n["events"], dict) or set(n["events"]) - {"assigned", "sent_back", "approved", "overdue", "escalated"}:
                raise BadRequest("notifications.events has an unknown event")
            merged["events"].update({k: bool(v) for k, v in n["events"].items()})
        out["notifications"] = merged
    if "organization" in body:
        org = body["organization"]
        if not isinstance(org, dict):
            raise BadRequest("organization must be an object")
        merged = dict((current.get("organization") or default_settings()["organization"]))
        if "name" in org:
            name = org["name"]
            if name is not None and (not isinstance(name, str) or not name.strip() or len(name.strip()) > 120):
                raise BadRequest("organization.name must be 1–120 characters or null")
            merged["name"] = name.strip() if isinstance(name, str) else None
        if "defaultCurrency" in org:
            cur = org["defaultCurrency"]
            if not isinstance(cur, str) or not re.fullmatch(r"[A-Za-z]{3}", cur):
                raise BadRequest("organization.defaultCurrency must be a 3-letter currency code")
            merged["defaultCurrency"] = cur.upper()
        if "fiscalYearStartMonth" in org:
            m = org["fiscalYearStartMonth"]
            if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12:
                raise BadRequest("organization.fiscalYearStartMonth must be a month number, 1–12")
            merged["fiscalYearStartMonth"] = m
        if "confirmedSteps" in org:
            steps = org["confirmedSteps"]
            if not isinstance(steps, list) or any(s_ not in SETUP_STEPS for s_ in steps):
                raise BadRequest(f"organization.confirmedSteps may only name: {', '.join(SETUP_STEPS)}")
            merged["confirmedSteps"] = [s_ for s_ in SETUP_STEPS if s_ in steps]
        if "setupCompletedAt" in org:
            v = org["setupCompletedAt"]
            if v is not None and (not isinstance(v, str) or parse_iso(v) is None):
                raise BadRequest("organization.setupCompletedAt must be a timestamp or null")
            merged["setupCompletedAt"] = v
        out["organization"] = merged
    return out


# ---------------------------------------------------------------------------
# Routing and assignment
# ---------------------------------------------------------------------------


def contract_value(c: dict[str, Any]) -> tuple[float | None, str | None]:
    for field, source in (("manualValue", "manual"), ("extractedValue", "extracted"), ("expectedValue", "expected")):
        v = c.get(field)
        if v is not None:
            try:
                return float(v), source
            except (TypeError, ValueError):
                continue
    return None, None


def evaluate_routing(c: dict[str, Any], cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Offices the routing rules require for this contract, and why (plain words).

    ``agreementTypes`` and ``direction`` NARROW which contracts a rule looks
    at (all must hold); ``minValue``, ``anyUnacceptable`` and ``minRisk`` are
    TRIGGERS (any one fires the rule). A rule without triggers fires for every
    contract it narrows to.

    Routing rules are a "Later" feature (GOVERN_FEATURES=routing_rules): while
    off, no office is required by a rule, so approval goes straight to Ready
    to sign. Offices a reviewer escalated to by hand still apply.
    """
    if not app_settings.feature_enabled("routing_rules"):
        return [], []
    value, _ = contract_value(c)
    unacceptable = int(((c.get("matrix") or {}).get("counts") or {}).get("unacceptable") or 0)
    required: list[str] = []
    reasons: list[str] = []
    for rule in cfg.get("routingRules") or []:
        if not rule.get("enabled", True):
            continue
        when = rule.get("when") or {}
        if when.get("agreementTypes") and c.get("agreementType") not in when["agreementTypes"]:
            continue
        if when.get("direction") and c.get("direction") != when["direction"]:
            continue
        triggers: list[str] = []
        fired: list[str] = []
        if when.get("minValue") is not None:
            triggers.append("minValue")
            if value is not None and value >= float(when["minValue"]):
                fired.append(f"its value is {money(value, c.get('currency'))}, at or above "
                             f"{money(when['minValue'], c.get('currency'))}")
        if when.get("anyUnacceptable"):
            triggers.append("anyUnacceptable")
            if unacceptable > 0:
                fired.append(f"it has {_plural(unacceptable, 'unacceptable term')}")
        if when.get("minRisk"):
            triggers.append("minRisk")
            if RISK_RANK.get(str(c.get("overallRisk") or ""), 0) >= RISK_RANK[when["minRisk"]]:
                fired.append(f"its risk is {c.get('overallRisk')}")
        if triggers and not fired:
            continue
        if not fired:
            scope = [agreement_label(c.get("agreementType")).lower() + "s"] if when.get("agreementTypes") else []
            if when.get("direction"):
                scope.append(f"{when['direction']} contracts")
            fired.append(f"the rule “{rule.get('name') or 'routing rule'}” covers "
                         + (" that are ".join(scope) if scope else "every contract"))
        route = [o for o in rule.get("route") or []]
        for office in route:
            if office not in required:
                required.append(office)
        reasons.append(f"{', '.join(office_label(o) for o in route)} must approve because {' and '.join(fired)}.")
    return required, reasons


def apply_routing(c: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Recompute ``routing.required`` = rule offices ∪ offices escalated to by hand."""
    routing = c.setdefault("routing", {"required": [], "approvals": [], "reasons": [], "escalated": []})
    rule_offices, reasons = evaluate_routing(c, cfg)
    escalated = list(routing.get("escalated") or [])
    routing["required"] = list(dict.fromkeys(rule_offices + escalated))
    since = dict(routing.get("requiredAt") or {})
    for office in routing["required"]:
        since.setdefault(office, c.get("updatedAt") or iso())
    routing["requiredAt"] = {o: t for o, t in since.items() if o in routing["required"]}
    for office in escalated:
        if office not in rule_offices:
            reasons.append(f"{office_label(office)} must approve because a reviewer escalated it.")
    routing["reasons"] = reasons
    routing.setdefault("approvals", [])


def approved_offices(c: dict[str, Any]) -> set[str]:
    return {a.get("office") for a in ((c.get("routing") or {}).get("approvals") or [])}


def pending_offices(c: dict[str, Any]) -> list[str]:
    done = approved_offices(c)
    return [o for o in ((c.get("routing") or {}).get("required") or []) if o not in done]


def auto_assignee(c: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Reviewer for a contract by agreementType + department. Most specific
    rule wins (type+department, then department, then type, then catch-all);
    then a reviewer whose agreement types include this one. (person, why)."""
    at = c.get("agreementType") or "other"
    dept = str(c.get("department") or "").strip().lower()
    best: tuple[int, dict[str, Any]] | None = None
    for rule in cfg.get("assignmentRules") or []:
        r_at, r_dept = rule.get("agreementType") or "*", str(rule.get("department") or "*").strip().lower()
        if r_at not in ("*", at) or (r_dept != "*" and r_dept != dept):
            continue
        score = (2 if r_dept != "*" else 0) + (1 if r_at != "*" else 0)
        if best is None or score > best[0]:
            best = (score, rule)
    if best:
        rev = best[1].get("reviewer") or {}
        p = person(rev.get("email"), rev.get("name"))
        if p:
            return p, "assignment rule"
    for rev in cfg.get("reviewers") or []:
        if at in (rev.get("agreementTypes") or []):
            p = person(rev.get("email"), rev.get("name"))
            if p:
                return p, f"reviews {agreement_label(at).lower()}s"
    return None, None


# ---------------------------------------------------------------------------
# Read-time views
# ---------------------------------------------------------------------------


def waiting_on(c: dict[str, Any]) -> dict[str, Any]:
    state = c.get("state")
    owner = c.get("owner")
    pi_request = c.get("piRequest")
    if pi_request and state in _ALLOWED_FROM["pi_answered"]:
        who = c.get("piName") or c.get("department")
        return {"kind": "pi_department", "label": "Waiting on PI or department" + (f" ({who})" if who else ""),
                "office": None, "person": None}
    if state in ("intake", "in_review"):
        if state == "intake" and c.get("stage") == "draft":
            return {"kind": "nobody", "label": "Sonar is still reading this agreement", "office": None, "person": None}
        if owner:
            return {"kind": "internal_reviewer", "label": f"Waiting on reviewer ({display(owner)})",
                    "office": None, "person": owner}
        return {"kind": "internal_reviewer", "label": "Waiting on a reviewer to pick this up",
                "office": None, "person": None}
    if state == "escalated":
        pending = pending_offices(c)
        if pending:
            return {"kind": "internal_office", "label": f"Waiting on {office_label(pending[0])}",
                    "office": pending[0], "person": None}
        return {"kind": "internal_reviewer", "label": f"Waiting on reviewer ({display(owner)})" if owner
                else "Waiting on a reviewer", "office": None, "person": owner}
    if state == "sent_back":
        name = c.get("counterparty") or c.get("sponsor")
        label = f"Waiting on {counterparty_noun(c)}" + (f" ({name})" if name else "")
        return {"kind": "counterparty", "label": label, "office": None, "person": None}
    if state in ("ready_to_sign", "out_for_signature"):
        signatory = (c.get("signature") or {}).get("signatory")
        if state == "ready_to_sign":
            label = "Ready to sign: waiting on the signatory"
        else:
            label = "Waiting on signature" + (f" ({display(signatory)})" if signatory else "")
        return {"kind": "signatory", "label": label, "office": None, "person": signatory}
    labels = {"signed": "Signed: nothing pending", "active": "Active: nothing pending",
              "rejected": "Rejected: nothing pending", "closed": "Closed: nothing pending"}
    return {"kind": "nobody", "label": labels.get(state, "Nothing pending"), "office": None, "person": None}


def sla(c: dict[str, Any], cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    days = _whole_days(c.get("stageEnteredAt"), now)
    end = parse_iso(c.get("signedAt")) if c.get("state") in ("signed", "active", "closed") else None
    total = _whole_days(c.get("createdAt"), end or now)
    target = (cfg.get("stageTargetDays") or {}).get(c.get("stage"))
    status = "none"
    if target is not None and c.get("state") in OPEN_STATES:
        mult = float(cfg.get("redAfterMultiple") or 2)
        status = "red" if days > float(target) * mult else "amber" if days > float(target) else "on_track"
    return {"daysInStage": days, "totalDays": total,
            "targetDays": (int(target) if target is not None and float(target) == int(target) else target),
            "slaStatus": status}


def value_bucket(c: dict[str, Any]) -> str:
    if c.get("stage") in _CURRENT_STAGES and c.get("state") not in TERMINAL_STATES:
        return "current"
    if c.get("stage") in _POTENTIAL_STAGES and c.get("state") not in TERMINAL_STATES:
        return "potential"
    return "none"


def _review_clause(c: dict[str, Any], clause_type: str | None) -> dict[str, Any]:
    return ((c.get("reviewIndex") or {}).get(clause_type or "") or {}) if clause_type else {}


def _step_clause(c: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    rc = _review_clause(c, ref.get("clauseType"))
    label = rc.get("label") or ref.get("label")
    if not label and ref.get("clauseType"):
        try:
            label = _m().matrix_clause_label(ref["clauseType"])
        except Exception:  # noqa: BLE001
            label = ref["clauseType"]
    return {"clauseType": ref.get("clauseType"), "label": label or "Reviewer item",
            "tier": rc.get("tier") or "review",
            "suggestedLanguage": ref.get("suggestedLanguage") or rc.get("suggestedLanguage")}


def next_step(c: dict[str, Any], analysis_status: str, now: datetime) -> dict[str, Any]:
    """The ONE recommended next step — first rule that matches (GOVERN_API.md)."""
    def step(action: str, headline: str, detail: str | None = None, office: str | None = None,
             clauses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"action": action, "headline": headline, "detail": detail, "office": office, "clauses": clauses or []}

    state = c.get("state")
    # 1. Finished or after signature.
    if state == "rejected":
        rej = c.get("rejection") or {}
        reason = str(rej.get("reasonCode") or "other").replace("_", " ")
        return step("none", "Rejected. Nothing more to do.", f"Reason: {reason}.")
    if state == "closed":
        return step("none", "Closed. Nothing more to do.")
    if state in ("signed", "active"):
        due = sorted(d for d in (c.get("obligationDueDates") or []) if d)
        word = "Signed" if state == "signed" else "Active"
        if not app_settings.feature_enabled("obligations"):
            return step("none", f"{word}. Nothing more to do.")       # obligation tracking is a "Later" feature
        if due:
            soon = sum(1 for d in due if d <= (now + timedelta(days=30)).date().isoformat())
            headline = (f"{word}. Track {_plural(len(due), 'open obligation')}; the next is due {due[0]}.")
            detail = f"{soon} due within 30 days or overdue." if soon else None
            return step("none", headline, detail)
        return step("none", f"{word}. No open obligations to track.")
    # 2. Sonar still reading.
    if str(analysis_status or "").upper() != "READY":
        return step("wait", "Sonar is still reading this agreement.",
                    "The review appears here as soon as the analysis finishes.")
    # 3. Nobody owns it.
    if not c.get("owner"):
        return step("assign", "Assign a reviewer to start the review.")
    # 3b. The PI or department owes an answer before review can go on.
    pi_request = c.get("piRequest")
    if pi_request and state in _ALLOWED_FROM["pi_answered"]:
        who = c.get("piName") or c.get("department") or "the PI or department"
        return step("wait", f"Waiting on {who} to answer.",
                    f"Asked: {_clip(str(pi_request.get('request') or ''), 200)} Mark it answered when they reply.")

    refs = [r for r in c.get("openBlockerRefs") or []]
    approved = approved_offices(c)
    required = set((c.get("routing") or {}).get("required") or [])

    def office_step(office: str, items: list[dict[str, Any]], unacceptable: bool) -> dict[str, Any]:
        clauses = [_step_clause(c, r) for r in items]
        what = _plural(len(clauses), "clause")
        if office in required and state == "escalated":
            return step("wait", f"Waiting on {office_label(office)} to approve {what}.",
                        "They can approve it or ask for changes.", office, clauses)
        verb = "is unacceptable" if unacceptable else "needs their review"
        return step("escalate", f"Escalate to {office_label(office)}: {what} {verb if len(clauses) == 1 else ('are unacceptable' if unacceptable else 'need their review')}.",
                    None, office, clauses)

    # 4. Unacceptable with no fallback → escalate to its office (if not yet approved) or reject.
    hard = []
    for r in refs:
        rc = _review_clause(c, r.get("clauseType"))
        if rc.get("tier") == "unacceptable" and not rc.get("hasFallback"):
            hard.append(r)
    if hard:
        for r in hard:
            office = r.get("office") or _review_clause(c, r.get("clauseType")).get("office")
            if office and office not in approved:
                return office_step(office, [x for x in hard if (x.get("office") or _review_clause(c, x.get("clauseType")).get("office")) == office], True)
        clauses = [_step_clause(c, r) for r in hard]
        names = ", ".join(x["label"] for x in clauses[:3])
        return step("reject", f"Reject: {names} {'is' if len(clauses) == 1 else 'are'} unacceptable under your matrix with no fallback.",
                    "Or send it back if the other side may still agree to your standard terms.", None, clauses)
    # 5. Blockers that need an office that has not approved.
    for r in refs:
        office = r.get("office")
        if office and office not in approved:
            return office_step(office, [x for x in refs if x.get("office") == office], False)
    # 6. Open blockers → send back (or wait while it is with the other side).
    if refs:
        clauses = [_step_clause(c, r) for r in refs]
        noun = counterparty_noun(c)
        if state == "sent_back":
            return step("wait", f"Waiting on the {noun} to send a revised version.",
                        f"{_plural(len(clauses), 'clause')} still open.", None, clauses)
        return step("send_back", f"Send back to the {noun}: {_plural(len(clauses), 'clause')} {'needs' if len(clauses) == 1 else 'need'} changes.",
                    "Suggested language is included for each clause.", None, clauses)
    # 7. Signature.
    if state == "ready_to_sign":
        return step("send_for_signature", "Send for signature.", "Everything is approved.")
    if state == "out_for_signature":
        return step("wait", "Waiting on signature.")
    # 8. Value unknown.
    value, _ = contract_value(c)
    if value is None:
        return step("add_value", "Add the contract value so the totals are complete.",
                    "You can still approve it without a value.")
    # 9. Approve.
    pending = pending_offices(c)
    if state == "escalated" and pending:
        return step("approve", f"{office_label(pending[0])} to approve.", None, pending[0])
    if state == "sent_back":
        return step("approve", "Approve if the other side accepted the changes.", None)
    return step("approve", "Approve: nothing in this agreement is outside your matrix.", None)


def to_api(c: dict[str, Any], now: datetime | None = None, cfg: dict[str, Any] | None = None,
           doc_meta: dict[str, Any] | None = None, viewer: "Viewer | None" = None) -> dict[str, Any]:
    """Contract JSON (GOVERN_API.md ``Contract``) from a stored item.
    ``viewer`` decides ``allowedActions`` (none for a system read)."""
    now = _now(now)
    cfg = cfg or default_settings()
    analysis_status = (doc_meta or {}).get("status") or c.get("analysisStatus") or "READY"
    value, source = contract_value(c)
    timing = sla(c, cfg, now)
    routing = c.get("routing") or {}
    due_soon_cut = (now + timedelta(days=30)).date().isoformat()
    fy_basis = c.get("signedAt") if value_bucket(c) == "current" else (c.get("requestedDate") or c.get("createdAt"))
    matrix = c.get("matrix")
    counts_default = {k: 0 for k in TIERS + ("beneficial",)}
    return {
        "contractId": c["contractId"],
        "currentDocId": c.get("currentDocId") or c["contractId"],
        "versionDocIds": list(c.get("versionDocIds") or [c["contractId"]]),
        "tenantId": c.get("tenantId"),
        "title": c.get("title") or "",
        "docType": c.get("docType") or "OTHER",
        "agreementType": c.get("agreementType") or "other",
        "direction": c.get("direction") or "incoming",
        "counterparty": c.get("counterparty"),
        "sponsor": c.get("sponsor"),
        "piName": c.get("piName"),
        "department": c.get("department"),
        "college": c.get("college"),
        "stage": c.get("stage") or "draft",
        "state": c.get("state") or "intake",
        "waitingOn": waiting_on(c),
        "owner": c.get("owner"),
        "createdAt": c.get("createdAt"),
        "updatedAt": c.get("updatedAt"),
        "stageEnteredAt": c.get("stageEnteredAt"),
        "signedAt": c.get("signedAt"),
        **timing,
        "rounds": int(c.get("rounds") or 0),
        "analysisStatus": analysis_status,
        "value": value,
        "valueSource": source,
        "extractedValue": c.get("extractedValue"),
        "manualValue": c.get("manualValue"),
        "expectedValue": c.get("expectedValue"),
        "currency": c.get("currency"),
        "fiscalYear": fiscal_year(fy_basis),
        "requestedDate": c.get("requestedDate"),
        "valueBucket": value_bucket(c),
        "matrix": ({"version": matrix.get("version"), "reviewedAt": matrix.get("reviewedAt"),
                    "counts": {**counts_default, **(matrix.get("counts") or {})}} if matrix else None),
        "overallRisk": c.get("overallRisk"),
        "openBlockers": len(c.get("openBlockerRefs") or []),
        "nextStep": next_step(c, analysis_status, now),
        "huronRecordId": c.get("huronRecordId"),
        "workdayRef": c.get("workdayRef"),
        "workdayMatch": c.get("workdayMatch") or "unmatched",
        "syncConflicts": list(c.get("syncConflicts") or []),
        "routing": {"required": list(routing.get("required") or []),
                    "approvals": list(routing.get("approvals") or []),
                    "reasons": list(routing.get("reasons") or [])},
        "rejection": c.get("rejection"),
        "signature": c.get("signature"),
        "piRequest": c.get("piRequest"),
        # Obligation tracking is a "Later" feature: obligations may be stored, but none count as due while it is off.
        "obligationsDue": (sum(1 for d in c.get("obligationDueDates") or [] if d and d <= due_soon_cut)
                           if app_settings.feature_enabled("obligations") else 0),
        "captureGaps": capture_gaps(c),
        "allowedActions": allowed_actions(c, viewer, cfg) if viewer else [],
        "effectiveDate": c.get("effectiveDate"),
        "termEndDate": c.get("termEndDate"),
        "rev": int(c.get("rev") or 0),
    }


_RESEARCH_TYPES = frozenset({"sponsored_research", "grant"})
_SIGNED_STAGES = frozenset({"signed", "active", "renewal", "expired"})
_APPROVAL_ONWARD = frozenset({"approval"}) | _SIGNED_STAGES
CAPTURE_GAPS = ("value", "counterparty", "sponsor", "piName", "department", "requestedDate",
                "huronRecordId", "workdayRef", "effectiveDate", "termEnd", "agreementTypeUnsure")


def capture_gaps(c: dict[str, Any]) -> list[str]:
    """Fields Govern could not capture and nobody entered, for this kind of
    contract — so a report is never silently incomplete.

    counterparty, huronRecordId: always · sponsor, piName, department,
    requestedDate: sponsored research and grants · value: all but NDA / MTA ·
    workdayRef: outgoing money, or incoming once at approval or later ·
    effectiveDate, termEnd: once signed · agreementTypeUnsure: the type was
    inferred as "other" and nobody confirmed it.
    """
    if c.get("state") == "rejected":
        return []
    at = c.get("agreementType") or "other"
    signed = c.get("stage") in _SIGNED_STAGES or c.get("state") in ("signed", "active", "closed")
    gaps: set[str] = set()

    def missing(field: str) -> bool:
        v = c.get(field)
        return v is None or (isinstance(v, str) and not v.strip())

    if at not in ("nda", "mta") and contract_value(c)[0] is None:
        gaps.add("value")
    if missing("counterparty"):
        gaps.add("counterparty")
    if at in _RESEARCH_TYPES:
        gaps.update(f for f in ("sponsor", "piName", "department", "requestedDate") if missing(f))
    if missing("huronRecordId"):
        gaps.add("huronRecordId")
    if missing("workdayRef") and (c.get("direction") == "outgoing"
                                  or (c.get("direction", "incoming") == "incoming" and c.get("stage") in _APPROVAL_ONWARD)):
        gaps.add("workdayRef")
    if signed:
        if missing("effectiveDate"):
            gaps.add("effectiveDate")
        if missing("termEndDate"):
            gaps.add("termEnd")
    if at == "other" and "agreementType" not in (c.get("userFields") or []):
        gaps.add("agreementTypeUnsure")
    return [g for g in CAPTURE_GAPS if g in gaps]


class Viewer:
    """Who is looking at a contract, for ``allowedActions``: may they edit
    (owner / editor of its document), are they a Govern leader, and their
    email (an office approver listed in the workflow settings)."""

    __slots__ = ("can_edit", "is_leader", "email")

    def __init__(self, *, can_edit: bool, is_leader: bool = False, email: str | None = None) -> None:
        self.can_edit, self.is_leader, self.email = can_edit, is_leader, (email or "").lower() or None


def approver_offices(email: str | None, cfg: dict[str, Any] | None) -> set[str]:
    """Offices a person approves for (settings.reviewers[].offices)."""
    if not email:
        return set()
    return {o for r in (cfg or {}).get("reviewers") or [] if str(r.get("email") or "").lower() == email
            for o in r.get("offices") or []}


def allowed_actions(c: dict[str, Any], viewer: Viewer, cfg: dict[str, Any] | None = None) -> list[str]:
    """Actions THIS viewer may take now — exactly what POST /actions accepts.

    Editors (not leaders): every action valid from the current state.
    Viewers and leaders: ``comment``; and ``office_approve`` when they approve
    for an office the contract is waiting on.
    """
    state = c.get("state") or "intake"
    asked = bool(c.get("piRequest"))
    valid = [a for a in ACTIONS if state in _ALLOWED_FROM[a]
             and not (a == "ask_pi" and asked) and not (a == "pi_answered" and not asked)]
    if viewer.can_edit and not viewer.is_leader:
        return valid
    allowed = {"comment"}
    if "office_approve" in valid and set(pending_offices(c)) & approver_offices(viewer.email, cfg):
        allowed.add("office_approve")
    return [a for a in ACTIONS if a in allowed]


def routed_offices_for(c: dict[str, Any], viewer: Viewer, cfg: dict[str, Any] | None) -> set[str]:
    """Pending offices this viewer may approve for (view-only approvers)."""
    return set(pending_offices(c)) & approver_offices(viewer.email, cfg)


def _activity_view(e: dict[str, Any]) -> dict[str, Any]:
    return {"id": e.get("id"), "at": e.get("at"), "actor": e.get("actor"), "action": e.get("action"),
            "fromStage": e.get("fromStage"), "toStage": e.get("toStage"),
            "summary": e.get("summary") or "", "detail": e.get("detail")}


def _blocker_view(b: dict[str, Any]) -> dict[str, Any]:
    return {k: b.get(k) for k in ("id", "text", "clauseType", "office", "suggestedLanguage", "source", "status",
                                  "createdAt", "createdBy", "closedAt", "closedBy")}


def obligation_view(o: dict[str, Any]) -> dict[str, Any]:
    out = {k: o.get(k) for k in ("id", "kind", "title", "dueDate", "amount", "status", "source", "completedAt",
                                 "verifiedAt", "verifiedBy")}
    # Older items predate verification: manual ones were entered by a person.
    out["verified"] = bool(o["verified"]) if o.get("verified") is not None else o.get("source") != "sonar"
    return out


def _income_view(i: dict[str, Any]) -> dict[str, Any]:
    return {k: i.get(k) for k in ("id", "kind", "description", "amount", "pct", "expectedDate", "source")}


def detail(c: dict[str, Any], *, now: datetime | None = None, cfg: dict[str, Any] | None = None,
           doc_meta: dict[str, Any] | None = None, viewer: "Viewer | None" = None, version_metas: dict[str, dict[str, Any]] | None = None,
           blockers: list[dict[str, Any]] | None = None, obligations: list[dict[str, Any]] | None = None,
           income: list[dict[str, Any]] | None = None, review: dict[str, Any] | None = None,
           activity: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """ContractDetail = Contract + blockers, obligations, income, review, activity, versions."""
    out = to_api(c, now, cfg, doc_meta, viewer)
    metas = version_metas or {}
    rounds = c.get("versionRounds") or {}
    counts = c.get("versionCounts") or {}
    versions = []
    for doc_id in out["versionDocIds"]:
        m = metas.get(doc_id) or {}
        versions.append({"docId": doc_id, "title": m.get("title") or c.get("title") or "",
                         "createdAt": m.get("createdAt"), "status": m.get("status") or "UNKNOWN",
                         "round": int(rounds.get(doc_id) or 1), "matrixCounts": counts.get(doc_id)})
    out.update({
        "blockers": [_blocker_view(b) for b in blockers or []],
        "obligations": [obligation_view(o) for o in obligations or []],
        "licensingIncome": [_income_view(i) for i in income or []],
        "review": review,
        "activity": [_activity_view(e) for e in (activity or [])[:200]],
        "versions": versions,
    })
    return out


# ---------------------------------------------------------------------------
# Item bookkeeping
# ---------------------------------------------------------------------------


def refresh_refs(c: dict[str, Any], blockers: list[dict[str, Any]] | None = None,
                 obligations: list[dict[str, Any]] | None = None) -> None:
    """Denormalise what list views need from the child items onto the contract:
    open blockers (for counts and the next step) and open obligation due dates."""
    if blockers is None:
        blockers = store.contracts.blockers(c["contractId"])
    if obligations is None:
        obligations = store.contracts.obligations(c["contractId"])
    c["openBlockerRefs"] = [{"id": b.get("id"), "clauseType": b.get("clauseType"), "office": b.get("office"),
                             "suggestedLanguage": b.get("suggestedLanguage")}
                            for b in blockers if b.get("status", "open") == "open"]
    c["obligationDueDates"] = sorted(str(o["dueDate"])[:10] for o in obligations
                                     if o.get("status", "open") == "open" and o.get("dueDate"))


def _enter_state(c: dict[str, Any], state: str, now_s: str, analysis_ready: bool = True) -> tuple[str, str]:
    """Set state (and stage). The stage clock restarts only when the STAGE changes."""
    from_stage = c.get("stage") or "draft"
    c["state"] = state
    to_stage = stage_for(state, from_stage, analysis_ready)
    if to_stage != from_stage or not c.get("stageEnteredAt"):
        c["stage"] = to_stage
        c["stageEnteredAt"] = now_s
        c.pop("overdueNotifiedFor", None)
    c["updatedAt"] = now_s
    return from_stage, to_stage


def _entry(action: str, actor: dict[str, Any] | None, summary: str, *, from_stage: str | None = None,
           to_stage: str | None = None, detail: dict[str, Any] | None = None, at: str | None = None) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "at": at or iso(), "actor": actor, "action": action,
            "fromStage": from_stage, "toStage": to_stage if to_stage != from_stage else None,
            "summary": summary, "detail": detail}


def write_activity(c: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    for e in entries:
        try:
            store.activity.append(c["contractId"], c["tenantId"], e)
        except Exception as exc:  # noqa: BLE001 — the state change already happened
            log.error("govern.activity_write_failed", contractId=c["contractId"], action=e.get("action"),
                      error_type=type(exc).__name__)


_EXTERNAL_ID_FIELDS = (("huron", "huronRecordId"), ("workday", "workdayRef"))


def sync_external_ids(c: dict[str, Any], before: dict[str, Any] | None = None) -> None:
    """Keep the GSI3 pointers (system id → contract) equal to the contract's
    Huron / Workday ids, so connectors find a contract by its record id."""
    for system, field in _EXTERNAL_ID_FIELDS:
        if (before or {}).get(field) == c.get(field) and before is not None:
            continue
        try:
            store.sync.set_external_id(c["tenantId"], c["contractId"], system, c.get(field))
        except Exception as exc:  # noqa: BLE001 — the contract itself is stored
            log.warning("govern.external_id_failed", system=system, error_type=type(exc).__name__)


def mirror_lifecycle(c: dict[str, Any]) -> None:
    """Keep the documents table's ``lifecycle`` equal to the contract stage, so
    the Library and Dashboard agree with Govern. Best-effort."""
    from ..dynamodb import update_doc_fields

    for doc_id in dict.fromkeys([c.get("currentDocId"), c.get("contractId")]):
        if not doc_id:
            continue
        try:
            update_doc_fields(doc_id, {"lifecycle": c.get("stage") or "draft"})
        except Exception as exc:  # noqa: BLE001 — e.g. the document was deleted
            log.warning("govern.lifecycle_mirror_failed", docId=doc_id, error_type=type(exc).__name__)


def _note_stage_exit(c: dict[str, Any], from_stage: str | None, entered: str | None,
                     entries: list[dict[str, Any]]) -> None:
    """Attach how long the contract spent in the stage it left (and whether
    that was within target) to the entry that moved it — the trends
    aggregates are built from exactly this."""
    if not from_stage or not entered:
        return
    start, end = parse_iso(entered), parse_iso(c.get("stageEnteredAt")) or datetime.now(timezone.utc)
    if start is None:
        return
    days = round(max(0.0, (end - start).total_seconds() / 86400), 3)
    target = (get_settings(c["tenantId"]).get("stageTargetDays") or {}).get(from_stage)
    mover = next((e for e in entries if e.get("toStage")), entries[-1])
    mover["detail"] = {**(mover.get("detail") or {}),
                       "stageExit": {"stage": from_stage, "days": days, "targetDays": target,
                                     "onTime": None if target is None else days <= float(target)}}


def _commit(contract_id: str, change: Callable[[dict[str, Any], list[dict[str, Any]]], Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """mutate_contract + the entries the winning attempt produced, written after
    the state is stored; the lifecycle mirror follows a stage change."""
    holder: dict[str, Any] = {}

    def wrapped(c: dict[str, Any]) -> Any:
        entries: list[dict[str, Any]] = []
        holder["before_stage"] = c.get("stage")
        before_entered = c.get("stageEnteredAt")
        result = change(c, entries)
        if result is not False and c.get("stage") != holder["before_stage"] and entries:
            _note_stage_exit(c, holder["before_stage"], before_entered, entries)
        holder["entries"] = entries
        return result

    stored = store.contracts.mutate(contract_id, wrapped)
    if stored is None:
        raise NotFound("Contract not found")
    entries = holder.get("entries") or []
    write_activity(stored, entries)
    if stored.get("stage") != holder.get("before_stage"):
        mirror_lifecycle(stored)
    return stored, entries


# ---------------------------------------------------------------------------
# Fields (PATCH /contracts/{id}, POST /contracts)
# ---------------------------------------------------------------------------


def clean_fields(body: dict[str, Any], *, allow: tuple[str, ...] = PATCH_FIELDS) -> dict[str, Any]:
    """Validate user-settable fields. Raises BadRequest."""
    unknown = set(body) - set(allow)
    if unknown:
        raise BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k == "agreementType":
            if v not in agreement_types():
                raise BadRequest(f"agreementType must be one of: {', '.join(agreement_types())}")
            out[k] = v
        elif k == "direction":
            if v not in DIRECTIONS:
                raise BadRequest("direction must be incoming or outgoing")
            out[k] = v
        elif k in _TEXT_FIELDS:
            if v is not None and not isinstance(v, str):
                raise BadRequest(f"{k} must be a string or null")
            out[k] = (v or "").strip()[:200] or None
        elif k in ("expectedValue", "manualValue"):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 1e13):
                raise BadRequest(f"{k} must be a non-negative number or null")
            out[k] = v
        elif k == "currency":
            if v is not None and (not isinstance(v, str) or not re.fullmatch(r"[A-Za-z]{3}", v)):
                raise BadRequest("currency must be a 3-letter code or null")
            out[k] = v.upper() if v else None
        elif k in _DATE_FIELDS:
            if v is not None and (not isinstance(v, str) or not _DATE_RE.match(v) or parse_iso(v) is None):
                raise BadRequest(f"{k} must be a date (YYYY-MM-DD) or null")
            out[k] = v
        elif k in ("huronRecordId", "workdayRef"):
            if v is not None and (not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._:/#-]{1,100}", v.strip())):
                raise BadRequest(f"{k} must be up to 100 letters, digits or - _ . : / #, or null")
            out[k] = v.strip() if v else None
        elif k == "workdayMatch":
            if v not in WORKDAY_MATCH:
                raise BadRequest("workdayMatch must be auto, manual or unmatched")
            out[k] = v
    if out.get("workdayRef") and "workdayMatch" not in out:
        out["workdayMatch"] = "manual"
    if "workdayRef" in out and out["workdayRef"] is None and "workdayMatch" not in out:
        out["workdayMatch"] = "unmatched"
    return out


def _field_text(field: str, value: Any, c: dict[str, Any]) -> str:
    if value is None:
        return "nothing"
    if field in ("expectedValue", "manualValue"):
        return money(value, c.get("currency"))
    if field == "agreementType":
        return agreement_label(value).lower()
    return str(value)


def _apply_fields(c: dict[str, Any], fields: dict[str, Any], cfg: dict[str, Any], now_s: str,
                  *, mark_user: bool = True) -> dict[str, dict[str, Any]]:
    """Set person-entered fields; returns {field: {from, to}} for real changes."""
    changes: dict[str, dict[str, Any]] = {}
    for k, v in fields.items():
        if c.get(k) != v:
            changes[k] = {"from": c.get(k), "to": v}
            c[k] = v
    if mark_user and fields:
        c["userFields"] = sorted(set(c.get("userFields") or []) | set(fields))
    if changes:
        c["updatedAt"] = now_s
        apply_routing(c, cfg)
    return changes


def update_fields(contract_id: str, fields: dict[str, Any], actor: dict[str, Any] | None,
                  *, now: datetime | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """PATCH: set fields a person entered; routing is re-evaluated; one
    ``field_updated`` entry lists every change."""
    now_s = iso(_now(now))
    before: dict[str, Any] = {}

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        before.clear()
        before.update({f: c.get(f) for _, f in _EXTERNAL_ID_FIELDS})
        changes = _apply_fields(c, fields, cfg or get_settings(c["tenantId"]), now_s)
        if not changes:
            # Same values: write only if the fields are newly marked as entered by a person.
            return False if set(fields) <= set(c.get("userFields") or []) else None
        entries.append(_entry("field_updated", actor, _fields_summary(actor, changes, c), at=now_s,
                              detail={"fields": sorted(changes), "changes": changes}))
        return None

    stored, _ = _commit(contract_id, change)
    sync_external_ids(stored, before)
    return stored


def _fields_summary(actor: dict[str, Any] | None, changes: dict[str, dict[str, Any]], c: dict[str, Any]) -> str:
    who = display(actor)
    if len(changes) == 1:
        (field, ch), = changes.items()
        if ch["to"] is None:
            return f"{who} cleared the {_FIELD_WORDS.get(field, field)}."
        return f"{who} set the {_FIELD_WORDS.get(field, field)} to {_field_text(field, ch['to'], c)}."
    names = [_FIELD_WORDS.get(f, f) for f in changes]
    return f"{who} updated the {', '.join(names[:-1])} and {names[-1]}."


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _require_from(c: dict[str, Any], action: str) -> None:
    if c.get("state") not in _ALLOWED_FROM[action]:
        state = _STATE_WORDS.get(c.get("state") or "", c.get("state"))
        raise InvalidTransition(f"You cannot {action.replace('_', ' ')} a contract that is {state}.")


def _after_approval(c: dict[str, Any], actor: dict[str, Any] | None, now_s: str) -> tuple[str, str, str]:
    """approve / office_approve outcome: the next office, else ready to sign."""
    if not app_settings.feature_enabled("routing_rules"):
        # Drop offices a rule stored before routing was switched off; hand
        # escalations stay (evaluate_routing returns nothing while it is off).
        apply_routing(c, {})
    pending = pending_offices(c)
    if pending:
        fs, ts = _enter_state(c, "escalated", now_s)
        return fs, ts, f"It now needs {office_label(pending[0])}."
    fs, ts = _enter_state(c, "ready_to_sign", now_s)
    return fs, ts, "It is ready to sign."


def _validate_body(action: str, body: dict[str, Any]) -> dict[str, Any]:
    """Shape checks that do not depend on the contract's state."""
    if action not in _ALLOWED_FROM:
        raise BadRequest(f"Unknown action. Use one of: {', '.join(ACTIONS)}")
    v: dict[str, Any] = {"note": _note(body)}
    if action == "assign":
        v["owner"] = parse_person(body.get("owner"), "owner")
    elif action in ("office_approve", "escalate"):
        office = body.get("office")
        if office not in offices():
            raise BadRequest(f"office must be one of: {', '.join(offices())}")
        v["office"] = office
    elif action == "send_back":
        clauses = body.get("clauses")
        if not isinstance(clauses, list) or len(clauses) > 100:
            raise BadRequest("clauses must be a list (max 100)")
        clean = []
        for i, cl in enumerate(clauses):
            if not isinstance(cl, dict):
                raise BadRequest(f"clauses[{i}] must be an object")
            clean.append({"clauseType": str(cl.get("clauseType") or "")[:64] or None,
                          "label": str(cl.get("label") or cl.get("clauseType") or "Clause")[:200],
                          "suggestedLanguage": (str(cl["suggestedLanguage"])[:4000]
                                                if cl.get("suggestedLanguage") else None)})
        v["clauses"] = clean
    elif action == "reject":
        if body.get("reasonCode") not in REJECT_REASONS:
            raise BadRequest(f"reasonCode must be one of: {', '.join(REJECT_REASONS)}")
        v["reasonCode"] = body["reasonCode"]
    elif action == "send_for_signature":
        if body.get("provider") not in ("docusign", "manual"):
            raise BadRequest("provider must be docusign or manual")
        if body["provider"] == "docusign":
            require_feature("docusign", "Sending through DocuSign")
        v["provider"] = body["provider"]
        v["signatory"] = parse_person(body["signatory"], "signatory") if body.get("signatory") else None
        v["envelopeId"] = str(body["envelopeId"])[:100] if body.get("envelopeId") else None
    elif action == "mark_signed":
        signed_at = body.get("signedAt")
        if signed_at is not None and (not isinstance(signed_at, str) or parse_iso(signed_at) is None):
            raise BadRequest("signedAt must be an ISO date or date-time")
        v["signedAt"] = signed_at
        v["envelopeId"] = str(body["envelopeId"])[:100] if body.get("envelopeId") else None
        v["provider"] = body.get("provider") if body.get("provider") in ("docusign", "manual") else None
    elif action == "comment":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BadRequest("text is required")
        v["text"] = text.strip()[:4000]
    elif action == "ask_pi":
        text = body.get("request")
        if not isinstance(text, str) or not text.strip():
            raise BadRequest("request is required: say what the PI or department needs to provide")
        v["request"] = text.strip()[:1000]
    return v


def perform_action(contract_id: str, action: str, body: dict[str, Any], actor: dict[str, Any] | None,
                   *, now: datetime | None = None, cfg: dict[str, Any] | None = None,
                   system_name: str = "Sonar") -> dict[str, Any]:
    """Validate and apply one action. Raises BadRequest / NotFound /
    InvalidTransition; ``store.ContractConflict`` after repeated lost races."""
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    v = _validate_body(action, body)
    now_dt = _now(now)
    now_s = iso(now_dt)
    who = display(actor, system_name)
    note = v.get("note")
    extra: dict[str, Any] = {}

    if action == "comment":
        c = store.contracts.get(contract_id)
        if c is None:
            raise NotFound("Contract not found")
        entry = _entry("comment", actor, f"{who} commented: “{_clip(v['text'], 160)}”", at=now_s,
                       detail={"text": v["text"]})
        write_activity(c, [entry])
        return c

    if action == "mark_signed":
        # Obligations come from the signed document; read it before the write
        # so the stored contract already carries their due dates.
        c0 = store.contracts.get(contract_id)
        if c0 is None:
            raise NotFound("Contract not found")
        if c0.get("state") in ("signed", "active") and v.get("envelopeId") \
                and (c0.get("signature") or {}).get("envelopeId") == v["envelopeId"]:
            return c0          # the same envelope reported twice — already done
        _require_from(c0, action)
        extra["obligations"] = _sonar_obligations(c0, v.get("signedAt") or now_s, now_s)

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        _require_from(c, action)
        routing = c.setdefault("routing", {"required": [], "approvals": [], "reasons": [], "escalated": []})
        detail_: dict[str, Any] = {"note": note} if note else {}
        if action in _CLEARS_PI_REQUEST:
            c.pop("piRequest", None)
        if action == "ask_pi":
            if c.get("piRequest"):
                raise InvalidTransition("The PI or department has already been asked. Mark it answered first.")
            c["piRequest"] = {"request": v["request"], "at": now_s, "by": actor}
            c["updatedAt"] = now_s
            who_pi = c.get("piName") or c.get("department") or "the PI or department"
            entries.append(_entry("pi_requested", actor, f"{who} asked {who_pi}: “{_clip(v['request'], 160)}”",
                                  at=now_s, detail={**detail_, "request": v["request"]}))
        elif action == "pi_answered":
            req = c.get("piRequest")
            if not req:
                raise InvalidTransition("Nothing is waiting on the PI or department.")
            c.pop("piRequest", None)
            c["updatedAt"] = now_s
            entries.append(_entry("pi_answered", actor, f"{who} marked the PI or department request answered.",
                                  at=now_s, detail={**detail_, "request": req.get("request"),
                                                    "daysWaited": _days_between(req.get("at"), now_s)}))
        elif action == "assign":
            old = c.get("owner")
            if old and old.get("email") == v["owner"]["email"] and old.get("name") == v["owner"]["name"]:
                return False
            c["owner"] = v["owner"]
            fs = ts = c.get("stage")
            if c.get("state") == "intake" and c.get("stage") != "draft":
                fs, ts = _enter_state(c, "in_review", now_s)
            c["updatedAt"] = now_s
            if old and old.get("email") != v["owner"]["email"]:
                entries.append(_entry("reassigned", actor, f"{who} reassigned this from {display(old)} to {display(v['owner'])}.",
                                      from_stage=fs, to_stage=ts, at=now_s, detail={**detail_, "owner": v["owner"], "previousOwner": old}))
            else:
                entries.append(_entry("assigned", actor, f"{who} assigned this to {display(v['owner'])}.",
                                      from_stage=fs, to_stage=ts, at=now_s, detail={**detail_, "owner": v["owner"]}))
        elif action == "approve":
            routing["approvals"] = [a for a in routing.get("approvals") or [] if a.get("office") != "reviewer"] + \
                [{"office": "reviewer", "by": actor, "at": now_s}]
            fs, ts, tail = _after_approval(c, actor, now_s)
            entries.append(_entry("approved", actor, f"{who} approved this. {tail}", from_stage=fs, to_stage=ts,
                                  at=now_s, detail=detail_ or None))
        elif action == "office_approve":
            office = v["office"]
            routing["approvals"] = [a for a in routing.get("approvals") or [] if a.get("office") != office] + \
                [{"office": office, "by": actor, "at": now_s}]
            since = parse_iso((routing.get("requiredAt") or {}).get(office))
            days = round(max(0.0, (now_dt - since).total_seconds() / 86400), 3) if since else None
            fs, ts, tail = _after_approval(c, actor, now_s)
            entries.append(_entry("office_approved", actor, f"{who} approved this for {office_label(office)}. {tail}",
                                  from_stage=fs, to_stage=ts, at=now_s,
                                  detail={**detail_, "office": office, "daysToApprove": days}))
        elif action == "send_back":
            c["rounds"] = int(c.get("rounds") or 0) + 1
            routing["approvals"] = []       # new terms need fresh approval
            fs, ts = _enter_state(c, "sent_back", now_s)
            n = len(v["clauses"])
            tail = f" with {_plural(n, 'clause')} to change" if n else ""
            entries.append(_entry("sent_back", actor, f"{who} sent this back to the {counterparty_noun(c)}{tail}.",
                                  from_stage=fs, to_stage=ts, at=now_s,
                                  detail={"clauses": v["clauses"], "note": note, "round": c["rounds"]}))
        elif action == "escalate":
            office = v["office"]
            routing["escalated"] = list(dict.fromkeys(list(routing.get("escalated") or []) + [office]))
            routing["approvals"] = [a for a in routing.get("approvals") or [] if a.get("office") != office]
            routing.setdefault("requiredAt", {})[office] = now_s
            apply_routing(c, cfg or get_settings(c["tenantId"]))
            fs, ts = _enter_state(c, "escalated", now_s)
            entries.append(_entry("escalated", actor, f"{who} escalated this to {office_label(office)}.",
                                  from_stage=fs, to_stage=ts, at=now_s, detail={**detail_, "office": office}))
        elif action == "reject":
            c["rejection"] = {"reasonCode": v["reasonCode"], "note": note, "at": now_s, "by": actor}
            fs, ts = _enter_state(c, "rejected", now_s)
            entries.append(_entry("rejected", actor, f"{who} rejected this: {v['reasonCode'].replace('_', ' ')}.",
                                  from_stage=fs, to_stage=ts, at=now_s,
                                  detail={**detail_, "reasonCode": v["reasonCode"]}))
        elif action == "send_for_signature":
            c["signature"] = {"provider": v["provider"], "envelopeId": v.get("envelopeId"), "sentAt": now_s,
                              "signedAt": None, "signatory": v.get("signatory")}
            fs, ts = _enter_state(c, "out_for_signature", now_s)
            how = "through DocuSign" if v["provider"] == "docusign" else "for a manual signature"
            to = f" to {display(v['signatory'])}" if v.get("signatory") else ""
            entries.append(_entry("signature_sent", actor, f"{who} sent this{to} {how}.", from_stage=fs, to_stage=ts,
                                  at=now_s, detail={**detail_, "provider": v["provider"]}))
        elif action == "mark_signed":
            signed_at = v.get("signedAt") or now_s
            sig = dict(c.get("signature") or {"provider": v.get("provider") or "manual", "envelopeId": None,
                                                "sentAt": None, "signatory": None})
            if v.get("provider"):
                sig["provider"] = v["provider"]
            if v.get("envelopeId"):
                sig["envelopeId"] = v["envelopeId"]
            sig["signedAt"] = signed_at
            c["signature"] = sig
            c["signedAt"] = signed_at
            fs, ts = _enter_state(c, "signed", now_s)
            obligations = extra.get("obligations") or []
            manual = [o for o in store.contracts.obligations(c["contractId"]) if o.get("source") != "sonar"]
            refresh_refs(c, obligations=manual + obligations)
            value, _ = contract_value(c)
            tail = f" {money(value, c.get('currency'))} moves to current value." if value is not None else ""
            summary = (f"{who} marked this signed.{tail}" if actor or system_name == "Sonar"
                       else f"{system_name} reported this agreement signed.{tail}")
            entries.append(_entry("signed", actor, summary, from_stage=fs, to_stage=ts, at=now_s,
                                  detail={**detail_, "signedAt": signed_at, "provider": sig.get("provider"),
                                          "envelopeId": sig.get("envelopeId"), "obligations": len(obligations),
                                          **_money_detail(c),
                                          "cycleDays": _days_between(c.get("createdAt"), signed_at),
                                          "rounds": int(c.get("rounds") or 0)}))
        elif action == "activate":
            fs, ts = _enter_state(c, "active", now_s)
            entries.append(_entry("activated", actor, f"{who} marked this active.", from_stage=fs, to_stage=ts,
                                  at=now_s, detail=detail_ or None))
        elif action == "close":
            fs, ts = _enter_state(c, "closed", now_s)
            entries.append(_entry("closed", actor, f"{who} closed this agreement.", from_stage=fs, to_stage=ts,
                                  at=now_s, detail=detail_ or None))
        elif action == "reopen":
            previous = c.get("state")
            c["rejection"] = None
            if previous == "out_for_signature":
                c["signature"] = None
            fs, ts = _enter_state(c, "in_review", now_s)
            entries.append(_entry("reopened", actor, f"{who} reopened this for review.", from_stage=fs, to_stage=ts,
                                  at=now_s, detail={**detail_, "previousState": previous}))
        return None

    stored, _ = _commit(contract_id, change)
    if action == "mark_signed" and stored.get("state") == "signed":
        _replace_sonar_obligations(stored, extra.get("obligations") or [])
    return stored


def _money_detail(c: dict[str, Any]) -> dict[str, Any]:
    value, _ = contract_value(c)
    return {"value": value, "currency": c.get("currency") or "USD", "agreementType": c.get("agreementType") or "other"}


def _days_between(start: Any, end: Any) -> float | None:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    return round(max(0.0, (b - a).total_seconds() / 86400), 3)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Blockers / obligations / income edited by people
# ---------------------------------------------------------------------------


def add_blocker(contract_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
                *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("text is required")
    office = body.get("office")
    if office is not None and office not in offices():
        raise BadRequest(f"office must be one of: {', '.join(offices())}")
    clause_type = body.get("clauseType")
    if clause_type is not None and (not isinstance(clause_type, str) or len(clause_type) > 64):
        raise BadRequest("clauseType must be a category id")
    sl = body.get("suggestedLanguage")
    if sl is not None and not isinstance(sl, str):
        raise BadRequest("suggestedLanguage must be a string")
    if store.contracts.get(contract_id) is None:
        raise NotFound("Contract not found")
    now_s = iso(_now(now))
    blocker = {"id": uuid.uuid4().hex[:16], "text": text.strip()[:2000], "clauseType": clause_type or None,
               "office": office, "suggestedLanguage": (sl or "").strip()[:4000] or None, "source": "reviewer",
               "status": "open", "createdAt": now_s, "createdBy": actor, "closedAt": None, "closedBy": None}
    store.contracts.put_blocker(contract_id, blocker)

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        refresh_refs(c)
        c["updatedAt"] = now_s
        entries.append(_entry("blocker_added", actor, f"{display(actor)} added an open item: {_clip(blocker['text'], 140)}",
                              at=now_s, detail={"blockerId": blocker["id"], "office": office}))

    stored, _ = _commit(contract_id, change)
    return stored


def edit_blocker(contract_id: str, blocker_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
                 *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    unknown = set(body) - {"text", "status", "office", "suggestedLanguage"}
    if unknown:
        raise BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    blocker = next((b for b in store.contracts.blockers(contract_id) if b.get("id") == blocker_id), None)
    if blocker is None:
        raise NotFound("Blocker not found")
    now_s = iso(_now(now))
    before_status = blocker.get("status", "open")
    if "text" in body:
        if not isinstance(body["text"], str) or not body["text"].strip():
            raise BadRequest("text cannot be empty")
        blocker["text"] = body["text"].strip()[:2000]
    if "office" in body:
        if body["office"] is not None and body["office"] not in offices():
            raise BadRequest(f"office must be one of: {', '.join(offices())}")
        blocker["office"] = body["office"]
    if "suggestedLanguage" in body:
        if body["suggestedLanguage"] is not None and not isinstance(body["suggestedLanguage"], str):
            raise BadRequest("suggestedLanguage must be a string or null")
        blocker["suggestedLanguage"] = (body["suggestedLanguage"] or "").strip()[:4000] or None
    if "status" in body:
        if body["status"] not in ("open", "closed"):
            raise BadRequest("status must be open or closed")
        blocker["status"] = body["status"]
        if body["status"] == "closed" and before_status != "closed":
            blocker.update(closedAt=now_s, closedBy=actor)
        elif body["status"] == "open":
            blocker.update(closedAt=None, closedBy=None)
    store.contracts.put_blocker(contract_id, blocker)
    if blocker["status"] != before_status:
        action = "blocker_closed" if blocker["status"] == "closed" else "blocker_reopened"
        summary = (f"{display(actor)} closed an open item: {_clip(blocker['text'], 140)}" if action == "blocker_closed"
                   else f"{display(actor)} reopened an item: {_clip(blocker['text'], 140)}")
    else:
        action, summary = "blocker_edited", f"{display(actor)} edited an open item: {_clip(blocker['text'], 140)}"

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        refresh_refs(c)
        c["updatedAt"] = now_s
        entries.append(_entry(action, actor, summary, at=now_s, detail={"blockerId": blocker_id}))

    stored, _ = _commit(contract_id, change)
    return stored


def _clean_obligation_fields(body: dict[str, Any], creating: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if creating or "kind" in body:
        if body.get("kind") not in OBLIGATION_KINDS:
            raise BadRequest(f"kind must be one of: {', '.join(OBLIGATION_KINDS)}")
        out["kind"] = body["kind"]
    if creating or "title" in body:
        if not isinstance(body.get("title"), str) or not body["title"].strip():
            raise BadRequest("title is required")
        out["title"] = body["title"].strip()[:300]
    if "dueDate" in body:
        d = body["dueDate"]
        if d is not None and (not isinstance(d, str) or not _DATE_RE.match(d) or parse_iso(d) is None):
            raise BadRequest("dueDate must be a date (YYYY-MM-DD) or null")
        out["dueDate"] = d
    if "amount" in body:
        a = body["amount"]
        if a is not None and (isinstance(a, bool) or not isinstance(a, (int, float)) or a < 0):
            raise BadRequest("amount must be a non-negative number or null")
        out["amount"] = a
    if "status" in body:
        if body["status"] not in ("open", "done"):
            raise BadRequest("status must be open or done")
        out["status"] = body["status"]
    if "verified" in body:
        if not isinstance(body["verified"], bool):
            raise BadRequest("verified must be true or false")
        out["verified"] = body["verified"]
    return out


def add_obligation(contract_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
                   *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    unknown = set(body) - {"kind", "title", "dueDate", "amount"}
    if unknown:
        raise BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    fields = _clean_obligation_fields(body, True)
    c0 = store.contracts.get(contract_id)
    if c0 is None:
        raise NotFound("Contract not found")
    now_s = iso(_now(now))
    obl = {"id": uuid.uuid4().hex[:16], "kind": fields["kind"], "title": fields["title"],
           "dueDate": fields.get("dueDate"), "amount": fields.get("amount"), "status": "open",
           "source": "manual", "completedAt": None, "verified": True, "verifiedAt": now_s, "verifiedBy": actor}
    store.contracts.put_obligation(contract_id, c0["tenantId"], obl)

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        refresh_refs(c)
        c["updatedAt"] = now_s
        due = f", due {obl['dueDate']}" if obl.get("dueDate") else ""
        entries.append(_entry("obligation_added", actor, f"{display(actor)} added an obligation: {obl['title']}{due}.",
                              at=now_s, detail={"obligationId": obl["id"]}))

    stored, _ = _commit(contract_id, change)
    return stored


def edit_obligation(contract_id: str, obligation_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
                    *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    unknown = set(body) - {"status", "dueDate", "title", "amount", "verified"}
    if unknown:
        raise BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    fields = _clean_obligation_fields(body, False)
    obl = next((o for o in store.contracts.obligations(contract_id) if o.get("id") == obligation_id), None)
    if obl is None:
        raise NotFound("Obligation not found")
    c0 = store.contracts.get(contract_id)
    if c0 is None:
        raise NotFound("Contract not found")
    now_s = iso(_now(now))
    was = obl.get("status", "open")
    was_verified = bool(obl.get("verified")) if obl.get("verified") is not None else obl.get("source") != "sonar"
    before = dict(obl)
    obl.update(fields)
    if fields.get("verified") is True and not was_verified:
        obl["verifiedAt"], obl["verifiedBy"] = now_s, actor
    elif fields.get("verified") is False:
        obl["verifiedAt"], obl["verifiedBy"] = None, None
    if obl.get("status") == "done" and was != "done":
        obl["completedAt"] = now_s
    elif obl.get("status") == "open":
        obl["completedAt"] = None
    for k in ("notifiedDue", "notifiedOverdue"):
        if "dueDate" in fields:
            obl.pop(k, None)
    store.contracts.put_obligation(contract_id, c0["tenantId"], obl)
    if obl.get("status") == "done" and was != "done":
        action, summary = "obligation_done", f"{display(actor)} marked an obligation done: {obl['title']}."
    elif fields.get("verified") is True and not was_verified:
        action, summary = "obligation_verified", f"{display(actor)} verified an obligation Sonar found: {obl['title']}."
    else:
        action, summary = "field_updated", f"{display(actor)} updated an obligation: {obl['title']}."

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        refresh_refs(c)
        c["updatedAt"] = now_s
        detail_ = {"obligationId": obligation_id, "fields": sorted(fields),
                   "changes": {k: {"from": before.get(k), "to": v} for k, v in fields.items()}}
        entries.append(_entry(action, actor, summary, at=now_s, detail=detail_))

    stored, _ = _commit(contract_id, change)
    return stored


def replace_income(contract_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
                   *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("items"), list) or len(body["items"]) > 200:
        raise BadRequest("Body must be {\"items\": [...]} (max 200)")
    clean = []
    for i, it in enumerate(body["items"]):
        if not isinstance(it, dict):
            raise BadRequest(f"items[{i}] must be an object")
        if it.get("kind") not in INCOME_KINDS:
            raise BadRequest(f"items[{i}].kind must be one of: {', '.join(INCOME_KINDS)}")
        for num in ("amount", "pct"):
            v = it.get(num)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise BadRequest(f"items[{i}].{num} must be a non-negative number or null")
        d = it.get("expectedDate")
        if d is not None and (not isinstance(d, str) or parse_iso(d) is None):
            raise BadRequest(f"items[{i}].expectedDate must be a date or null")
        clean.append({"id": str(it.get("id") or uuid.uuid4().hex[:16])[:64], "kind": it["kind"],
                      "description": str(it.get("description") or "")[:500], "amount": it.get("amount"),
                      "pct": it.get("pct"), "expectedDate": d,
                      "source": it.get("source") if it.get("source") in ("sonar", "manual") else "manual"})
    if store.contracts.get(contract_id) is None:
        raise NotFound("Contract not found")
    store.contracts.replace_income(contract_id, clean)
    now_s = iso(_now(now))

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        c["updatedAt"] = now_s
        entries.append(_entry("field_updated", actor, f"{display(actor)} updated the licensing income ({_plural(len(clean), 'item')}).",
                              at=now_s, detail={"incomeItems": len(clean)}))

    stored, _ = _commit(contract_id, change)
    return stored


# ---------------------------------------------------------------------------
# Analysis → contract (intake, rescoring, revisions)
# ---------------------------------------------------------------------------


def load_classification(doc_id: str) -> dict[str, Any] | None:
    """Latest classification JSON of a document from the processed bucket."""
    from ..dynamodb import query_doc_versions
    from ..s3 import get_json

    versions = query_doc_versions(doc_id)
    if not versions:
        return None
    latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
    key = latest.get("classificationKey")
    bucket = app_settings.processed_bucket
    if not key or not bucket:
        return None
    try:
        data = get_json(bucket, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.classification_read_failed", docId=doc_id, error_type=type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def load_header_text(doc_id: str, max_lines: int = 60) -> str:
    """The first ~60 non-empty lines of a document's parsed text — where
    research agreements state their Huron number, Workday reference, PI,
    department and sponsor. Empty when unavailable."""
    from ..dynamodb import query_doc_versions
    from ..s3 import get_json

    try:
        versions = query_doc_versions(doc_id)
        if not versions:
            return ""
        latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
        key = latest.get("parsedKey")
        if not key or not app_settings.processed_bucket:
            return ""
        parsed = get_json(app_settings.processed_bucket, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.parsed_read_failed", docId=doc_id, error_type=type(exc).__name__)
        return ""
    text = str((parsed or {}).get("text") or "") if isinstance(parsed, dict) else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines])


_HURON_RE = re.compile(r"Huron\s+(?:Agreement|Record|Contract)?\s*(?:No\.?|Number|ID|#)?\s*[:#]?\s*([A-Z]{2,6}[-_]?\d[\w-]{2,40})", re.I)
_WORKDAY_RE = re.compile(r"Workday\s+(?:Ref(?:erence)?|ID|No\.?|Number|Award)\s*(?:No\.?|ID)?\s*[:#]?\s*([A-Z]{1,6}-[\w-]{2,40}|\w*\d[\w-]{2,40})", re.I)
_PI_RE = re.compile(r"Principal\s+Investigator\s*(?:\(PI\))?\s*[:\-]\s*((?:Dr\.?|Prof\.?|Professor)?\s*[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})")
_DEPT_RE = re.compile(r"\bDepartment(?:\s+of)?\s*[:\-]\s*([A-Z][\w&,.' -]{2,80}?)(?:\n|;|$|\.\s)")
# "Our" party, to tell it apart from the counterparty.
_US_RE = re.compile(r"\buniversity\b|\binstitution\b|\bcollege\b|\bour organi[sz]ation\b", re.I)
# Prose forms used in preambles: 'Dr. Priya Raman (the "Principal Investigator")',
# 'Department of Materials Science & Engineering, College of Engineering'.
_PI_PROSE_RE = re.compile(r"((?:Dr\.|Prof\.|Professor)\s+[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})\s*\(\s*(?:the\s+)?[\"“]?(?:Principal\s+Investigator|PI)\b")
_TITLED_WORDS = r"((?:[A-Z][\w'-]*|&|and|of)(?:\s+(?:[A-Z][\w'-]*|&|and|of))*)"
_DEPT_PROSE_RE = re.compile(r"\bDepartment of " + _TITLED_WORDS)
_COLLEGE_PROSE_RE = re.compile(r"\bCollege of " + _TITLED_WORDS)

# Labelled header lines ("Principal Investigator: Dr. Jane Smith"). Each label
# maps to the contract field it fills; the value is the rest of the line.
_HEADER_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("huronRecordId", re.compile(r"^Huron\s+(?:Agreement|Record|Contract)\s*(?:No\.?|Number|ID|#)?\s*[:#]?\s*(.+)$", re.I)),
    ("workdayRef", re.compile(r"^Workday\s+(?:Ref(?:erence)?|ID|Award(?:\s+ID)?)\.?\s*(?:No\.?)?\s*[:#]?\s*(.+)$", re.I)),
    ("piName", re.compile(r"^(?:Principal\s+Investigator(?:\s*\(PI\))?|PI)\s*[:\-]\s*(.+)$", re.I)),
    ("department", re.compile(r"^(?:[A-Z][\w.]*\s+)?(?:Department|Dept\.?)\s*[:\-]\s*(.+)$", re.I)),
    ("college", re.compile(r"^College\s*[:\-]\s*(.+)$", re.I)),
    ("sponsor", re.compile(r"^Sponsor(?:\s+Name)?\s*[:\-]\s*(.+)$", re.I)),
    ("licensee", re.compile(r"^Licensee(?:\s+Name)?\s*[:\-]\s*(.+)$", re.I)),
    ("requestedDate", re.compile(r"^(?:Requested\s+Date|Date\s+Requested|Request\s+Date|Date\s+Received)\s*[:\-]\s*(.+)$", re.I)),
    ("effectiveDate", re.compile(r"^Effective\s+Date\s*[:\-]\s*(.+)$", re.I)),
)
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{1,99}")


def _to_date(text: str) -> str | None:
    """'2026-10-01', '10/01/2026', 'October 1, 2026' → '2026-10-01'."""
    text = text.strip().rstrip(".")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m and parse_iso(m.group(0)) else None


def parse_header(text: str) -> dict[str, Any]:
    """Labelled fields from a document's opening lines. Only labels that are
    present appear in the result."""
    out: dict[str, Any] = {}
    for line in (text or "").splitlines()[:60]:
        line = line.strip().strip("*").strip()
        if not line:
            continue
        for field, pattern in _HEADER_LABELS:
            if field in out:
                continue
            m = pattern.match(line)
            if not m:
                continue
            value = m.group(1).strip().strip(".;,").strip()
            if not value:
                continue
            if field in ("huronRecordId", "workdayRef"):
                tok = _ID_TOKEN.search(value)
                if tok:
                    out[field] = tok.group(0)
            elif field in ("requestedDate", "effectiveDate"):
                d = _to_date(value)
                if d:
                    out[field] = d
            else:
                out[field] = re.split(r"\s{2,}|\t", value)[0][:200]
            break
    return out


def _texts(classification: dict[str, Any]) -> str:
    parts = [str(classification.get("title") or ""), str(classification.get("summary") or "")]
    for c in (classification.get("clauses") or [])[:400]:
        if isinstance(c, dict):
            parts.append(str(c.get("body") or c.get("text") or "")[:4000])
    return "\n".join(parts)


def infer_fields(meta: dict[str, Any], classification: dict[str, Any] | None,
                 header_text: str = "") -> dict[str, Any]:
    """What Sonar can tell about a contract from its document: the analysis
    (type, parties, value, dates) and the labelled header lines of the parsed
    text (Huron number, Workday reference, PI, department, sponsor, dates).
    A header label wins over a pattern found elsewhere in the text."""
    cls = classification or {}
    m = _m()
    try:
        agreement_type = m.infer_agreement_type(meta, cls) or "other"
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.infer_type_failed", error_type=type(exc).__name__)
        agreement_type = "other"
    try:
        direction = m.infer_direction(agreement_type, cls) or "incoming"
    except Exception:  # noqa: BLE001
        direction = "incoming"
    header = parse_header(header_text)
    parties = [str(p) for p in (cls.get("parties") or meta.get("parties") or []) if p]
    others = [p for p in parties if not _US_RE.search(p)]
    counterparty = (others or [None])[0]
    if agreement_type in ("license", "option") and header.get("licensee"):
        counterparty = header["licensee"]
    elif header.get("sponsor") and not counterparty:
        counterparty = header["sponsor"]
    text = _texts(cls) if cls else ""
    huron = _HURON_RE.search(text)
    workday = _WORKDAY_RE.search(text)
    pi = header.get("piName")
    if not pi:
        for person_ in cls.get("personnel") or []:
            if isinstance(person_, dict) and re.search(r"principal investigator|\bPI\b", str(person_.get("role") or ""), re.I):
                pi = person_.get("name")
                break
    prose = f"{header_text}\n{text}"
    if not pi:
        mpi = _PI_RE.search(prose) or _PI_PROSE_RE.search(prose)
        pi = mpi.group(1).strip() if mpi else None
    dept = header.get("department")
    if not dept:
        md = _DEPT_RE.search(prose)
        dept = md.group(1).strip() if md else None
    if not dept:
        md = _DEPT_PROSE_RE.search(prose)
        dept = f"Department of {md.group(1).strip()}" if md else None
    college = header.get("college")
    if not college:
        mc = _COLLEGE_PROSE_RE.search(prose)
        college = f"College of {mc.group(1).strip()}" if mc else None
    sponsor = header.get("sponsor") or (counterparty if agreement_type in ("sponsored_research", "grant", "collaboration") else None)
    value = meta.get("contractValue")
    if value is None:
        value = meta.get("newTotalValue")
    return {
        "title": meta.get("title") or "",
        "docType": meta.get("docType") or "OTHER",
        "agreementType": agreement_type,
        "direction": direction,
        "counterparty": counterparty,
        "sponsor": sponsor,
        "piName": (pi or None) and str(pi)[:200],
        "department": (dept or None) and str(dept)[:200],
        "college": college,
        "requestedDate": header.get("requestedDate"),
        "effectiveDate": header.get("effectiveDate") or meta.get("effectiveDate") or meta.get("startDate"),
        "termEndDate": meta.get("termEndDate"),
        "extractedValue": float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None,
        "currency": meta.get("currency"),
        "overallRisk": meta.get("overallRisk"),
        "huronRecordId": header.get("huronRecordId") or (huron.group(1) if huron else None),
        "workdayRef": header.get("workdayRef") or (workday.group(1) if workday else None),
    }


def _apply_inferred(c: dict[str, Any], inferred: dict[str, Any]) -> None:
    """Inferred values never replace what a person entered (``userFields``),
    and a missing inference never erases a known value."""
    user = set(c.get("userFields") or [])
    for k, v in inferred.items():
        if k in user:
            continue
        if v is None and c.get(k) is not None and k not in ("extractedValue", "overallRisk"):
            continue
        c[k] = v
    if c.get("workdayRef") and "workdayMatch" not in user and c.get("workdayMatch") in (None, "unmatched"):
        c["workdayMatch"] = "auto"


def new_contract(meta: dict[str, Any], *, now_s: str, analysis_ready: bool,
                 classification: dict[str, Any] | None = None, fields: dict[str, Any] | None = None,
                 header_text: str = "") -> dict[str, Any]:
    """A fresh contract item for a document (the docId becomes the contractId)."""
    doc_id = meta["docId"]
    c: dict[str, Any] = {
        "contractId": doc_id, "tenantId": meta.get("tenantId"), "currentDocId": doc_id,
        "versionDocIds": [doc_id], "versionRounds": {doc_id: 1}, "versionCounts": {},
        "createdAt": now_s, "updatedAt": now_s, "stageEnteredAt": now_s,
        "state": "intake", "stage": "review" if analysis_ready else "draft",
        "owner": None, "signedAt": None, "rounds": 0,
        "analysisStatus": meta.get("status") or "PENDING",
        "manualValue": None, "expectedValue": None, "requestedDate": None, "college": None,
        "matrix": None, "reviewIndex": {}, "openBlockerRefs": [], "obligationDueDates": [],
        "workdayMatch": "unmatched", "syncConflicts": [],
        "routing": {"required": [], "approvals": [], "reasons": [], "escalated": []},
        "rejection": None, "signature": None, "userFields": [],
    }
    _apply_inferred(c, infer_fields(meta, classification, header_text))
    if fields:
        for k, v in fields.items():
            c[k] = v
        c["userFields"] = sorted(fields)
    return c


def _auto_assign(c: dict[str, Any], cfg: dict[str, Any], now_s: str) -> list[dict[str, Any]]:
    if c.get("owner"):
        return []
    who, why = auto_assignee(c, cfg)
    if not who:
        return []
    c["owner"] = who
    reason = f" ({why})" if why else ""
    return [_entry("assigned", None, f"Sonar assigned this to {display(who)}{reason}.", at=now_s,
                   detail={"owner": who, "auto": True})]


def create_from_document(meta: dict[str, Any], actor: dict[str, Any] | None, *, fields: dict[str, Any] | None = None,
                         now: datetime | None = None, source: str = "upload",
                         cfg: dict[str, Any] | None = None, classification: dict[str, Any] | None = None,
                         header_text: str = "") -> tuple[dict[str, Any], bool]:
    """Create a contract for a document that has none (POST /contracts, lazy
    creation of library documents, intake). (contract, created). An existing
    contract is returned untouched — callers patch it if they need to."""
    now_s = iso(_now(now))
    ready = str(meta.get("status") or "").upper() == "READY"
    c = new_contract(meta, now_s=now_s, analysis_ready=ready, fields=fields, classification=classification,
                     header_text=header_text)
    cfg = cfg or get_settings(c["tenantId"])
    entries = _auto_assign(c, cfg, now_s)
    if c.get("owner") and ready:
        c["state"] = "in_review"
    apply_routing(c, cfg)
    if not store.contracts.create(c):
        existing = store.contracts.get(meta["docId"])
        return existing or c, False
    words = {"upload": f"{display(actor)} added this agreement to Govern.",
             "library": "Sonar added this agreement to Govern from the document library.",
             "intake": "Sonar added this agreement to Govern after reading it."}
    intake = _entry("intake", actor, words.get(source, words["upload"]), to_stage=c["stage"], at=now_s,
                    detail={"docId": meta["docId"], "source": source, **_money_detail(c),
                            "matrixVersion": None, "counts": None, "deviatingClauseTypes": []})
    write_activity(c, [intake] + entries)
    mirror_lifecycle(c)
    if c.get("huronRecordId") or c.get("workdayRef"):
        sync_external_ids(c)
    c["rev"] = 1
    return c, True


def _norm_blockers(raw: list[dict[str, Any]], now_s: str) -> list[dict[str, Any]]:
    out, seen = [], set()
    for i, b in enumerate(raw or []):
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or f"sonar-{b.get('clauseType') or i}")[:64]
        if bid in seen:
            continue
        seen.add(bid)
        out.append({"id": bid, "text": str(b.get("text") or "")[:2000], "clauseType": b.get("clauseType"),
                    "office": b.get("office"), "suggestedLanguage": b.get("suggestedLanguage"), "source": "sonar",
                    "status": "open", "createdAt": b.get("createdAt") or now_s, "createdBy": None,
                    "closedAt": None, "closedBy": None})
    return out


def _replace_sonar_blockers(contract_id: str, fresh: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """Swap the Sonar blockers for a new review's. Reviewer blockers are not
    touched; a Sonar blocker that is still raised keeps its id, its creation
    time and whether someone closed it. (all blockers, added, resolved)."""
    existing = store.contracts.blockers(contract_id)
    old_sonar = {b["id"]: b for b in existing if b.get("source") == "sonar"}
    fresh_ids = {b["id"] for b in fresh}
    resolved = 0
    for bid, b in old_sonar.items():
        if bid not in fresh_ids:
            store.contracts.delete_blocker(contract_id, bid)
            resolved += 1 if b.get("status", "open") == "open" else 0
    added = 0
    for b in fresh:
        old = old_sonar.get(b["id"])
        if old:
            b.update(createdAt=old.get("createdAt") or b["createdAt"], status=old.get("status", "open"),
                     closedAt=old.get("closedAt"), closedBy=old.get("closedBy"))
        else:
            added += 1
        store.contracts.put_blocker(contract_id, b)
    reviewer = [b for b in existing if b.get("source") != "sonar"]
    return reviewer + fresh, added, resolved


def _sonar_income(c: dict[str, Any], classification: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        raw = _m().extract_income(classification, c.get("agreementType") or "other") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.income_extract_failed", error_type=type(exc).__name__)
        return []
    out = []
    for i, it in enumerate(raw):
        if isinstance(it, dict) and it.get("kind") in INCOME_KINDS:
            out.append({"id": str(it.get("id") or f"sonar-{i}")[:64], "kind": it["kind"],
                        "description": str(it.get("description") or "")[:500], "amount": it.get("amount"),
                        "pct": it.get("pct"), "expectedDate": it.get("expectedDate"), "source": "sonar"})
    return out


def _sonar_obligations(c: dict[str, Any], signed_at: str, now_s: str) -> list[dict[str, Any]]:
    doc_id = c.get("currentDocId") or c["contractId"]
    classification = load_classification(doc_id) or {}
    try:
        raw = _m().extract_obligations(classification, c.get("agreementType") or "other", signed_at) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.obligation_extract_failed", error_type=type(exc).__name__)
        return []
    out = []
    for i, o in enumerate(raw):
        if not isinstance(o, dict):
            continue
        out.append({"id": str(o.get("id") or f"sonar-{i}")[:64],
                    "kind": o.get("kind") if o.get("kind") in OBLIGATION_KINDS else "other",
                    "title": str(o.get("title") or "Obligation")[:300],
                    "dueDate": (str(o["dueDate"])[:10] if o.get("dueDate") else None), "amount": o.get("amount"),
                    "status": "open", "source": "sonar", "completedAt": None,
                    # AI output waits for a person to confirm it (human in the loop).
                    "verified": False, "verifiedAt": None, "verifiedBy": None})
    return out


def _replace_sonar_obligations(c: dict[str, Any], fresh: list[dict[str, Any]]) -> None:
    for o in store.contracts.obligations(c["contractId"]):
        if o.get("source") == "sonar":
            store.contracts.delete_obligation(c["contractId"], o["id"])
    for o in fresh:
        store.contracts.put_obligation(c["contractId"], c["tenantId"], o)


def _review_index(review: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cl in review.get("clauses") or []:
        ct = cl.get("clauseType")
        if not ct:
            continue
        out[ct] = {"label": cl.get("label"), "tier": cl.get("tier"), "hasFallback": bool(cl.get("fallback")),
                   "office": cl.get("escalationOffice"), "suggestedLanguage": cl.get("suggestedLanguage"),
                   "beneficial": bool(cl.get("beneficial"))}
    return out


def _counts_text(counts: dict[str, Any]) -> str:
    need = sum(int(counts.get(k) or 0) for k in ("deviates", "unacceptable", "missing"))
    good = int(counts.get("within") or 0) + int(counts.get("fallback") or 0)
    parts = [f"{_plural(need, 'clause')} {'needs' if need == 1 else 'need'} attention" if need else "nothing outside the matrix",
             f"{good} within the matrix or its fallback"]
    if counts.get("beneficial"):
        parts.append(f"{counts['beneficial']} favourable to you")
    return "; ".join(parts)


def rescore(contract_id: str, actor: dict[str, Any] | None, *, doc_id: str | None = None,
            reason: str = "manual", now: datetime | None = None, cfg: dict[str, Any] | None = None,
            classification: dict[str, Any] | None = None, doc_meta: dict[str, Any] | None = None,
            assign: bool = False, header_text: str | None = None) -> dict[str, Any]:
    """Grade the contract's document against the CURRENT matrix version.

    ``reason``: ``manual`` (POST /rescore) · ``intake`` (first analysis) ·
    ``reanalysed`` (the same document was analysed again) · ``revision`` (a
    new counterparty version: it becomes current and the contract goes back
    to ``in_review``; ``rounds`` is unchanged).
    """
    from ..dynamodb import get_doc_meta

    c0 = store.contracts.get(contract_id)
    if c0 is None:
        raise NotFound("Contract not found")
    doc_id = doc_id or c0.get("currentDocId") or contract_id
    meta = store.clean_item(doc_meta or get_doc_meta(doc_id)) or {}
    if str(meta.get("status") or "").upper() != "READY":
        raise NotReady("Sonar has not finished reading this document yet.")
    classification = classification if classification is not None else load_classification(doc_id)
    if classification is None:
        raise NotReady("The analysis of this document was not found.")
    now_dt = _now(now)
    now_s = iso(now_dt)
    cfg = cfg or get_settings(c0["tenantId"])
    matrix = store.config.current_matrix(c0["tenantId"])

    # Agreement type may be re-inferred from the newly read document first.
    inferred = infer_fields(meta, classification, header_text if header_text is not None else load_header_text(doc_id))
    agreement_type = c0.get("agreementType") if "agreementType" in (c0.get("userFields") or []) \
        else (inferred.get("agreementType") or c0.get("agreementType") or "other")
    review = _m().review_document(classification.get("clauses") or [], agreement_type, matrix,
                                  doc_id=doc_id, now_iso=now_s)
    review = dict(review)
    try:
        raw_blockers = _m().sonar_blockers(review) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("govern.sonar_blockers_failed", error_type=type(exc).__name__)
        raw_blockers = []
    blockers, added, resolved = _replace_sonar_blockers(contract_id, _norm_blockers(raw_blockers, now_s))
    store.contracts.put_review(contract_id, c0["tenantId"], review)
    income_c = dict(c0, agreementType=agreement_type)
    sonar_income = _sonar_income(income_c, classification)
    manual_income = [i for i in store.contracts.income(contract_id) if i.get("source") != "sonar"]
    store.contracts.replace_income(contract_id, manual_income + sonar_income)
    counts = dict(review.get("counts") or {})

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        _apply_inferred(c, inferred)
        c["agreementType"] = agreement_type
        c["analysisStatus"] = "READY"
        c["matrix"] = {"version": review.get("matrixVersion", matrix.get("version")),
                       "reviewedAt": review.get("reviewedAt") or now_s, "counts": counts}
        c["reviewIndex"] = _review_index(review)
        c["reviewedDocId"] = doc_id
        c["reviewedDocVersion"] = int(meta.get("latestVersion") or 0)
        c.setdefault("versionCounts", {})[doc_id] = counts
        refresh_refs(c, blockers=blockers)
        c["updatedAt"] = now_s
        if reason == "revision":
            versions = list(c.get("versionDocIds") or [c["contractId"]])
            if doc_id not in versions:
                versions.append(doc_id)
            c["versionDocIds"] = versions
            c.setdefault("versionRounds", {})[doc_id] = int(c.get("rounds") or 0) + 1
            c["currentDocId"] = doc_id
            c.pop("pendingDocId", None)
            routing = c.setdefault("routing", {"required": [], "approvals": [], "reasons": [], "escalated": []})
            routing["approvals"] = []
            fs, ts = c.get("stage"), c.get("stage")
            if c.get("state") not in ("signed", "active", "closed"):
                fs, ts = _enter_state(c, "in_review" if c.get("owner") else "intake", now_s)
            entries.append(_entry("revision_received", None,
                                  f"A revised version arrived from the {counterparty_noun(c)}; it is back in review.",
                                  from_stage=fs, to_stage=ts, at=now_s, detail={"docId": doc_id}))
        elif c.get("state") == "intake" and c.get("stage") == "draft":
            if assign:
                entries.extend(_auto_assign(c, cfg, now_s))
            fs, ts = _enter_state(c, "in_review" if c.get("owner") else "intake", now_s, analysis_ready=True)
            entries.append(_entry("stage_changed", None, "Sonar finished reading this agreement; it is now in review.",
                                  from_stage=fs, to_stage=ts, at=now_s))
        elif assign:
            entries.extend(_auto_assign(c, cfg, now_s))
            if c.get("owner") and c.get("state") == "intake":
                _enter_state(c, "in_review", now_s)
        apply_routing(c, cfg)
        version = c["matrix"]["version"]
        who = display(actor, "Sonar")
        lead = {"manual": f"{who} rescored this against matrix version {version}",
                "intake": f"Sonar reviewed this against matrix version {version}",
                "reanalysed": f"Sonar re-read this agreement and reviewed it against matrix version {version}",
                "revision": f"Sonar reviewed the revised version against matrix version {version}"}[reason]
        tail = ""
        if resolved:
            tail += f" {_plural(resolved, 'open item')} resolved."
        if added:
            tail += f" {_plural(added, 'new open item')}."
        entries.append(_entry("rescored", actor, f"{lead}: {_counts_text(counts)}.{tail}", at=now_s,
                              detail={"docId": doc_id, "matrixVersion": version, "counts": counts,
                                      "reason": reason, "blockersAdded": added, "blockersResolved": resolved,
                                      "agreementType": c.get("agreementType"),
                                      "deviatingClauseTypes": sorted({cl.get("clauseType") for cl in review.get("clauses") or []
                                                                      if cl.get("tier") in ("deviates", "unacceptable")
                                                                      and cl.get("clauseType")})}))
        return None

    stored, _ = _commit(contract_id, change)
    sync_external_ids(stored, c0)
    return stored


# ---------------------------------------------------------------------------
# Time-driven (sweeper)
# ---------------------------------------------------------------------------


def mark_overdue(contract_id: str, *, now: datetime | None = None, cfg: dict[str, Any] | None = None) -> bool:
    """Write ONE ``overdue`` entry per stage visit once the SLA turns amber or
    red. True when an entry was written."""
    now_dt = _now(now)
    now_s = iso(now_dt)

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        conf = cfg or get_settings(c["tenantId"])
        timing = sla(c, conf, now_dt)
        if timing["slaStatus"] not in ("amber", "red"):
            return False
        if c.get("overdueNotifiedFor") == c.get("stageEnteredAt"):
            return False
        c["overdueNotifiedFor"] = c.get("stageEnteredAt")
        target = timing["targetDays"]
        target_words = f"{target:g}-day" if isinstance(target, (int, float)) else str(target)
        entries.append(_entry("overdue", None,
                              f"This has been in {c.get('stage')} for {_plural(timing['daysInStage'], 'day')}, "
                              f"past its {target_words} target. {waiting_on(c)['label']}.",
                              at=now_s, detail={"slaStatus": timing["slaStatus"], "daysInStage": timing["daysInStage"],
                                                "targetDays": timing["targetDays"], "stage": c.get("stage"),
                                                "owner": c.get("owner")}))
        return None

    try:
        _, entries = _commit(contract_id, change)
    except NotFound:
        return False
    return bool(entries)


def note_obligation(c: dict[str, Any], obligation: dict[str, Any], kind: str, now: datetime | None = None) -> None:
    """An obligation is due within 14 days (``kind="due"``) or overdue."""
    now_s = iso(_now(now))
    title = obligation.get("title") or "An obligation"
    due = obligation.get("dueDate")
    summary = (f"{title} is due on {due}." if kind == "due" else f"{title} was due on {due} and is overdue.")
    write_activity(c, [_entry("overdue", None, summary, at=now_s,
                              detail={"obligationId": obligation.get("id"), "kind": f"obligation_{kind}",
                                      "dueDate": due, "owner": c.get("owner")})])


def record_system_entry(c: dict[str, Any], action: str, summary: str, detail: dict[str, Any] | None = None,
                        actor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append an entry that does not change state (sync, conflict,
    notification_sent)."""
    entry = _entry(action, actor, summary, detail=detail)
    write_activity(c, [entry])
    return entry


# ---------------------------------------------------------------------------
# Systems of record (connectors)
# ---------------------------------------------------------------------------

_MAX_SYNC_CONFLICTS = 20


def sync_fields(contract_id: str, system: str, system_label: str, values: dict[str, Any],
                owned: tuple[str, ...] | list[str], *, now: datetime | None = None,
                cfg: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    """Apply values read from a system of record. (contract, conflicts).

    SYSTEM OF RECORD WINS: for a field the system owns, its value replaces
    Govern's; if Govern held a different non-empty value that is a conflict —
    recorded in ``syncConflicts`` and as a ``conflict`` entry. A field the
    system does not own only fills a gap (Govern keeps its own value). Fields
    that are not Contract fields are kept under ``external.<system>``.
    """
    now_s = iso(_now(now))
    conflicts_holder: list[int] = [0]

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        conf = cfg or get_settings(c["tenantId"])
        changed: dict[str, dict[str, Any]] = {}
        conflicts: list[dict[str, Any]] = []
        extra = dict((c.get("external") or {}).get(system) or {})
        for field, value in values.items():
            if value is None or value == "":
                continue
            if field not in PATCH_FIELDS:
                if extra.get(field) != value:
                    extra[field] = value
                    changed[field] = {"from": None, "to": value}
                continue
            current = c.get(field)
            if current == value:
                continue
            if field in owned:
                if current not in (None, ""):
                    conflicts.append({"field": field, "govern": current, "recordValue": value,
                                      "system": system, "at": now_s})
                c[field] = value
                changed[field] = {"from": current, "to": value}
            elif current in (None, ""):
                c[field] = value
                changed[field] = {"from": None, "to": value}
        if not changed:
            return False
        c.setdefault("external", {})[system] = extra
        if "workdayRef" in changed and c.get("workdayMatch") != "manual":
            c["workdayMatch"] = "auto"
        if conflicts:
            c["syncConflicts"] = (list(c.get("syncConflicts") or []) + conflicts)[-_MAX_SYNC_CONFLICTS:]
        c["updatedAt"] = now_s
        apply_routing(c, conf)
        plain = [ch for f, ch in changed.items() if f not in {x["field"] for x in conflicts}]
        if plain:
            names = [_FIELD_WORDS.get(f, f) for f in changed if f not in {x["field"] for x in conflicts}]
            plain_changes = {f: changed[f] for f in changed if f not in {x["field"] for x in conflicts}}
            entries.append(_entry("sync", None, f"{system_label} updated the {', '.join(names)}.", at=now_s,
                                  detail={"system": system, "fields": sorted(plain_changes), "changes": plain_changes}))
        for x in conflicts:
            word = _FIELD_WORDS.get(x["field"], x["field"])
            entries.append(_entry("conflict", None,
                                  f"{system_label} and Govern disagreed on the {word}; {system_label}'s value "
                                  f"({_field_text(x['field'], x['recordValue'], c)}) was kept "
                                  f"(Govern had {_field_text(x['field'], x['govern'], c)}).",
                                  at=now_s, detail=x))
        conflicts_holder[0] = len(conflicts)
        return None

    stored, _ = _commit(contract_id, change)
    return stored, conflicts_holder[0]


def set_workday_unmatched(contract_id: str, *, now: datetime | None = None) -> bool:
    """A Workday pull found no record for this contract's reference: mark it
    for the manual match screen (a manual match is never undone)."""
    now_s = iso(_now(now))

    def change(c: dict[str, Any], entries: list[dict[str, Any]]) -> Any:
        if c.get("workdayMatch") in ("unmatched", "manual"):
            return False
        c["workdayMatch"] = "unmatched"
        c["updatedAt"] = now_s
        entries.append(_entry("sync", None, "Workday has no record with this reference; it needs a manual match.",
                              at=now_s, detail={"system": "workday", "workdayRef": c.get("workdayRef")}))
        return None

    try:
        _, entries = _commit(contract_id, change)
    except NotFound:
        return False
    return bool(entries)
