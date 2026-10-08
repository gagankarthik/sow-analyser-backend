"""your organization review matrix — per-agreement-type playbooks, deterministic grading, and
the deterministic extractors Govern needs after a document is analysed.

WHAT THE MATRIX IS
------------------
A matrix is a dated, immutable version (``version``, ``effectiveDate``) holding
one playbook per agreement type (sponsored research, grant, license, option,
MTA, NDA, collaboration, other). A playbook is a list of matrix clauses; each
clause names a clause type (a known category id such as ``"PublicationRights"``,
or ``type.<key>`` for a custom type, exactly like ``shared/playbook.py``) and
carries:

  standard            your organization's standard position (text shown to reviewers)
  fallback            the acceptable fallback position, or None
  unacceptable        phrases your organization never accepts (literal, case-insensitive)
  beneficial          phrases that are favourable to your organization
  escalationOffice    the office that must review a deviation, or None
  suggestedLanguage   the redline your organization proposes when the clause deviates
  thresholds          numbers the built-in check reads ("maxReviewDays": 30)
  required            a required clause missing from the document is a finding

``default_matrix()`` is the proposed your organization-style matrix (your organization to confirm). Admins
replace it through ``PUT /matrix`` (``validate_matrix``) or a CSV/Excel import
(``parse_import`` + ``merge_import``); every save is a new version and every
review records the version it used.

HOW A DOCUMENT IS GRADED (deterministic — no model call)
-------------------------------------------------------
For every clause of the agreement type's playbook:

  1. The document clauses of that type are found: by category (the classify
     stage labels them), else by a closely related category the playbook does
     not grade on its own (``IP`` for ``BackgroundIP`` …), else by the clause's
     own heading (one plainly about the subject, e.g. "Publication").
  2. Their text is graded by the built-in check for that clause type (ten
     research/licensing checks plus a few commercial ones), using the matrix
     clause's thresholds, then by its unacceptable / beneficial phrase lists.
     The more serious result wins. A phrase preceded in its sentence by a
     negation ("Nothing in this Agreement waives …") does not count.
  3. The result is one tier:

       within        inside your organization's standard position
       fallback      outside the standard but inside the acceptable fallback
       deviates      outside the fallback — needs a change
       unacceptable  a term your organization never accepts (or an unacceptable phrase)
       review        the clause is there but its text does not settle the
                     question (or the rule has no automatic check) — a human
                     must compare it with the standard position
       missing       a REQUIRED matrix clause has no matching clause at all

     and, independently, ``beneficial`` when the clause carries a term that is
     favourable to your organization (equity, a minimum annual royalty, dated diligence
     milestones, a matching beneficial phrase …).

A matrix clause that is not required and is absent from the document is not
reported. Clauses of the document that the playbook has no rule for are not
graded here (the general playbook in ``shared/playbook.py`` still grades them).
The same clauses and the same matrix always give the same review.

EXTRACTORS
----------
``infer_agreement_type``, ``infer_direction``, ``extract_income`` and
``extract_obligations`` read the classification the pipeline already produced
(clause bodies, commercials, key dates) with regular expressions and the shared
money / date parsers — again with no model call, so ids and values are stable
across re-runs.
"""
from __future__ import annotations

import copy
import csv
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, NamedTuple

from ..clause_types import KNOWN_CATEGORIES, known_label, normalise_type
from ..dates import add_offset, find_dates, parse_quantity
from ..money import find_amounts
from ..playbook import rule_id_for_clause, valid_rule_id

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

AGREEMENT_TYPES: list[str] = [
    "sponsored_research", "clinical_trial", "grant", "license", "option", "mta", "data_use", "nda",
    "collaboration", "software", "sow", "msa", "staffing", "other",
]

# Requirement 7: one Govern, two editions. Each edition offers its own
# agreement types (the vocabulary) and the matrix positions for them (the rule
# set); everything else is shared. Types not listed for an edition stay in the
# data model and the matrix, so switching an edition back loses nothing.
EDITIONS: tuple[str, ...] = ("campus", "workforce")
EDITION_AGREEMENT_TYPES: dict[str, list[str]] = {
    "campus": ["sponsored_research", "license", "mta", "grant", "clinical_trial", "option", "data_use", "nda",
               "collaboration", "software", "other"],
    "workforce": ["sow", "msa", "staffing", "nda", "software", "other"],
}


def edition_agreement_types(edition: str | None) -> list[str]:
    """The agreement types an edition offers (campus when unknown)."""
    return list(EDITION_AGREEMENT_TYPES.get(edition or "campus", EDITION_AGREEMENT_TYPES["campus"]))
AGREEMENT_TYPE_LABELS: dict[str, str] = {
    "sponsored_research": "Sponsored research",
    "clinical_trial": "Clinical trial",
    "grant": "Grant / subaward",
    "license": "License",
    "option": "Option",
    "mta": "Material transfer (MTA)",
    "data_use": "Data use (DUA)",
    "nda": "Non-disclosure (NDA / CDA)",
    "collaboration": "Collaboration",
    "software": "Software or SaaS (purchase)",
    "sow": "Statement of work (SOW)",
    "msa": "Master services agreement (MSA)",
    "staffing": "Staffing vendor agreement",
    "other": "Other",
}

OFFICES: list[str] = [
    "legal_affairs", "tech_commercialization", "sponsored_programs", "export_control", "risk_management",
    "procurement", "it_security", "accessibility",
]
OFFICE_LABELS: dict[str, str] = {
    "legal_affairs": "Legal Affairs",
    "tech_commercialization": "Technology Commercialization",
    "sponsored_programs": "Sponsored Programs",
    "export_control": "Export Control",
    "risk_management": "Risk Management",
    "procurement": "Procurement",
    "it_security": "IT Security",
    "accessibility": "Digital Accessibility",
}

# The ten research / licensing clause types the built-in checks cover.
RESEARCH_CLAUSE_TYPES: list[str] = [
    "PublicationRights", "BackgroundIP", "LicenseScope", "Royalties", "Indemnity",
    "GoverningLaw", "ExportControl", "DataRights", "SponsorReporting", "Diligence",
]

TIERS: list[str] = ["within", "fallback", "deviates", "unacceptable", "review", "missing"]

# Matrix labels for the ten research / licensing types (the requirement's own
# wording). Other clause types use the clause taxonomy's label.
_MATRIX_LABELS: dict[str, str] = {
    "PublicationRights": "Publication rights and review period",
    "BackgroundIP": "Background and foreground IP ownership",
    "LicenseScope": "License grant scope (exclusivity, field of use, territory)",
    "Royalties": "Royalties, milestones, equity and sublicense income",
    "Indemnity": "Indemnification and insurance",
    "GoverningLaw": "Governing law and sovereign immunity",
    "ExportControl": "Export control and foreign party restrictions",
    "DataRights": "Data rights, confidentiality term and use of your organization's name",
    "SponsorReporting": "Sponsor reporting and grant flow-down terms",
    "Diligence": "Diligence and termination for failure to commercialize",
}

DEFAULT_EFFECTIVE_DATE = "2026-10-08"
DEFAULT_NOTE = "Default research and licensing matrix (edit to match your positions)"

_TIER_RANK = {"within": 0, "fallback": 1, "review": 2, "deviates": 3, "unacceptable": 4}
_QUOTE_CHARS = 600


def matrix_clause_label(clause_type: str) -> str:
    """Human label of a matrix clause type ("Royalties" → "Royalties, milestones,
    equity and sublicense income"; "type.non-solicitation" → "Non solicitation")."""
    if clause_type in _MATRIX_LABELS:
        return _MATRIX_LABELS[clause_type]
    if clause_type.startswith("type."):
        key = clause_type.split(".", 1)[1]
        return key.replace("-", " ").capitalize()
    return known_label(clause_type)


# ---------------------------------------------------------------------------
# Text helpers (deterministic)
# ---------------------------------------------------------------------------

# Sentence boundary: ". " / "; " before a capital, a blank line, or a new list
# item. Common abbreviations ("Inc.", "U.S.", "No.", "Dr.") do not end a sentence.
_SENTENCE_RE = re.compile(
    r"(?<=[.;])(?<!\bInc\.)(?<!\bCorp\.)(?<!\bLtd\.)(?<!\bNo\.)(?<!\bDr\.)(?<!U\.S\.)(?<!\bSt\.)(?<!\bCo\.)"
    r"\s+(?=[A-Z(\"“])"
    r"|\n\s*\n"
    r"|\n(?=\s*(?:\(?[a-z0-9ivx]{1,4}[.)]|[-•*])\s)"
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:\(?[a-z0-9ivx]{1,4}[.)]|[-•*])\s")
_NEGATION_RE = re.compile(r"\b(not|no|nothing|never|neither|nor|without)\b", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3})?)\s*(?:%|percent\b|per\s+cent\b)", re.IGNORECASE)
_DUR_RE = re.compile(
    r"\(?\b(\d{1,4})\)?\s*(?:calendar\s+|business\s+|working\s+|consecutive\s+)?(day|week|month|year)s?\b",
    re.IGNORECASE,
)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _sentences(text: str) -> list[str]:
    """Split clause text into sentences / list items, whitespace-normalised."""
    out = []
    for part in _SENTENCE_RE.split(text or ""):
        part = re.sub(r"\s+", " ", part or "").strip()
        if part:
            out.append(part)
    return out


def _negated(sentence: str, idx: int, window: int = 80) -> bool:
    """True when a negation word precedes position ``idx`` in its sentence."""
    return bool(_NEGATION_RE.search(sentence[max(0, idx - window):idx]))


def _money_percents(sentence: str) -> list[tuple[str, float]]:
    """Each percentage in a sentence with what it is a percentage of, read from
    the words around it: "royalty" (of net sales), "sublicense" (of sublicense
    income), "equity" (of shares / capitalisation) or "other". One sentence may
    state several ("3% of Net Sales and 25% of Sublicense Income")."""
    out: list[tuple[str, float]] = []
    for m in _PCT_RE.finditer(sentence or ""):
        after = sentence[m.end():m.end() + 70].lower()
        before = sentence[max(0, m.start() - 90):m.start()].lower()
        # what follows the figure decides, nearest mention first ("3% of Net Sales and 25% of Sublicense Income")
        sales = re.search(r"net\s+sales|sales\s+of|net\s+revenue", after)
        sub_at = after.find("sublicens")
        if sub_at >= 0 and (sales is None or sub_at < sales.start()):
            kind = "sublicense"
        elif sales:
            kind = "royalty"
        elif re.search(r"equity|shares|stock|capitaliz", before + " " + after):
            kind = "equity"
        elif "sublicens" in before:
            kind = "sublicense"
        elif "royalt" in before:
            kind = "royalty"
        else:
            kind = "other"
        out.append((kind, float(m.group(1))))
    return out


def _durations(text: str) -> list[tuple[int, str, int]]:
    """Every duration in ``text`` as (days, unit, start offset). "ninety (90)
    days" → (90, "day", …); a duration written only in words falls back to the
    shared parser ("thirty days")."""
    out = [(int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()], m.group(2).lower(), m.start())
           for m in _DUR_RE.finditer(text or "")]
    if not out:
        qty = parse_quantity(text or "")
        if qty:
            unit = {"days": "day", "business_days": "day", "weeks": "week", "months": "month",
                    "years": "year"}.get(qty[1], "day")
            out.append((qty[0] * _UNIT_DAYS[unit], unit, 0))
    return out


def _fmt(n: float) -> str:
    return f"{n:g}"


def _th(thresholds: dict[str, Any], key: str) -> float:
    """A threshold value: the clause's own, else the built-in default."""
    value = thresholds.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if key in _THRESHOLD_DEFAULTS:
        return float(_THRESHOLD_DEFAULTS[key])
    from . import software, workforce  # buyer-side thresholds (software, services, staffing)
    if key in software.THRESHOLD_DEFAULTS:
        return float(software.THRESHOLD_DEFAULTS[key])
    return float(workforce.THRESHOLD_DEFAULTS.get(key, 0))


# "Our" party in a research or licensing agreement.
_US = r"(?:the\s+)?(?:university|institution|college|our\s+organi[sz]ation)"
_OTHER = (r"(?:the\s+)?(?:sponsor|licensee|optionee|company|recipient|subrecipient|provider|collaborator|"
          r"pass-through\s+entity|contractor)")


class _Check(NamedTuple):
    """Outcome of a built-in check: tier, reason sentence, value read, and the
    reason the clause is beneficial to your organization (None if it is not)."""

    tier: str
    reason: str | None = None
    found: str | None = None
    beneficial: str | None = None


# ---------------------------------------------------------------------------
# Built-in checks — one per clause type. Each reads the clause text and the
# matrix clause's thresholds. Reasons are plain sentences a reviewer can send.
# ---------------------------------------------------------------------------


def _check_publication(text: str, th: dict[str, Any]) -> _Check:
    """Right to publish; sponsor review ≤ maxReviewDays; total delay including a
    patent deferral ≤ fallbackReviewDays. Sponsor approval / veto → unacceptable."""
    max_days, fb_days = _th(th, "maxReviewDays"), _th(th, "fallbackReviewDays")
    sents = _sentences(text)
    # Approval / veto wording counts only where publication is the subject; the
    # review and delay periods may sit in the next sentence ("Sponsor has 30
    # days to review …"), so durations are read from the whole clause.
    pub = [s for s in sents if re.search(r"publi(?:sh|cation)|manuscript|present", s, re.IGNORECASE)]
    for s in pub:
        low = s.lower()
        # (pattern, whether a preceding negation cancels it)
        for pattern, negatable in (
                (rf"{_US}\s+(?:shall|may|will)\s+not\s+publish", False),
                (r"(?:prohibit|prevent|block|veto)\w*[^.;]{0,60}publication", True),
                (r"(?:approval|consent)\s+of\s+(?:the\s+)?(?:sponsor|company|provider|licensee)", True),
                (r"(?:sponsor|company|provider|licensee)'?s?\s+(?:prior\s+)?(?:written\s+)?"
                 r"(?:approval|consent)", True)):
            m = re.search(pattern, low)
            if m and not (negatable and _negated(low, m.start())):
                return _Check("unacceptable", "Publication needs the other party's approval or can be blocked; "
                                              "your organization must keep the right to publish.", "sponsor approval of publications")
    review = deferral = 0
    for s in sents:
        for days, unit, pos in _durations(s):
            if unit == "year":
                continue
            before = s[max(0, pos - 70):pos].lower()
            if re.search(r"additional|further|delay|defer|postpone|extend", before):
                deferral = max(deferral, days)
            else:
                review = max(review, days)
    total = review + deferral
    if total == 0:
        if re.search(r"(?:free|right)\s+to\s+publish|may\s+publish", text, re.IGNORECASE):
            return _Check("within", None, "right to publish, no review delay")
        return _Check("review", "No publication review period was found; the matrix requires your organization's right to "
                                f"publish with a review of no more than {_fmt(max_days)} days.")
    found = f"{review} days' review" + (f" + {deferral} days' patent delay" if deferral else "")
    if total <= max_days:
        return _Check("within", None, found)
    what = "Publication review and patent delay total" if deferral else "Publication review period is"
    reason = f"{what} {total} days; the matrix allows {_fmt(max_days)} (fallback {_fmt(fb_days)})."
    if total <= fb_days:
        return _Check("fallback", reason, found)
    return _Check("deviates", reason, found)


def _check_background_ip(text: str, th: dict[str, Any]) -> _Check:
    """your organization keeps its background IP and owns inventions made by its employees; the
    other party may get an option or a non-exclusive research licence. An
    assignment of your organization's rights or "work made for hire" is unacceptable."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    for pattern in (r"works?\s+(?:made\s+)?for\s+hire",
                    rf"{_US}\s+(?:hereby\s+)?(?:assigns?|shall\s+assign|agrees\s+to\s+assign|transfers?)\b",
                    r"\b(?:all|any)\s+(?:right,?\s+title\s+and\s+interest\s+in\s+(?:and\s+to\s+)?)?"
                    r"(?:inventions?|intellectual\s+property|results|foreground\s+ip|developments)"
                    rf"[^.;]{{0,120}}(?:owned\s+(?:solely\s+|exclusively\s+)?by|property\s+of|vest\s+in|belong\s+to)\s+{_OTHER}"):
        m = re.search(pattern, low)
        if m and not _negated(low, m.start(), 40):
            return _Check("unacceptable", "your organization would assign or give up ownership of its inventions or background "
                                          "IP; your organization owns inventions made by its employees.", "assignment of your organization IP")
    exclusive_royalty_free = (r"royalty[- ]free[^.;]{0,40}(?<!non-)(?<!non)\bexclusive"
                              r"|(?<!non-)(?<!non)\bexclusive[^.;]{0,40}royalty[- ]free")
    if re.search(exclusive_royalty_free, low):
        return _Check("deviates", "The other party gets a royalty-free exclusive licence to your organization inventions; the "
                                  "matrix allows an option to negotiate or a non-exclusive research licence.",
                      "royalty-free exclusive licence")
    beneficial = None
    if re.search(rf"{_OTHER}\s+(?:hereby\s+)?grants?\s+(?:back\s+)?to\s+{_US}[^.;]{{0,120}}licen[cs]e", low):
        beneficial = "your organization receives a licence back to the other party's improvements."
    if re.search(rf"(?:each\s+party|{_US})\s+(?:shall\s+|will\s+)?(?:retain|retains|own|owns|hold|holds|keep)"
                 rf"|title\s+[^.;]{{0,80}}(?:remain|vest)s?\s+(?:with|in)\s+{_US}"
                 rf"|(?:owned\s+by|property\s+of|vest\s+in)\s+{_US}|background[^.;]{{0,80}}retain", low):
        return _Check("within", None, "your organization retains its IP", beneficial)
    return _Check("review", "Ownership of background IP and inventions is not stated clearly; confirm your organization retains "
                            "its background IP and owns inventions made by its employees.", None, beneficial)


def _check_license_scope(text: str, th: dict[str, Any]) -> _Check:
    """A licence limited to a defined field of use, with your organization's reserved rights to
    practise the technology for research, teaching and education."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    reserved = re.search(r"reserv\w*[^.;]{0,200}(?:research|educational|teaching|academic|non-?commercial)", low)
    all_fields = re.search(r"\b(?:any\s+and\s+)?all\s+fields\b|\bany\s+field\b"
                           r"|\bwithout\s+(?:limitation\s+as\s+to\s+)?field", low)
    exclusive = re.search(r"(?<!non-)(?<!non)\bexclusive", low)
    worldwide = "worldwide" in low or "world-wide" in low
    if reserved and not all_fields:
        return _Check("within", None, "field-limited, research rights reserved")
    if reserved:
        return _Check("fallback", "The licence covers all fields of use; your organization's research rights are reserved, which "
                                  "the matrix accepts as a fallback.", "all fields, research rights reserved")
    parts = [p for p in ("exclusive" if exclusive else "", "worldwide" if worldwide else "",
                         "in all fields of use" if all_fields else "") if p]
    desc = ", ".join(parts) if parts else "granted"
    return _Check("deviates", f"The licence is {desc} with no reserved right for your organization to use the technology for "
                              "research and education; the matrix requires a defined field of use and reserved "
                              "research rights.", f"{desc}; no reserved research rights")


def _check_royalties(text: str, th: dict[str, Any]) -> _Check:
    """Running royalty ≥ minRoyaltyPct (fallback ≥ fallbackRoyaltyPct) and a share
    of sublicense income ≥ minSublicensePct (fallback ≥ fallbackSublicensePct).
    Equity and a minimum annual royalty are beneficial."""
    min_r, fb_r = _th(th, "minRoyaltyPct"), _th(th, "fallbackRoyaltyPct")
    min_s, fb_s = _th(th, "minSublicensePct"), _th(th, "fallbackSublicensePct")
    rate = sub = equity = None
    minimum = upfront = False
    for s in _sentences(text):
        low = s.lower()
        if re.search(r"minimum\s+annual\s+royalt|annual\s+minimum", low):
            minimum = True
            continue
        if re.search(r"license\s+issue\s+fee|up-?front|execution\s+fee|option\s+fee|signing\s+fee", low):
            upfront = True
        if re.search(r"\bequity\b|common\s+stock", low) and not _PCT_RE.search(s):
            equity = equity or 0.0
        for kind, pct in _money_percents(s):
            if kind == "sublicense":
                sub = pct if sub is None else max(sub, pct)
            elif kind == "royalty":
                rate = pct if rate is None else min(rate, pct)
            elif kind == "equity":
                equity = pct if equity is None else max(equity, pct)
    beneficial = []
    if equity is not None:
        beneficial.append(f"your organization receives equity ({_fmt(equity)}%)." if equity else "your organization receives equity.")
    if minimum:
        beneficial.append("A minimum annual royalty is guaranteed.")
    ben = " ".join(beneficial) or None
    if rate is None and sub is None:
        return _Check("review", "No running royalty rate or sublicense income share was found; confirm the "
                                f"financial terms (matrix: at least {_fmt(min_r)}% of net sales and "
                                f"{_fmt(min_s)}% of sublicense income).",
                      "upfront fee only" if upfront else None, ben)
    tiers, reasons, found = [], [], []
    if rate is None:
        tiers.append("review")
        reasons.append(f"No running royalty rate was found; the matrix requires at least {_fmt(min_r)}% of net sales.")
    else:
        found.append(f"{_fmt(rate)}% royalty")
        if rate >= min_r:
            tiers.append("within")
        else:
            tiers.append("fallback" if rate >= fb_r else "deviates")
            reasons.append(f"Running royalty is {_fmt(rate)}% of net sales; the matrix requires at least "
                           f"{_fmt(min_r)}% (fallback {_fmt(fb_r)}%).")
    if sub is None:
        tiers.append("deviates")
        found.append("no sublicense income share")
        reasons.append(f"No share of sublicense income is stated; the matrix requires at least {_fmt(min_s)}% "
                       f"(fallback {_fmt(fb_s)}%).")
    else:
        found.append(f"{_fmt(sub)}% of sublicense income")
        if sub >= min_s:
            tiers.append("within")
        else:
            tiers.append("fallback" if sub >= fb_s else "deviates")
            reasons.append(f"your organization's share of sublicense income is {_fmt(sub)}%; the matrix requires at least "
                           f"{_fmt(min_s)}% (fallback {_fmt(fb_s)}%).")
    tier = max(tiers, key=lambda t: _TIER_RANK[t])
    return _Check(tier, " ".join(reasons) or None, "; ".join(found), ben)


def _check_indemnity(text: str, th: dict[str, Any]) -> _Check:
    """your organization does not indemnify. The other party indemnifies your organization
    and carries insurance. Organization indemnity only "to the extent permitted by its home state
    law" is the fallback; an unqualified your organization (or mutual) indemnity is unacceptable."""
    worst: _Check | None = None
    other = own = False
    for s in _sentences(text):
        low = s.lower()
        ours = re.search(rf"\b{_US}\s+(?:shall|will|agrees\s+to|hereby\s+agrees\s+to)\s+(?:defend,?\s+)?"
                        r"(?:indemnify|hold\s+harmless)", low)
        mutual = re.search(r"\beach\s+party\s+(?:shall|will|agrees\s+to)\s+(?:defend,?\s+)?"
                           r"(?:indemnify|hold\s+harmless)"
                           r"|\bmutual(?:ly)?\s+indemn|indemnify\s+each\s+other", low)
        if ours or mutual:
            if re.search(r"to\s+the\s+extent\s+(?:permitted|authorized|allowed)\s+(?:by|under)\s+(?:the\s+)?"
                         r"(?:(?:constitution\s+and\s+)?laws?\s+of\s+(?:the\s+state\s+of\s+)?[a-z][a-z ]{2,30}?"
                         r"|applicable\s+(?:state\s+)?law|state\s+law|[a-z]+\s+(?:revised\s+code|law))",
                         low):
                cand = _Check("fallback", "Your organization indemnifies only to the extent permitted by its home-state law, which the "
                                          "matrix accepts as a fallback.", "Organization indemnity limited by home-state law")
            else:
                cand = _Check("unacceptable", "Your organization is asked to indemnify the other party; as a public body your organization "
                                              "cannot indemnify (at most, to the extent permitted by its home-state law).",
                              "Your organization indemnifies " + ("each party (mutual)" if mutual and not ours else "the other party"))
            if worst is None or _TIER_RANK[cand.tier] > _TIER_RANK[worst.tier]:
                worst = cand
        if re.search(rf"\b{_OTHER}\s+(?:shall|will|agrees\s+to|hereby\s+agrees\s+to)\s+(?:defend,?\s+)?"
                     r"(?:indemnify|hold\s+harmless)", low):
            other = True
        if re.search(r"responsible\s+for\s+(?:its|their)\s+own"
                     r"|neither\s+party\s+shall\s+(?:be\s+required\s+to\s+)?indemnify"
                     rf"|{_US}\s+(?:does\s+not|shall\s+not|cannot|will\s+not)\s+indemnify|no\s+indemnif", low):
            own = True
    beneficial = None
    if other and re.search(r"additional\s+insured", text or "", re.IGNORECASE):
        beneficial = "The other party indemnifies your organization and names your organization as an additional insured."
    if worst is not None:
        return worst._replace(beneficial=beneficial)
    if other:
        return _Check("within", None, "other party indemnifies your organization", beneficial)
    if own:
        return _Check("within", None, "each party responsible for its own acts")
    return _Check("review", "Who indemnifies whom is not clear; confirm your organization gives no indemnity and the other "
                            "party indemnifies your organization.")


_US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
    "England", "England and Wales",
]
_STATE_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _US_STATES), key=len, reverse=True)) + r")\b",
                       re.IGNORECASE)


def _check_governing_law(text: str, th: dict[str, Any]) -> _Check:
    """Your organization's home-state law; no waiver of its sovereign immunity.
    The home state is the clause's `homeState` setting in the matrix. Another
    state's law deviates; a waiver of immunity is unacceptable; silence on
    governing law is the fallback. With no home state set, a stated law is
    left for a reviewer to check."""
    home = str(th.get("homeState") or "").strip().title() or None
    waived = False
    for s in _sentences(text):
        low = s.lower()
        if "immunit" not in low:
            continue
        m = re.search(r"waive", low)
        if m and not re.search(r"\b(?:nothing|not|no|neither|never|without)\b[^.;]{0,140}waive", low) \
                and not re.search(r"waive\w*\s+(?:nor\s+|or\s+)?(?:shall\s+)?not\b", low):
            waived = True
    states = []
    for m in _STATE_RE.finditer(re.sub(r"\s+", " ", text or "")):
        name = m.group(1).title()
        if name not in states:
            states.append(name)
    others = [s for s in states if s != home]
    law = ", ".join(states) + " law" if states else "no governing law stated"
    required = f"the laws of {home}" if home else "your home-state law"
    if waived:
        what = f"Governing law is {', '.join(others)} and your organization" if others else "Your organization"
        return _Check("unacceptable", f"{what} would waive its sovereign immunity; the matrix requires {required} "
                                      "and no waiver of immunity.", f"{law}; sovereign immunity waived")
    if not home and states:
        return _Check("review", f"Governing law is {', '.join(states)}. Set the home state on this clause in the "
                                "matrix so Sonar can check it.", law)
    if others:
        return _Check("deviates", f"Governing law is {', '.join(others)}; the matrix requires {required}.", law)
    if home and home in states:
        return _Check("within", None, f"{home} law")
    return _Check("fallback", "The agreement is silent on governing law, which the matrix accepts as a fallback "
                              "provided your organization does not waive its immunity.", law)


def _check_export_control(text: str, th: dict[str, Any]) -> _Check:
    """Fundamental research; export-controlled information only with prior notice
    and your organization's consent; no restriction on foreign nationals (unacceptable)."""
    worst: _Check | None = None
    within = False
    for s in _sentences(text):
        low = s.lower()
        people = re.search(r"foreign\s+(?:nationals?|persons?|students?|researchers?)|non-?\s?u\.?\s?s\.?\s+"
                           r"(?:citizens?|persons?|nationals?)|citizenship|u\.?\s?s\.?\s+(?:citizens?|persons?)", low)
        if people:
            allowed = re.search(r"(?:will|shall)\s+not\s+accept|no\s+restrictions?\s+on|without\s+regard\s+to\s+"
                                r"(?:citizenship|nationality)|regardless\s+of\s+(?:citizenship|nationality)|"
                                r"not\s+subject\s+to\s+(?:any\s+)?restrict|free\s+to\s+(?:include|involve|employ)", low)
            restricts = re.search(r"shall\s+not|may\s+not|not\s+(?:be\s+)?permit|prohibit|restrict|exclud|"
                                  r"\bonly\b|without\s+(?:the\s+)?prior\s+(?:written\s+)?approval|limited\s+to", low)
            if restricts and not allowed:
                return _Check("unacceptable", "Participation of foreign nationals is restricted; your organization does not accept "
                                              "foreign national restrictions, which would end the fundamental "
                                              "research exclusion.", "foreign national restriction")
            if allowed:
                within = True
        if re.search(r"fundamental\s+research", low):
            within = True
        if re.search(r"export", low) and re.search(r"prior\s+(?:written\s+)?(?:notice|notif|consent)|in\s+advance|"
                                                     r"before\s+(?:providing|disclosing|delivering)|first\s+giving|"
                                                     r"written\s+notice\s+and", low):
            within = True
        if re.search(r"comply\s+with\s+(?:all\s+)?(?:applicable\s+)?(?:u\.?s\.?\s+|united\s+states\s+)?export", low):
            within = True
        if re.search(r"(?:shall|will)\s+not\s+(?:provide|disclose|deliver|transfer)[^.;]{0,80}"
                     r"(?:export[- ]controlled|itar|\bear\b|export\s+administration|international\s+traffic)", low):
            within = True
        m = re.search(r"(?:may|will|shall)\s+(?:provide|deliver|disclose|furnish)[^.;]{0,80}"
                      r"(?:export[- ]controlled|itar|\bear\b)", low)
        if m and not _negated(low, m.start(), 30) and not re.search(r"notice|notif|consent|approval", low):
            cand = _Check("deviates", "Export-controlled information may be provided without prior notice and your organization's "
                                      "consent; the matrix requires both.",
                          "export-controlled information without notice")
            worst = worst or cand
    if worst:
        return worst
    if within:
        return _Check("within", None, "export terms acceptable")
    return _Check("review", "Export control terms are unclear; confirm the work is fundamental research and no "
                            "export-controlled information will be provided without notice.")


def _confidentiality_years(text: str) -> tuple[float | None, bool]:
    """(longest confidentiality period in years, perpetual?) from sentences that
    talk about confidentiality. Trade-secret carve-outs are ignored."""
    years: float | None = None
    perpetual = False
    for s in _sentences(text):
        low = s.lower()
        if not re.search(r"confidential|non-?disclosure|secrecy|survive", low):
            continue
        if re.search(r"perpetu|indefinite|in\s+perpetuity|no\s+expir", low) and "trade secret" not in low:
            perpetual = True
        for days, unit, _pos in _durations(s):
            if unit in ("year", "month"):
                y = round(days / 365, 2) if unit == "year" else round(days / 360, 2)
                years = y if years is None else max(years, y)
    return years, perpetual


def _years_tier(years: float | None, perpetual: bool, th: dict[str, Any]) -> _Check | None:
    max_y, fb_y = _th(th, "maxConfidentialityYears"), _th(th, "fallbackConfidentialityYears")
    if perpetual:
        return _Check("deviates", f"Confidentiality obligations never end; the matrix allows {_fmt(max_y)} years "
                                  f"(fallback {_fmt(fb_y)}).", "perpetual confidentiality")
    if years is None:
        return None
    found = f"{_fmt(years)} years"
    if years <= max_y:
        return _Check("within", None, found)
    reason = (f"Confidentiality obligations last {_fmt(years)} years; the matrix allows {_fmt(max_y)} "
              f"(fallback {_fmt(fb_y)}).")
    return _Check("fallback" if years <= fb_y else "deviates", reason, found)


def _check_confidentiality(text: str, th: dict[str, Any]) -> _Check:
    """Confidentiality period ≤ maxConfidentialityYears (fallback ≤
    fallbackConfidentialityYears); never perpetual (trade secrets aside)."""
    years, perpetual = _confidentiality_years(text)
    graded = _years_tier(years, perpetual, th)
    if graded:
        return graded
    return _Check("review", "No confidentiality period was found; confirm obligations end within "
                            f"{_fmt(_th(th, 'maxConfidentialityYears'))} years.")


def _check_data_rights(text: str, th: dict[str, Any]) -> _Check:
    """your organization keeps the right to use research data; confidentiality ≤ 5 years
    (fallback 7); no use of your organization's name without its prior written consent."""
    results: list[_Check] = []
    years, perpetual = _confidentiality_years(text)
    graded = _years_tier(years, perpetual, th)
    if graded:
        results.append(graded)
    for s in _sentences(text):
        low = s.lower()
        if re.search(r"\bname|trademark|logo|marks\b", low) and re.search(r"\buse\b", low):
            if re.search(r"consent|approv|permission", low) or re.search(r"(?:shall|will|may)\s+not\s+use", low):
                results.append(_Check("within", None, "use of name only with consent"))
            else:
                results.append(_Check("deviates", "The other party may use your organization's name without your organization's prior written "
                                                  "consent.", "use of your organization name without consent"))
        m = re.search(r"(?:all\s+)?(?:research\s+)?(?:data|results)[^.;]{0,80}(?:sole(?:ly)?\s+|exclusive(?:ly)?\s+)?"
                      rf"(?:property\s+of|owned\s+by|belong\s+to)\s+{_OTHER}", low)
        if m and not _negated(low, m.start(), 40):
            results.append(_Check("deviates", "The other party would own the research data; your organization must keep the right "
                                              "to use its data for research and education.", "sponsor owns data"))
        elif re.search(rf"{_US}\s+(?:shall\s+|will\s+)?(?:retain|own|owns|may\s+use)[^.;]{{0,60}}data", low):
            results.append(_Check("within", None, "your organization retains data rights"))
    if not results:
        return _Check("review", "Data rights, confidentiality period and use of name are not clearly addressed.")
    worst = max(results, key=lambda r: _TIER_RANK[r.tier])
    found = "; ".join(dict.fromkeys(r.found for r in results if r.found))
    return worst._replace(found=found or worst.found)


# (wording, reports a year, plain name)
_FREQ: list[tuple[str, int, str]] = [
    ("weekly", 52, "weekly"), ("monthly", 12, "monthly"), ("each month", 12, "monthly"),
    ("every month", 12, "monthly"), ("quarterly", 4, "quarterly"), ("each calendar quarter", 4, "quarterly"),
    ("each quarter", 4, "quarterly"), ("every three months", 4, "quarterly"), ("semi-annual", 2, "semi-annual"),
    ("semiannual", 2, "semi-annual"), ("biannual", 2, "semi-annual"), ("every six months", 2, "semi-annual"),
    ("annual", 1, "annual"), ("each year", 1, "annual"), ("yearly", 1, "annual"),
]


def _report_frequency(sentence: str) -> tuple[int, str] | None:
    low = sentence.lower()
    for wording, per_year, name in _FREQ:
        if wording in low:
            return per_year, name
    return None


def _check_sponsor_reporting(text: str, th: dict[str, Any]) -> _Check:
    """Reports no more often than maxReportsPerYear (fallback
    fallbackReportsPerYear); flow-down terms from a prime award are identified
    (listed clause numbers, an attachment, or 2 CFR 200)."""
    max_n, fb_n = _th(th, "maxReportsPerYear"), _th(th, "fallbackReportsPerYear")
    results: list[_Check] = []
    for s in _sentences(text):
        if not re.search(r"report", s, re.IGNORECASE):
            continue
        freq = _report_frequency(s)
        if not freq:
            continue
        n, word = freq
        if n <= max_n:
            results.append(_Check("within", None, f"{word} reports"))
        else:
            reason = (f"Reports are due {word} ({n} a year); the matrix allows {_fmt(max_n)} a year "
                      f"(fallback {_fmt(fb_n)}).")
            results.append(_Check("fallback" if n <= fb_n else "deviates", reason, f"{word} reports"))
    low = re.sub(r"\s+", " ", (text or "").lower())
    if re.search(r"flow[- ]?down|\bfar\b|\bdfars\b|2\s*c\.?f\.?r|prime\s+award"
                 r"|terms\s+and\s+conditions\s+of\s+the\s+prime", low):
        if re.search(r"\b52\.\d{3}-\d{1,3}\b|\b252\.\d{3}-\d{4}\b|attachment|exhibit|appendix|schedule\s+[a-z0-9]|"
                     r"listed|2\s*c\.?f\.?r\.?\s*(?:part\s*)?200", low):
            results.append(_Check("within", None, "flow-down terms identified"))
        else:
            results.append(_Check("deviates", "Prime-award terms are flowed down without identifying them; the "
                                              "matrix requires the flow-down clauses to be listed.",
                                  "flow-down terms not identified"))
    if not results:
        return _Check("review", "Reporting obligations are unclear; confirm what reports are due and how often.")
    worst = max(results, key=lambda r: _TIER_RANK[r.tier])
    found = "; ".join(dict.fromkeys(r.found for r in results if r.found))
    return worst._replace(found=found)


def _check_diligence(text: str, th: dict[str, Any]) -> _Check:
    """Commercially reasonable efforts, dated development milestones, and your organization's
    right to terminate (or convert the licence) for failure to commercialise.
    Specific dated milestones are beneficial to your organization."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    efforts = re.search(r"(?:commercially\s+reasonable|diligent|best|reasonable)\s+efforts|diligen", low)
    dated = len(find_dates(text or ""))
    relative = len(re.findall(r"within\s+\(?\d+\)?\s*(?:months|years)|\(?\d+\)?\s*months\s+(?:after|of|from)", low))
    milestones = dated + relative
    terminate = any(re.search(r"terminat|convert", s) and re.search(r"fail", s)
                    for s in _sentences(low))
    beneficial = (f"{milestones} dated diligence milestones with your organization's right to terminate."
                  if dated >= 2 and terminate else None)
    if milestones and terminate:
        return _Check("within", None, f"{milestones} milestones; termination for failure", beneficial)
    if efforts and (milestones or terminate):
        what = "no dated milestones" if not milestones else "no right to terminate for missed milestones"
        return _Check("fallback", f"Diligence has {what}; the matrix accepts this as a fallback.", what)
    if efforts:
        return _Check("deviates", "Diligence is limited to an efforts obligation with no milestones and no right to "
                                  "terminate for failure to commercialize.", "efforts only")
    return _Check("review", "No diligence obligations were found; confirm milestones and your organization's termination right.")


def _check_term(text: str, th: dict[str, Any]) -> _Check:
    """Option / term length ≤ maxTermMonths (fallback ≤ fallbackTermMonths)."""
    max_m, fb_m = _th(th, "maxTermMonths"), _th(th, "fallbackTermMonths")
    months = None
    for s in _sentences(text):
        if not re.search(r"option\s+period|term\b|expire", s, re.IGNORECASE):
            continue
        for days, unit, _pos in _durations(s):
            if unit in ("month", "year"):
                m = days / 30 if unit == "month" else days / 365 * 12
                months = m if months is None else max(months, m)
    if months is None:
        return _Check("review", f"The term could not be read; confirm it is no more than {_fmt(max_m)} months.")
    found = f"{_fmt(round(months))} months"
    if months <= max_m:
        return _Check("within", None, found)
    reason = f"The term is {_fmt(round(months))} months; the matrix allows {_fmt(max_m)} (fallback {_fmt(fb_m)})."
    return _Check("fallback" if months <= fb_m else "deviates", reason, found)


def _check_payment(text: str, th: dict[str, Any]) -> _Check:
    """Invoices paid within netDays (fallback fallbackNetDays); payment in
    advance is beneficial."""
    std, fb = _th(th, "netDays"), _th(th, "fallbackNetDays")
    days = None
    for s in _sentences(text):
        low = s.lower()
        if not re.search(r"payable|\bdue\b|\bpay\b|\bpaid\b", low):
            continue
        net = re.search(r"\bnet\s*(\d{1,3})\b", low)
        cands = [int(net.group(1))] if net else [d for d, unit, _p in _durations(s) if unit in ("day", "week")]
        if cands:
            days = max(cands) if days is None else max(days, max(cands))
    in_advance = re.search(r"in\s+advance|advance\s+payment", text or "", re.IGNORECASE)
    ben = "Payment is made in advance." if in_advance else None
    if days is None:
        return _Check("review", f"No payment window was found; the matrix expects payment within {_fmt(std)} days.",
                      None, ben)
    if days <= std:
        return _Check("within", None, f"{days} days", ben)
    reason = f"Payment is due in {days} days; the matrix expects {_fmt(std)} (fallback {_fmt(fb)})."
    return _Check("fallback" if days <= fb else "deviates", reason, f"{days} days", ben)


def _check_warranty(text: str, th: dict[str, Any]) -> _Check:
    """your organization gives no warranties (materials / technology provided "as is"); an your organization
    warranty of non-infringement, merchantability or fitness deviates."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    m = re.search(rf"{_US}\s+(?:represents\s+and\s+)?warrants\s+that[^.;]{{0,160}}"
                  r"(?:infring|merchantab|fitness|valid|enforceab)", low)
    if m and not _negated(low, m.start(), 30):
        return _Check("deviates", "your organization gives a warranty of non-infringement, validity, merchantability or fitness; "
                                  "your organization provides its technology and materials as is.", "your organization warranty")
    if re.search(r"as\s+is|makes\s+no\s+(?:representations?\s+or\s+)?warrant|disclaim|no\s+warrant", low):
        return _Check("within", None, "as is; warranties disclaimed")
    return _Check("review", "Warranties are not clearly disclaimed; confirm your organization gives no warranties.")


def _check_liability(text: str, th: dict[str, Any]) -> _Check:
    """your organization's liability excluded or limited to the extent permitted by its home-state law."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    if re.search(rf"{_US}\s+shall\s+be\s+liable\s+for\s+(?:any\s+and\s+)?all|unlimited\s+liability|uncapped", low):
        return _Check("deviates", "your organization's liability is unlimited; the matrix requires your organization's liability to be "
                                  "excluded or limited as permitted by its home-state law.", "unlimited organization liability")
    if re.search(r"in\s+no\s+event|shall\s+not\s+be\s+liable|no\s+liability|limited\s+to|shall\s+not\s+exceed", low):
        return _Check("within", None, "liability excluded / limited")
    return _Check("review", "Liability terms are unclear; confirm your organization's liability is excluded or limited.")


def _check_insurance(text: str, th: dict[str, Any]) -> _Check:
    """The other party carries insurance; Your organization is self-insured under home-state law."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    if re.search(rf"{_OTHER}\s+shall\s+(?:maintain|procure|obtain|carry)", low):
        ben = "Your organization is named as an additional insured." if "additional insured" in low else None
        return _Check("within", None, "other party insured", ben)
    if re.search(rf"{_US}\s+shall\s+(?:maintain|procure|obtain|carry)", low) and "self-insur" not in low:
        return _Check("fallback", "Your organization is asked to carry commercial insurance; Your organization is self-insured, which the matrix "
                                  "accepts if stated as such.", "your organization insurance")
    return _Check("review", "Insurance obligations are unclear.")


def _check_termination(text: str, th: dict[str, Any]) -> _Check:
    """Termination with written notice and a cure period."""
    low = (text or "").lower()
    if "terminat" in low and re.search(r"cure|notice", low):
        return _Check("within", None, "termination on notice")
    return _Check("review", "Termination rights are unclear; confirm your organization may terminate on notice.")


# clause type → (check, its thresholds and their defaults)
_CHECKS: dict[str, Callable[[str, dict[str, Any]], _Check]] = {
    "PublicationRights": _check_publication,
    "BackgroundIP": _check_background_ip,
    "IP": _check_background_ip,
    "LicenseScope": _check_license_scope,
    "LicenseGrant": _check_license_scope,
    "Royalties": _check_royalties,
    "Indemnity": _check_indemnity,
    "GoverningLaw": _check_governing_law,
    "ExportControl": _check_export_control,
    "DataRights": _check_data_rights,
    "Confidentiality": _check_confidentiality,
    "SponsorReporting": _check_sponsor_reporting,
    "Diligence": _check_diligence,
    "Term": _check_term,
    "Payment": _check_payment,
    "Warranty": _check_warranty,
    "Liability": _check_liability,
    "Insurance": _check_insurance,
    "Termination": _check_termination,
}

_THRESHOLDS: dict[str, dict[str, float]] = {
    "PublicationRights": {"maxReviewDays": 30, "fallbackReviewDays": 60},
    "Royalties": {"minRoyaltyPct": 3, "fallbackRoyaltyPct": 2, "minSublicensePct": 25, "fallbackSublicensePct": 15},
    "DataRights": {"maxConfidentialityYears": 5, "fallbackConfidentialityYears": 7},
    "Confidentiality": {"maxConfidentialityYears": 5, "fallbackConfidentialityYears": 7},
    "SponsorReporting": {"maxReportsPerYear": 4, "fallbackReportsPerYear": 12},
    "Term": {"maxTermMonths": 12, "fallbackTermMonths": 18},
    "Payment": {"netDays": 30, "fallbackNetDays": 45},
}
# Every clause type also accepts this switch: 1 = a deviation (not only an
# unacceptable term) must be escalated to the clause's escalation office.
_ESCALATE_KEY = "escalateOnDeviation"
_THRESHOLD_DEFAULTS: dict[str, float] = {k: v for t in _THRESHOLDS.values() for k, v in t.items()}


def thresholds_for(clause_type: str, agreement_type: str | None = None) -> dict[str, float]:
    """The thresholds a clause type's built-in check reads, with their defaults.
    A software purchase uses the buyer-side checks, which read their own."""
    if agreement_type == "software":
        from . import software
        return dict(software.THRESHOLDS.get(clause_type, {}))
    from . import workforce
    if agreement_type in workforce.WORKFORCE_TYPES:
        from . import software
        return dict(workforce.THRESHOLDS.get(clause_type) or software.THRESHOLDS.get(clause_type, {}))
    return dict(_THRESHOLDS.get(clause_type, {}))


# ---------------------------------------------------------------------------
# Default (your organization-style) matrix
# ---------------------------------------------------------------------------


def _clause(clause_type: str, standard: str, fallback: str | None, unacceptable: list[str],
            beneficial: list[str], office: str | None, language: str | None,
            thresholds: dict[str, float] | None = None, required: bool = True) -> dict[str, Any]:
    return {
        "clauseType": clause_type, "label": matrix_clause_label(clause_type),
        "standard": standard, "fallback": fallback,
        "unacceptable": list(unacceptable), "beneficial": list(beneficial),
        "escalationOffice": office, "suggestedLanguage": language,
        "thresholds": {**thresholds_for(clause_type), **(thresholds or {})},
        "required": required,
    }


_LANG = {
    "publication": (
        "Organization and its investigators shall be free to publish and present the results of the Research. "
        "Organization will give Sponsor a copy of each proposed publication at least thirty (30) days before "
        "submission. Within that period Sponsor may (a) identify Sponsor Confidential Information, which "
        "Organization will remove, and (b) request a delay of up to an additional thirty (30) days to allow a "
        "patent application to be filed. Organization shall not be required to delay publication for more than "
        "sixty (60) days in total."),
    "background_ip_research": (
        "Each party retains all right, title and interest in its Background Intellectual Property. Inventions "
        "made solely by Organization employees or students are owned by Organization; inventions made solely by "
        "Sponsor employees are owned by Sponsor; joint inventions are owned jointly. Organization grants Sponsor a "
        "non-exclusive, royalty-free licence to use Organization Inventions for internal research purposes and an "
        "option, exercisable within six (6) months of disclosure, to negotiate an exclusive, royalty-bearing "
        "licence on commercially reasonable terms."),
    "background_ip_license": (
        "Title to the Licensed Patents remains with Organization. Improvements made solely by Organization "
        "employees are owned by Organization and, at Licensee's request, may be added to this Agreement by "
        "amendment. Nothing in this Agreement assigns or transfers any Organization intellectual property to "
        "Licensee."),
    "background_ip_mta": (
        "Provider retains ownership of the Material. Organization owns all inventions, data and results made by "
        "Organization employees using the Material, other than the Material itself and unmodified derivatives. "
        "Provider has no rights in Organization inventions except a right of first negotiation for a licence."),
    "license_scope": (
        "Organization grants Licensee an exclusive licence under the Licensed Patents, in the Field of Use and "
        "the Territory, to make, have made, use, sell, offer for sale and import Licensed Products. Organization "
        "reserves for itself and other academic and non-profit research organizations the right to practise the "
        "Licensed Patents for research, teaching and educational purposes. The licence is subject to the rights "
        "of the United States Government under 35 U.S.C. 200-212."),
    "royalties": (
        "Licensee shall pay Organization (a) a non-refundable licence issue fee; (b) a running royalty of three "
        "and one-half percent (3.5%) of Net Sales of Licensed Products; (c) twenty-five percent (25%) of all "
        "Sublicense Income; (d) the milestone payments in Section 3.4; and (e) a minimum annual royalty, "
        "creditable against running royalties in the same year."),
    "option_fee": (
        "Optionee shall pay Organization a non-refundable option fee within thirty (30) days of the Effective "
        "Date. Any licence granted on exercise of the option shall include a running royalty of not less than "
        "three percent (3%) of Net Sales and not less than twenty-five percent (25%) of Sublicense Income, "
        "diligence milestones, and Organization's standard reserved rights."),
    "indemnity": (
        "Sponsor shall indemnify, defend and hold harmless Organization, its trustees, officers, employees and "
        "students from any claims, losses and expenses arising from Sponsor's use of the results of the "
        "Research or of any product, process or service made or sold by Sponsor. Organization shall be "
        "responsible for its own acts and omissions only to the extent permitted by the laws of the State of "
        "its home state; Organization does not otherwise indemnify any party."),
    "indemnity_license": (
        "Licensee shall indemnify, defend and hold harmless Organization, its trustees, officers, employees and "
        "students from all claims arising from the manufacture, use or sale of Licensed Products, and shall "
        "maintain commercial general and product liability insurance of at least $2,000,000 per occurrence "
        "naming Organization as an additional insured. Organization makes no indemnity."),
    "governing_law": (
        "This Agreement is governed by the laws of Organization's home state, without regard to its conflict-of-laws "
        "rules. Nothing in this Agreement waives the sovereign immunity of Organization or the Organization's home state, and "
        "any claim against Organization may be brought only in the forum its home-state law requires."),
    "export": (
        "The parties intend the Research to be fundamental research, the results of which are ordinarily "
        "published. Sponsor shall not provide Organization with any information, technology or material subject "
        "to the EAR or ITAR without first giving Organization written notice and receiving Organization's written "
        "consent. Organization will not accept restrictions on the participation of foreign nationals in the "
        "Research."),
    "export_license": (
        "Licensee shall comply with all applicable United States export control laws and regulations, "
        "including the EAR and ITAR, in its use, sale and export of Licensed Products. Organization makes no "
        "representation that an export licence is not required."),
    "data_rights": (
        "Organization retains the right to use all data and results generated in the Research for research, "
        "teaching and publication. Confidentiality obligations shall last five (5) years from disclosure. "
        "Neither party shall use the name, marks or logos of the other party, or of Organization, "
        "in any advertising or publicity without the other party's prior written consent."),
    "use_of_name": (
        "Licensee shall not use the name, trademarks, logos or other marks of Organization, or the "
        "name of any Organization employee, in any advertising, promotion or publicity without Organization's prior "
        "written consent, except as required by law."),
    "confidentiality": (
        "The receiving party's obligations of confidentiality shall survive for five (5) years from the date of "
        "each disclosure. Information that is publicly available, independently developed, already known or "
        "lawfully received from a third party is excluded, and Organization may disclose information as required "
        "by applicable public records law."),
    "reporting": (
        "Organization will provide Sponsor with written technical progress reports quarterly and a final report "
        "within ninety (90) days after the end of the Research. Terms of the prime award flowed down to "
        "Organization are listed in Attachment 2 by clause number and apply only as required by the prime award."),
    "diligence": (
        "Licensee shall use commercially reasonable efforts to develop and commercialize Licensed Products and "
        "shall meet the diligence milestones in Appendix C by the dates stated. If Licensee fails to meet a "
        "milestone and does not cure the failure within ninety (90) days of notice, Organization may terminate "
        "this Agreement or convert the licence to non-exclusive."),
    "option_term": (
        "The option period is twelve (12) months from the Effective Date. Optionee may request one extension of "
        "up to six (6) months on payment of an extension fee."),
    "payment": (
        "Sponsor shall pay Organization the fixed price of the Research in installments, invoiced in advance, "
        "each payable within thirty (30) days of the invoice date."),
    "warranty": (
        "THE MATERIAL AND ALL TECHNOLOGY ARE PROVIDED \"AS IS\". ORGANIZATION MAKES NO REPRESENTATIONS OR "
        "WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING OF MERCHANTABILITY, FITNESS FOR A PARTICULAR "
        "PURPOSE, VALIDITY OR NON-INFRINGEMENT."),
    "liability": (
        "In no event shall Organization be liable for any indirect, incidental, consequential or punitive "
        "damages. Organization's liability is limited to the extent permitted by the laws of Organization's home state."),
    "sublicensing": (
        "Licensee may grant sublicenses consistent with this Agreement and shall give Organization a copy of each "
        "sublicense within thirty (30) days of execution. Each sublicense shall survive termination of this "
        "Agreement at the sublicensee's option, with Organization as licensor."),
    "termination": (
        "Either party may terminate this Agreement on ninety (90) days' written notice. Either party may "
        "terminate for material breach not cured within sixty (60) days of written notice. Sponsor shall pay "
        "all costs incurred and non-cancellable obligations made before termination."),
}

_UNACC = {
    "publication": ["prior written approval of Sponsor", "Sponsor's prior written consent to publish",
                    "shall not publish", "right to prohibit publication",
                    "results shall be Sponsor's Confidential Information"],
    "background_ip": ["work made for hire", "Organization hereby assigns", "Organization shall assign",
                      "all inventions shall be owned by Sponsor", "all intellectual property shall vest in"],
    "license_scope": ["Organization hereby assigns", "irrevocable assignment of the Licensed Patents"],
    "royalties": ["royalty-free", "fully paid-up", "no royalties shall be due"],
    "indemnity": ["Organization shall defend", "Organization waives the limitations of its home-state law",
                  "indemnify without limitation"],
    "governing_law": ["waives sovereign immunity", "waives any sovereign immunity", "waive its sovereign immunity",
                      "submits to the jurisdiction of the courts of"],
    "export": ["U.S. citizens only", "no foreign nationals", "foreign nationals shall not",
               "restricted to U.S. persons"],
    "data_rights": ["all data shall be the property of Sponsor", "Organization shall not use the data",
                    "may use the name of the Organization without"],
    "reporting": ["monthly technical reports", "weekly reports", "all terms of the prime award apply"],
    "diligence": ["no diligence obligations"],
    "confidentiality": ["in perpetuity", "shall never expire"],
    "warranty": ["Organization warrants that the Licensed Patents are valid", "Organization warrants non-infringement"],
    "liability": ["unlimited liability"],
    "sublicensing": ["sublicenses shall terminate automatically"],
}

# Terms better for your organization than its standard position (the standard itself is not
# "beneficial"). Built-in checks add their own (equity, dated milestones …).
_BENEF = {
    "publication": ["free to publish without review", "no review period"],
    "background_ip": ["grants back to Organization", "license back to Organization"],
    "license_scope": ["non-exclusive license back to Organization"],
    "royalties": ["shares of common stock", "minimum annual royalty", "equity"],
    "indemnity": ["additional insured"],
    "governing_law": [],
    "export": [],
    "data_rights": ["Organization may publish the data"],
    "reporting": [],
    "diligence": ["convert the license to non-exclusive"],
    "payment": ["in advance"],
    "sublicensing": ["sublicense income shall be paid to Organization within"],
}


def _pub(office: str) -> dict[str, Any]:
    return _clause(
        "PublicationRights",
        "your organization and its investigators may publish the results. The other party may review a proposed publication "
        "for up to 30 days to identify its confidential information (which your organization removes) and patentable "
        "inventions.",
        "A total delay of up to 60 days, including an additional deferral to file a patent application.",
        _UNACC["publication"], _BENEF["publication"], office, _LANG["publication"])


def _bip_research(office: str = "sponsored_programs") -> dict[str, Any]:
    return _clause(
        "BackgroundIP",
        "Each party keeps its background IP. your organization owns inventions made by its employees and students; the "
        "sponsor receives a non-exclusive research licence and an option to negotiate an exclusive licence.",
        "Jointly owned inventions with your organization's share licensable on commercial terms; a time-limited exclusive "
        "option (≤ 6 months).",
        _UNACC["background_ip"], _BENEF["background_ip"], office, _LANG["background_ip_research"])


def _indemnity(language: str = "indemnity") -> dict[str, Any]:
    return _clause(
        "Indemnity",
        "your organization, a public body, does not indemnify. The other party indemnifies your organization and carries insurance; "
        "Your organization is responsible for its own acts only to the extent permitted by its home-state law.",
        "Organization indemnity expressly limited \"to the extent permitted by the laws of Organization's home state\".",
        _UNACC["indemnity"], _BENEF["indemnity"], "risk_management", _LANG[language])


def _law() -> dict[str, Any]:
    return _clause(
        "GoverningLaw",
        "Laws of Organization's home state. No waiver of your organization's sovereign immunity; claims against your organization only in the its home state "
        "Court of Claims.",
        "Silent on governing law (no other state's law named) with no waiver of immunity.",
        _UNACC["governing_law"], _BENEF["governing_law"], "legal_affairs", _LANG["governing_law"])


def _export(language: str = "export", required: bool = True) -> dict[str, Any]:
    return _clause(
        "ExportControl",
        "Fundamental research; no export-controlled information without prior written notice and your organization's consent; "
        "no restrictions on foreign nationals.",
        "Export-controlled information accepted under an your organization technology control plan approved by Export Control.",
        _UNACC["export"], _BENEF["export"], "export_control", _LANG[language],
        {_ESCALATE_KEY: 1}, required)


def _data_rights(language: str = "data_rights", required: bool = True) -> dict[str, Any]:
    return _clause(
        "DataRights",
        "your organization keeps the right to use its data and results for research and teaching; confidentiality lasts no "
        "more than 5 years; no use of your organization's name without its prior written consent.",
        "Confidentiality of up to 7 years.",
        _UNACC["data_rights"], _BENEF["data_rights"], "legal_affairs", _LANG[language], None, required)


def _confidentiality(required: bool = False, office: str | None = "legal_affairs") -> dict[str, Any]:
    return _clause(
        "Confidentiality",
        "Confidentiality obligations last no more than 5 years from disclosure, subject to your home state's public "
        "records law; standard exclusions apply.",
        "Up to 7 years.",
        _UNACC["confidentiality"], [], office, _LANG["confidentiality"], None, required)


def _reporting(office: str = "sponsored_programs", required: bool = True) -> dict[str, Any]:
    return _clause(
        "SponsorReporting",
        "Technical reports no more often than quarterly and a final report within 90 days; any prime-award "
        "terms flowed down are identified by clause number.",
        "Monthly reports when funded by the sponsor.",
        _UNACC["reporting"], _BENEF["reporting"], office, _LANG["reporting"], None, required)


def _warranty(required: bool = False) -> dict[str, Any]:
    return _clause(
        "Warranty",
        "your organization makes no warranties; technology and materials are provided as is.",
        None, _UNACC["warranty"], [], "tech_commercialization", _LANG["warranty"], None, required)


def _payment(required: bool = True) -> dict[str, Any]:
    return _clause(
        "Payment",
        "Fixed price or cost-reimbursable budget, invoiced in advance or monthly, payable within 30 days.",
        "Payable within 45 days.",
        [], _BENEF["payment"], "sponsored_programs", _LANG["payment"], None, required)


def default_matrix() -> dict[str, Any]:
    """The built-in your organization-style matrix (version 1). Positions reflect what a public
    research organization holds: home-state law and no waiver of immunity, no your organization
    indemnity, the right to publish, your organization ownership of its inventions, reserved
    research rights, diligence and fair financial terms. your organization to confirm."""
    license_clauses = [
        _clause("LicenseScope",
                "Exclusive licence limited to a defined field of use and territory, subject to your organization's reserved right "
                "to practise the technology for research, teaching and education and to U.S. Government rights.",
                "All fields of use, provided your organization's research and educational rights are reserved and diligence "
                "milestones apply per field.",
                _UNACC["license_scope"], _BENEF["license_scope"], "tech_commercialization", _LANG["license_scope"]),
        _clause("Royalties",
                "Licence issue fee, running royalty of at least 3% of net sales, at least 25% of sublicense "
                "income, milestone payments and a minimum annual royalty; equity in a start-up where appropriate.",
                "Running royalty of at least 2% and at least 15% of sublicense income.",
                _UNACC["royalties"], _BENEF["royalties"], "tech_commercialization", _LANG["royalties"]),
        _clause("Diligence",
                "Commercially reasonable efforts with dated development and sales milestones; your organization may terminate "
                "(or make the licence non-exclusive) if a milestone is missed and not cured.",
                "Efforts obligation with annual diligence reports and either milestones or a termination right.",
                _UNACC["diligence"], _BENEF["diligence"], "tech_commercialization", _LANG["diligence"]),
        _clause("BackgroundIP",
                "your organization keeps title to the licensed patents and its background IP; improvements by your organization employees "
                "are owned by your organization; nothing is assigned to the licensee.",
                "Licensee owns its own improvements with a non-exclusive licence back to your organization for research.",
                _UNACC["background_ip"], _BENEF["background_ip"], "tech_commercialization",
                _LANG["background_ip_license"]),
        _indemnity("indemnity_license"),
        _law(),
        _data_rights("use_of_name"),
        _clause("Sublicensing",
                "Sublicenses allowed with a copy to your organization; sublicenses survive termination with your organization as licensor.",
                "Sublicenses with your organization's prior consent, not unreasonably withheld.",
                _UNACC["sublicensing"], _BENEF["sublicensing"], "tech_commercialization", _LANG["sublicensing"],
                None, False),
        _confidentiality(False, "tech_commercialization"),
        _warranty(),
        _export("export_license", False),
        _reporting("tech_commercialization", False),
    ]
    option_clauses = [
        _clause("LicenseScope",
                "Exclusive option to negotiate a licence in a defined field of use; during the option period only "
                "a non-exclusive evaluation licence for internal research; your organization's research rights reserved.",
                "Option covering all fields of use with your organization's research rights reserved.",
                _UNACC["license_scope"], _BENEF["license_scope"], "tech_commercialization", _LANG["license_scope"]),
        _clause("Term",
                "Option period of no more than 12 months.",
                "Up to 18 months, including one paid extension.",
                [], [], "tech_commercialization", _LANG["option_term"]),
        _clause("Royalties",
                "Non-refundable option fee; the licence on exercise includes a running royalty of at least 3% of "
                "net sales and at least 25% of sublicense income.",
                "Royalty of at least 2% and at least 15% of sublicense income.",
                _UNACC["royalties"], _BENEF["royalties"], "tech_commercialization", _LANG["option_fee"]),
        _clause("BackgroundIP",
                "your organization keeps title to the optioned technology; no assignment; evaluation results do not give the "
                "optionee rights in your organization inventions.",
                None, _UNACC["background_ip"], _BENEF["background_ip"], "tech_commercialization",
                _LANG["background_ip_license"]),
        _data_rights("use_of_name"),
        _indemnity("indemnity_license"),
        _law(),
        _confidentiality(False, "tech_commercialization"),
        _warranty(),
    ]
    research_clauses = [
        _pub("sponsored_programs"),
        _bip_research(),
        _indemnity(),
        _law(),
        _export(),
        _reporting(),
        _data_rights(),
        _payment(),
        _clause("Termination",
                "Either party may terminate on notice; the sponsor pays costs incurred and non-cancellable "
                "obligations.", None, [], [], "sponsored_programs", _LANG["termination"], None, False),
        _clause("Liability",
                "your organization's liability excluded for indirect damages and limited as permitted by its home-state law.",
                None, _UNACC["liability"], [], "risk_management", _LANG["liability"], None, False),
    ]
    grant_clauses = [
        _reporting(),
        _pub("sponsored_programs"),
        _bip_research(),
        _export(),
        _indemnity(),
        _law(),
        _data_rights(required=False),
        _payment(required=False),
    ]
    mta_clauses = [
        _clause("BackgroundIP",
                "The provider owns the material; your organization owns inventions, data and results made by its employees "
                "using it; no reach-through rights for the provider beyond a right to negotiate a licence.",
                "A non-exclusive, royalty-free research licence to the provider for inventions that incorporate "
                "the material.",
                _UNACC["background_ip"], _BENEF["background_ip"], "tech_commercialization", _LANG["background_ip_mta"]),
        _pub("tech_commercialization"),
        _indemnity(),
        _law(),
        _warranty(),
        _data_rights(required=False),
        _export(required=False),
    ]
    nda_clauses = [
        _confidentiality(True),
        _law(),
        _export(required=False),
        _data_rights(required=False),
        _indemnity()  | {"required": False},
    ]
    collaboration_clauses = [
        _pub("sponsored_programs"),
        _bip_research(),
        _data_rights(),
        _indemnity(),
        _law(),
        _export(),
        _confidentiality(False),
    ]
    other_clauses = [
        _indemnity(),
        _law(),
        _clause("Liability", "your organization's liability excluded for indirect damages and limited as permitted by its home-state law.",
                None, _UNACC["liability"], [], "risk_management", _LANG["liability"], None, False),
        _confidentiality(False),
        _payment(required=False),
        _clause("Termination", "Either party may terminate on written notice, with a cure period for breach.",
                None, [], [], "legal_affairs", _LANG["termination"], None, False),
    ]
    # Clinical trials follow sponsored research, with the sponsor covering
    # subject injury; data use agreements protect data and the right to publish.
    clinical_trial_clauses = copy.deepcopy(research_clauses)
    data_use_clauses = [
        _data_rights(),
        _pub("legal_affairs"),
        _confidentiality(True),
        _indemnity(),
        _law(),
        _export(required=False),
    ]
    from . import software
    by_type = {
        "sponsored_research": research_clauses, "clinical_trial": clinical_trial_clauses,
        "grant": grant_clauses, "license": license_clauses, "option": option_clauses, "mta": mta_clauses,
        "data_use": data_use_clauses, "nda": nda_clauses, "collaboration": collaboration_clauses,
        "software": software.default_clauses(), "other": other_clauses,
    }
    from . import workforce
    by_type.update(workforce.default_playbooks())
    return {
        "version": 1,
        "effectiveDate": DEFAULT_EFFECTIVE_DATE,
        "createdAt": f"{DEFAULT_EFFECTIVE_DATE}T00:00:00Z",
        "createdBy": None,
        "note": DEFAULT_NOTE,
        "playbooks": {t: {"agreementType": t, "label": AGREEMENT_TYPE_LABELS[t], "clauses": by_type[t]}
                      for t in AGREEMENT_TYPES},
    }


# ---------------------------------------------------------------------------
# Validation of a client-sent matrix
# ---------------------------------------------------------------------------

_CLAUSE_FIELDS = {"clauseType", "label", "standard", "fallback", "unacceptable", "beneficial",
                  "escalationOffice", "suggestedLanguage", "thresholds", "required"}
_PLAYBOOK_FIELDS = {"agreementType", "label", "clauses"}
_MAX_CLAUSES = 60
_MAX_PHRASES = 30
_MAX_PHRASE_CHARS = 200


def valid_clause_type(clause_type: Any) -> bool:
    """A known clause category (not "Other") or ``type.<specificTypeKey>``."""
    return isinstance(clause_type, str) and valid_rule_id(clause_type)


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_phrases(value: Any) -> list[str] | None:
    """A phrase list: a JSON list of strings, or one ";"-separated string."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [p for p in re.split(r"[;\n]", value)]
    if not isinstance(value, list):
        return None
    out = []
    for p in value:
        if not isinstance(p, str):
            return None
        p = p.strip()
        if not p:
            continue
        if len(p) > _MAX_PHRASE_CHARS:
            return None
        out.append(p)
    out = list(dict.fromkeys(out))
    return out if len(out) <= _MAX_PHRASES else None


def _validate_clause(raw: Any, where: str, agreement_type: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"{where}: each clause must be an object"
    unknown = set(raw) - _CLAUSE_FIELDS
    if unknown:
        return None, f"{where}: unknown field(s): {', '.join(sorted(unknown))}"
    clause_type = raw.get("clauseType")
    if not valid_clause_type(clause_type):
        return None, f"{where}: clauseType must be a known clause category or type.<clause-type-key>"
    standard = _clean_text(raw.get("standard"), 1000)
    if not standard:
        return None, f"{where} ({clause_type}): standard (the position text) is required"
    unacceptable = _clean_phrases(raw.get("unacceptable"))
    beneficial = _clean_phrases(raw.get("beneficial"))
    if unacceptable is None or beneficial is None:
        return None, (f"{where} ({clause_type}): unacceptable / beneficial must be lists of up to {_MAX_PHRASES} "
                      f"strings of at most {_MAX_PHRASE_CHARS} characters")
    office = raw.get("escalationOffice")
    if office in ("", None):
        office = None
    elif office not in OFFICES:
        return None, f"{where} ({clause_type}): escalationOffice must be one of {', '.join(OFFICES)} or null"
    thresholds = raw.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return None, f"{where} ({clause_type}): thresholds must be an object"
    known = set(thresholds_for(clause_type, agreement_type)) | {_ESCALATE_KEY}
    clean_th: dict[str, float] = {}
    for key, value in thresholds.items():
        if key not in known:
            return None, (f"{where} ({clause_type}): unknown threshold '{key}'. This clause type accepts: "
                          f"{', '.join(sorted(known))}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100_000:
            return None, f"{where} ({clause_type}): threshold '{key}' must be a number between 0 and 100000"
        clean_th[key] = float(value) if not float(value).is_integer() else int(value)
    required = raw.get("required", True)
    if not isinstance(required, bool):
        return None, f"{where} ({clause_type}): required must be true or false"
    return {
        "clauseType": clause_type,
        "label": _clean_text(raw.get("label"), 160) or matrix_clause_label(clause_type),
        "standard": standard,
        "fallback": _clean_text(raw.get("fallback"), 1000),
        "unacceptable": unacceptable,
        "beneficial": beneficial,
        "escalationOffice": office,
        "suggestedLanguage": _clean_text(raw.get("suggestedLanguage"), 4000),
        "thresholds": clean_th,
        "required": required,
    }, None


def validate_matrix(playbooks: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a ``playbooks`` map sent by a client (``PUT /matrix``). Returns
    (clean playbooks, None) or (None, error message). Unknown agreement types,
    fields, offices and thresholds are rejected, not silently dropped (as in
    ``playbook.validate_rule``). Agreement types left out are simply absent."""
    if not isinstance(playbooks, dict) or not playbooks:
        return None, "playbooks must be a non-empty object keyed by agreement type"
    out: dict[str, Any] = {}
    for agreement_type in playbooks:
        if agreement_type not in AGREEMENT_TYPES:
            return None, f"Unknown agreement type '{agreement_type}'. Use one of: {', '.join(AGREEMENT_TYPES)}"
    for agreement_type in AGREEMENT_TYPES:
        if agreement_type not in playbooks:
            continue
        book = playbooks[agreement_type]
        if isinstance(book, list):
            book = {"clauses": book}
        if not isinstance(book, dict):
            return None, f"{agreement_type}: playbook must be an object with a clauses list"
        unknown = set(book) - _PLAYBOOK_FIELDS
        if unknown:
            return None, f"{agreement_type}: unknown field(s): {', '.join(sorted(unknown))}"
        if book.get("agreementType") not in (None, agreement_type):
            return None, f"{agreement_type}: agreementType does not match its key"
        clauses = book.get("clauses")
        if not isinstance(clauses, list) or len(clauses) > _MAX_CLAUSES:
            return None, f"{agreement_type}: clauses must be a list of at most {_MAX_CLAUSES} clauses"
        clean: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, raw in enumerate(clauses):
            item, err = _validate_clause(raw, f"{agreement_type} clause {i + 1}", agreement_type)
            if err:
                return None, err
            if item["clauseType"] in seen:
                return None, f"{agreement_type}: clause type {item['clauseType']} appears more than once"
            seen.add(item["clauseType"])
            clean.append(item)
        out[agreement_type] = {"agreementType": agreement_type,
                               "label": _clean_text(book.get("label"), 120) or AGREEMENT_TYPE_LABELS[agreement_type],
                               "clauses": clean}
    return out, None


# ---------------------------------------------------------------------------
# Bulk import (CSV / Excel rows)
# ---------------------------------------------------------------------------

# normalised header → ImportRow field
_HEADERS = {
    "clausetype": "clauseType", "clause": "clauseType", "type": "clauseType", "category": "clauseType",
    "clausecategory": "clauseType", "term": "clauseType",
    "standard": "standard", "standardposition": "standard", "position": "standard", "osuposition": "standard",
    "fallback": "fallback", "fallbackposition": "fallback", "acceptablefallback": "fallback",
    "unacceptable": "unacceptable", "unacceptableterms": "unacceptable", "unacceptablephrases": "unacceptable",
    "dealbreakers": "unacceptable",
    "beneficial": "beneficial", "beneficialterms": "beneficial", "favorableterms": "beneficial",
    "favourableterms": "beneficial", "beneficialphrases": "beneficial",
    "escalationoffice": "escalationOffice", "office": "escalationOffice", "escalation": "escalationOffice",
    "escalateto": "escalationOffice", "reviewoffice": "escalationOffice",
    "suggestedlanguage": "suggestedLanguage", "suggestedredline": "suggestedLanguage", "redline": "suggestedLanguage",
    "suggestedwording": "suggestedLanguage", "language": "suggestedLanguage", "modellanguage": "suggestedLanguage",
    "required": "required", "mandatory": "required",
    "thresholds": "thresholds",
    "agreementtype": "agreementType", "agreement": "agreementType", "contracttype": "agreementType",
    "playbook": "agreementType",
}

# Ordered keyword fallbacks for a clause-type cell that is neither an id nor a label.
_CLAUSE_KEYWORDS: list[tuple[str, str]] = [
    (r"publica", "PublicationRights"),
    (r"background|foreground|invention", "BackgroundIP"),
    (r"royalt|sublicense\s+income|milestone\s+payment|equity", "Royalties"),
    (r"licen[cs]e\s+(?:grant|scope)|field\s+of\s+use|exclusiv", "LicenseScope"),
    (r"indemn", "Indemnity"),
    (r"governing\s+law|sovereign|choice\s+of\s+law|jurisdiction", "GoverningLaw"),
    (r"export|itar|\bear\b|foreign\s+(?:national|part)", "ExportControl"),
    (r"data\s+rights?|use\s+of\s+(?:[\w'.-]+\s+){0,3}name|publicity", "DataRights"),
    (r"report|flow[- ]?down", "SponsorReporting"),
    (r"diligen|commerciali[sz]", "Diligence"),
    (r"confidential|non-?disclosure", "Confidentiality"),
    (r"liabil", "Liability"),
    (r"insur", "Insurance"),
    (r"warrant", "Warranty"),
    (r"sublicen", "Sublicensing"),
    (r"payment|invoice", "Payment"),
    (r"terminat", "Termination"),
    (r"option\s+period|\bterm\b", "Term"),
]

_AGREEMENT_SYNONYMS: list[tuple[str, str]] = [
    (r"statement\s+of\s+work|\bsow\b", "sow"),
    (r"master\s+(?:services?|consulting)|\bmsa\b", "msa"),
    (r"staffing|contingent|temporary\s+(?:staff|labou?r)", "staffing"),
    (r"clinical|\bcta\b", "clinical_trial"),
    (r"data\s+use|\bdua\b|data\s+sharing", "data_use"),
    (r"software|saas|subscription|eula|cloud", "software"),
    (r"sponsored|\bsra\b|research\s+agreement", "sponsored_research"),
    (r"grant|award|subaward|sub-award", "grant"),
    (r"option", "option"),
    (r"material\s+transfer|\bmta\b", "mta"),
    (r"non-?disclosure|\bnda\b|\bcda\b|confidential", "nda"),
    (r"collaborat", "collaboration"),
    (r"licen[cs]e", "license"),
    (r"other", "other"),
]


def _norm_header(h: Any) -> str:
    return re.sub(r"[^a-z]", "", str(h or "").lower())


def resolve_agreement_type(value: Any) -> str | None:
    """An agreement type from an id or label ("license", "Sponsored research",
    "MTA", "Confidential Disclosure Agreement"), or None."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.replace(" ", "_") in AGREEMENT_TYPES:
        return text.replace(" ", "_")
    for t, label in AGREEMENT_TYPE_LABELS.items():
        if text == label.lower():
            return t
    for pattern, t in _AGREEMENT_SYNONYMS:
        if re.search(pattern, text):
            return t
    return None


def resolve_clause_type(value: Any) -> str | None:
    """A matrix clause type from a category id ("PublicationRights"), its label
    (either taxonomy or matrix label, any case), a synonym ("Background IP",
    "Export control"), or ``type.<key>``. None if nothing fits."""
    text = str(value or "").strip()
    if not text:
        return None
    if valid_clause_type(text):
        return text
    low = text.lower()
    for cat in KNOWN_CATEGORIES:
        if cat == "Other":
            continue
        if low in (cat.lower(), known_label(cat).lower(), matrix_clause_label(cat).lower()):
            return cat
    resolved = normalise_type("Other", text)
    if resolved["category"] != "Other":
        return resolved["category"]
    for pattern, cat in _CLAUSE_KEYWORDS:
        if re.search(pattern, low):
            return cat
    return None


def resolve_office(value: Any) -> tuple[str | None, bool]:
    """(office id, ok) from an id or label ("Legal Affairs", "OSP", "Office of
    Technology Commercialization"). An empty cell is (None, True); an
    unrecognised one is (None, False)."""
    text = str(value or "").strip().lower()
    if not text or text in ("none", "n/a", "-", "no"):
        return None, True
    if text.replace(" ", "_") in OFFICES:
        return text.replace(" ", "_"), True
    for office, label in OFFICE_LABELS.items():
        if text == label.lower():
            return office, True
    for pattern, office in ((r"legal|\bola\b|general\s+counsel", "legal_affairs"),
                            (r"commerciali|technology|licens|\btco\b|\botc\b|innovation", "tech_commercialization"),
                            (r"sponsored|\bosp\b|grants?\b|research\s+admin", "sponsored_programs"),
                            (r"export", "export_control"),
                            (r"risk|insurance", "risk_management")):
        if re.search(pattern, text):
            return office, True
    return None, False


def _parse_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in ("no", "n", "false", "0", "optional", "not required")


def _parse_thresholds(value: Any) -> dict[str, float] | None:
    """"maxReviewDays=30; fallbackReviewDays=60" or a dict → {name: number}."""
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    out: dict[str, float] = {}
    for part in re.split(r"[;,\n]", str(value)):
        if not part.strip():
            continue
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)\s*[=:]\s*(-?\d+(?:\.\d+)?)\s*$", part)
        if not m:
            return None
        num = float(m.group(2))
        out[m.group(1)] = int(num) if num.is_integer() else num
    return out


def _read_csv(csv_text: str) -> list[dict[str, Any]]:
    text = (csv_text or "").lstrip("﻿")
    first = text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ",")
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def parse_import(agreement_type: str, rows: list[dict[str, Any]] | None = None,
                 csv_text: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn imported rows (``rows``) or CSV text (``csv_text``) into matrix
    clauses for ``agreement_type``. Returns (clauses, skipped) where skipped is
    ``[{row, reason}]`` and ``row`` is the 1-based data row (the header is not
    counted).

    Headers are matched loosely ("Clause type", "Standard position", "Fallback",
    "Unacceptable terms", "Escalation office", "Beneficial terms", "Suggested
    language", "Required", "Thresholds"; camelCase ImportRow keys work too).
    Clause types accept ids, labels or synonyms; phrase cells are ";"-separated;
    offices accept ids or labels. A row whose "Agreement type" column names a
    different agreement type is skipped with a reason, so one sheet can hold the
    whole matrix. Blank rows are ignored. Raises ValueError for an unknown
    ``agreement_type``."""
    if agreement_type not in AGREEMENT_TYPES:
        raise ValueError(f"Unknown agreement type '{agreement_type}'")
    raw_rows = list(rows or []) if rows is not None else _read_csv(csv_text or "")
    clauses: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            skipped.append({"row": index, "reason": "row is not an object"})
            continue
        row: dict[str, Any] = {}
        for key, value in raw.items():
            field = key if key in _CLAUSE_FIELDS | {"agreementType"} else _HEADERS.get(_norm_header(key))
            if field and (field not in row or row[field] in (None, "")):
                row[field] = value.strip() if isinstance(value, str) else value
        if not any(v not in (None, "", []) for v in row.values()):
            continue
        if row.get("agreementType") not in (None, ""):
            row_type = resolve_agreement_type(row["agreementType"])
            if row_type is None:
                skipped.append({"row": index, "reason": f"unknown agreement type '{row['agreementType']}'"})
                continue
            if row_type != agreement_type:
                skipped.append({"row": index, "reason": f"row is for agreement type '{row_type}', not "
                                                        f"'{agreement_type}'"})
                continue
        clause_type = resolve_clause_type(row.get("clauseType"))
        if clause_type is None:
            skipped.append({"row": index, "reason": f"unknown clause type '{row.get('clauseType') or ''}'"})
            continue
        office, ok = resolve_office(row.get("escalationOffice"))
        if not ok:
            skipped.append({"row": index, "reason": f"unknown escalation office '{row.get('escalationOffice')}'"})
            continue
        thresholds = _parse_thresholds(row.get("thresholds"))
        if thresholds is None:
            skipped.append({"row": index,
                            "reason": "thresholds must look like 'maxReviewDays=30; fallbackReviewDays=60'"})
            continue
        candidate = {
            "clauseType": clause_type,
            "standard": row.get("standard"),
            "fallback": row.get("fallback"),
            "unacceptable": row.get("unacceptable"),
            "beneficial": row.get("beneficial"),
            "escalationOffice": office,
            "suggestedLanguage": row.get("suggestedLanguage"),
            "thresholds": {**thresholds_for(clause_type, agreement_type), **thresholds},
            "required": _parse_bool(row.get("required")),
        }
        item, err = _validate_clause(candidate, f"row {index}", agreement_type)
        if err:
            skipped.append({"row": index, "reason": err.split(": ", 1)[-1]})
            continue
        if clause_type in seen:
            skipped.append({"row": index, "reason": f"duplicate clause type {clause_type} (first on row "
                                                    f"{seen[clause_type]})"})
            continue
        seen[clause_type] = index
        clauses.append(item)
    return clauses, skipped


def merge_import(current_playbooks: dict[str, Any], agreement_type: str, clauses: list[dict[str, Any]],
                 mode: str) -> dict[str, Any]:
    """Apply imported clauses to a copy of ``current_playbooks``.

    ``replace``: the agreement type's playbook becomes exactly ``clauses``.
    ``merge``: an imported clause replaces the existing clause of the same type
    in place; new types are appended. Other agreement types are untouched."""
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    if agreement_type not in AGREEMENT_TYPES:
        raise ValueError(f"Unknown agreement type '{agreement_type}'")
    out = copy.deepcopy(current_playbooks or {})
    book = out.get(agreement_type) or {"agreementType": agreement_type,
                                       "label": AGREEMENT_TYPE_LABELS[agreement_type], "clauses": []}
    incoming = [copy.deepcopy(c) for c in clauses]
    if mode == "replace":
        book["clauses"] = incoming
    else:
        existing = list(book.get("clauses") or [])
        index = {c.get("clauseType"): i for i, c in enumerate(existing)}
        for clause in incoming:
            if clause["clauseType"] in index:
                existing[index[clause["clauseType"]]] = clause
            else:
                index[clause["clauseType"]] = len(existing)
                existing.append(clause)
        book["clauses"] = existing
    book["agreementType"] = agreement_type
    book.setdefault("label", AGREEMENT_TYPE_LABELS[agreement_type])
    out[agreement_type] = book
    return out


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

# Related categories a matrix clause may borrow when the document has none of
# its own type and the playbook does not grade the related category itself.
_ALIASES: dict[str, list[str]] = {
    "BackgroundIP": ["IP"],
    "LicenseScope": ["LicenseGrant"],
    "Royalties": ["Fees"],
    "DataRights": ["Confidentiality", "DataProtection"],
    "Confidentiality": ["DataRights"],
    "GoverningLaw": ["DisputeResolution"],
    "ExportControl": ["Compliance"],
    "SponsorReporting": ["Compliance"],
    "Indemnity": ["Insurance"],
    "Diligence": [],
    "DataProcessing": ["DataProtection", "BreachNotification", "DataRetention", "SubProcessors"],
    "Fees": ["Payment", "Royalties"],
}
# Wording that marks a clause as being about a matrix clause type (heading first,
# then the body) — the last resort before "missing".
_SIGNALS: dict[str, str] = {
    "PublicationRights": r"\bpublication|right\s+to\s+publish",
    "BackgroundIP": r"background\s+(?:ip|intellectual)|ownership\s+of\s+inventions|inventions?\s+and\s+patents",
    "LicenseScope": r"grant\s+of\s+licen[cs]e|licen[cs]e\s+grant|field\s+of\s+use",
    "Royalties": r"royalt",
    "Indemnity": r"indemni",
    "GoverningLaw": r"governing\s+law|laws\s+of\s+the\s+state\s+of|sovereign\s+immunity",
    "ExportControl": r"export\s+control|\bitar\b|export\s+administration",
    "DataRights": r"use\s+of\s+(?:the\s+)?names?|data\s+rights|publicity",
    "SponsorReporting": r"\breports?\b|flow[- ]?down",
    "Diligence": r"diligen",
    "Confidentiality": r"confidential",
    "type.service-levels": r"service\s+levels?|\bsla\b|uptime|availability",
    "Accessibility": r"accessib|wcag|section\s+508",
    "SecurityControls": r"\bsecurity\b|safeguards",
    "Term": r"\bterm\b|renewal",
    "AuditRights": r"\baudit",
    "DataProcessing": r"data\s+(?:protection|privacy|security)|privacy|ferpa|hipaa",
}


def _match(clause_type: str, typed: list[tuple[str | None, dict[str, Any]]],
           graded_types: set[str]) -> list[dict[str, Any]]:
    """The document clauses a matrix clause grades (see the module docstring).
    ``typed`` is each document clause with its rule id
    (``playbook.rule_id_for_clause``), computed once per review."""
    direct = [c for rule_id, c in typed if rule_id == clause_type]
    if direct:
        return direct
    for alias in _ALIASES.get(clause_type, []):
        if alias in graded_types:
            continue
        found = [c for rule_id, c in typed if rule_id == alias]
        if found:
            return found
    signal = _SIGNALS.get(clause_type)
    if signal:
        for rule_id, c in typed:          # a clause another matrix clause grades is not borrowed
            if rule_id not in graded_types and re.search(signal, c.get("title") or "", re.IGNORECASE):
                return [c]
    return []


def _phrase_hit(text: str, phrases: list[str]) -> str | None:
    """The first phrase present in ``text`` and not negated in its sentence."""
    flat = re.sub(r"\s+", " ", text or "")
    low = flat.lower()
    for phrase in phrases or []:
        p = re.sub(r"\s+", " ", phrase.strip().lower())
        if not p:
            continue
        start = 0
        while True:
            idx = low.find(p, start)
            if idx < 0:
                break
            sentence_start = max(low.rfind(". ", 0, idx), low.rfind("; ", 0, idx), 0)
            if not _negated(low[sentence_start:idx + 1], idx - sentence_start, 80):
                return phrase.strip()
            start = idx + 1
    return None


def home_state(value: Any) -> str | None:
    """A US state (or DC) name as the matrix stores it, else None."""
    name = str(value or "").strip()
    match = next((s for s in _US_STATES if s.lower() == name.lower()), None)
    return match


def _grade(mclause: dict[str, Any], text: str, home: str | None = None,
           agreement_type: str | None = None) -> _Check:
    """Built-in check, then the phrase lists; the more serious tier wins.
    ``home`` is the matrix's home state, read by the governing-law check. A
    software purchase is graded by the buyer-side checks (``software.py``):
    there the university is the customer and the vendor the other party."""
    clause_type = mclause["clauseType"]
    from . import workforce
    if agreement_type == "software":
        from . import software
        thresholds = {**software.THRESHOLDS.get(clause_type, {}), **(mclause.get("thresholds") or {}), "homeState": home}
        check = software.CHECKS.get(clause_type)
    elif agreement_type in workforce.WORKFORCE_TYPES:
        # Services and staffing: graded from the client's side (workforce.py).
        thresholds = {**thresholds_for(clause_type, agreement_type), **(mclause.get("thresholds") or {}), "homeState": home}
        check = workforce.CHECKS.get(clause_type)
    else:
        thresholds = {**thresholds_for(clause_type), **(mclause.get("thresholds") or {}), "homeState": home}
        check = _CHECKS.get(clause_type)
    result: _Check | None = check(text, thresholds) if check else None
    hit = _phrase_hit(text, mclause.get("unacceptable") or [])
    if hit and not (result and result.tier == "unacceptable"):
        result = _Check("unacceptable", f"Contains \"{hit}\", which the matrix lists as unacceptable.", f"\"{hit}\"",
                        result.beneficial if result else None)
    good = _phrase_hit(text, mclause.get("beneficial") or [])
    if result is None:
        if mclause.get("unacceptable") or mclause.get("beneficial"):
            result = _Check("within", None, None)
        else:
            result = _Check("review", "No automatic check is defined for this clause type; compare the clause with "
                                      "the standard position.")
    if good:
        reason = f"Contains \"{good}\", which the matrix lists as beneficial to your organization."
        if not result.beneficial:
            result = result._replace(beneficial=reason)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_counts() -> dict[str, int]:
    return {"within": 0, "fallback": 0, "deviates": 0, "unacceptable": 0, "review": 0, "missing": 0, "beneficial": 0}


def review_document(clauses: list[dict[str, Any]], agreement_type: str, matrix: dict[str, Any],
                    doc_id: str | None = None, now_iso: str | None = None) -> dict[str, Any]:
    """Grade a document's classified clauses against the matrix playbook for
    ``agreement_type`` (falling back to the "other" playbook). Returns a
    ``MatrixReview`` (GOVERN_API.md): one ``MatrixClauseResult`` per matrix
    clause found in the document, plus one ``missing`` result per required
    matrix clause that is absent. Results follow the playbook's order.

    Each result also carries two fields beyond the API object, used by
    ``sonar_blockers``: ``required`` (the matrix clause is required) and
    ``escalationRequired`` (the matrix asks for any deviation, not only an
    unacceptable term, to go to its escalation office: threshold
    ``escalateOnDeviation`` = 1)."""
    playbooks = (matrix or {}).get("playbooks") or {}
    home = home_state((matrix or {}).get("homeState"))
    # A matrix saved before an agreement type existed has no playbook for it:
    # use the built-in one rather than grading it as "other".
    book = (playbooks.get(agreement_type)
            or (default_matrix()["playbooks"].get(agreement_type) if agreement_type != "other" else None)
            or playbooks.get("other") or {"clauses": []})
    mclauses = [c for c in book.get("clauses") or [] if isinstance(c, dict) and c.get("clauseType")]
    graded_types = {c["clauseType"] for c in mclauses}
    typed = [(rule_id_for_clause(c), c) for c in clauses or [] if isinstance(c, dict)]
    counts = _empty_counts()
    results: list[dict[str, Any]] = []
    for mclause in mclauses:
        clause_type = mclause["clauseType"]
        base = {
            "clauseType": clause_type,
            "label": mclause.get("label") or matrix_clause_label(clause_type),
            "clauseNumber": None, "clauseId": None,
            "standard": mclause.get("standard"), "fallback": mclause.get("fallback"),
            "escalationOffice": mclause.get("escalationOffice"),
            "suggestedLanguage": mclause.get("suggestedLanguage"),
            "required": bool(mclause.get("required")),
            "escalationRequired": bool((mclause.get("thresholds") or {}).get(_ESCALATE_KEY)),
        }
        matched = _match(clause_type, typed, graded_types)
        if not matched:
            if not mclause.get("required"):
                continue
            counts["missing"] += 1
            results.append({**base, "tier": "missing", "beneficial": False, "beneficialReason": None,
                            "reason": f"The agreement has no {base['label'].lower()} clause; the matrix requires: "
                                      f"{mclause.get('standard')}",
                            "found": None, "quote": None})
            continue
        text = "\n\n".join(str(c.get("body") or "") for c in matched)
        graded = _grade(mclause, text, home, agreement_type)
        # Point at the clause whose own text carries the outcome.
        anchor = matched[0]
        if len(matched) > 1:
            for c in matched:
                if _grade(mclause, str(c.get("body") or ""), home, agreement_type).tier == graded.tier:
                    anchor = c
                    break
        counts[graded.tier] += 1
        if graded.beneficial:
            counts["beneficial"] += 1
        number = anchor.get("number")
        results.append({
            **base,
            "clauseNumber": str(number) if number not in (None, "") else None,
            "clauseId": anchor.get("id"),
            "tier": graded.tier,
            "beneficial": bool(graded.beneficial), "beneficialReason": graded.beneficial,
            "reason": graded.reason, "found": graded.found,
            "quote": (str(anchor.get("body") or "")[:_QUOTE_CHARS] or None),
        })
    return {
        "docId": doc_id,
        "agreementType": agreement_type,
        "matrixVersion": (matrix or {}).get("version"),
        "matrixEffectiveDate": (matrix or {}).get("effectiveDate"),
        "reviewedAt": now_iso or _now_iso(),
        "counts": counts,
        "clauses": results,
    }


def sonar_blockers(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Open Sonar blockers from a review: one per clause tiered ``deviates``,
    ``unacceptable`` or ``missing``, plus ``review`` clauses of a required
    matrix clause (they cannot be signed off unread). Ids are stable
    (``sonar-<clauseType>[-<clauseNumber>]``) so a rescore replaces, not
    duplicates. ``office`` is the escalation office when the clause is
    unacceptable or the matrix asks for every deviation to be escalated."""
    out: list[dict[str, Any]] = []
    created = (review or {}).get("reviewedAt")
    seen: set[str] = set()
    for r in (review or {}).get("clauses") or []:
        tier = r.get("tier")
        if tier not in ("deviates", "unacceptable", "missing"):
            if not (tier == "review" and r.get("required")):
                continue
        number = r.get("clauseNumber")
        bid = f"sonar-{r.get('clauseType')}" + (f"-{re.sub(r'[^A-Za-z0-9.]+', '', str(number))}" if number else "")
        if bid in seen:
            continue
        seen.add(bid)
        label = r.get("label") or matrix_clause_label(r.get("clauseType") or "")
        if tier == "missing":
            text = f"{label} is missing; the matrix requires: {r.get('standard')}"
        elif tier == "review":
            text = r.get("reason") or f"{label} needs a manual check against the matrix."
        else:
            text = r.get("reason") or f"{label} does not meet the matrix standard: {r.get('standard')}"
        escalate = tier == "unacceptable" or (tier in ("deviates", "missing") and r.get("escalationRequired"))
        out.append({
            "id": bid, "text": text, "clauseType": r.get("clauseType"),
            "office": r.get("escalationOffice") if escalate else None,
            "suggestedLanguage": r.get("suggestedLanguage"),
            "source": "sonar", "status": "open",
            "createdAt": created, "createdBy": None, "closedAt": None, "closedBy": None,
        })
    return out


# ---------------------------------------------------------------------------
# Agreement type and direction
# ---------------------------------------------------------------------------


def _doc_text(doc_meta: dict[str, Any] | None, classification: dict[str, Any] | None) -> tuple[str, str]:
    """(title text, everything text) lower-cased, for type inference."""
    meta = doc_meta or {}
    cls = classification or {}
    ident = cls.get("identification") or {}
    title = " ".join(str(x) for x in (meta.get("title"), meta.get("fileName"), meta.get("filename"),
                                      meta.get("name"), ident.get("projectName")) if x)
    parties = meta.get("parties") or cls.get("parties") or []
    rest = " ".join(str(x) for x in (meta.get("summary"), cls.get("summary"),
                                     " ".join(str(p) for p in parties if p)) if x)
    return title.lower().replace("_", " ").replace("-", " "), rest.lower()


def infer_agreement_type(doc_meta: dict[str, Any] | None, classification: dict[str, Any] | None) -> str:
    """Agreement type from the document's type, title, summary and parties.

    Title first (strongest), then the summary: an option agreement (a LICENSE
    whose title says option), "material transfer" → mta, "sponsored research" /
    "research agreement" → sponsored_research, grant / award / subaward → grant,
    collaboration, NDA / "confidential disclosure" → nda, any other LICENSE →
    license, else other."""
    # The model reads the whole agreement and says what kind it is; the title
    # and keyword rules below are the fallback when it could not tell.
    read = ((classification or {}).get("agreement") or {}).get("agreementType")
    if read in AGREEMENT_TYPES and read != "other":
        return read
    title, rest = _doc_text(doc_meta, classification)
    doc_type = str((doc_meta or {}).get("docType") or (classification or {}).get("docType") or "").upper()
    rules: list[tuple[str, str]] = [
        (r"\boption\b", "option"),
        (r"material\s+transfer|\bmta\b", "mta"),
        (r"clinical\s+(?:trial|study)|\bcta\b", "clinical_trial"),
        (r"data\s+use|\bdua\b|data\s+sharing|data\s+transfer\s+agreement", "data_use"),
        (r"sponsored\s+research|research\s+agreement|clinical\s+research|\bsra\b", "sponsored_research"),
        (r"\bsub-?awards?\b|\bgrant\s+agreement\b|\bgrant\b|\baward\b|cooperative\s+agreement", "grant"),
        (r"collaborat", "collaboration"),
        (r"non-?\s?disclosure|confidential\s+disclosure|confidentiality\s+agreement|\bnda\b|\bcda\b", "nda"),
        (r"licen[cs]e\s+agreement|\blicen[cs]e\b", "license"),
    ]
    from . import software, workforce
    services = workforce.workforce_type(title, rest, doc_type)
    if services:
        return services
    if software.looks_like_software_purchase(title, rest + " " + _all_text(classification).lower(), doc_type):
        return "software"
    for pattern, kind in rules:
        if re.search(pattern, title):
            if kind == "option" and not (doc_type == "LICENSE" or re.search(r"licen[cs]|agreement", title)):
                continue
            return kind
    summary_rules: list[tuple[str, str]] = [
        (r"\boption\s+agreement\b|exclusive\s+option", "option"),
        (r"material\s+transfer", "mta"),
        (r"clinical\s+trial|clinical\s+study\s+agreement|investigational\s+(?:drug|product|device)", "clinical_trial"),
        (r"data\s+use\s+agreement|data\s+sharing\s+agreement|limited\s+data\s+set", "data_use"),
        (r"sponsored\s+research|research\s+agreement", "sponsored_research"),
        (r"\bsub-?award\b|grant\s+agreement|federal\s+award|prime\s+award|cooperative\s+agreement", "grant"),
        (r"collaboration\s+agreement|research\s+collaboration", "collaboration"),
        (r"non-?\s?disclosure|confidential\s+disclosure", "nda"),
    ]
    for pattern, kind in summary_rules:
        if re.search(pattern, rest):
            return kind
    if doc_type == "NDA":
        return "nda"
    if doc_type == "LICENSE" or re.search(r"licen[cs]e\s+agreement", rest):
        return "license"
    return "other"


def _all_text(classification: dict[str, Any] | None) -> str:
    cls = classification or {}
    parts = [str(cls.get("summary") or "")]
    parts += [str(c.get("body") or "") for c in cls.get("clauses") or [] if isinstance(c, dict)]
    return re.sub(r"\s+", " ", "\n".join(parts))


def infer_direction(agreement_type: str, classification: dict[str, Any] | None) -> str:
    """incoming (sponsor funding, licence fees, royalties) or outgoing (your organization pays:
    subawards, vendor spend). License, option, sponsored research and grants are
    incoming unless the text shows your organization paying — your organization as pass-through entity or
    "Organization shall pay / reimburse" the other party."""
    if agreement_type in ("software", "sow", "msa", "staffing"):
        return "outgoing"
    read = ((classification or {}).get("agreement") or {}).get("moneyDirection")
    if read in ("incoming", "outgoing"):
        return read
    low = _all_text(classification).lower()
    ours_pays = re.search(
        rf"pass-?through\s+entity|{_US}\s+(?:shall|will|agrees\s+to)\s+(?:pay|reimburse)\b"
        r"|\bsubrecipient\b[^.]{0,80}(?:reimburse|paid|invoice)", low)
    if ours_pays:
        return "outgoing"
    return "incoming"


# ---------------------------------------------------------------------------
# Licensing income and obligations
# ---------------------------------------------------------------------------


def _effective_date(classification: dict[str, Any] | None) -> str | None:
    cls = classification or {}
    for value in (cls.get("effectiveDate"), (cls.get("timeline") or {}).get("startDate")):
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
    for kd in cls.get("keyDates") or []:
        if isinstance(kd, dict) and kd.get("kind") == "effective" and kd.get("date"):
            return str(kd["date"])[:10]
    # The preamble usually states it: "entered into as of October 1, 2026 ("Effective Date")".
    for c in cls.get("clauses") or []:
        body = str((c or {}).get("body") or "")
        m = re.search(r"effective\s+date", body, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 120)
            before = [d for d in find_dates(body[start:m.start()])]
            if before:
                return before[-1][0]            # the date nearest the words "Effective Date"
            after = find_dates(body[m.end():m.end() + 60])
            if after:
                return after[0][0]
    return None


def _term_end(classification: dict[str, Any] | None) -> str | None:
    cls = classification or {}
    for kd in cls.get("keyDates") or []:
        if isinstance(kd, dict) and kd.get("kind") == "term_end" and kd.get("date"):
            return str(kd["date"])[:10]
    end = (cls.get("timeline") or {}).get("endDate")
    if isinstance(end, str) and re.match(r"^\d{4}-\d{2}-\d{2}", end):
        return end[:10]
    return None


def _notice_days(timeline: dict[str, Any], classification: dict[str, Any]) -> int | None:
    """Days of notice needed to stop an automatic renewal: the extracted
    ``timeline.renewalNoticeDays``, else read from a renewal sentence."""
    value = timeline.get("renewalNoticeDays")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 400:
        return int(value)
    for clause, sentences in _clause_sentences(classification):
        for sentence in sentences:
            low = sentence.lower()
            if re.search(r"automatic(?:ally)?\s+renew|auto[- ]?renew|renew\s+automatically", low) or \
                    (re.search(r"non[- ]?renew", low) and "notice" in low):
                days = [d for d, unit, _p in _durations(sentence) if unit in ("day", "week", "month") and d <= 400]
                if days and "notice" in low:
                    return max(days)
    return None


def _expected_date(sentence: str, effective: str | None) -> str | None:
    """A calendar date in the sentence, else "within N months of the Effective
    Date" resolved against the effective date."""
    dates = find_dates(sentence)
    if dates:
        return dates[0][0]
    m = re.search(r"within\s+(?:[a-z\- ]+\s+)?\(?(\d{1,3})\)?\s*(day|month|year)s?\s+(?:after|of|from|following)\s+"
                  r"(?:the\s+)?effective\s+date", sentence, re.IGNORECASE)
    if m and effective:
        unit = {"day": "days", "month": "months", "year": "years"}[m.group(2).lower()]
        return add_offset(effective, int(m.group(1)), unit)
    return None


def _short(sentence: str, limit: int = 200) -> str:
    text = re.sub(r"^\s*(?:\(?[a-z0-9ivx]{1,4}[.)]|\d+(?:\.\d+)+)\s+", "", sentence.strip())
    text = text.rstrip(";.,: ")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _amounts(sentence: str) -> list[float]:
    """Money amounts in a sentence, as positive numbers. Contracts write figures
    as "Fifty Thousand Dollars ($50,000)", which the shared parser reads as an
    accounting negative; a fee or payment here is never negative."""
    return [abs(a["amount"]) for a in find_amounts(sentence) if a["amount"]]


def _clause_sentences(classification: dict[str, Any] | None) -> list[tuple[dict[str, Any], list[str]]]:
    return [(c, _sentences(str(c.get("body") or ""))) for c in (classification or {}).get("clauses") or []
            if isinstance(c, dict)]


def extract_income(classification: dict[str, Any] | None, agreement_type: str) -> list[dict[str, Any]]:
    """Licensing income and sponsor funding read from the clause text.

    Upfront / licence issue / option fees, milestone payments (with their
    expected dates — a calendar date in the sentence, or "within N months of the
    Effective Date"), running royalty %, minimum annual royalty, equity %, the
    sublicense income share, and — for sponsored research, grants and
    collaborations — the total sponsor funding (or, when your organization pays, the subaward
    amount). Falls back to ``commercials.totalContractValue`` for the funding.
    Items are in document order with stable ids ``sonar-income-<n>``."""
    cls = classification or {}
    effective = _effective_date(cls)
    items: list[dict[str, Any]] = []

    def add(kind: str, description: str, amount: float | None = None, pct: float | None = None,
            expected: str | None = None) -> None:
        for it in items:
            if it["kind"] == kind and it["amount"] == amount and it["pct"] == pct:
                return
        items.append({"kind": kind, "description": description, "amount": amount, "pct": pct,
                      "expectedDate": expected, "source": "sonar"})

    funding: tuple[float, str] | None = None
    for _clause, sentences in _clause_sentences(cls):
        in_milestones = False
        for s in sentences:
            low = s.lower()
            is_item = bool(_LIST_ITEM_RE.match(s))
            amounts = _amounts(s)
            if not is_item:
                in_milestones = bool(re.search(r"milestone", low)) and s.rstrip().endswith(":")
            if re.search(r"license\s+issue\s+fee|licence\s+issue\s+fee|up-?front|execution\s+fee|signing\s+fee|"
                         r"option\s+fee", low) and amounts and not re.search(r"extension\s+fee", low):
                desc = "Option fee" if "option fee" in low else "License issue fee"
                add("upfront", desc, amounts[0], None, _expected_date(s, effective))
                continue
            if amounts and (re.search(r"milestone", low) or (is_item and in_milestones)) \
                    and not re.search(r"minimum\s+annual", low):
                add("milestone", _short(s), amounts[0], None, _expected_date(s, effective))
                continue
            if re.search(r"minimum\s+annual\s+royalt|annual\s+minimum", low) and amounts:
                add("royalty", "Minimum annual royalty", amounts[0], None, None)
                continue
            shares = _money_percents(s)
            for kind, pct in shares:
                if kind == "royalty":
                    add("royalty", f"Running royalty of {_fmt(pct)}% of net sales", None, pct)
                elif kind == "sublicense":
                    add("sublicense", f"{_fmt(pct)}% of sublicense income", None, pct)
                elif kind == "equity":
                    add("equity", f"Equity of {_fmt(pct)}%", None, pct)
            if any(kind != "other" for kind, _pct in shares):
                continue
            if agreement_type in ("sponsored_research", "clinical_trial", "grant", "collaboration") and amounts and re.search(
                    r"total|not\s+to\s+exceed|budget|fixed\s+price|amount\s+of|funding|support", low):
                biggest = max(amounts)
                if funding is None or biggest > funding[0]:
                    funding = (biggest, _short(s))
    if agreement_type in ("sponsored_research", "clinical_trial", "grant", "collaboration"):
        if funding is None:
            total = (cls.get("commercials") or {}).get("totalContractValue")
            if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
                funding = (float(total), "Total contract value")
        if funding is not None:
            outgoing = infer_direction(agreement_type, cls) == "outgoing"
            add("subaward" if outgoing else "sponsor_funding",
                ("Subaward amount: " if outgoing else "Sponsor funding: ") + funding[1], funding[0], None, effective)
    for n, item in enumerate(items, start=1):
        item["id"] = f"sonar-income-{n}"
    return [{"id": i["id"], "kind": i["kind"], "description": i["description"], "amount": i["amount"],
             "pct": i["pct"], "expectedDate": i["expectedDate"], "source": i["source"]} for i in items]


def _next_quarter_end(iso: str) -> date | None:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return None
    q_end_month = ((d.month - 1) // 3 + 1) * 3
    end = add_offset(f"{d.year}-{q_end_month:02d}-01", 1, "months")
    return date.fromisoformat(end) - timedelta(days=1) if end else None




def extract_obligations(classification: dict[str, Any] | None, agreement_type: str,
                        signed_at: str | None) -> list[dict[str, Any]]:
    """Post-signature obligations read from the clause text and key dates.

    * sponsor / progress reports (monthly, quarterly, semi-annual, annual): the
      first one falls due one period after ``signed_at``;
    * a final report "within N days" of the end of the term (needs a term end);
    * royalty reports: "within N days after the end of each calendar quarter"
      → the first quarter end after signing + N days;
    * milestone payments (amount + expected date) and dated diligence milestones;
    * the publication review window (no due date — it recurs per manuscript);
    * the term end (from key dates / timeline).

    Without ``signed_at`` the recurring items are still listed, with no due date.
    Ids are stable: ``sonar-obl-<n>`` in document order, term end last."""
    cls = classification or {}
    signed = signed_at[:10] if isinstance(signed_at, str) and re.match(r"^\d{4}-\d{2}-\d{2}", signed_at) else None
    effective = _effective_date(cls)
    term_end = _term_end(cls)
    out: list[dict[str, Any]] = []

    def add(kind: str, title: str, due: str | None = None, amount: float | None = None) -> None:
        for o in out:
            if o["kind"] == kind and o["title"] == title and o["dueDate"] == due:
                return
        out.append({"kind": kind, "title": title, "dueDate": due, "amount": amount, "status": "open",
                    "source": "sonar", "completedAt": None})

    periods = {52: (1, "weeks"), 12: (1, "months"), 4: (3, "months"), 2: (6, "months"), 1: (12, "months")}
    for clause, sentences in _clause_sentences(cls):
        category = clause.get("category")
        in_milestones = False
        for s in sentences:
            low = s.lower()
            is_item = bool(_LIST_ITEM_RE.match(s))
            if not is_item:
                in_milestones = bool(re.search(r"milestone", low)) and s.rstrip().endswith(":")
            amounts = _amounts(s)
            if "report" in low:
                within = re.search(r"within\s+(?:[a-z\- ]+\s+)?\(?(\d{1,3})\)?\s*days?", low)
                if "royalt" in low:
                    due = None
                    if signed and within and "quarter" in low:
                        q_end = _next_quarter_end(signed)
                        due = (q_end + timedelta(days=int(within.group(1)))).isoformat() if q_end else None
                    elif signed and re.search(r"annual|each\s+year", low):
                        due = add_offset(signed, 12, "months")
                    add("royalty_report", "Royalty report" + (" (quarterly)" if "quarter" in low else ""), due)
                    continue
                freq = _report_frequency(s)
                if "final" in low:
                    due = add_offset(term_end, int(within.group(1)), "days") if (within and term_end) else None
                    add("sponsor_report" if agreement_type in ("sponsored_research", "clinical_trial", "grant", "collaboration")
                        else "closeout", "Final report" + (f" (within {within.group(1)} days of the end of the term)"
                                                           if within else ""), due)
                if freq:
                    n, word = freq
                    value, unit = periods.get(n, (12, "months"))
                    due = add_offset(signed, value, unit) if signed else None
                    if agreement_type in ("license", "option"):
                        add("other", f"{word.capitalize()} diligence / progress report to your organization", due)
                    else:
                        add("sponsor_report", f"{word.capitalize()} progress report", due)
                    continue
            if re.search(r"final\s+invoice", low):
                within = re.search(r"within\s+(?:[a-z\- ]+\s+)?\(?(\d{1,3})\)?\s*days?", low)
                due = add_offset(term_end, int(within.group(1)), "days") if (within and term_end) else None
                add("closeout", "Final invoice and financial close-out", due)
                continue
            milestone_ctx = "milestone" in low or (is_item and in_milestones)
            if milestone_ctx and amounts and not re.search(r"minimum\s+annual", low):
                add("milestone_payment", _short(s), _expected_date(s, effective), amounts[0])
                continue
            if (category == "Diligence" or milestone_ctx) and not amounts and (is_item or "milestone" in low):
                due = _expected_date(s, effective)
                if due:
                    add("diligence_milestone", _short(s), due)
                    continue
            if category == "PublicationRights" or (re.search(r"publi(?:sh|cation)", low) and "review" in low):
                durs = [d for d, unit, _p in _durations(s) if unit != "year"]
                if durs and re.search(r"review|comment|submit|prior\s+to", low):
                    add("publication_review", f"Publication review window ({max(durs)} days per manuscript)")
    # Auto-renewal: the last day to stop the renewal is the deadline that costs
    # money when missed (a whole extra term), so it is tracked like any other.
    timeline = cls.get("timeline") or {}
    notice_days = _notice_days(timeline, cls)
    renews = bool(timeline.get("autoRenews")) or notice_days is not None
    if term_end and renews and notice_days:
        add("renewal_notice", f"Last day to give notice to stop the automatic renewal ({notice_days} days before the term ends)",
            add_offset(term_end, notice_days, "days", direction=-1))
    if agreement_type == "software" and term_end:
        add("data_return", "Export and retrieve your data before the subscription ends", term_end)
    if term_end:
        add("term_end", "Agreement term ends", term_end)
    for n, o in enumerate(out, start=1):
        o["id"] = f"sonar-obl-{n}"
    return [{"id": o["id"], "kind": o["kind"], "title": o["title"], "dueDate": o["dueDate"], "amount": o["amount"],
             "status": o["status"], "source": o["source"], "completedAt": o["completedAt"]} for o in out]


__all__ = [
    "AGREEMENT_TYPES", "AGREEMENT_TYPE_LABELS", "OFFICES", "OFFICE_LABELS", "RESEARCH_CLAUSE_TYPES", "TIERS",
    "default_matrix", "validate_matrix", "parse_import", "merge_import", "review_document", "sonar_blockers",
    "infer_agreement_type", "infer_direction", "extract_income", "extract_obligations", "matrix_clause_label",
    "resolve_clause_type", "resolve_office", "resolve_agreement_type", "thresholds_for", "valid_clause_type",
]
