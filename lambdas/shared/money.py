"""Deterministic money parsing.

The extraction model returns amounts as plain numbers. This module re-reads the
verbatim quote each figure was taken from, so the pipeline can catch a model
that dropped a multiplier ("USD 1.2 million" → 1.2) or a sign, and so the
playbook checks read the same formats the contracts actually use.

Handled: ``$1,200,000``, ``USD 1.2 million``, ``€1.200.000,50`` (continental
grouping), ``Rs. 5,00,000`` / ``₹5 lakh`` / ``2 crore`` (Indian grouping and
units), ``25k``, ``(1,000)`` and ``-1,000`` (negative), ``1,000 USD``.
Nothing here guesses: a string with no currency marker and no amount returns None.
"""
from __future__ import annotations

import re
from typing import Any

# symbol / code → ISO 4217
_CURRENCY_MARKS: list[tuple[str, str]] = [
    (r"US\$", "USD"), (r"USD", "USD"), (r"CAD\$", "CAD"), (r"C\$", "CAD"), (r"CAD", "CAD"),
    (r"AUD\$", "AUD"), (r"A\$", "AUD"),
    (r"AUD", "AUD"), (r"S\$", "SGD"), (r"SGD", "SGD"), (r"NZ\$", "NZD"), (r"NZD", "NZD"),
    (r"HK\$", "HKD"), (r"HKD", "HKD"), (r"EUR", "EUR"), (r"€", "EUR"), (r"GBP", "GBP"),
    (r"£", "GBP"), (r"INR", "INR"), (r"Rs\.?", "INR"), (r"₹", "INR"), (r"JPY", "JPY"),
    (r"¥", "JPY"), (r"CHF", "CHF"), (r"AED", "AED"), (r"SAR", "SAR"), (r"ZAR", "ZAR"),
    (r"CNY", "CNY"), (r"RMB", "CNY"), (r"SEK", "SEK"), (r"NOK", "NOK"), (r"DKK", "DKK"),
    (r"MXN", "MXN"), (r"BRL", "BRL"), (r"\$", "USD"),
]
_MARK_ALT = "|".join(m for m, _ in _CURRENCY_MARKS)
_MARK_LOOKUP = [(re.compile(rf"^(?:{m})$", re.IGNORECASE), code) for m, code in _CURRENCY_MARKS]

# Grouped (1,234,567.89 / 1.234.567,89 / 1'234.56 / 5,00,000 / NBSP-grouped) or plain.
# An ordinary space is never a group separator: "$5,000 120 days" is two numbers.
_NUMBER = (r"\d{1,3}(?:[,.'\u00a0\u202f]\d{2,3})+(?:[.,]\d{1,2})?(?!\d)"
           r"|\d+(?:[.,]\d+)?")
_SCALE = r"(?:thousand|million|billion|trillion|lakhs?|lacs?|crores?|mn|bn|mm|[kKmMbB](?![A-Za-z]))"
# Word-like codes (USD, Rs) must not be glued to a preceding letter ("purposes" ≠ "Rs").
_AMOUNT_RE = re.compile(
    rf"(?P<neg1>(?<![\w)])[-−]|\(\s?)?"
    rf"(?:(?<![A-Za-z])(?P<mark1>{_MARK_ALT})\s?(?P<neg2>[-−–]\s?)?(?P<num1>{_NUMBER})"
    rf"|(?P<num2>{_NUMBER})\s?(?P<scale2>{_SCALE})?\s?(?P<mark2>{_MARK_ALT})(?![A-Za-z]))"
    rf"(?:\s?(?P<scale1>{_SCALE}))?"
    rf"(?P<close>\s?\))?",
    re.IGNORECASE,
)
_PLAIN_NUMBER_RE = re.compile(rf"(?<![\w.])({_NUMBER})(?:\s?({_SCALE}))?(?![\w])")

_SCALES = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "trillion": 1e12,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7,
}
_REDUCTION_RE = re.compile(
    r"\b(reduc\w*|decreas\w*|lower\w*|deduct\w*|credit(?:ed|s)?|refund\w*|de[\s\-]?scop\w*|less|minus|discount\w*)\b",
    re.IGNORECASE,
)
_INCREASE_RE = re.compile(r"\b(increas\w*|add(?:s|ed|ing|itional)?|plus|rais\w*|uplift\w*|additional)\b",
                          re.IGNORECASE)


def currency_code(mark: str | None) -> str | None:
    if not mark:
        return None
    for pattern, code in _MARK_LOOKUP:
        if pattern.match(mark.strip()):
            return code
    return None


def parse_number(raw: str, scaled: bool = False) -> float | None:
    """Parse a grouped number in US (1,234.56), continental (1.234,56), Swiss
    (1'234.56), spaced (1 234,56) or Indian (5,00,000) notation."""
    s = re.sub(r"[\s'\u00a0\u202f]", "", raw or "")
    if not s or not re.search(r"\d", s):
        return None
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):          # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                     # 1,234.56
            s = s.replace(",", "")
    elif has_comma:
        head, _, tail = s.rpartition(",")
        # "1,5" / "12,50" → decimal comma; "1,234" / "5,00,000" → grouping.
        if len(tail) in (1, 2) and "," not in head:
            s = f"{head}.{tail}"
        else:
            s = s.replace(",", "")
    elif has_dot and s.count(".") > 1:            # 1.200.000
        s = s.replace(".", "")
    elif has_dot:
        head, _, tail = s.rpartition(".")
        # ...but not before a scale word: "1.125 million" is one and an eighth.
        if not scaled and len(tail) == 3 and head.isdigit() and len(head) <= 3 and head != "0":
            # "1.200" is ambiguous in isolation (1.2 vs 1200); a money amount with
            # exactly three decimals is almost always continental grouping.
            s = head + tail
    try:
        return float(s)
    except ValueError:
        return None


def _scale(raw: str | None) -> float:
    if not raw:
        return 1.0
    return _SCALES.get(raw.strip().lower(), 1.0)


def find_amounts(text: str) -> list[dict[str, Any]]:
    """Every currency-marked amount in ``text``: [{amount, currency, start, end, raw}]."""
    out: list[dict[str, Any]] = []
    for m in _AMOUNT_RE.finditer(text or ""):
        number = m.group("num1") or m.group("num2")
        scale = m.group("scale1") or m.group("scale2")
        value = parse_number(number, scaled=bool(scale))
        if value is None:
            continue
        value *= _scale(scale)
        opened = (m.group("neg1") or "").strip()
        negative = bool(m.group("neg2")) or opened in ("-", "−", "–") or (opened == "(" and bool(m.group("close")))
        out.append({
            "amount": -value if negative else value,
            "currency": currency_code(m.group("mark1") or m.group("mark2")),
            "start": m.start(), "end": m.end(), "raw": m.group(0).strip(),
        })
    return out


def parse_amount(text: str) -> float | None:
    """The first currency-marked amount in ``text`` (scaled, signed), or None."""
    found = find_amounts(text)
    return found[0]["amount"] if found else None


def detect_currency(text: str) -> str | None:
    """The currency the document's amounts are stated in — only when unambiguous
    enough to assert: the single currency used, or one used for ≥ 80% of amounts."""
    counts: dict[str, int] = {}
    for a in find_amounts(text or ""):
        if a["currency"]:
            counts[a["currency"]] = counts.get(a["currency"], 0) + 1
    if not counts:
        return None
    best = max(counts, key=lambda c: counts[c])
    return best if counts[best] / sum(counts.values()) >= 0.8 else None


def amounts_in_quote(quote: str) -> list[float]:
    """All amounts a quote could evidence: currency-marked ones first, then bare
    numbers (a table cell quoted as "Milestone 2 | 12,500")."""
    marked = [a["amount"] for a in find_amounts(quote or "")]
    if marked:
        return marked
    out: list[float] = []
    for m in _PLAIN_NUMBER_RE.finditer(quote or ""):
        value = parse_number(m.group(1))
        if value is not None:
            out.append(value * _scale(m.group(2)))
    return out


def quote_supports(amount: float | None, quote: str | None, tolerance: float = 1.0) -> bool | None:
    """Does ``quote`` actually state ``amount``? None when the quote has no number
    to check against (cannot verify either way)."""
    if amount is None or not quote:
        return None
    candidates = amounts_in_quote(quote)
    if not candidates:
        return None
    return any(abs(abs(c) - abs(float(amount))) <= tolerance for c in candidates)


def implied_sign(quote: str | None) -> int | None:
    """-1 if the wording describes a reduction, +1 an increase, None if unclear."""
    if not quote:
        return None
    down, up = bool(_REDUCTION_RE.search(quote)), bool(_INCREASE_RE.search(quote))
    if down and not up:
        return -1
    if up and not down:
        return 1
    return None
