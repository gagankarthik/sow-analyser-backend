"""Key dates — every dated event or obligation in a document, normalised.

The classify stage asks the model for the dates it can see; this module turns
that raw material (plus the dates already sitting in the structured fields:
effective date, term end, milestones, payment schedule, signatures …) into one
validated list the UI can build a timeline on:

    keyDates: [{
        id, kind, label, date (ISO or null), rawText,
        precision, isEstimated, isDerived, ambiguous,
        anchor, offsetValue, offsetUnit, offsetDays, recurring,
        amount, currency, clauseId, clauseNumber, sectionRef,
        confidence, issues, origin
    }]

Rules
-----
* Nothing is dropped. A date that cannot be read (ambiguous 03/04/2026, an
  impossible 30 February, a rule whose anchor is unknown) is kept with
  ``date = null``, its ``rawText`` and the reason in ``issues``.
* Nothing is guessed. Day/month order comes from the document's own dates; with
  no evidence the entry is marked ``ambiguous``.
* A relative rule ("30 days after the Effective Date") is resolved when its
  anchor date is known (``isDerived``) and otherwise kept as anchor + offset.
* The notice-to-cancel deadline is computed from term end − notice days.
* No status (completed / current / upcoming) is stored — that depends on today's
  date and is derived by the reader.
* The legacy fields (effectiveDate, termEndDate, renewalDate, renewalNoticeDays,
  autoRenews) are the first source of the list, so the two always agree.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .dates import (
    add_offset, anchor_key, detect_day_first, find_dates, is_iso_date, offset_days, parse_date,
    parse_offset, plausible,
)
from .text import normalize, title_similarity

KINDS = [
    "effective", "signature", "start", "term_end", "renewal", "notice_deadline",
    "milestone", "deliverable", "payment", "acceptance", "sla_reporting",
    "amendment_effective", "termination", "deadline", "other",
]
_KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
OFFSET_UNITS = ["days", "business_days", "weeks", "months", "years"]

# The same event may be reported under sibling kinds (a milestone that is also a
# payment; an amendment's effective date); they merge when they share a date.
_GROUP = {"milestone": "event", "deliverable": "event", "payment": "event",
          "effective": "effective", "amendment_effective": "effective"}
# Groups of which a document has one logical instance per date.
_SINGLETON = {"effective", "start", "term_end", "renewal", "notice_deadline"}

_LABELS = {
    "effective": "Effective date", "signature": "Signature date", "start": "Start date",
    "term_end": "Term end", "renewal": "Renewal date",
    "notice_deadline": "Last day to give notice of non-renewal",
    "amendment_effective": "Amendment effective date",
}
_MAX_RAW = 300


def _cand(kind: str, label: str | None, date_raw: Any = None, raw_text: Any = None, *,
          origin: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind if kind in _KIND_ORDER else "other",
        "label": (label or "").strip() or _LABELS.get(kind, ""),
        "dateRaw": date_raw, "rawText": (str(raw_text).strip() if raw_text else None),
        "origin": origin, "amount": extra.get("amount"), "recurring": extra.get("recurring"),
        "anchorText": extra.get("anchor"), "offsetValue": extra.get("offsetValue"),
        "offsetUnit": extra.get("offsetUnit"), "offsetDirection": extra.get("offsetDirection"),
        "sourceRef": extra.get("sourceRef"),
    }


def _collect(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather candidates: structured fields first (authoritative), then the
    model's own keyDates list."""
    out: list[dict[str, Any]] = []
    ident = result.get("identification") or {}
    tl = result.get("timeline") or {}
    com = result.get("commercials") or {}
    amendment = result.get("amendment") or {}
    is_amendment = (amendment.get("amendmentType") or "none") != "none" or result.get("docType") == "AMENDMENT"

    if result.get("effectiveDate"):
        out.append(_cand("amendment_effective" if is_amendment else "effective", None,
                         result["effectiveDate"], result["effectiveDate"], origin="structured"))
    if ident.get("executionDate"):
        out.append(_cand("signature", "Execution date", ident["executionDate"], ident["executionDate"],
                         origin="structured"))
    for s in ident.get("signatories") or []:
        if s.get("date"):
            who = s.get("name") or s.get("party")
            out.append(_cand("signature", f"Signed by {who}" if who else "Signature date",
                             s["date"], s["date"], origin="structured"))
    if tl.get("startDate"):
        out.append(_cand("start", None, tl["startDate"], tl["startDate"], origin="structured"))
    if tl.get("endDate") and not tl.get("endDateDerived"):
        # (a derived end date is rebuilt from its rule below, keeping its provenance)
        out.append(_cand("term_end", None, tl["endDate"], tl["endDate"], origin="structured"))
    if tl.get("renewalDate"):
        out.append(_cand("renewal", None, tl["renewalDate"], tl["renewalDate"], origin="structured"))
    for ph in tl.get("phases") or []:
        name = ph.get("name") or "Phase"
        if ph.get("start"):
            out.append(_cand("milestone", f"{name} — starts", ph["start"], ph["start"], origin="structured"))
        if ph.get("end"):
            out.append(_cand("milestone", f"{name} — ends", ph["end"], ph["end"], origin="structured"))
    for ms in tl.get("milestones") or []:
        if ms.get("date") or ms.get("source"):
            out.append(_cand("milestone", ms.get("name"), ms.get("date"), ms.get("source") or ms.get("date"),
                             origin="structured", amount=ms.get("payment")))
    for d in result.get("deliverables") or []:
        if d.get("dueDate"):
            out.append(_cand("deliverable", d.get("name"), d["dueDate"], d["dueDate"],
                             origin="structured", amount=d.get("value")))
    for p in com.get("paymentSchedule") or []:
        if p.get("trigger"):
            out.append(_cand("payment", p.get("label"), None, p["trigger"],
                             origin="structured", amount=p.get("amount")))
    if com.get("paymentTerms") and parse_offset(str(com["paymentTerms"])):
        out.append(_cand("payment", "Payment terms", None, com["paymentTerms"],
                         origin="structured", recurring="per invoice"))

    for kd in result.get("keyDatesRaw") or []:
        if not isinstance(kd, dict):
            continue
        if not (kd.get("date") or kd.get("rawText") or kd.get("offsetValue")):
            continue
        out.append(_cand(
            str(kd.get("kind") or "other"), kd.get("label"), kd.get("date"),
            kd.get("rawText") or kd.get("date"), origin="extracted",
            amount=kd.get("amount"), recurring=kd.get("recurring"), anchor=kd.get("anchor"),
            offsetValue=kd.get("offsetValue"), offsetUnit=kd.get("offsetUnit"),
            offsetDirection=kd.get("offsetDirection"), sourceRef=kd.get("source"),
        ))
    return out


def _normalise(c: dict[str, Any], day_first: bool | None) -> None:
    """Fill date / precision / flags / rule on one candidate.

    Order of evidence: the date the model stated → a rule anchored on a date
    written in the same sentence ("30 days after 1 March 2026") → a calendar date
    in the quote → a rule to resolve later from the document's anchor dates.
    """
    c.update(date=None, precision=None, isEstimated=False, isDerived=False, ambiguous=False,
             anchor=None, offsetDays=None, issues=[])
    raw_date, raw_text = c["dateRaw"], c["rawText"]
    stated = parse_date(raw_date, day_first=day_first) if raw_date else None

    rule = None
    if c.get("offsetValue") is not None and c.get("offsetUnit") in OFFSET_UNITS:
        anchor_text = c.get("anchorText") or raw_text
        value, unit = float(c["offsetValue"]), c["offsetUnit"]
        if value != int(value) and unit in ("years", "months"):     # "1.5 years" → 18 months
            value, unit = value * (12 if unit == "years" else 30), ("months" if unit == "years" else "days")
        rule = {"value": int(round(value)), "unit": unit,
                "direction": -1 if c.get("offsetDirection") == "before" else 1,
                "anchor": anchor_key(c.get("anchorText")) or anchor_key(raw_text),
                "anchorText": anchor_text}
    elif raw_text:
        rule = parse_offset(raw_text)
        if rule and not rule.get("anchor") and c.get("anchorText"):
            rule["anchor"], rule["anchorText"] = anchor_key(c["anchorText"]), c["anchorText"]

    inline_anchor = None
    if rule and rule.get("anchorText"):
        at = parse_date(rule["anchorText"], day_first=day_first)
        if at["date"] and at["precision"] == "day":
            inline_anchor = at["date"]

    if stated and stated["date"]:
        _apply_parse(c, stated)
    elif not inline_anchor:
        in_text = parse_date(raw_text, day_first=day_first) if raw_text else None
        if in_text and (in_text["date"] or in_text["ambiguous"] or in_text["reason"] == "invalid_date"):
            _apply_parse(c, in_text)
        elif stated:
            _apply_parse(c, stated)     # carries the ambiguity / invalid-date reason

    if rule:
        c["anchor"] = rule.get("anchor")
        c["anchorText"] = rule.get("anchorText") or c.get("anchorText")
        c["offsetValue"], c["offsetUnit"] = rule["value"], rule["unit"]
        c["offsetDays"] = offset_days(rule["value"], rule["unit"]) * (1 if rule["direction"] >= 0 else -1)
        c["_rule"] = rule
        if not c["date"] and inline_anchor:
            _resolve(c, inline_anchor)


def _apply_parse(c: dict[str, Any], parsed: dict[str, Any]) -> None:
    if parsed["date"]:
        if plausible(parsed["date"]):
            c["date"], c["precision"] = parsed["date"], parsed["precision"]
            c["isEstimated"] = bool(parsed["estimated"])
            if parsed.get("periodStart"):
                c["periodStart"] = parsed["periodStart"]
        else:
            c["issues"].append("implausible_date")
    elif parsed["ambiguous"]:
        c["ambiguous"] = True
        c["issues"].append("ambiguous_day_month")
    elif parsed["reason"] == "invalid_date":
        c["issues"].append("invalid_date")


def _resolve(c: dict[str, Any], anchor_iso: str) -> bool:
    rule = c.get("_rule")
    if not rule:
        return False
    resolved = add_offset(anchor_iso, rule["value"], rule["unit"], rule["direction"])
    if not resolved or not plausible(resolved):
        return False
    c["date"], c["precision"], c["isDerived"] = resolved, "day", True
    # Business days ignore public holidays we cannot know.
    c["isEstimated"] = rule["unit"] == "business_days"
    return True


def _similar(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return not na and not nb
    return na == nb or na in nb or nb in na or title_similarity(a, b) > 0.5


def _mergeable(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ga, gb = _GROUP.get(a["kind"], a["kind"]), _GROUP.get(b["kind"], b["kind"])
    if ga != gb:
        return False
    if a["date"] or b["date"]:
        if a["date"] != b["date"]:
            return False
    elif normalize(a["rawText"] or "") != normalize(b["rawText"] or ""):
        return False
    if ga in _SINGLETON:
        return True
    # Two events of the same kind on the same day are the SAME event only if
    # they are named alike and their amounts do not disagree. Two milestones due
    # the same day, two instalments with the same trigger, two signatures on one
    # date are all distinct.
    if a["amount"] is not None and b["amount"] is not None \
            and abs(float(a["amount"]) - float(b["amount"])) >= 0.005:
        return False
    return _similar(a["label"], b["label"])


def _merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key in ("amount", "recurring", "anchor", "anchorText", "offsetValue", "offsetUnit",
                "offsetDays", "sourceRef", "precision", "periodStart"):
        if into.get(key) is None and other.get(key) is not None:
            into[key] = other[key]
    if not into["label"] and other["label"]:
        into["label"] = other["label"]
    # Keep the longer verbatim quote: it is the better citation.
    if len(other.get("rawText") or "") > len(into.get("rawText") or ""):
        into["rawText"] = other["rawText"]
    into["isDerived"] = into["isDerived"] and other["isDerived"]
    into["issues"] = sorted(set(into["issues"]) | set(other["issues"]))


class ClauseIndex:
    """Finds the clause a verbatim quote came from."""

    def __init__(self, clauses: list[dict[str, Any]], day_first: bool | None = None):
        self._clauses = clauses or []
        self._rows = [(c, normalize(f"{c.get('title') or ''} {c.get('body') or ''}")) for c in self._clauses]
        self._day_first = day_first
        self._by_date: dict[str, dict[str, Any]] | None = None

    def locate(self, quote: str | None) -> dict[str, Any] | None:
        probe = normalize(quote or "")
        if len(probe) < 6:
            return None
        for candidate in (probe, probe[:80], probe[:40]):
            if len(candidate) < 6:
                continue
            for clause, body in self._rows:
                if candidate in body:
                    return clause
        return None

    def locate_date(self, iso: str | None) -> dict[str, Any] | None:
        """First clause in which this calendar date is written, in any format."""
        if not iso:
            return None
        if self._by_date is None:
            self._by_date = {}
            for clause in self._clauses:
                for found, _, _ in find_dates(str(clause.get("body") or ""), day_first=self._day_first):
                    self._by_date.setdefault(found, clause)
        return self._by_date.get(iso)


def _confidence(c: dict[str, Any], cited: bool) -> str:
    if c["issues"] or c["ambiguous"] or not c["date"]:
        return "low"
    if c["isEstimated"] or c["isDerived"] or not cited:
        return "medium"
    return "high"


def build_key_dates(
    result: dict[str, Any],
    clauses: list[dict[str, Any]] | None = None,
    text: str = "",
) -> dict[str, Any]:
    """Build the validated key-date list for one classified document.

    Returns ``{"keyDates": [...], "issues": [...], "dayFirst": bool|None,
    "derived": {"termEndDate": iso|None}}``. ``issues`` are human-readable notes
    for the document's confidence block; ``derived.termEndDate`` is set when the
    term end was computed from a rule and the stated field was empty.
    """
    clauses = clauses if clauses is not None else (result.get("clauses") or [])
    day_first = detect_day_first(text)
    cands = _collect(result)
    for c in cands:
        _normalise(c, day_first)

    def anchors() -> dict[str, str]:
        found: dict[str, str] = {}
        for kind, key in (("effective", "effective"), ("amendment_effective", "effective"),
                          ("start", "start"), ("signature", "signature"), ("term_end", "term_end")):
            dated = sorted(c["date"] for c in cands
                           if c["kind"] == kind and c["date"] and c["precision"] == "day" and not c["issues"])
            if dated and key not in found:
                # several signatures → the agreement is signed when the last party signs
                found[key] = dated[-1] if kind == "signature" else dated[0]
        if "start" not in found and "effective" in found:
            found["start"] = found["effective"]
        if "effective" not in found and "start" in found:
            found["effective"] = found["start"]
        return found

    # Resolve relative rules; a resolved term end can unlock a notice deadline.
    for _ in range(3):
        known = anchors()
        progressed = False
        for c in cands:
            if c["date"] or not c.get("_rule"):
                continue
            anchor_iso = known.get(c["_rule"].get("anchor") or "")
            if anchor_iso and _resolve(c, anchor_iso):
                progressed = True
        if not progressed:
            break
    known = anchors()

    tl = result.get("timeline") or {}
    derived_term_end = known.get("term_end") if not tl.get("endDate") else None

    # Notice-to-cancel deadline = term end (or renewal date) − notice days.
    notice_days = tl.get("renewalNoticeDays")
    if isinstance(notice_days, (int, float)) and not isinstance(notice_days, bool) and notice_days > 0:
        notices = [c for c in cands if c["kind"] == "notice_deadline"]
        if not any(c["date"] for c in notices):
            base = known.get("term_end") or next(
                (c["date"] for c in cands if c["kind"] == "renewal" and c["date"] and not c["issues"]), None)
            if notices:
                target = notices[0]
            else:
                target = _cand("notice_deadline", None, None, None, origin="derived")
                _normalise(target, day_first)
                cands.append(target)
            if target.get("offsetValue") is None:
                target.update(offsetValue=int(notice_days), offsetUnit="days", offsetDays=-int(notice_days))
            target["anchor"] = target.get("anchor") or "term_end"
            if base:
                resolved = add_offset(base, int(notice_days), "days", -1)
                if resolved:
                    target.update(date=resolved, precision="day", isDerived=True)
            target.pop("_rule", None) if target["date"] else None

    # Unresolved rules and unreadable dates are kept, with the reason.
    for c in cands:
        if c["date"] or c["issues"]:
            continue
        rule_anchor = (c.get("_rule") or {}).get("anchor") or c.get("anchor")
        if c.get("_rule") or c.get("offsetValue") is not None:
            if rule_anchor in ("invoice", "acceptance", "delivery", "notice"):
                c["issues"].append("event_based")       # counted from an event, not a calendar date
            else:
                c["issues"].append("anchor_unknown")
        else:
            c["issues"].append("no_calendar_date")

    # Impossible orderings are flagged, never silently dropped.
    doc_issues: list[str] = []
    start_iso = known.get("effective") or known.get("start")
    for c in cands:
        if c["kind"] in ("term_end", "renewal") and c["date"] and start_iso and c["date"] < start_iso:
            c["issues"].append("before_effective_date")
            doc_issues.append(
                f"{_LABELS.get(c['kind'], c['kind'])} ({c['date']}) is before the effective date "
                f"({start_iso}); check both dates against the document."
            )
        if "ambiguous_day_month" in c["issues"]:
            doc_issues.append(
                f"The date \"{(c['rawText'] or '')[:40]}\" could be read as day/month or month/day "
                "and was left unresolved."
            )
        if "invalid_date" in c["issues"] or "implausible_date" in c["issues"]:
            doc_issues.append(f"The date \"{(c['rawText'] or '')[:40]}\" is not a valid calendar date.")

    # Merge duplicates (structured candidates come first, so they win).
    merged: list[dict[str, Any]] = []
    for c in cands:
        target = next((m for m in merged if _mergeable(m, c)), None)
        if target is None:
            merged.append(c)
        else:
            _merge(target, c)

    index = ClauseIndex(clauses, day_first)
    out: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    currency = (result.get("commercials") or {}).get("currency")
    for c in merged:
        clause = index.locate(c["rawText"]) if c["rawText"] else None
        if clause is None and c["date"] and not c["isDerived"] and c["precision"] == "day":
            clause = index.locate_date(c["date"])
        if clause is None and c["kind"] == "notice_deadline":
            clause = next((cl for cl in clauses if cl.get("category") == "Term"), None)
        raw = (c["rawText"] or "")[:_MAX_RAW] or None
        basis = f"{c['kind']}|{c['date']}|{normalize(c['label'])}|{normalize(raw or '')[:60]}"
        base_id = "kd-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
        kid, n = base_id, 2
        while kid in used_ids:
            kid, n = f"{base_id}-{n}", n + 1
        used_ids.add(kid)
        section = None
        if clause is not None:
            section = " ".join(x for x in (str(clause.get("number") or ""), clause.get("title") or "") if x) or None
        entry = {
            "id": kid,
            "kind": c["kind"],
            "label": c["label"] or _LABELS.get(c["kind"]) or (raw or "")[:80],
            "date": c["date"],
            "rawText": raw,
            "precision": c["precision"],
            "isEstimated": bool(c["isEstimated"]),
            "isDerived": bool(c["isDerived"]),
            "ambiguous": bool(c["ambiguous"]),
            "anchor": c.get("anchor"),
            "offsetValue": c.get("offsetValue"),
            "offsetUnit": c.get("offsetUnit"),
            "offsetDays": c.get("offsetDays"),
            "recurring": c.get("recurring"),
            "amount": c.get("amount"),
            "currency": currency if c.get("amount") is not None else None,
            "clauseId": clause.get("id") if clause else None,
            "clauseNumber": clause.get("number") if clause else None,
            "sectionRef": section or c.get("sourceRef"),
            "confidence": _confidence(c, clause is not None),
            "issues": sorted(set(c["issues"])),
            "origin": c["origin"],
            "periodStart": c.get("periodStart"),
        }
        out.append(entry)

    out.sort(key=lambda e: (e["date"] is None, e["date"] or "", _KIND_ORDER.get(e["kind"], 99), e["label"]))
    return {
        "keyDates": out,
        "issues": list(dict.fromkeys(doc_issues)),
        "dayFirst": day_first,
        "derived": {"termEndDate": derived_term_end},
    }


def normalise_legacy_dates(result: dict[str, Any], text: str = "") -> None:
    """Rewrite the long-standing date fields to ISO when — and only when — they
    parse unambiguously. Anything else is left exactly as the model returned it."""
    day_first = detect_day_first(text)

    def fix(holder: dict[str, Any] | None, key: str) -> None:
        if not isinstance(holder, dict):
            return
        value = holder.get(key)
        if not value or is_iso_date(value):
            return
        parsed = parse_date(value, day_first=day_first)
        if parsed["date"] and parsed["precision"] == "day":
            holder[key] = parsed["date"]

    fix(result, "effectiveDate")
    tl = result.get("timeline")
    for key in ("startDate", "endDate", "renewalDate"):
        fix(tl, key)
    fix(result.get("identification"), "executionDate")


def compact_for_record(key_dates: list[dict[str, Any]], max_bytes: int = 60_000) -> tuple[list[dict[str, Any]], bool]:
    """A size-bounded copy for the DynamoDB document record (400 KB item limit;
    the record is also returned by every list call). Same field names as the full
    list, with rawText shortened. The full list always lives in
    classification.json / timeline.json. Returns (list, truncated)."""
    import orjson

    slim: list[dict[str, Any]] = []
    for kd in key_dates:
        row = dict(kd)
        if row.get("rawText") and len(row["rawText"]) > 160:
            row["rawText"] = row["rawText"][:160]
        slim.append(row)
    if len(orjson.dumps(slim)) <= max_bytes:
        return slim, False
    kept: list[dict[str, Any]] = []
    size = 2
    for row in slim:
        size += len(orjson.dumps(row)) + 1
        if size > max_bytes:
            return kept, True
        kept.append(row)
    return kept, False
