"""Workforce edition: services and staffing agreements, read from the CLIENT's
side (the organization buys services or labour from a vendor).

The Workforce edition's agreement types are the statement of work (``sow``),
the master services agreement (``msa``) and the staffing vendor agreement
(``staffing``). Their matrix focuses on what a client of a services vendor
cares about: hourly rates and caps, overtime, who owns the work product,
co-employment, plus the buyer-side liability, indemnity, warranty and
termination checks shared with software purchases (``software.py``).

Deterministic like the rest of the matrix: regular expressions over clause
text, no model call.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import matrix as _m
from . import software as _sw

_Check = _m._Check

WORKFORCE_TYPES = ("sow", "msa", "staffing", "subcontract")


def _low(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def check_rates(text: str, th: dict[str, Any]) -> _Check:
    """Rates are fixed or capped (not-to-exceed), and any annual rate increase
    is at most maxRateIncreasePct (fallback fallbackRateIncreasePct). Rates the
    vendor may change at will deviate; a not-to-exceed total is beneficial."""
    max_p, fb_p = _m._th(th, "maxRateIncreasePct"), _m._th(th, "fallbackRateIncreasePct")
    low = _low(text)
    ben = "The engagement has a not-to-exceed total." if re.search(r"not[- ]to[- ]exceed|\bnte\b|shall\s+not\s+exceed", low) else None
    if re.search(r"(?:sole|absolute)\s+discretion[^.]{0,60}(?:rate|fee|price)|then[- ]current\s+(?:rates|rate\s+card)|change\s+(?:its\s+)?rates\s+at\s+any\s+time", low):
        return _Check("deviates", "The vendor can change its rates at will; the matrix requires fixed rates with increases "
                                  f"capped at {_m._fmt(max_p)}% a year.", "rates at vendor's discretion", ben)
    worst = None
    for s in _m._sentences(text):
        if re.search(r"increase|escalat|adjust", s, re.I) and re.search(r"rate|fee|price", s, re.I):
            for m in _m._PCT_RE.finditer(s):
                v = float(m.group(1))
                worst = v if worst is None else max(worst, v)
    if worst is None:
        if re.search(r"hourly\s+rate|per\s+hour|rate\s+card|time\s+and\s+materials|fixed\s+fee|fixed\s+price", low):
            return _Check("within", None, "fixed or listed rates", ben)
        return _Check("review", "Rates and any increases could not be read; confirm the rate card and a cap on increases.", None, ben)
    found = f"rate increase up to {_m._fmt(worst)}%"
    if worst <= max_p:
        return _Check("within", None, found, ben)
    return _Check("fallback" if worst <= fb_p else "deviates",
                  f"Rates may rise {_m._fmt(worst)}% a year; the matrix allows {_m._fmt(max_p)}% (fallback {_m._fmt(fb_p)}%).",
                  found, ben)


def check_hours_cap(text: str, th: dict[str, Any]) -> _Check:
    """Billable hours are capped (a weekly or total cap, or a not-to-exceed
    budget), and work beyond it needs the client's written approval."""
    low = _low(text)
    capped = re.search(r"(?:shall|will)\s+not\s+exceed\s+[^.]{0,40}hours|(?:maximum|cap)\s+of\s+[^.]{0,20}hours|hours?\s+(?:cap|limit)|not[- ]to[- ]exceed", low)
    approval = re.search(r"(?:prior\s+)?written\s+(?:approval|authori[sz]ation|consent)|change\s+order|pre[- ]?approv", low)
    if capped and approval:
        return _Check("within", None, "hours capped, extra hours need approval")
    if capped:
        return _Check("fallback", "Hours are capped, but work beyond the cap does not need written approval.", "hours capped")
    if re.search(r"unlimited\s+hours|as\s+many\s+hours\s+as|bill\s+(?:all|any)\s+hours", low):
        return _Check("deviates", "Hours are uncapped; the matrix requires a cap and written approval for more.", "uncapped hours")
    return _Check("review", "No cap on billable hours was found; ask for a cap with written approval for more.")


def check_overtime(text: str, th: dict[str, Any]) -> _Check:
    """Overtime only with the client's prior written approval, billed at no
    more than maxOvertimeMultiple × the standard rate (fallback
    fallbackOvertimeMultiple). Overtime billed without approval deviates."""
    max_x, fb_x = _m._th(th, "maxOvertimeMultiple"), _m._th(th, "fallbackOvertimeMultiple")
    low = _low(text)
    if "overtime" not in low and "over time" not in low:
        return _Check("within", None, "no overtime billed")
    approval = re.search(r"(?:prior\s+)?written\s+(?:approval|authori[sz]ation|consent)|pre[- ]?approv|approved\s+in\s+advance", low)
    mult = None
    m = re.search(r"(\d(?:\.\d{1,2})?)\s*(?:x|times)\s+(?:the\s+)?(?:standard|regular|base|normal)", low)
    if m:
        mult = float(m.group(1))
    elif re.search(r"time\s+and\s+a\s+half", low):
        mult = 1.5
    elif re.search(r"double\s+time", low):
        mult = 2.0
    if not approval:
        return _Check("deviates", "Overtime can be billed without the client's prior written approval.",
                      f"overtime {mult:g}× without approval" if mult else "overtime without approval")
    if mult is None or mult <= max_x:
        return _Check("within", None, "overtime pre-approved" + (f", {mult:g}×" if mult else ""))
    return _Check("fallback" if mult <= fb_x else "deviates",
                  f"Overtime is billed at {mult:g}× the standard rate; the matrix allows {_m._fmt(max_x)}× "
                  f"(fallback {_m._fmt(fb_x)}×).", f"overtime {mult:g}×")


def check_work_for_hire(text: str, th: dict[str, Any]) -> _Check:
    """The client owns the work product: deliverables are works made for hire
    and the vendor assigns all rights, keeping only its pre-existing tools
    under a licence to the client. Vendor ownership of deliverables deviates."""
    low = _low(text)
    vendor_owns = re.search(r"(?:vendor|contractor|supplier|consultant|provider)\s+(?:shall\s+)?(?:retain|own)s?\s+(?:all\s+)?"
                            r"(?:right,?\s+title|ownership|intellectual\s+property)[^.]{0,80}(?:deliverable|work\s+product)", low)
    if vendor_owns:
        return _Check("deviates", "The vendor keeps ownership of the deliverables; the matrix requires the client to own the "
                                  "work product (work made for hire, with an assignment).", "vendor owns deliverables")
    wfh = re.search(r"work(?:s)?\s+made\s+for\s+hire|work[- ]for[- ]hire", low)
    assign = re.search(r"(?:hereby\s+)?assigns?\s+(?:to\s+(?:the\s+)?(?:client|customer|company)\s+)?all\s+(?:right|rights)", low)
    if wfh or assign:
        ben = "The vendor's pre-existing tools are licensed to you." if re.search(r"pre[- ]existing|background|licen[cs]e\s+to\s+(?:use\s+)?(?:any|its)", low) else None
        return _Check("within", None, "work made for hire" if wfh else "assignment to the client", ben)
    if re.search(r"licen[cs]e\s+to\s+(?:use\s+)?(?:the\s+)?deliverables", low):
        return _Check("fallback", "The client only gets a licence to the deliverables, not ownership.", "licence only")
    return _Check("review", "Ownership of the work product is not clear; confirm it is work made for hire or assigned to you.")


def check_co_employment(text: str, th: dict[str, Any]) -> _Check:
    """Staffing: workers are the vendor's employees (independent contractor
    status, vendor pays wages, taxes and benefits), and the vendor indemnifies
    for employment claims. Anything treating them as the client's employees deviates."""
    low = _low(text)
    if re.search(r"(?:client|customer)\s+(?:shall\s+)?(?:be\s+)?(?:responsible\s+for|pay)[^.]{0,40}(?:payroll|wages|benefits|withholding)", low):
        return _Check("deviates", "The client is made responsible for workers' wages, benefits or payroll taxes.",
                      "client pays wages or benefits")
    vendor_employer = re.search(r"(?:employees?\s+of\s+(?:the\s+)?(?:vendor|contractor|supplier|agency)|independent\s+contractor"
                                r"|(?:vendor|contractor|supplier|agency)\s+(?:shall\s+)?(?:be\s+)?(?:solely\s+)?responsible\s+for[^.]{0,60}(?:wages|payroll|benefits|taxes))", low)
    if vendor_employer:
        ben = "The vendor indemnifies for employment claims." if re.search(r"indemn[^.]{0,80}(?:employment|wage|co-?employ|misclassif)", low) else None
        return _Check("within", None, "workers employed by the vendor", ben)
    return _Check("review", "Who employs the workers is not clear; confirm they are the vendor's employees.")


def check_expenses(text: str, th: dict[str, Any]) -> _Check:
    """Reimbursable expenses are pre-approved and capped (a not-to-exceed amount
    or a share of the fees, at most maxExpensePct), under the client's travel
    policy. Expenses reimbursed in full with no cap deviate."""
    max_p, fb_p = _m._th(th, "maxExpensePct"), _m._th(th, "fallbackExpensePct")
    low = _low(text)
    if not re.search(r"expens|reimburs|travel", low):
        return _Check("within", None, "no expenses billed")
    if re.search(r"(?:all|any)\s+(?:reasonable\s+)?(?:out[- ]of[- ]pocket\s+)?expenses[^.]{0,40}(?:reimburs|at\s+cost)", low) and not re.search(
            r"not\s+(?:to\s+)?exceed|cap|prior\s+(?:written\s+)?approv|pre[- ]?approv|policy", low):
        return _Check("deviates", "Expenses are reimbursed in full with no cap or approval.", "uncapped expenses")
    pct = None
    for m in _m._PCT_RE.finditer(low):
        pct = float(m.group(1))
    policy = re.search(r"travel\s+(?:and\s+expense\s+)?policy|prior\s+(?:written\s+)?approv|pre[- ]?approv", low)
    capped = re.search(r"not\s+(?:to\s+)?exceed|\bcap\b|capped|maximum", low)
    if pct is not None and capped:
        if pct <= max_p:
            return _Check("within", None, f"expenses capped at {_m._fmt(pct)}% of fees")
        return _Check("fallback" if pct <= fb_p else "deviates",
                      f"Expenses are capped at {_m._fmt(pct)}% of fees; the matrix allows {_m._fmt(max_p)}% "
                      f"(fallback {_m._fmt(fb_p)}%).", f"expenses up to {_m._fmt(pct)}%")
    if capped and policy:
        return _Check("within", None, "expenses capped and pre-approved")
    if capped or policy:
        return _Check("fallback", "Expenses are either capped or pre-approved, not both.", "expenses partly controlled")
    return _Check("review", "No cap or approval rule for expenses was found.")


def pricing_model(text: str) -> str | None:
    """fixed_fee, time_materials or mixed, from how the agreement prices the work."""
    low = _low(text)
    fixed = bool(re.search(r"fixed[- ](?:fee|price)|firm[- ]fixed|lump[- ]sum", low))
    tm = bool(re.search(r"time[- ]and[- ]materials?|\bt&m\b|hourly\s+rate|per\s+hour|rate\s+card|daily\s+rate", low))
    if fixed and tm:
        return "mixed"
    return "fixed_fee" if fixed else "time_materials" if tm else None


CHECKS: dict[str, Callable[[str, dict[str, Any]], _Check]] = {
    **_sw.CHECKS,
    "Fees": check_rates,
    "Payment": _m._check_payment,
    "type.hours-cap": check_hours_cap,
    "type.overtime": check_overtime,
    "IP": check_work_for_hire,
    "Deliverables": check_work_for_hire,
    "type.co-employment": check_co_employment,
    "type.expenses": check_expenses,
    "Term": _m._check_termination,
}

THRESHOLDS: dict[str, dict[str, float]] = {
    "Fees": {"maxRateIncreasePct": 3, "fallbackRateIncreasePct": 5},
    "type.overtime": {"maxOvertimeMultiple": 1.5, "fallbackOvertimeMultiple": 2},
    "Payment": {"netDays": 30, "fallbackNetDays": 60},
    "type.expenses": {"maxExpensePct": 10, "fallbackExpensePct": 15},
}
THRESHOLD_DEFAULTS: dict[str, float] = {k: v for t in THRESHOLDS.values() for k, v in t.items()}

LABELS: dict[str, str] = {
    "Fees": "Rates and rate increases",
    "type.hours-cap": "Hourly caps and approval for more hours",
    "type.overtime": "Overtime",
    "IP": "Work product ownership (work made for hire)",
    "type.co-employment": "Co-employment and worker status",
    "Indemnity": "Indemnification",
    "Liability": "Limitation of liability",
    "Warranty": "Warranties and acceptance",
    "Termination": "Termination for convenience",
    "Insurance": "Insurance",
    "Payment": "Payment terms and milestones",
    "type.expenses": "Expense reimbursement",
    "type.service-levels": "Service levels and penalties",
    "Confidentiality": "Confidentiality",
    "DataProcessing": "Data protection and security",
}


def _c(clause_type: str, standard: str, fallback: str | None, unacceptable: list[str], beneficial: list[str],
       office: str | None, language: str, required: bool = True) -> dict[str, Any]:
    clause = _m._clause(clause_type, standard, fallback, unacceptable, beneficial, office, language, None, required)
    clause["thresholds"] = dict(THRESHOLDS.get(clause_type, {}))
    clause["label"] = LABELS.get(clause_type, clause["label"])
    return clause


def _shared() -> list[dict[str, Any]]:
    return [
        _c("IP", "The client owns all deliverables and work product as works made for hire, with an assignment of any "
                 "rights that do not vest automatically; the vendor's pre-existing tools are licensed to the client.",
           "The vendor owns its generic tools; the client owns everything made specifically for it.",
           ["vendor retains all rights in the deliverables"], ["work made for hire"], "legal_affairs",
           "All Deliverables are works made for hire for Client. To the extent any Deliverable is not, Vendor hereby "
           "assigns to Client all right, title and interest in it. Vendor grants Client a perpetual, royalty-free "
           "licence to any Vendor pre-existing materials incorporated in the Deliverables."),
        _c("Indemnity", "The vendor indemnifies the client for IP infringement, its personnel's acts and data breaches; "
                        "the client gives no indemnity.",
           "Mutual indemnity limited to each party's own negligence.", ["client shall indemnify"], [],
           "legal_affairs",
           "Vendor shall defend, indemnify and hold harmless Client from any third-party claim arising from Vendor's "
           "personnel, its breach of this Agreement, or any allegation that the Deliverables infringe."),
        _c("Liability", "Mutual cap no lower than the fees paid in the prior 12 months, with indemnities, confidentiality "
                        "and data breach outside the cap.", "A cap at total fees paid.", ["unlimited liability"], [],
           "risk_management",
           "Each party's aggregate liability is limited to the fees paid or payable in the twelve (12) months before "
           "the claim, except for indemnification obligations, breach of confidentiality and data security breaches."),
        _c("Termination", "The client may terminate for convenience on 30 days' notice and pays only for accepted work.",
           "Termination for convenience on 60 days' notice.", [], ["transition assistance"], "procurement",
           "Client may terminate this Agreement or any SOW for convenience on thirty (30) days' written notice and will "
           "pay only for Services performed and accepted before termination.", required=False),
        _c("Payment", "Payment net 30 days from an undisputed, approved invoice, against accepted milestones.", "Net 60 days.",
           ["payment in advance"], [], "procurement",
           "Client will pay undisputed invoices within thirty (30) days of receipt, against milestones accepted in writing."),
        _c("type.expenses", "Reimbursable expenses only with prior approval, under Client's travel policy, capped at 10% "
                            "of fees.", "Capped at 15% of fees.", ["all expenses at cost"], [], "procurement",
           "Vendor will be reimbursed only for reasonable expenses approved in advance in writing and incurred under "
           "Client's travel policy, not to exceed ten percent (10%) of the fees under the applicable SOW."),
        _c("Insurance", "The vendor carries commercial general liability, professional liability and workers' "
                        "compensation insurance and names the client as an additional insured.",
           None, [], ["additional insured"], "risk_management",
           "Vendor will maintain commercial general liability, professional liability and workers' compensation "
           "insurance in amounts reasonably acceptable to Client and name Client as an additional insured.",
           required=False),
        _c("Confidentiality", "Mutual confidentiality for the term and five years after; trade secrets for as long as they "
                              "remain secret.", None, [], [], "legal_affairs",
           "Each party will keep the other's Confidential Information confidential during the Term and for five (5) "
           "years after, and trade secrets for as long as they remain trade secrets.", required=False),
    ]


def default_playbooks() -> dict[str, list[dict[str, Any]]]:
    """Default positions for the three Workforce agreement types."""
    rates = _c("Fees", "Fixed rates per the rate card; increases at most 3% a year and only on renewal; a "
                       "not-to-exceed total for the engagement.", "Increases up to 5% a year.",
               ["rates at vendor's sole discretion"], ["not to exceed"], "procurement",
               "Rates are fixed as set out in the Rate Card for the Term. Any increase takes effect only on renewal, "
               "on ninety (90) days' notice, and will not exceed three percent (3%).")
    hours = _c("type.hours-cap", "Billable hours are capped per week and in total; more hours need the client's prior "
                                 "written approval or a change order.", "A total cap only.", ["unlimited hours"], [],
               "procurement",
               "Vendor will not bill more than the hours stated in the SOW without Client's prior written approval in "
               "a signed change order.")
    overtime = _c("type.overtime", "Overtime only with the client's prior written approval, billed at no more than "
                                   "1.5× the standard rate.", "Up to 2× the standard rate, with prior approval.", [],
                  [], "procurement",
                  "Overtime must be approved in writing by Client in advance and is billed at no more than one and "
                  "one-half (1.5) times the standard hourly rate.", required=False)
    co_employment = _c("type.co-employment", "Workers are the vendor's employees; the vendor pays wages, taxes and "
                                             "benefits and indemnifies the client for employment claims.", None,
                       ["client shall be responsible for wages"], ["indemnify"], "legal_affairs",
                       "Vendor's personnel are employees of Vendor, not Client. Vendor is solely responsible for their "
                       "wages, benefits, taxes and withholding and will indemnify Client against any claim arising from "
                       "their employment or classification.")
    from . import software as _s
    sla = next(c for c in _s.default_clauses() if c["clauseType"] == "type.service-levels")
    sla = {**sla, "required": False, "label": LABELS["type.service-levels"]}
    flow_down = _c("type.flow-down", "The subcontractor accepts the prime agreement's terms that apply to its work "
                                     "(confidentiality, data, insurance, audit), and the client may terminate the "
                                     "subcontract with the prime.", None, [], [], "procurement",
                   "Subcontractor is bound by the terms of the Prime Agreement that apply to the Services, including "
                   "confidentiality, data protection, insurance and audit, and this Addendum ends with the Prime Agreement.")
    sow = [rates, hours, overtime, dict(sla)] + _shared()
    msa = [rates, dict(sla)] + _shared()
    staffing = [rates, hours, overtime, co_employment] + _shared()
    subcontract = [flow_down, rates, hours] + _shared()
    return {"sow": sow, "msa": msa, "staffing": staffing, "subcontract": subcontract}


_STAFFING_TITLE = re.compile(r"staffing|temporary\s+(?:worker|staff|labou?r)|contingent\s+(?:worker|labou?r)")
_STAFFING_TEXT = re.compile(r"staffing\s+(?:services\s+)?agreement|staffing\s+vendor|contingent\s+workers?\s+(?:agreement|services)")


def workforce_type(title: str, text: str, doc_type: str) -> str | None:
    """sow / msa / staffing from the document type and wording, or None."""
    title, text = (title or "").lower(), (text or "").lower()
    if re.search(r"subcontract", title):
        return "subcontract"
    if _STAFFING_TITLE.search(title) or _STAFFING_TEXT.search(text):
        return "staffing"
    if doc_type == "SOW" or re.search(r"statement\s+of\s+work|\bsow\b", title):
        return "sow"
    if doc_type == "MSA" or re.search(r"master\s+(?:services|service|consulting)\s+agreement|\bmsa\b", title):
        return "msa"
    return None
