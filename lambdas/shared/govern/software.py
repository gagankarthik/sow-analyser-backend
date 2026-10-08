"""Software and SaaS purchases: the university as the CUSTOMER.

The research and licensing playbooks read every agreement from the side of an
organization licensing its own technology out ("the licensee shall indemnify",
"provided as is" are good news there). In a software or SaaS purchase the
roles are reversed: the vendor licenses to the university, so the same words
mean the opposite. This module holds the buyer-side checks and the default
playbook for the ``software`` agreement type, and the signals that tell a
software purchase apart from a technology licence the university grants.

Every check is deterministic (regular expressions over the clause text, no
model call) and returns a ``matrix._Check``: tier, a plain reason a reviewer
can send, the value read, and why a term is beneficial.
"""
from __future__ import annotations

import re
from typing import Any, Callable

# matrix imports this module lazily (inside functions), so importing matrix
# here at load time is safe.
from . import matrix as _m

_Check = _m._Check

# "Us" is the customer; "them" is the vendor.
_US = r"(?:the\s+)?(?:university|institution|college|customer|licensee|subscriber|client|our\s+organi[sz]ation)"
_VENDOR = r"(?:the\s+)?(?:vendor|licensor|provider|supplier|contractor|company|service\s+provider|seller|reseller)"


def _low(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _pcts(sentence: str) -> list[float]:
    return [float(m.group(1)) for m in _m._PCT_RE.finditer(sentence or "")]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_scope(text: str, th: dict[str, Any]) -> _Check:
    """Licence metric. Enterprise / site / campus-wide / FTE, covering affiliates
    and contractors → within. Named or concurrent users with an annual true-up
    that is not retroactive → fallback. Retroactive true-up, true-up at list
    price, or a vendor right to charge for overuse at its own rates → deviates."""
    low = _low(text)
    if re.search(r"retroactive|back[- ]?fees|true[- ]?up[^.]{0,80}(?:list\s+price|then[- ]current)", low):
        return _Check("deviates", "Overuse can be charged retroactively or at list price; the matrix expects an annual "
                                  "true-up at the contracted rate, going forward only.", "retroactive or list-price true-up")
    if re.search(r"enterprise|site[- ]wide|site\s+licen|campus[- ]wide|unlimited\s+(?:users|use)|all\s+(?:faculty|staff|students|employees)|\bfte\b|full[- ]time\s+equivalent", low):
        ben = "Contractors and affiliates may use it." if re.search(r"contractor|affiliate|agent", low) else None
        return _Check("within", None, "enterprise or site-wide use", ben)
    seats = re.search(r"(\d[\d,]*)\s+(?:named\s+|authori[sz]ed\s+|concurrent\s+)?(?:users?|seats?|licen[cs]es?)", low)
    if seats or re.search(r"named\s+users?|concurrent\s+users?|per\s+(?:user|seat)", low):
        found = f"{seats.group(1)} users" if seats else "per-user licence"
        if re.search(r"true[- ]?up", low) and re.search(r"annual|yearly|once\s+(?:a|per)\s+year", low):
            return _Check("fallback", "Use is counted per user with an annual true-up, which the matrix accepts as a fallback.", found)
        return _Check("fallback", "Use is limited to a number of users; confirm growth can be added at the contracted rate.", found)
    return _Check("review", "The licence metric (users, seats, enterprise) is not clear; confirm who may use the software.")


def check_fees(text: str, th: dict[str, Any]) -> _Check:
    """Renewal price increases capped at maxIncreasePct (fallback
    fallbackIncreasePct or CPI). "Then-current" or unilateral pricing → deviates.
    Fixed price for the term is beneficial."""
    max_p, fb_p = _m._th(th, "maxIncreasePct"), _m._th(th, "fallbackIncreasePct")
    low = _low(text)
    fixed = re.search(r"fixed\s+(?:for|during|throughout)\s+the\s+(?:initial\s+)?term|price\s+lock|no\s+(?:price\s+)?increase", low)
    ben = "Prices are fixed for the term." if fixed else None
    if re.search(r"then[- ]current\s+(?:list\s+)?(?:price|rate|fee)|(?:sole|absolute)\s+discretion[^.]{0,60}(?:price|fee|rate)|change\s+(?:its\s+)?(?:prices|fees)\s+at\s+any\s+time", low):
        return _Check("deviates", "The vendor can set renewal prices at its own rates; the matrix caps increases at "
                                  f"{_m._fmt(max_p)}% a year (fallback {_m._fmt(fb_p)}% or CPI).", "uncapped renewal price", ben)
    worst = None
    for s in _m._sentences(text):
        ls = s.lower()
        if not re.search(r"increase|escalat|uplift|adjust|renewal\s+(?:fee|price|rate)", ls):
            continue
        for p in _pcts(s):
            worst = p if worst is None else max(worst, p)
        if worst is None and re.search(r"\bcpi\b|consumer\s+price\s+index", ls):
            return _Check("fallback", "Increases follow CPI, which the matrix accepts as a fallback.", "CPI", ben)
    if worst is None:
        return _Check("within", None, "no increase stated", ben) if fixed else \
            _Check("review", f"No cap on renewal price increases was found; ask for at most {_m._fmt(max_p)}% a year.", None, ben)
    found = f"{_m._fmt(worst)}% a year"
    if worst <= max_p:
        return _Check("within", None, found, ben)
    return _Check("fallback" if worst <= fb_p else "deviates",
                  f"Renewal prices may rise {_m._fmt(worst)}% a year; the matrix allows {_m._fmt(max_p)}% "
                  f"(fallback {_m._fmt(fb_p)}%).", found, ben)


def check_renewal(text: str, th: dict[str, Any]) -> _Check:
    """Auto-renewal: none, or a non-renewal notice window of at most
    maxNoticeDays (fallback fallbackNoticeDays). A longer window, or an
    evergreen term with no way out, deviates."""
    max_d, fb_d = _m._th(th, "maxNoticeDays"), _m._th(th, "fallbackNoticeDays")
    low = _low(text)
    auto = re.search(r"automatic(?:ally)?\s+renew|auto[- ]?renew|renew\s+automatically|evergreen|successive\s+(?:renewal\s+)?(?:terms|periods)", low)
    if not auto:
        if re.search(r"renew(?:ed|al)?\s+(?:only\s+)?(?:by|upon|with)\s+(?:mutual\s+)?(?:written\s+)?agreement|shall\s+not\s+(?:automatically\s+)?renew", low):
            return _Check("within", None, "renews only by agreement", "Renewal needs your written agreement.")
        return _Check("within", None, "no automatic renewal")
    notice = None
    for s in _m._sentences(text):
        ls = s.lower()
        if re.search(r"notice|non[- ]?renew|prior\s+to|before\s+the\s+end", ls):
            for days, unit, _pos in _m._durations(s):
                if unit in ("day", "week", "month"):
                    notice = days if notice is None else max(notice, days)
    if notice is None:
        if "evergreen" in low or not re.search(r"terminat|non[- ]?renew|cancel", low):
            return _Check("deviates", "The term renews automatically with no clear way to stop it; the matrix requires a "
                                      f"non-renewal right on at most {_m._fmt(max_d)} days' notice.", "auto-renewal, no exit")
        return _Check("review", "The term renews automatically; the notice needed to stop it could not be read.", "auto-renewal")
    found = f"{notice} days' notice to stop renewal"
    if notice <= max_d:
        return _Check("within", None, found)
    return _Check("fallback" if notice <= fb_d else "deviates",
                  f"Stopping the renewal needs {notice} days' notice; the matrix allows {_m._fmt(max_d)} "
                  f"(fallback {_m._fmt(fb_d)}). Put the deadline in the calendar.", found)


def check_audit(text: str, th: dict[str, Any]) -> _Check:
    """Vendor audits of the university: at most once a year, on at least
    minAuditNoticeDays' notice, at the vendor's cost, by remote self-certification
    first. Unannounced or on-site-at-will audits, or the university paying for the
    audit, deviate."""
    min_n = _m._th(th, "minAuditNoticeDays")
    low = _low(text)
    if not re.search(r"audit|inspect|verify\s+(?:compliance|usage|use)", low):
        return _Check("within", None, "no vendor audit right")
    if re.search(r"without\s+(?:prior\s+)?notice|unannounced|at\s+any\s+time", low):
        return _Check("deviates", "The vendor may audit without notice or at any time; the matrix allows at most one audit "
                                  f"a year on {_m._fmt(min_n)} days' notice.", "audit without notice")
    if re.search(rf"{_US}\s+(?:shall|will)\s+(?:pay|bear|reimburse)[^.]{{0,60}}(?:cost|expense)s?\s+of\s+(?:the|such|any)\s+audit", low) \
            and not re.search(r"(?:5|five|10|ten)\s*%|discrepanc|underpay", low):
        return _Check("deviates", "The university is asked to pay for the vendor's audit; the vendor should bear it unless "
                                  "a material underpayment is found.", "university pays for audit")
    notice = None
    for days, unit, _p in _m._durations(text):
        if unit in ("day", "week"):
            notice = days if notice is None else max(notice, days)
    if re.search(r"self[- ]certif|written\s+certification|usage\s+report", low):
        return _Check("within", None, "self-certification", "Compliance is shown by self-certification.")
    if notice is not None and notice >= min_n:
        return _Check("within", None, f"{notice} days' notice")
    if notice is not None:
        return _Check("fallback", f"Audits need {notice} days' notice; the matrix asks for at least {_m._fmt(min_n)}.",
                      f"{notice} days' notice")
    return _Check("review", "The vendor may audit use; confirm notice, frequency and who pays.")


def check_sla(text: str, th: dict[str, Any]) -> _Check:
    """Availability of at least minUptimePct with service credits (fallback
    fallbackUptimePct); a right to terminate for chronic failure is beneficial."""
    min_u, fb_u = _m._th(th, "minUptimePct"), _m._th(th, "fallbackUptimePct")
    low = _low(text)
    ups = [p for s in _m._sentences(text) if re.search(r"uptime|availab|service\s+level", s, re.I) for p in _pcts(s) if p >= 90]
    ben = "You may terminate for repeated service failures." if re.search(r"chronic|repeated(?:ly)?\s+fail|terminat[^.]{0,80}(?:service\s+level|sla|availability)", low) else None
    credits = re.search(r"service\s+credit|credit\s+against|fee\s+reduction", low)
    if not ups:
        if re.search(r"no\s+(?:service\s+level|sla|guarantee\s+of\s+availability)|as\s+available", low):
            return _Check("deviates", f"There is no availability commitment; the matrix expects {_m._fmt(min_u)}% uptime "
                                      "with service credits.", "no SLA", ben)
        return _Check("review", f"No uptime commitment was found; ask for {_m._fmt(min_u)}% with service credits.", None, ben)
    up = min(ups)
    found = f"{_m._fmt(up)}% uptime" + (" with credits" if credits else "")
    if up >= min_u and credits:
        return _Check("within", None, found, ben)
    if up >= fb_u:
        return _Check("fallback", f"Uptime is {_m._fmt(up)}%{'' if credits else ' with no service credits'}; the matrix "
                                  f"expects {_m._fmt(min_u)}% with credits.", found, ben)
    return _Check("deviates", f"Uptime is only {_m._fmt(up)}%; the matrix expects at least {_m._fmt(fb_u)}%.", found, ben)


def check_warranty(text: str, th: dict[str, Any]) -> _Check:
    """The VENDOR warrants the software performs to its documentation, is free
    of malicious code and does not infringe. A blanket "as is" disclaimer by the
    vendor deviates; a short performance warranty is the fallback."""
    low = _low(text)
    warrants = re.search(rf"{_VENDOR}\s+(?:represents\s+and\s+)?warrants", low)
    perform = re.search(r"(?:perform|operate|function)\w*\s+(?:materially\s+|substantially\s+)?(?:in\s+accordance|as\s+described)|conform\w*\s+to\s+(?:the\s+)?documentation", low)
    malware = re.search(r"virus|malicious\s+code|malware|disabling\s+(?:code|device)|time\s+bomb", low)
    noninf = re.search(r"not\s+infringe|non[- ]?infringement", low)
    if warrants and perform:
        days = [d for d, unit, _p in _m._durations(text) if unit in ("day", "month")]
        if days and max(days) < 365 and not malware:
            return _Check("fallback", f"The performance warranty lasts {max(days)} days and does not cover malicious code; "
                                      "the matrix expects it for the whole term.", f"{max(days)}-day warranty")
        ben = "The vendor also warrants against malicious code." if malware else None
        return _Check("within", None, "vendor performance warranty" + (", non-infringement" if noninf else ""), ben)
    if re.search(r"as\s+is|as\s+available|disclaims?\s+all\s+warrant|makes\s+no\s+(?:representations?\s+or\s+)?warrant", low):
        return _Check("deviates", "The vendor provides the software as is; the matrix requires a warranty that it performs "
                                  "to its documentation and contains no malicious code.", "as is")
    return _Check("review", "No vendor warranty was found; ask for a performance and no-malicious-code warranty.")


def check_indemnity(text: str, th: dict[str, Any]) -> _Check:
    """The VENDOR defends and indemnifies the university against IP infringement
    and data-breach claims. The university indemnifying the vendor is
    unacceptable for a public body (fallback: only "to the extent permitted by
    law"). No vendor IP indemnity deviates."""
    low = _low(text)
    vendor_ind = re.search(rf"{_VENDOR}\s+(?:shall|will|agrees\s+to)\s+(?:defend(?:,|\s+and)?\s+)?(?:indemnify|hold\s+harmless)", low)
    ours = re.search(rf"\b(?:the\s+)?(?:university|institution|college|customer|licensee|subscriber|client)\s+(?:shall|will|agrees\s+to)\s+(?:defend(?:,|\s+and)?\s+)?(?:indemnify|hold\s+harmless)", low)
    if ours:
        if re.search(r"to\s+the\s+extent\s+(?:permitted|authorized|allowed)\s+by", low):
            return _Check("fallback", "The university indemnifies the vendor only to the extent its home-state law permits, "
                                      "which the matrix accepts as a fallback.", "university indemnity limited by law")
        return _Check("unacceptable", "The university is asked to indemnify the vendor; as a public body it cannot.",
                      "university indemnifies vendor")
    if vendor_ind:
        ip = re.search(r"infring|intellectual\s+property|patent|copyright|trade\s+secret", low)
        breach = re.search(r"data\s+breach|security\s+(?:breach|incident)|unauthori[sz]ed\s+(?:access|disclosure)", low)
        if not ip:
            return _Check("fallback", "The vendor indemnifies, but not clearly for IP infringement; the matrix expects an IP "
                                      "infringement indemnity.", "vendor indemnity, IP not named")
        ben = "The vendor also indemnifies for data breaches." if breach else None
        return _Check("within", None, "vendor IP indemnity", ben)
    return _Check("deviates", "The vendor gives no indemnity for IP infringement claims; the matrix requires one.",
                  "no vendor indemnity")


def check_liability(text: str, th: dict[str, Any]) -> _Check:
    """A mutual cap with carve-outs (data breach, confidentiality, IP
    indemnity) → within. A cap at fees paid with no carve-outs → fallback.
    Vendor liability excluded altogether, or the university's liability
    unlimited → deviates."""
    low = _low(text)
    if re.search(r"(?:university|institution|customer|licensee)[^.]{0,60}(?:unlimited|shall\s+be\s+liable\s+for\s+(?:any\s+and\s+)?all)", low):
        return _Check("deviates", "The university's liability is unlimited; the matrix requires it to be capped or limited "
                                  "as its home-state law permits.", "university liability unlimited")
    if re.search(rf"{_VENDOR}\s+shall\s+(?:have\s+)?no\s+liability|in\s+no\s+event\s+shall\s+{_VENDOR}\s+be\s+liable\s+for\s+any\s+(?:damages|loss)", low) \
            and not re.search(r"exceed|limited\s+to|cap", low):
        return _Check("deviates", "The vendor excludes all of its liability; the matrix expects a cap of at least the fees "
                                  "paid, with carve-outs for data breach and IP claims.", "vendor liability excluded")
    capped = re.search(r"shall\s+not\s+exceed|limited\s+to|aggregate\s+liability|cap", low)
    carve = re.search(r"(?:except|exclud|shall\s+not\s+apply)[^.]{0,160}(?:confidential|data|breach|indemn|gross\s+negligence|wilful|willful)", low)
    if capped and carve:
        return _Check("within", None, "capped, with carve-outs", "Data-breach or indemnity claims sit outside the cap." if re.search(r"data|breach|indemn", carve.group(0)) else None)
    if capped:
        return _Check("fallback", "Liability is capped with no carve-outs; ask that data breach and IP indemnity sit "
                                  "outside the cap.", "capped, no carve-outs")
    return _Check("review", "Liability limits are not clear; confirm a mutual cap with carve-outs for data breach and IP.")


def check_data(text: str, th: dict[str, Any]) -> _Check:
    """Data protection: breach notice within maxBreachHours (fallback
    fallbackBreachHours); the university owns its data; data returned or deleted
    on exit; FERPA / HIPAA named where student or health data is involved."""
    max_h, fb_h = _m._th(th, "maxBreachHours"), _m._th(th, "fallbackBreachHours")
    low = _low(text)
    if re.search(rf"{_VENDOR}\s+(?:shall\s+)?(?:own|retain\s+(?:all\s+)?(?:right|title|ownership))[^.]{{0,60}}(?:customer|university|your)\s+data"
                 r"|(?:sell|monetiz|use)[^.]{0,40}(?:customer|university|your)\s+data[^.]{0,40}(?:any\s+purpose|commercial|advertis)", low):
        return _Check("unacceptable", "The vendor may own or commercially use the university's data.", "vendor uses university data")
    hours = None
    for s in _m._sentences(text):
        if re.search(r"breach|security\s+incident|unauthori[sz]ed", s, re.I):
            for m in re.finditer(r"(\d{1,3})\s*(?:\(\w+\)\s*)?hours?", s, re.I):
                h = int(m.group(1))
                hours = h if hours is None else max(hours, h)
            for days, unit, _p in _m._durations(s):
                if unit in ("day", "week"):
                    hours = days * 24 if hours is None else max(hours, days * 24)
    exit_ok = re.search(r"(?:return|delete|destroy)\w*[^.]{0,80}(?:data|content)|data\s+(?:return|deletion|portability)", low)
    ben = "FERPA or HIPAA obligations are written in." if re.search(r"ferpa|hipaa|school\s+official", low) else None
    if hours is None:
        if re.search(r"without\s+undue\s+delay|promptly", low) and re.search(r"breach|incident", low):
            return _Check("fallback", f"Breaches are reported \"promptly\" with no deadline; ask for {_m._fmt(max_h)} hours.",
                          "prompt breach notice", ben)
        return _Check("review", f"No breach-notice deadline was found; the matrix expects notice within {_m._fmt(max_h)} hours"
                                " and data return or deletion on exit.", None, ben)
    found = f"breach notice in {hours} hours"
    if hours <= max_h and exit_ok:
        return _Check("within", None, found, ben)
    if hours <= fb_h:
        return _Check("fallback", f"Breach notice in {hours} hours{'' if exit_ok else ' and no data return on exit'}; the "
                                  f"matrix expects {_m._fmt(max_h)} hours and return or deletion of data on exit.", found, ben)
    return _Check("deviates", f"Breach notice takes {hours} hours; the matrix expects at most {_m._fmt(fb_h)}.", found, ben)


def check_security(text: str, th: dict[str, Any]) -> _Check:
    """Security: an independent attestation (SOC 2 Type II, ISO 27001,
    FedRAMP) or a completed HECVAT → within; "industry standard" safeguards
    only → fallback; nothing → review."""
    low = _low(text)
    if re.search(r"soc\s*2\s*(?:type\s*(?:ii|2))?|iso\s*/?\s*(?:iec\s*)?27001|fedramp|hecvat|stateramp", low):
        return _Check("within", None, "independent security attestation")
    if re.search(r"industry[- ]standard|commercially\s+reasonable\s+(?:security|safeguards)|reasonable\s+(?:administrative|technical)", low):
        return _Check("fallback", "Security is described only as industry standard; ask for a SOC 2 Type II report or a "
                                  "completed HECVAT.", "industry-standard safeguards")
    return _Check("review", "No security commitments were found; ask for SOC 2 Type II or a HECVAT.")


def check_accessibility(text: str, th: dict[str, Any]) -> _Check:
    """Accessibility: conformance with WCAG 2.1 AA (or Section 508) with a
    VPAT → within; a remediation roadmap or "reasonable efforts" → fallback."""
    low = _low(text)
    if re.search(r"wcag\s*2\.[12]|section\s+508", low):
        ben = "A VPAT or accessibility conformance report is provided." if re.search(r"vpat|conformance\s+report|acr\b", low) else None
        return _Check("within", None, "WCAG 2.1 AA / Section 508", ben)
    if re.search(r"accessib", low):
        return _Check("fallback", "Accessibility is promised without a standard; ask for WCAG 2.1 AA conformance and a VPAT.",
                      "accessibility, no standard")
    return _Check("review", "No accessibility commitment was found; public universities need WCAG 2.1 AA.")


def check_termination(text: str, th: dict[str, Any]) -> _Check:
    """The university may terminate for convenience (or for non-appropriation of
    funds) and gets transition assistance and its data back."""
    low = _low(text)
    conv = re.search(r"terminat\w*\s+(?:this\s+agreement\s+)?for\s+(?:its\s+)?convenience|non[- ]?appropriation|without\s+cause", low)
    trans = re.search(r"transition\s+(?:assistance|services|period)|export\s+(?:its|all)\s+data", low)
    if conv:
        return _Check("within", None, "termination for convenience",
                      "Transition assistance on exit." if trans else None)
    if "terminat" in low and re.search(r"cure|breach", low):
        return _Check("fallback", "The university may terminate only for breach; ask for termination for convenience or "
                                  "non-appropriation of funds.", "termination for breach only")
    return _Check("review", "The university's termination rights are not clear.")


def check_insurance(text: str, th: dict[str, Any]) -> _Check:
    """The vendor carries cyber / technology E&O insurance."""
    low = _low(text)
    if re.search(r"cyber|network\s+security|privacy\s+liability|technology\s+errors|tech(?:nology)?\s+e\s*&\s*o", low):
        return _Check("within", None, "vendor cyber insurance",
                      "The university is an additional insured." if "additional insured" in low else None)
    if re.search(rf"{_VENDOR}\s+shall\s+(?:maintain|procure|carry)", low):
        return _Check("fallback", "The vendor carries insurance but not cyber cover; ask for cyber liability insurance.",
                      "vendor insurance, no cyber")
    return _Check("review", "No vendor insurance was found; ask for cyber liability cover.")


def check_escrow(text: str, th: dict[str, Any]) -> _Check:
    low = _low(text)
    if re.search(r"escrow", low):
        return _Check("within", None, "source code escrow")
    return _Check("review", "No source code escrow; needed only for on-premises or business-critical systems.")


def check_assignment(text: str, th: dict[str, Any]) -> _Check:
    """The vendor may not assign without consent; a change of control lets
    the university terminate."""
    low = _low(text)
    if re.search(r"change\s+(?:of|in)\s+control", low) and re.search(r"terminat", low):
        return _Check("within", None, "termination right on change of control")
    if re.search(r"without\s+(?:the\s+)?(?:prior\s+)?(?:written\s+)?consent", low):
        return _Check("within", None, "no assignment without consent")
    if re.search(rf"{_VENDOR}\s+may\s+(?:freely\s+)?assign", low):
        return _Check("deviates", "The vendor may assign the agreement without the university's consent.", "free assignment")
    return _Check("review", "Assignment rights are not clear.")


CHECKS: dict[str, Callable[[str, dict[str, Any]], _Check]] = {
    "LicenseScope": check_scope,
    "LicenseGrant": check_scope,
    "Fees": check_fees,
    "Payment": check_fees,
    "Royalties": check_fees,
    "Term": check_renewal,
    "AutoRenewal": check_renewal,
    "AuditRights": check_audit,
    "type.service-levels": check_sla,
    "type.sla": check_sla,
    "Warranty": check_warranty,
    "Indemnity": check_indemnity,
    "Liability": check_liability,
    "DataProcessing": check_data,
    "DataRetention": check_data,
    "BreachNotification": check_data,
    "DataRights": check_data,
    "SecurityControls": check_security,
    "Accessibility": check_accessibility,
    "Termination": check_termination,
    "Insurance": check_insurance,
    "SourceCodeEscrow": check_escrow,
    "Assignment": check_assignment,
    # Governing law and export control read the same way for a buyer.
    "GoverningLaw": _m._check_governing_law,
    "ExportControl": _m._check_export_control,
}

THRESHOLDS: dict[str, dict[str, float]] = {
    "Fees": {"maxIncreasePct": 3, "fallbackIncreasePct": 5},
    "Term": {"maxNoticeDays": 30, "fallbackNoticeDays": 60},
    "AuditRights": {"minAuditNoticeDays": 30},
    "type.service-levels": {"minUptimePct": 99.9, "fallbackUptimePct": 99.5},
    "DataProcessing": {"maxBreachHours": 72, "fallbackBreachHours": 120},
}
THRESHOLD_DEFAULTS: dict[str, float] = {k: v for t in THRESHOLDS.values() for k, v in t.items()}


# ---------------------------------------------------------------------------
# Default playbook
# ---------------------------------------------------------------------------


def _c(clause_type: str, standard: str, fallback: str | None, unacceptable: list[str], beneficial: list[str],
       office: str | None, language: str | None, required: bool = True) -> dict[str, Any]:
    clause = _m._clause(clause_type, standard, fallback, unacceptable, beneficial, office, language, None, required)
    clause["thresholds"] = dict(THRESHOLDS.get(clause_type, {}))
    clause["label"] = LABELS.get(clause_type, clause["label"])
    return clause


LABELS: dict[str, str] = {
    "LicenseScope": "Licence scope and metric (users, seats, enterprise)",
    "Fees": "Fees and renewal price increases",
    "Term": "Term, auto-renewal and notice to cancel",
    "AuditRights": "Vendor audit rights",
    "type.service-levels": "Service levels, uptime and service credits",
    "Warranty": "Vendor warranties",
    "Indemnity": "IP infringement indemnity",
    "Liability": "Limitation of liability",
    "DataProcessing": "Data protection, breach notice and data return (FERPA / HIPAA)",
    "SecurityControls": "Security controls (SOC 2, HECVAT)",
    "Accessibility": "Accessibility (WCAG 2.1 AA, Section 508)",
    "Termination": "Termination for convenience and transition",
    "Insurance": "Vendor cyber insurance",
    "GoverningLaw": "Governing law and sovereign immunity",
    "Assignment": "Assignment and change of control",
}


def default_clauses() -> list[dict[str, Any]]:
    """Default positions for a software or SaaS purchase (the university buys).
    Offices: Procurement for commercial terms, IT Security for data and
    security, Accessibility for WCAG, Legal Affairs and Risk Management for
    legal risk."""
    return [
        _c("LicenseScope", "Enterprise or campus-wide use (or an FTE metric) covering affiliates and contractors.",
           "Named or concurrent users with an annual true-up at the contracted rate, going forward only.",
           ["retroactive fees", "true-up at list price"], ["unlimited users", "site license"], "procurement",
           "Customer and its Affiliates, and their contractors acting on Customer's behalf, may use the Software. "
           "Any additional use will be reconciled once a year at the per-unit rate in this Order, going forward only."),
        _c("Fees", "Renewal price increases capped at 3% a year; prices fixed for the initial term.",
           "Increases capped at 5% a year or CPI, whichever is lower.",
           ["then-current list price"], ["fixed for the term", "price lock"], "procurement",
           "Fees are fixed for the Initial Term. Any increase on renewal will not exceed three percent (3%) of the "
           "fees for the preceding term and requires ninety (90) days' written notice."),
        _c("Term", "No automatic renewal, or renewal that can be stopped on no more than 30 days' notice.",
           "Notice of no more than 60 days, with a renewal reminder from the vendor.",
           ["evergreen"], ["renew only by mutual written agreement"], "procurement",
           "This Agreement renews only by written agreement of the parties. Vendor will remind Customer of the "
           "renewal date at least ninety (90) days before it."),
        _c("AuditRights", "At most one audit a year, on 30 days' notice, during business hours, at the vendor's cost; "
                          "self-certification first.",
           "Audits on at least 10 days' notice.", ["audit without notice", "at any time"], ["self-certification"],
           "legal_affairs",
           "No more than once in any twelve (12) month period, on at least thirty (30) days' written notice, Vendor "
           "may ask Customer to certify its use. Any audit is at Vendor's expense and conducted remotely where possible.",
           required=False),
        _c("type.service-levels", "99.9% monthly uptime with service credits and a right to terminate for repeated failures.",
           "99.5% uptime with service credits.", ["no service level"], ["chronic failure"], "it_security",
           "Vendor will make the Service available 99.9% of each month, excluding scheduled maintenance announced "
           "48 hours in advance. If it does not, Customer receives the service credits in Exhibit B and may terminate "
           "if availability falls below 99.5% in any two months of a six-month period.", required=False),
        _c("Warranty", "The vendor warrants the software performs to its documentation for the term, contains no "
                       "malicious code and does not infringe.",
           "A 90-day performance warranty plus a no-malicious-code warranty.", [],
           ["malicious code"], "legal_affairs",
           "Vendor warrants that the Software will perform materially in accordance with its Documentation during "
           "the Term and will not contain any virus, malware or disabling code."),
        _c("Indemnity", "The vendor defends and indemnifies the university against IP infringement and data-breach "
                        "claims. The university gives no indemnity.",
           "The university indemnifies only to the extent permitted by its home-state law.",
           ["customer shall indemnify", "licensee shall indemnify"], ["data breach"], "legal_affairs",
           "Vendor shall defend, indemnify and hold harmless Customer from any claim that the Software infringes "
           "a third party's intellectual property rights or arising from a breach of Vendor's data security obligations."),
        _c("Liability", "A mutual cap no lower than the fees for 12 months, with data breach, confidentiality and "
                        "the IP indemnity outside the cap.",
           "A cap at the fees paid, with data breach outside it.", ["unlimited liability"], [], "risk_management",
           "Each party's aggregate liability is limited to the greater of the fees paid or payable in the twelve (12) "
           "months before the claim; this limit does not apply to Vendor's indemnity, breach of confidentiality or "
           "breach of data security obligations."),
        _c("DataProcessing", "The university owns its data; breach notice within 72 hours; data returned and then "
                             "deleted within 30 days of exit; FERPA (school official) and HIPAA terms where they apply.",
           "Breach notice within 5 days.", ["sell customer data"], ["FERPA", "school official"], "it_security",
           "Customer owns all Customer Data. Vendor will notify Customer of any security incident affecting Customer "
           "Data within seventy-two (72) hours and, within thirty (30) days after termination, return the data in a "
           "standard format and then delete it. For education records, Vendor acts as a school official under FERPA."),
        _c("SecurityControls", "An annual SOC 2 Type II report (or ISO 27001) and a completed HECVAT.",
           "Industry-standard safeguards with a security questionnaire.", [], ["SOC 2", "HECVAT"], "it_security",
           "Vendor will maintain a SOC 2 Type II report and provide it, with a completed HECVAT, once a year on request.",
           required=False),
        _c("Accessibility", "Conforms to WCAG 2.1 AA (Section 508), with a VPAT.",
           "A remediation roadmap with dates.", [], ["VPAT"], "accessibility",
           "Vendor warrants that the Software conforms to WCAG 2.1 Level AA, will provide a current VPAT, and will "
           "remedy any non-conformance at no charge within a reasonable time.", required=False),
        _c("Termination", "The university may terminate for convenience or non-appropriation of funds, with "
                          "transition assistance and its data returned.",
           "Termination for breach with a 30-day cure period.", [], ["transition assistance"], "procurement",
           "Customer may terminate this Agreement for convenience or for non-appropriation of funds on thirty (30) "
           "days' written notice, with a pro-rata refund of prepaid fees.", required=False),
        _c("Insurance", "The vendor carries cyber liability insurance of at least $5 million.",
           None, [], ["additional insured"], "risk_management",
           "Vendor will maintain cyber liability and technology errors and omissions insurance of at least five "
           "million dollars ($5,000,000) per claim and provide a certificate on request.", required=False),
        _c("GoverningLaw", "Laws of the university's home state; no waiver of sovereign immunity.",
           None, ["waive sovereign immunity", "waives sovereign immunity"], [], "legal_affairs",
           "This Agreement is governed by the laws of the State in which Customer is located. Nothing in it waives "
           "Customer's sovereign immunity."),
        _c("Assignment", "The vendor may not assign without consent; a change of control lets the university terminate.",
           None, [], [], "legal_affairs",
           "Vendor may not assign this Agreement without Customer's prior written consent. If Vendor undergoes a "
           "change of control, Customer may terminate on thirty (30) days' notice with a pro-rata refund.",
           required=False),
    ]


# ---------------------------------------------------------------------------
# Recognising a software purchase
# ---------------------------------------------------------------------------

_SOFTWARE_SIGNALS = re.compile(
    r"software\s+as\s+a\s+service|\bsaas\b|subscription\s+(?:agreement|term|fee|services)|end[- ]user\s+licen|\beula\b"
    r"|cloud\s+services?|hosted\s+(?:service|solution)|order\s+form|named\s+users?|concurrent\s+users?|\bseats?\b"
    r"|uptime|service\s+level\s+agreement|terms\s+of\s+service|master\s+subscription|software\s+licen[cs]e\s+agreement"
    r"|maintenance\s+and\s+support|support\s+services")
_TECH_TRANSFER = re.compile(
    r"licensed\s+patents?|patent\s+rights|net\s+sales|running\s+royalt|sublicense\s+income|diligence\s+milestones?"
    r"|commerciali[sz]")


def looks_like_software_purchase(title: str, text: str, doc_type: str = "") -> bool:
    """True for an inbound software or SaaS purchase: software / subscription
    language and none of the tech-transfer markers of a licence the university
    grants (licensed patents, net sales, sublicense income)."""
    blob = f"{title} {text}"
    if _TECH_TRANSFER.search(blob):
        return False
    hits = len(set(m.group(0) for m in _SOFTWARE_SIGNALS.finditer(blob)))
    return hits >= 2 or (hits >= 1 and doc_type in ("LICENSE", "MSA", "SOW", "DPA"))
