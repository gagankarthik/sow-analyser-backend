"""Deterministic date parsing, normalisation and relative-date arithmetic.

The extraction model is asked for ISO dates, but a contract states dates in many
shapes ("1st March 2026", "03/01/2026", "Q2 2026", "thirty (30) days after the
Effective Date"). Everything that becomes a stored date goes through this module
so the rules are the same everywhere and are unit-testable without a model:

* an explicit calendar date is normalised to ISO 8601 (``YYYY-MM-DD``);
* a numeric date whose day/month order cannot be established is NOT guessed —
  it is returned with ``ambiguous=True`` and no ISO date;
* a period ("Q2 2026", "March 2026", "2026") resolves to the LAST day of the
  period (the latest an obligation "due in" that period falls due), flagged
  ``estimated`` with its ``precision`` and the period start alongside;
* a relative rule ("30 days after the Effective Date") is parsed into
  value / unit / direction / anchor so it can be resolved when the anchor date is
  known and kept as a rule when it is not;
* an impossible date (30 February) is rejected, never rolled over.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T\s].*)?$")
_DMY_TEXT_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+day)?(?:\s+of)?[\s\-.,]+({_MONTH_ALT})\.?,?[\s\-.,]+(\d{{4}}|\d{{2}})\b",
    re.IGNORECASE,
)
_MDY_TEXT_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
# With "/" or "-" a two-digit year is accepted; with "." only a four-digit year,
# because "12.3.10" is far more often a clause number than a date.
_NUMERIC_RE = re.compile(
    r"(?<![\d./\-])(\d{1,2})(?:([/\-])(\d{1,2})\2(\d{4}|\d{2})|(\.)(\d{1,2})\.(\d{4}))(?!\d)(?![./\-]\d)"
)
_ORDINAL_DAY_RE = re.compile(r"\b(first|last)\s+day\s+of\b", re.IGNORECASE)
_MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_NUM_MONTH_YEAR_RE = re.compile(r"^\s*(\d{1,2})[/\-](\d{4})\s*$")
_QUARTER_RE = re.compile(r"\b(?:Q([1-4])|([1-4])(?:st|nd|rd|th)\s+quarter|(first|second|third|fourth)\s+quarter)"
                         r"(?:\s+of)?[\s,\-]*(?:FY\s*|CY\s*)?(\d{4})\b", re.IGNORECASE)
_HALF_RE = re.compile(r"\bH([12])[\s\-]*(\d{4})\b", re.IGNORECASE)
_YEAR_ONLY_RE = re.compile(r"^\s*(?:(?:end|close)\s+of\s+|by\s+|in\s+|during\s+)?(?:FY\s*|CY\s*|year\s+)?(\d{4})\s*$",
                           re.IGNORECASE)
_ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4}

MIN_YEAR, MAX_YEAR = 1950, 2150


def _result(**kw: Any) -> dict[str, Any]:
    out = {"date": None, "precision": None, "ambiguous": False, "estimated": False,
           "periodStart": None, "reason": None}
    out.update(kw)
    return out


def _year(raw: str) -> int:
    y = int(raw)
    if len(raw) == 2:
        y += 2000 if y < 70 else 1900
    return y


def _make(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def is_iso_date(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) \
        and _safe_date(value) is not None


def _safe_date(iso: str) -> date | None:
    try:
        return date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return None


def parse_date(raw: Any, *, day_first: bool | None = None) -> dict[str, Any]:
    """Normalise one date expression.

    Returns ``{date, precision, ambiguous, estimated, periodStart, reason}``.
    ``date`` is ISO or None. ``day_first`` is the document's numeric-date
    convention (True = DD/MM, False = MM/DD, None = unknown).
    """
    if raw is None:
        return _result(reason="absent")
    text = str(raw).strip()
    if not text:
        return _result(reason="absent")

    m = _ISO_RE.match(text)
    if m:
        iso = _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return _result(date=iso, precision="day") if iso else _result(reason="invalid_date")

    # Whichever written-out date comes FIRST in the text wins ("April 1, 2024
    # through 31 March 2025" is the 1st of April).
    dmy, mdy = _DMY_TEXT_RE.search(text), _MDY_TEXT_RE.search(text)
    if dmy and (not mdy or dmy.start() <= mdy.start()):
        iso = _make(_year(dmy.group(3)), _MONTHS[dmy.group(2).lower()], int(dmy.group(1)))
        return _result(date=iso, precision="day") if iso else _result(reason="invalid_date")
    if mdy:
        iso = _make(int(mdy.group(3)), _MONTHS[mdy.group(1).lower()], int(mdy.group(2)))
        return _result(date=iso, precision="day") if iso else _result(reason="invalid_date")

    m = _NUMERIC_RE.search(text)
    if m:
        a, b, y = _numeric_parts(m)
        if a > 12 and b > 12:
            return _result(reason="invalid_date")
        if a > 12:                      # can only be DD/MM
            iso = _make(y, b, a)
        elif b > 12:                    # can only be MM/DD
            iso = _make(y, a, b)
        elif a == b:                    # 05/05 — same either way
            iso = _make(y, a, b)
        elif day_first is True:
            iso = _make(y, b, a)
        elif day_first is False:
            iso = _make(y, a, b)
        else:
            return _result(ambiguous=True, reason="ambiguous_day_month")
        return _result(date=iso, precision="day") if iso else _result(reason="invalid_date")

    m = _QUARTER_RE.search(text)
    if m:
        q = int(m.group(1) or m.group(2) or _ORDINAL_Q[m.group(3).lower()])
        y = int(m.group(4))
        end_month = q * 3
        return _result(
            date=_make(y, end_month, calendar.monthrange(y, end_month)[1]),
            periodStart=_make(y, end_month - 2, 1), precision="quarter", estimated=True,
        )

    m = _HALF_RE.search(text)
    if m:
        h, y = int(m.group(1)), int(m.group(2))
        end_month = 6 if h == 1 else 12
        return _result(date=_make(y, end_month, calendar.monthrange(y, end_month)[1]),
                       periodStart=_make(y, end_month - 5, 1), precision="half", estimated=True)

    m = _MONTH_YEAR_RE.search(text)
    if m:
        mo, y = _MONTHS[m.group(1).lower()], int(m.group(2))
        which = _ORDINAL_DAY_RE.search(text[:m.start()])
        if which:                       # "the first day of January 2026"
            day = 1 if which.group(1).lower() == "first" else calendar.monthrange(y, mo)[1]
            return _result(date=_make(y, mo, day), precision="day")
        return _result(date=_make(y, mo, calendar.monthrange(y, mo)[1]),
                       periodStart=_make(y, mo, 1), precision="month", estimated=True)

    m = _NUM_MONTH_YEAR_RE.match(text)
    if m and 1 <= int(m.group(1)) <= 12:
        mo, y = int(m.group(1)), int(m.group(2))
        return _result(date=_make(y, mo, calendar.monthrange(y, mo)[1]),
                       periodStart=_make(y, mo, 1), precision="month", estimated=True)

    m = _YEAR_ONLY_RE.match(text)
    if m and MIN_YEAR <= int(m.group(1)) <= MAX_YEAR:
        y = int(m.group(1))
        return _result(date=_make(y, 12, 31), periodStart=_make(y, 1, 1),
                       precision="year", estimated=True)

    return _result(reason="unparsed")


def _numeric_parts(m: re.Match[str]) -> tuple[int, int, int]:
    if m.group(2):
        return int(m.group(1)), int(m.group(3)), _year(m.group(4))
    return int(m.group(1)), int(m.group(6)), int(m.group(7))


def detect_day_first(text: str) -> bool | None:
    """Work out the document's numeric-date convention from its own dates.

    Evidence, strongest first: a numeric date that is only valid one way
    (13/02/2026 → day-first; 02/13/2026 → month-first); then the style of the
    written-out dates ("1 March 2026" vs "March 1, 2026"). Conflicting or absent
    evidence returns None — the caller must then treat 03/04/2026 as ambiguous.
    """
    if not text:
        return None
    day_first = month_first = 0
    for m in _NUMERIC_RE.finditer(text):
        a, b, _ = _numeric_parts(m)
        if a > 12 >= b:
            day_first += 1
        elif b > 12 >= a:
            month_first += 1
    if day_first and not month_first:
        return True
    if month_first and not day_first:
        return False
    if day_first and month_first:
        return None
    dmy = len(_DMY_TEXT_RE.findall(text))
    mdy = len(_MDY_TEXT_RE.findall(text))
    if dmy >= 2 and mdy == 0:
        return True
    if mdy >= 2 and dmy == 0:
        return False
    return None


# ---------------------------------------------------------------------------
# Relative dates ("30 days after the Effective Date")
# ---------------------------------------------------------------------------

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_WORD_ALT = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_NUM_TOKEN = rf"(?:{_NUM_WORD_ALT}|hundred)"
_UNIT_ALT = r"(business\s+days?|working\s+days?|calendar\s+days?|days?|weeks?|months?|years?)"
_FILLER = r"(?:consecutive\s+|full\s+|calendar\s+(?=months?|years?))?"
# "thirty (30) days" / "thirty days" (words, optional digits in brackets) | "30 days" / "(30) days"
_QUANTITY_RE = re.compile(
    rf"\b({_NUM_TOKEN}(?:[\s\-]+(?:and\s+)?{_NUM_TOKEN})*)\s*(?:\(\s*(\d{{1,4}})\s*\)\s*)?{_FILLER}{_UNIT_ALT}\b"
    rf"|\(?(?<![\d.,])(\d{{1,4}})\)?\s*{_FILLER}{_UNIT_ALT}\b",
    re.IGNORECASE,
)
_NET_RE = re.compile(r"\bnet\s*[\-]?\s*(\d{1,3})\b", re.IGNORECASE)
_BEFORE_RE = re.compile(r"\b(before|prior\s+to|preceding|in\s+advance\s+of|ahead\s+of)\b", re.IGNORECASE)
_AFTER_RE = re.compile(
    r"\b(after|from|following|of|upon|since|beyond|post|commencing\s+on|starting\s+on|from\s+and\s+after)\b",
    re.IGNORECASE,
)

# anchor phrase → canonical anchor key
_ANCHORS: list[tuple[str, re.Pattern[str]]] = [
    ("effective", re.compile(r"\beffective\s+date\b|\bdate\s+of\s+this\s+(?:agreement|sow|statement|amendment)\b|\bdate\s+hereof\b", re.I)),
    ("signature", re.compile(r"\bsignature\b|\bsigning\b|\bexecution\b|\bexecuted\b|\bsigned\b", re.I)),
    ("start", re.compile(r"\bcommencement\b|\bstart\s+date\b|\bkick[\s\-]?off\b|\bproject\s+start\b|\bgo[\s\-]?live\b", re.I)),
    ("term_end", re.compile(r"\bexpir(?:y|ation)\b|\bend\s+of\s+the\s+(?:initial\s+|then[\s\-]current\s+|renewal\s+)?term\b"
                            r"|\bterm\s+end\b|\btermination\s+date\b|\brenewal\s+date\b|\bend\s+date\b"
                            r"|\bthen[\s\-]current\s+term\b|\bend\s+of\s+the\s+term\b", re.I)),
    ("invoice", re.compile(r"\binvoice\b|\bbilling\b", re.I)),
    ("acceptance", re.compile(r"\bacceptance\b|\baccepted\b", re.I)),
    ("delivery", re.compile(r"\bdelivery\b|\bdelivered\b|\breceipt\b|\bcompletion\b|\bsubmission\b", re.I)),
    ("notice", re.compile(r"\bnotice\b|\bnotification\b", re.I)),
]

_UNIT_DAYS = {"days": 1, "business_days": 1, "weeks": 7, "months": 30, "years": 365}


def words_to_int(text: str) -> int | None:
    """'thirty' → 30, 'twenty-one' → 21, 'one hundred and twenty' → 120."""
    if not text:
        return None
    tokens = re.findall(r"[a-z]+", text.lower())
    total, seen = 0, False
    for tok in tokens:
        if tok in ("and", "a", "an"):
            continue
        if tok == "hundred":
            total = (total or 1) * 100
            seen = True
        elif tok in _NUM_WORDS:
            total += _NUM_WORDS[tok]
            seen = True
        else:
            return None
    return total if seen else None


def _unit(raw: str) -> str:
    u = re.sub(r"\s+", " ", raw.lower())
    if u.startswith(("business", "working")):
        return "business_days"
    if u.startswith(("calendar", "day")):
        return "days"
    if u.startswith("week"):
        return "weeks"
    if u.startswith("month"):
        return "months"
    return "years"


def parse_quantity(text: str) -> tuple[int, str] | None:
    """First duration in ``text`` as (value, unit). Digits in brackets win over
    the written-out number: "thirty (30) days" → (30, "days")."""
    m = _QUANTITY_RE.search(text or "")
    return _quantity(m) if m else None


def _quantity(m: re.Match[str]) -> tuple[int, str] | None:
    if m.group(4):
        return int(m.group(4)), _unit(m.group(5))
    if m.group(2):
        return int(m.group(2)), _unit(m.group(3))
    value = words_to_int(m.group(1) or "")
    return (value, _unit(m.group(3))) if value is not None else None


def anchor_key(text: str | None) -> str | None:
    """Map an anchor phrase ("the Effective Date", "receipt of invoice") to a key."""
    if not text:
        return None
    for key, pattern in _ANCHORS:
        if pattern.search(text):
            return key
    return None


def parse_offset(text: str | None) -> dict[str, Any] | None:
    """Parse a relative-date rule.

    "thirty (30) days after the Effective Date" →
        {value: 30, unit: "days", direction: 1, anchor: "effective", anchorText: "..."}
    "Net 30 from invoice" → {value: 30, unit: "days", direction: 1, anchor: "invoice"}
    "90 days prior to expiry" → direction -1, anchor "term_end".
    Returns None when the text holds no duration.
    """
    if not text:
        return None
    net = _NET_RE.search(text)
    if net:
        tail = text[net.end():]
        return {"value": int(net.group(1)), "unit": "days", "direction": 1,
                "anchor": anchor_key(tail) or "invoice",
                "anchorText": tail.strip(" .,;:") or "invoice"}
    m = _QUANTITY_RE.search(text)
    qty = _quantity(m) if m else None
    if not qty:
        return None
    tail = text[m.end():]
    before = _BEFORE_RE.search(tail)
    after = _AFTER_RE.search(tail)
    if before and (not after or before.start() <= after.start()):
        direction, anchor_text = -1, tail[before.end():]
    elif after:
        direction, anchor_text = 1, tail[after.end():]
    else:
        direction, anchor_text = 1, tail
    anchor_text = anchor_text.strip(" .,;:")
    return {"value": qty[0], "unit": qty[1], "direction": direction,
            "anchor": anchor_key(anchor_text) or anchor_key(text), "anchorText": anchor_text or None}


def offset_days(value: int, unit: str) -> int:
    """Approximate length of an offset in days (months = 30, years = 365)."""
    return int(value) * _UNIT_DAYS.get(unit, 1)


def add_offset(iso: str, value: int, unit: str, direction: int = 1) -> str | None:
    """Apply an offset to an ISO date with calendar arithmetic.

    Months and years move by the calendar (31 Jan + 1 month = 28/29 Feb), business
    days skip weekends (public holidays are not known, so the caller should mark
    the result as estimated).
    """
    base = _safe_date(iso)
    if base is None:
        return None
    n = int(value) * (1 if direction >= 0 else -1)
    try:
        if unit == "days":
            return (base + timedelta(days=n)).isoformat()
        if unit == "weeks":
            return (base + timedelta(weeks=n)).isoformat()
        if unit == "business_days":
            step, left, cur = (1 if n >= 0 else -1), abs(n), base
            while left:
                cur += timedelta(days=step)
                if cur.weekday() < 5:
                    left -= 1
            return cur.isoformat()
        months = n if unit == "months" else n * 12
        idx = base.year * 12 + (base.month - 1) + months
        y, mo = divmod(idx, 12)
        mo += 1
        return date(y, mo, min(base.day, calendar.monthrange(y, mo)[1])).isoformat()
    except (ValueError, OverflowError):
        return None


def plausible(iso: str | None) -> bool:
    d = _safe_date(iso) if iso else None
    return d is not None and MIN_YEAR <= d.year <= MAX_YEAR


_ISO_INLINE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def find_dates(text: str, *, day_first: bool | None = None) -> list[tuple[str, int, int]]:
    """Every unambiguous calendar date written in ``text`` as (iso, start, end),
    in order of appearance. Used to cite the clause a date came from."""
    out: list[tuple[str, int, int]] = []
    for pattern in (_ISO_INLINE_RE, _DMY_TEXT_RE, _MDY_TEXT_RE, _NUMERIC_RE):
        for m in pattern.finditer(text or ""):
            parsed = parse_date(m.group(0), day_first=day_first)
            if parsed["date"] and parsed["precision"] == "day":
                out.append((parsed["date"], m.start(), m.end()))
    out.sort(key=lambda t: t[1])
    return out
