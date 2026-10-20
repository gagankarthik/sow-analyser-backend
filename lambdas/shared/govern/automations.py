"""Automations — rules that govern without grinding (docs/GOVERN_API.md
"Automations"): WHEN a trigger happens, ONLY IF every condition holds, THEN
run the actions.

Storage. govern-config ``T#<tenant>#AUTOMATIONS / A#<id>`` (store.config),
optimistic ``rev``, at most ``MAX_RULES`` per tenant. ``runCount`` and
``lastRunAt`` are kept with an atomic UpdateItem that never bumps ``rev``.

Triggers.
* Time (govern-sweeper, hourly): ``in_stage_for {stage, days}``,
  ``obligation_due_in {days}``, ``term_ends_in {days}``.
* Events (govern-stream, on every new activity entry): ``contract_received``,
  ``stage_changed {to?}``, ``sent_back``, ``blocker_added {tier?}``, ``signed``.

Conditions (all must hold): ``agreementType in``, ``direction is``,
``value gte|lte``, ``department is``, ``owner set|unset``,
``counterparty contains``.

Actions: ``notify`` (owner, an office's reviewers, or named emails) — an
``automation_notify`` entry the notifier delivers by SES / Teams;
``escalate`` and ``assign`` — the workflow actions, as the system actor
"Automation: <rule name>"; ``comment``. Every entry an automation writes
carries ``detail.automation = {ruleId, ruleName}``, and entries so tagged
never trigger a rule, so automations cannot loop or chain.

Idempotency (govern-metrics markers, scope ``AUTO#<tenant>``):
* a firing has a KEY: ``<contractId>#<stageEnteredAt>`` for a stage visit,
  ``<contractId>#term#<termEndDate>``, ``<contractId>#obl#<id>#<dueDate>``,
  or ``<contractId>#<entryId>`` for an event — so a time rule fires once per
  qualifying episode and an event rule once per activity entry;
* each action claims ``<ruleId>#<key>#<n>`` before it runs: a retry (stream
  re-delivery, a re-run sweep) never repeats an action that ran, and an
  infrastructure failure releases its claim so the retry does it;
* ``<ruleId>#<key>#run`` marks the firing complete (and counts it). The
  sweeper reads these with BatchGetItem first, so an episode that has fired
  costs a read per hour, not a write.

An action the contract's state does not allow (escalating a signed
agreement) is skipped and logged; it is not retried.
"""
from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings as app_settings
from ..logger import get_logger
from . import store, workflow
from .store import iso, parse_iso
from .workflow import BadRequest, GovernError, NotFound

log = get_logger("blue-iq.govern.automations")

FEATURE = "automations"
MAX_RULES = 50
MAX_NAME = 120
MAX_CONDITIONS = 10
MAX_ACTIONS = 5
MAX_EMAILS = 20
MAX_LIST = 50
MAX_TEXT = 200
MAX_MESSAGE = 500
MAX_COMMENT = 2000
MAX_DAYS = 3650

TIME_TRIGGERS = ("in_stage_for", "obligation_due_in", "term_ends_in")
EVENT_TRIGGERS = ("contract_received", "stage_changed", "sent_back", "blocker_added", "signed")
TRIGGERS = TIME_TRIGGERS + EVENT_TRIGGERS
BLOCKER_TIERS = ("unacceptable", "deviates")
ACTION_TYPES = ("notify", "escalate", "assign", "comment")
NOTIFY_TO = ("owner", "office", "emails")
# field → the operators it takes
CONDITION_OPS: dict[str, tuple[str, ...]] = {
    "agreementType": ("in",), "direction": ("is",), "value": ("gte", "lte"),
    "department": ("is",), "owner": ("set", "unset"), "counterparty": ("contains",),
}
PLACEHOLDERS = ("contract", "stage", "days", "due", "owner", "counterparty", "rule")

# trigger type → (allowed keys besides "type", required keys)
_TRIGGER_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "in_stage_for": (frozenset({"stage", "days"}), frozenset({"stage", "days"})),
    "obligation_due_in": (frozenset({"days"}), frozenset({"days"})),
    "term_ends_in": (frozenset({"days"}), frozenset({"days"})),
    "contract_received": (frozenset(), frozenset()),
    "stage_changed": (frozenset({"to"}), frozenset()),
    "sent_back": (frozenset(), frozenset()),
    "blocker_added": (frozenset({"tier"}), frozenset()),
    "signed": (frozenset(), frozenset()),
}
# action type → (allowed keys besides "type", required keys)
_ACTION_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "notify": (frozenset({"to", "office", "emails", "message"}), frozenset({"to"})),
    "escalate": (frozenset({"office"}), frozenset({"office"})),
    "assign": (frozenset({"owner"}), frozenset()),
    "comment": (frozenset({"text"}), frozenset({"text"})),
}
_CREATE_KEYS = frozenset({"name", "enabled", "trigger", "conditions", "actions"})
_PATCH_KEYS = _CREATE_KEYS | {"rev"}
_RULE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
# Activity actions an event trigger can match (besides any stage change).
_EVENT_ACTIONS = frozenset({"intake", "sent_back", "blocker_added", "rescored", "signed"})
_TIME_MARKER_DAYS = 400       # a stage visit / term / obligation episode can last long
_EVENT_MARKER_DAYS = 30
_NOT_FOUND = "Automation not found"


class LimitReached(BadRequest):
    code = "limit"


class StaleRule(GovernError):
    """PATCH sent a ``rev`` that is no longer current (HTTP 409)."""
    status = 409
    code = "conflict"


def _scope(tenant_id: str) -> str:
    return f"AUTO#{tenant_id}"


def system_name(rule: dict[str, Any]) -> str:
    """The actor automated changes are attributed to."""
    return f"Automation: {rule.get('name') or 'rule'}"


def _tag(rule: dict[str, Any]) -> dict[str, Any]:
    return {"automation": {"ruleId": rule.get("id"), "ruleName": rule.get("name")}}


def enabled_for(tenant_id: str, cfg: dict[str, Any] | None = None) -> bool:
    """Is the feature on for this deployment AND the organization's modules?"""
    if not app_settings.feature_enabled(FEATURE):
        return False
    try:
        mods = ((cfg or workflow.get_settings(tenant_id)).get("organization") or {}).get("enabledModules")
    except Exception:  # noqa: BLE001 — a settings read failure must not switch rules on
        return False
    return not isinstance(mods, list) or FEATURE in mods


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _unknown(obj: dict[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = set(obj) - set(allowed)
    if extra:
        raise BadRequest(f"Unknown field(s) in {where}: {', '.join(sorted(map(str, extra)))}")


def _obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BadRequest(f"{where} must be an object")
    return value


def _days(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value != int(value) or not minimum <= value <= MAX_DAYS:
        raise BadRequest(f"{where} must be a whole number of days ({minimum}–{MAX_DAYS})")
    return int(value)


def _text(value: Any, where: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"{where} is required")
    text = " ".join(value.split()) if limit <= MAX_TEXT else value.strip()
    if len(text) > limit:
        raise BadRequest(f"{where} must be at most {limit} characters")
    return text


def _office(value: Any, where: str) -> str:
    if value not in workflow.offices():
        raise BadRequest(f"{where} must be one of: {', '.join(workflow.offices())}")
    return value


def _clean_trigger(raw: Any) -> dict[str, Any]:
    t = _obj(raw, "trigger")
    kind = t.get("type")
    if kind not in TRIGGERS:
        raise BadRequest(f"trigger.type must be one of: {', '.join(TRIGGERS)}")
    allowed, required = _TRIGGER_KEYS[kind]
    _unknown(t, allowed | {"type"}, "trigger")
    missing = required - set(t)
    if missing:
        raise BadRequest(f"trigger needs {', '.join(sorted(missing))} for {kind}")
    out: dict[str, Any] = {"type": kind}
    if "stage" in allowed:
        if t["stage"] not in workflow.STAGES:
            raise BadRequest(f"trigger.stage must be one of: {', '.join(workflow.STAGES)}")
        out["stage"] = t["stage"]
    if "days" in allowed:
        out["days"] = _days(t["days"], "trigger.days", minimum=1 if kind == "in_stage_for" else 0)
    if kind == "stage_changed" and t.get("to") is not None:
        if t["to"] not in workflow.STAGES:
            raise BadRequest(f"trigger.to must be one of: {', '.join(workflow.STAGES)}, or null")
        out["to"] = t["to"]
    if kind == "blocker_added" and t.get("tier") is not None:
        if t["tier"] not in BLOCKER_TIERS:
            raise BadRequest("trigger.tier must be unacceptable, deviates or null")
        out["tier"] = t["tier"]
    return out


def _clean_condition(raw: Any, i: int) -> dict[str, Any]:
    where = f"conditions[{i}]"
    c = _obj(raw, where)
    field, op = c.get("field"), c.get("op")
    if field not in CONDITION_OPS:
        raise BadRequest(f"{where}.field must be one of: {', '.join(CONDITION_OPS)}")
    if op not in CONDITION_OPS[field]:
        raise BadRequest(f"{where}.op for {field} must be one of: {', '.join(CONDITION_OPS[field])}")
    if field == "owner":
        _unknown(c, {"field", "op"}, where)
        return {"field": field, "op": op}
    _unknown(c, {"field", "op", "value"}, where)
    if "value" not in c:
        raise BadRequest(f"{where}.value is required")
    v = c["value"]
    if field == "agreementType":
        types = set(workflow.agreement_types())
        if not isinstance(v, list) or not v or len(v) > MAX_LIST or any(x not in types for x in v):
            raise BadRequest(f"{where}.value must list known agreement types")
        value: Any = list(dict.fromkeys(v))
    elif field == "direction":
        if v not in workflow.DIRECTIONS:
            raise BadRequest(f"{where}.value must be incoming or outgoing")
        value = v
    elif field == "value":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise BadRequest(f"{where}.value must be a number of 0 or more")
        value = int(v) if v == int(v) else float(v)
    else:
        value = _text(v, f"{where}.value")
    return {"field": field, "op": op, "value": value}


def _clean_person(raw: Any, where: str) -> dict[str, Any]:
    p = _obj(raw, where)
    _unknown(p, {"email", "name"}, where)
    return workflow.parse_person(p, where)


def _check_placeholders(text: str, where: str) -> None:
    for token in re.findall(r"\{([^{}]*)\}", text):
        if token not in PLACEHOLDERS:
            raise BadRequest(f"{where} uses {{{token}}}; use {', '.join('{' + p + '}' for p in PLACEHOLDERS)}")


def _clean_action(raw: Any, i: int) -> dict[str, Any]:
    where = f"actions[{i}]"
    a = _obj(raw, where)
    kind = a.get("type")
    if kind not in ACTION_TYPES:
        raise BadRequest(f"{where}.type must be one of: {', '.join(ACTION_TYPES)}")
    allowed, required = _ACTION_KEYS[kind]
    _unknown(a, allowed | {"type"}, where)
    missing = required - set(a)
    if missing:
        raise BadRequest(f"{where} needs {', '.join(sorted(missing))}")
    out: dict[str, Any] = {"type": kind}
    if kind == "notify":
        to = a["to"]
        if to not in NOTIFY_TO:
            raise BadRequest(f"{where}.to must be one of: {', '.join(NOTIFY_TO)}")
        out["to"] = to
        if to == "office":
            out["office"] = _office(a.get("office"), f"{where}.office")
        elif a.get("office") is not None:
            raise BadRequest(f"{where}.office is only for to = office")
        if to == "emails":
            emails = a.get("emails")
            if not isinstance(emails, list) or not emails or len(emails) > MAX_EMAILS:
                raise BadRequest(f"{where}.emails must list 1–{MAX_EMAILS} email addresses")
            clean = []
            for j, e in enumerate(emails):
                address = str(e or "").strip().lower() if isinstance(e, str) else ""
                if not workflow._EMAIL_RE.match(address) or len(address) > 200:
                    raise BadRequest(f"{where}.emails[{j}] must be a valid email address")
                clean.append(address)
            out["emails"] = list(dict.fromkeys(clean))
        elif a.get("emails") is not None:
            raise BadRequest(f"{where}.emails is only for to = emails")
        if a.get("message") is not None:
            message = _text(a["message"], f"{where}.message", MAX_MESSAGE)
            _check_placeholders(message, f"{where}.message")
            out["message"] = message
    elif kind == "escalate":
        out["office"] = _office(a["office"], f"{where}.office")
    elif kind == "assign":
        if a.get("owner") is not None:
            out["owner"] = _clean_person(a["owner"], f"{where}.owner")
    elif kind == "comment":
        text = _text(a["text"], f"{where}.text", MAX_COMMENT)
        _check_placeholders(text, f"{where}.text")
        out["text"] = text
    return out


def clean_rule(body: Any) -> dict[str, Any]:
    """A complete rule body {name, enabled, trigger, conditions, actions} →
    its clean form. Raises BadRequest on anything unknown or out of range."""
    body = _obj(body, "Body")
    _unknown(body, _CREATE_KEYS, "the rule")
    for key in ("name", "trigger", "actions"):
        if key not in body:
            raise BadRequest(f"{key} is required")
    name = _text(body["name"], "name", MAX_NAME)
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise BadRequest("enabled must be true or false")
    conditions = body.get("conditions", [])
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        raise BadRequest(f"conditions must be a list (max {MAX_CONDITIONS})")
    actions = body["actions"]
    if not isinstance(actions, list) or not actions or len(actions) > MAX_ACTIONS:
        raise BadRequest(f"actions must list 1–{MAX_ACTIONS} actions")
    return {"name": name, "enabled": enabled, "trigger": _clean_trigger(body["trigger"]),
            "conditions": [_clean_condition(c, i) for i, c in enumerate(conditions)],
            "actions": [_clean_action(a, i) for i, a in enumerate(actions)]}


def _rule_body(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: rule[k] for k in ("name", "enabled", "trigger", "conditions", "actions") if k in rule}


# ---------------------------------------------------------------------------
# CRUD (GET/POST /automations, PATCH/DELETE /automations/{id})
# ---------------------------------------------------------------------------


def view(rule: dict[str, Any]) -> dict[str, Any]:
    """The Automation object of GOVERN_API.md."""
    return {"id": rule.get("id"), "name": rule.get("name"), "enabled": bool(rule.get("enabled", True)),
            "trigger": rule.get("trigger") or {}, "conditions": list(rule.get("conditions") or []),
            "actions": list(rule.get("actions") or []), "createdBy": rule.get("createdBy"),
            "createdAt": rule.get("createdAt"), "updatedBy": rule.get("updatedBy"),
            "updatedAt": rule.get("updatedAt"), "lastRunAt": rule.get("lastRunAt"),
            "runCount": int(rule.get("runCount") or 0), "rev": int(rule.get("rev") or 1)}


def list_rules(tenant_id: str) -> list[dict[str, Any]]:
    """Every rule of the tenant, oldest first (the order they were made)."""
    rules = store.config.automations(tenant_id)
    return [view(r) for r in sorted(rules, key=lambda r: (str(r.get("createdAt") or ""), str(r.get("id") or "")))]


def _check_id(rule_id: str) -> None:
    if not _RULE_ID_RE.match(rule_id or ""):
        raise NotFound(_NOT_FOUND)


def get_rule(tenant_id: str, rule_id: str) -> dict[str, Any]:
    _check_id(rule_id)
    rule = store.config.automation(tenant_id, rule_id)
    if rule is None:
        raise NotFound(_NOT_FOUND)
    return rule


def create_rule(tenant_id: str, body: Any, actor: dict[str, Any] | None,
                now: datetime | None = None) -> dict[str, Any]:
    clean = clean_rule(body)
    if len(store.config.automations(tenant_id)) >= MAX_RULES:
        raise LimitReached(f"An organization can have at most {MAX_RULES} automations. Delete one first.")
    now_s = iso(now)
    rule = {"id": uuid.uuid4().hex, **clean, "createdBy": actor, "createdAt": now_s, "updatedBy": actor,
            "updatedAt": now_s, "lastRunAt": None, "runCount": 0}
    if not store.config.create_automation(tenant_id, rule):
        raise StaleRule("Could not create the automation; try again.")
    return view({**rule, "rev": 1})


def update_rule(tenant_id: str, rule_id: str, body: Any, actor: dict[str, Any] | None,
                now: datetime | None = None) -> dict[str, Any]:
    body = _obj(body, "Body")
    _unknown(body, _PATCH_KEYS, "the rule")
    rev = body.get("rev")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        raise BadRequest("rev must be a whole number")
    get_rule(tenant_id, rule_id)
    patch = {k: v for k, v in body.items() if k != "rev"}

    def change(rule: dict[str, Any]) -> Any:
        if rev is not None and int(rule.get("rev") or 1) != rev:
            raise StaleRule("Someone else changed this automation. Reload it and try again.")
        clean = clean_rule({**_rule_body(rule), **patch})
        if clean == _rule_body(rule):
            return False
        rule.update(clean, updatedBy=actor, updatedAt=iso(now))
        return None

    stored = store.config.mutate_automation(tenant_id, rule_id, change)
    if stored is None:
        raise NotFound(_NOT_FOUND)
    return view(stored)


def delete_rule(tenant_id: str, rule_id: str) -> str:
    get_rule(tenant_id, rule_id)
    store.config.delete_automation(tenant_id, rule_id)
    return rule_id


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def condition_actual(field: str, c: dict[str, Any]) -> Any:
    if field == "value":
        return workflow.contract_value(c)[0]
    if field == "owner":
        return ((c.get("owner") or {}).get("email")) or None
    if field == "counterparty":
        return c.get("counterparty") or c.get("sponsor")
    return c.get(field)


def condition_holds(cond: dict[str, Any], c: dict[str, Any]) -> bool:
    field, op, want = cond.get("field"), cond.get("op"), cond.get("value")
    actual = condition_actual(str(field), c)
    if field == "agreementType":
        return (actual or "other") in (want or [])
    if field == "direction":
        return actual == want
    if field == "value":
        if actual is None:
            return False
        return actual >= float(want) if op == "gte" else actual <= float(want)
    if field == "department":
        return bool(actual) and _norm(actual) == _norm(want)
    if field == "owner":
        return bool(actual) if op == "set" else not actual
    if field == "counterparty":
        names = " | ".join(_norm(x) for x in (c.get("counterparty"), c.get("sponsor")) if x)
        return bool(names) and _norm(want) in names
    return False


def conditions_hold(rule: dict[str, Any], c: dict[str, Any]) -> bool:
    return all(condition_holds(cond, c) for cond in rule.get("conditions") or [])


def _today(now: datetime) -> Any:
    return now.astimezone(timezone.utc).date()


def time_firings(rule: dict[str, Any], c: dict[str, Any], now: datetime, cfg: dict[str, Any],
                 obligations: list[dict[str, Any]] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """The (key, context) episodes a time trigger qualifies for on contract ``c`` now."""
    t = rule.get("trigger") or {}
    kind, cid = t.get("type"), c["contractId"]
    if kind == "in_stage_for":
        if c.get("state") in workflow.TERMINAL_STATES or c.get("stage") != t.get("stage") or not c.get("stageEnteredAt"):
            return []
        days = workflow.sla(c, cfg, now)["daysInStage"]
        return [(f"{cid}#{c['stageEnteredAt']}", {"days": days})] if days >= int(t.get("days") or 0) else []
    if kind == "term_ends_in":
        end = parse_iso(c.get("termEndDate"))
        if c.get("state") not in ("signed", "active") or end is None:
            return []
        left = (end.date() - _today(now)).days
        if 0 <= left <= int(t.get("days") or 0):
            return [(f"{cid}#term#{end.date().isoformat()}", {"days": left, "due": end.date().isoformat()})]
        return []
    if kind == "obligation_due_in":
        if not app_settings.feature_enabled("obligations"):
            return []
        out = []
        for o in obligations or []:
            due = parse_iso(o.get("dueDate"))
            if o.get("status", "open") != "open" or due is None or not o.get("id"):
                continue
            left = (due.date() - _today(now)).days
            if 0 <= left <= int(t.get("days") or 0):
                out.append((f"{cid}#obl#{o['id']}#{due.date().isoformat()}",
                            {"days": left, "due": due.date().isoformat(), "obligation": o.get("title") or "An obligation"}))
        return out
    return []


def event_matches(trigger: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Does an activity entry fire this event trigger? Entries an automation
    wrote never do."""
    detail = entry.get("detail") or {}
    if detail.get("automation"):
        return False
    kind, action = trigger.get("type"), entry.get("action")
    if kind == "contract_received":
        return action == "intake"
    if kind == "stage_changed":
        to = entry.get("toStage")
        return bool(to) and to != entry.get("fromStage") and (not trigger.get("to") or to == trigger["to"])
    if kind == "sent_back":
        return action == "sent_back"
    if kind == "signed":
        return action == "signed"
    if kind == "blocker_added":
        tier = trigger.get("tier")
        if action == "blocker_added":
            return tier is None                   # a reviewer's item has no tier
        if action != "rescored" or int(detail.get("blockersAdded") or 0) <= 0:
            return False
        counts = detail.get("counts") or {}
        unacceptable = int(counts.get("unacceptable") or 0)
        if tier == "unacceptable":
            return unacceptable > 0
        if tier == "deviates":                   # deviates or worse
            return unacceptable + int(counts.get("deviates") or 0) > 0
        return True
    return False


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _stage_words(stage: Any) -> str:
    return {"renewal": "up for renewal"}.get(str(stage or ""), str(stage or "its stage").replace("_", " "))


def _fill(template: str, rule: dict[str, Any], c: dict[str, Any], ctx: dict[str, Any]) -> str:
    values = {
        "contract": c.get("title") or "this agreement",
        "stage": _stage_words(c.get("stage")),
        "days": str(ctx.get("days")) if ctx.get("days") is not None else "",
        "due": str(ctx.get("due") or ""),
        "owner": workflow.display(c.get("owner"), "nobody"),
        "counterparty": c.get("counterparty") or c.get("sponsor") or "the other party",
        "rule": rule.get("name") or "",
    }
    return " ".join(_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template).split())


def default_message(rule: dict[str, Any], c: dict[str, Any], ctx: dict[str, Any]) -> str:
    title = f"“{c.get('title') or 'This agreement'}”"
    kind = (rule.get("trigger") or {}).get("type")
    if kind == "in_stage_for":
        return f"{title} has been in {_stage_words(c.get('stage'))} for {workflow._plural(int(ctx.get('days') or 0), 'day')}."
    if kind == "term_ends_in":
        return f"The term of {title} ends on {ctx.get('due')} ({workflow._plural(int(ctx.get('days') or 0), 'day')} from now)."
    if kind == "obligation_due_in":
        return f"{ctx.get('obligation') or 'An obligation'} for {title} is due on {ctx.get('due')}."
    summary = (ctx.get("entry") or {}).get("summary")
    return f"{title}: {summary}" if summary else f"{title} needs a look."


def _recipients(action: dict[str, Any], c: dict[str, Any], cfg: dict[str, Any]) -> tuple[list[str], str]:
    """(emails, who in words) for a notify action."""
    to = action.get("to")
    if to == "owner":
        owner = c.get("owner") or {}
        email = str(owner.get("email") or "").strip().lower()
        return ([email] if email else []), (f"the owner ({workflow.display(owner)})" if email else "the owner")
    if to == "office":
        office = action.get("office")
        emails = [str(r.get("email") or "").strip().lower() for r in cfg.get("reviewers") or []
                  if office in (r.get("offices") or [])]
        return [e for e in dict.fromkeys(emails) if e], workflow.office_label(office)
    emails = list(action.get("emails") or [])
    return emails, ", ".join(emails[:3]) + (f" and {len(emails) - 3} more" if len(emails) > 3 else "")


def run_action(rule: dict[str, Any], action: dict[str, Any], c: dict[str, Any], ctx: dict[str, Any],
               cfg: dict[str, Any], now: datetime, *, dry_run: bool) -> dict[str, Any]:
    """Do (or, with ``dry_run``, describe) one action.
    Returns {type, summary, wouldRun, reason}; ``wouldRun`` false = skipped."""
    kind = action.get("type")
    actor_name = system_name(rule)
    tag = _tag(rule)
    out: dict[str, Any] = {"type": kind, "summary": "", "wouldRun": True, "reason": None}

    def skip(reason: str) -> dict[str, Any]:
        return {**out, "wouldRun": False, "reason": reason}

    if kind == "notify":
        emails, who = _recipients(action, c, cfg)
        message = _fill(action["message"], rule, c, ctx) if action.get("message") else default_message(rule, c, ctx)
        out["summary"] = f"Notify {who}: {message}"
        out["recipients"] = emails
        if not emails:
            return skip("No one to notify: the contract has no owner." if action.get("to") == "owner"
                        else f"No reviewers are set up for {who}.")
        if not app_settings.feature_enabled("notifications"):
            out["reason"] = "Notifications are off on this deployment, so nothing will be sent."
        if not dry_run:
            subject = f"Govern: {c.get('title') or 'An agreement'} ({rule.get('name')})"[:200]
            workflow.record_system_entry(
                c, "automation_notify", f"{actor_name} raised an alert for {who}: “{workflow._clip(message, 160)}”",
                detail={**tag, "to": action.get("to"), "office": action.get("office"), "recipients": emails,
                        "subject": subject, "message": message})
        return out

    if kind == "escalate":
        office = action["office"]
        out["summary"] = f"Escalate to {workflow.office_label(office)}"
        if office in workflow.pending_offices(c) and c.get("state") == "escalated":
            return skip(f"It is already waiting on {workflow.office_label(office)}.")
        if c.get("state") not in workflow._ALLOWED_FROM["escalate"]:
            return skip(f"A contract that is {workflow._STATE_WORDS.get(c.get('state') or '', c.get('state'))} cannot be escalated.")
        if not dry_run:
            workflow.perform_action(c["contractId"], "escalate", {"office": office}, None, now=now, cfg=cfg,
                                    system_name=actor_name, detail_tag=tag)
        return out

    if kind == "assign":
        owner = action.get("owner")
        why = None
        if not owner:
            owner, why = workflow.auto_assignee(c, cfg)
        if not owner:
            out["summary"] = "Assign a reviewer by the assignment rules"
            return skip("No assignment rule or reviewer matches this contract.")
        out["summary"] = f"Assign to {workflow.display(owner)}" + (f" ({why})" if why else "")
        current = c.get("owner") or {}
        if str(current.get("email") or "").lower() == str(owner.get("email") or "").lower():
            return skip(f"It is already assigned to {workflow.display(current)}.")
        if c.get("state") not in workflow._ALLOWED_FROM["assign"]:
            return skip(f"A contract that is {workflow._STATE_WORDS.get(c.get('state') or '', c.get('state'))} cannot be assigned.")
        if not dry_run:
            workflow.perform_action(c["contractId"], "assign", {"owner": {"email": owner["email"], "name": owner.get("name")}},
                                    None, now=now, cfg=cfg, system_name=actor_name, detail_tag=tag)
        return out

    if kind == "comment":
        text = _fill(action["text"], rule, c, ctx)
        out["summary"] = f"Comment: {workflow._clip(text, 160)}"
        if not dry_run:
            workflow.perform_action(c["contractId"], "comment", {"text": text}, None, now=now, cfg=cfg,
                                    system_name=actor_name, detail_tag=tag)
        return out
    return skip("Unknown action.")


# ---------------------------------------------------------------------------
# Firing (sweeper and stream)
# ---------------------------------------------------------------------------


def _fire(tenant_id: str, rule: dict[str, Any], c: dict[str, Any], key: str, ctx: dict[str, Any],
          cfg: dict[str, Any], now: datetime, marker_days: int) -> bool:
    """Run a rule's actions once for one episode. True when the firing
    completed now (False if it had already run). Infrastructure errors release
    the action's claim and propagate, so the caller's retry finishes the job."""
    scope = _scope(tenant_id)
    base = f"{rule['id']}#{key}"
    current = c
    for n, action in enumerate(rule.get("actions") or []):
        marker = f"{base}#{n}"
        if not store.metrics.claim(scope, marker, days=marker_days):
            continue                                  # this action already ran
        try:
            result = run_action(rule, action, current, ctx, cfg, now, dry_run=False)
        except GovernError as exc:                    # the contract's state forbids it: skip, do not retry
            log.warning("govern.automation_action_skipped", ruleId=rule["id"], contractId=c["contractId"],
                        action=action.get("type"), error_type=type(exc).__name__)
            continue
        except Exception:
            store.metrics.release(scope, marker)
            raise
        if not result["wouldRun"]:
            log.info("govern.automation_action_skipped", ruleId=rule["id"], contractId=c["contractId"],
                     action=action.get("type"), reason=result["reason"])
        elif action.get("type") in ("escalate", "assign"):
            current = store.contracts.get(c["contractId"]) or current   # the next action sees the change
    if not store.metrics.claim(scope, f"{base}#run", days=marker_days):
        return False
    store.config.record_automation_run(tenant_id, rule["id"], iso(now))
    log.info("govern.automation_fired", ruleId=rule["id"], contractId=c["contractId"], key=key)
    return True


def _active_rules(tenant_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    return [r for r in store.config.automations(tenant_id)
            if r.get("enabled", True) and (r.get("trigger") or {}).get("type") in kinds and r.get("id")]


def run_time_rules(tenant_id: str, contracts: list[dict[str, Any]], now: datetime,
                   cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """govern-sweeper: fire every time-based rule on the contracts that
    qualify now. {fired, failed}. A failing contract does not stop the others;
    the caller raises when ``failed`` so the run is visibly retried."""
    totals = {"fired": 0, "failed": 0}
    rules = _active_rules(tenant_id, TIME_TRIGGERS)
    if not rules:
        return totals
    cfg = cfg or workflow.get_settings(tenant_id)
    if not enabled_for(tenant_id, cfg):
        return totals
    obligations: dict[str, list[dict[str, Any]]] = {}
    obl_days = [int(r["trigger"].get("days") or 0) for r in rules if r["trigger"]["type"] == "obligation_due_in"]
    if obl_days and app_settings.feature_enabled("obligations"):
        through = (now + timedelta(days=max(obl_days))).date().isoformat()
        for o in store.contracts.obligations_due(tenant_id, through):
            obligations.setdefault(str(o.get("contractId") or ""), []).append(o)
    scope = _scope(tenant_id)
    for rule in rules:
        candidates: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for c in contracts:
            for key, ctx in time_firings(rule, c, now, cfg, obligations.get(c["contractId"])):
                candidates.append((c, key, ctx))
        if not candidates:
            continue
        done = store.metrics.seen(scope, [f"{rule['id']}#{key}#run" for _, key, _ in candidates])
        for c, key, ctx in candidates:
            if f"{rule['id']}#{key}#run" in done or not conditions_hold(rule, c):
                continue
            try:
                totals["fired"] += int(_fire(tenant_id, rule, c, key, ctx, cfg, now, _TIME_MARKER_DAYS))
            except Exception as exc:  # noqa: BLE001 — keep going; the next sweep retries this one
                totals["failed"] += 1
                log.error("govern.automation_failed", ruleId=rule["id"], contractId=c["contractId"],
                          error_type=type(exc).__name__)
    return totals


def on_entry(entry: dict[str, Any], now: datetime | None = None) -> int:
    """govern-stream: fire the event rules one new activity entry matches.
    Returns how many fired. Errors propagate (the stream retries the record;
    the markers make the retry finish without repeating anything)."""
    tenant_id, contract_id, entry_id = entry.get("tenantId"), entry.get("contractId"), entry.get("id")
    if not (tenant_id and contract_id and entry_id):
        return 0
    if (entry.get("detail") or {}).get("automation"):
        return 0                                         # automations never trigger automations
    if entry.get("action") not in _EVENT_ACTIONS and not entry.get("toStage"):
        return 0
    if not app_settings.feature_enabled(FEATURE):
        return 0
    rules = [r for r in _active_rules(str(tenant_id), EVENT_TRIGGERS) if event_matches(r["trigger"], entry)]
    if not rules:
        return 0
    cfg = workflow.get_settings(str(tenant_id))
    if not enabled_for(str(tenant_id), cfg):
        return 0
    c = store.contracts.get(str(contract_id))
    if c is None:
        return 0
    now = now or datetime.now(timezone.utc)
    fired = 0
    for rule in rules:
        if conditions_hold(rule, c):
            fired += int(_fire(str(tenant_id), rule, c, f"{contract_id}#{entry_id}", {"entry": entry},
                               cfg, now, _EVENT_MARKER_DAYS))
    return fired


# ---------------------------------------------------------------------------
# Dry run (POST /automations/{id}/test)
# ---------------------------------------------------------------------------


def _event_words(entry: dict[str, Any]) -> str:
    return f"“{workflow._clip(entry.get('summary') or entry.get('action') or 'an event', 120)}” on {str(entry.get('at') or '')[:10]}"


def dry_run(tenant_id: str, rule: dict[str, Any], c: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the rule would do on contract ``c`` right now. Reads only: no
    contract, activity, marker or rule counter is written."""
    now = now or datetime.now(timezone.utc)
    cfg = workflow.get_settings(str(c.get("tenantId") or tenant_id))
    trigger = rule.get("trigger") or {}
    kind = trigger.get("type")
    key: str | None = None
    ctx: dict[str, Any] = {}
    if kind in TIME_TRIGGERS:
        obligations = store.contracts.obligations(c["contractId"]) if kind == "obligation_due_in" else None
        firings = time_firings(rule, c, now, cfg, obligations)
        if firings:
            key, ctx = firings[0]
        detail = _time_detail(trigger, c, now, cfg, bool(firings), ctx)
    else:
        entry = next((e for e in store.activity.recent(c["contractId"], 200) if event_matches(trigger, e)), None)
        if entry is not None:
            key, ctx = f"{c['contractId']}#{entry.get('id')}", {"entry": entry}
            detail = f"Matches {_event_words(entry)}."
        else:
            detail = "Nothing in this contract's recent activity matches. The rule fires on the next matching event."
    conditions = [{**cond, "matched": condition_holds(cond, c), "actual": condition_actual(str(cond.get("field")), c)}
                  for cond in rule.get("conditions") or []]
    already = bool(key) and rule.get("id") is not None and \
        f"{rule['id']}#{key}#run" in store.metrics.seen(_scope(tenant_id), [f"{rule['id']}#{key}#run"])
    matched = key is not None and all(x["matched"] for x in conditions)
    actions = [run_action(rule, a, c, ctx, cfg, now, dry_run=True) for a in rule.get("actions") or []]
    return {"ruleId": rule.get("id"), "contractId": c["contractId"], "contractTitle": c.get("title"),
            "wouldFire": matched and not already and bool(rule.get("enabled", True)),
            "enabled": bool(rule.get("enabled", True)), "featureEnabled": enabled_for(tenant_id, cfg),
            "alreadyRan": already, "trigger": {"matched": key is not None, "detail": detail},
            "conditions": conditions, "actions": actions}


def _time_detail(trigger: dict[str, Any], c: dict[str, Any], now: datetime, cfg: dict[str, Any],
                 matched: bool, ctx: dict[str, Any]) -> str:
    kind = trigger.get("type")
    if kind == "in_stage_for":
        days = workflow.sla(c, cfg, now)["daysInStage"]
        if c.get("stage") != trigger.get("stage"):
            return f"It is in {_stage_words(c.get('stage'))}, not {_stage_words(trigger.get('stage'))}."
        word = "and" if matched else "but"
        return f"It has been in {_stage_words(c.get('stage'))} for {workflow._plural(days, 'day')}, {word} the rule waits {workflow._plural(int(trigger.get('days') or 0), 'day')}."
    if kind == "term_ends_in":
        if matched:
            return f"The term ends on {ctx.get('due')}, in {workflow._plural(int(ctx.get('days') or 0), 'day')}."
        if c.get("state") not in ("signed", "active"):
            return "Only signed or active agreements have a running term."
        return f"The term end ({c.get('termEndDate') or 'not set'}) is not within {workflow._plural(int(trigger.get('days') or 0), 'day')}."
    if matched:
        return f"{ctx.get('obligation')} is due on {ctx.get('due')}, in {workflow._plural(int(ctx.get('days') or 0), 'day')}."
    if not app_settings.feature_enabled("obligations"):
        return "Obligation tracking is off on this deployment."
    return f"No open obligation is due within {workflow._plural(int(trigger.get('days') or 0), 'day')}."


def resolve_for_test(tenant_id: str, rule_id: str, body: Any) -> tuple[dict[str, Any], str]:
    """(rule, contractId) for a dry run. ``rule`` in the body tests unsaved
    edits; ``rule_id`` "new" tests a rule that is not saved yet."""
    body = _obj(body, "Body")
    _unknown(body, {"contractId", "rule"}, "the request")
    contract_id = body.get("contractId")
    if not isinstance(contract_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", contract_id):
        raise BadRequest("contractId is required")
    if rule_id == "new":
        if body.get("rule") is None:
            raise BadRequest("rule is required to test an automation that is not saved")
        return {"id": None, **clean_rule(body["rule"])}, contract_id
    stored = get_rule(tenant_id, rule_id)
    if body.get("rule") is not None:
        return {**stored, **clean_rule(body["rule"])}, contract_id
    return stored, contract_id
