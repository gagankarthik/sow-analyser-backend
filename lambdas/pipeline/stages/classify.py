"""Stage 02 — Classify: extract structured contract intelligence via OpenAI.

This stage does the heavy lifting of *understanding* the document. It returns a
Classification object that mirrors the real anatomy of a SOW / amendment:

  - identification — title, SOW number, the PARENT agreement reference (the
    single most valuable field for lineage), project name, parties, signatures
  - scope          — in-scope / out-of-scope / assumptions / dependencies
  - deliverables   — structured array (name, due date, acceptance, owner, value)
  - timeline       — start/end, phases, milestones (often tied to payments)
  - commercials    — the pricing block: TCV, model, rate card, payment schedule,
    terms, caps, currency — every money figure carries a verbatim `source` quote
  - slas           — metric / target / window / penalty
  - personnel + governance
  - clauses        — verbatim body, category, LLM-assessed risk, one-line summary
  - keyFindings    — what a reviewer MUST know before signing
  - amendment      — for AMENDMENT docs: the delta (number, parent reference,
    recitals chain, and a structured change array: type / target / before→after)
  - confidence     — flags that route ambiguous extractions to human review

After extraction a second LLM pass (the *validation agent*) re-reads the document
and reconciles every monetary figure against the text, so the dashboard never
shows a value that doesn't appear in — or add up against — the source.

The result is written to S3 as classification.json and is the source of truth
for the SOW analyzer, overview, and portfolio dashboard.
"""
from __future__ import annotations

from typing import Any

from shared.config import settings
from shared.dynamodb import update_status
from shared.logger import get_logger
from shared.openai_client import OutputTruncatedError, chat_json
from shared.playbook import evaluate_clauses
from shared.compliance import evaluate_compliance
from shared.domains import classify_domain
from shared.s3 import processed_key, put_json
from shared.text import detect_clause_headers, structural_hash, truncate_to_tokens

log = get_logger("blue-iq.classify")

# ---------------------------------------------------------------------------
# Schema + prompt
# ---------------------------------------------------------------------------

_CLAUSE_CATEGORIES = [
    "Definitions", "ScopeOfWork", "Deliverables", "Fees", "Payment", "Term",
    "Termination", "IP", "Liability", "Indemnity", "Warranty", "Confidentiality",
    "DataProtection", "Compliance", "ChangeControl", "Acceptance", "ForceMajeure",
    "DisputeResolution", "GoverningLaw", "Notices", "Assignment", "Subcontracting",
    "Insurance",
    # ── Technology / software licensing ────────────────────────────────────
    "LicenseGrant", "LicenseScope", "Restrictions", "Royalties", "Sublicensing",
    "SourceCodeEscrow", "AuditRights", "OpenSource",
    # ── Compliance / data-protection agreements (DPA / BAA / SOC 2 / VPAT) ─
    "DataProcessing", "DataResidency", "SubProcessors", "BreachNotification",
    "DataRetention", "SecurityControls", "Accessibility",
    "Other",
]

_RISK_LEVELS = ["low", "medium", "high", "critical"]
_FINDING_SEVERITY = ["info", "low", "medium", "high", "critical"]
_PRICING_MODELS = ["fixed", "time_and_materials", "milestone", "retainer", "mixed", "unknown"]
_AMENDMENT_TYPES = ["amendment", "change_order", "addendum", "side_letter", "none"]
_CHANGE_TYPES = ["replacement", "addition", "deletion", "modification"]
_CHANGE_CATEGORIES = ["scope", "value", "timeline", "payment", "personnel", "term", "sla", "other"]
_CONFIDENCE = ["high", "medium", "low"]


# --- reusable leaf builders (keep the strict schema readable) ----------------

def _str() -> dict[str, Any]:
    return {"type": "string"}


def _nstr() -> dict[str, Any]:
    return {"type": ["string", "null"]}


def _nnum() -> dict[str, Any]:
    return {"type": ["number", "null"]}


def _bool() -> dict[str, Any]:
    return {"type": "boolean"}


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    """Strict object: every property required, no extras."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(props.keys()),
        "properties": props,
    }


_SCHEMA: dict[str, Any] = _obj({
    # ── Header / identification ──────────────────────────────────────────
    "docType":       {"type": "string", "enum": ["SOW", "MSA", "AMENDMENT", "NDA", "LICENSE", "DPA", "BAA", "COMPLIANCE", "OTHER"]},
    "title":         _str(),
    "parties":       _arr(_str()),
    "effectiveDate": _nstr(),
    "lifecycle": {
        "type": "string",
        "enum": ["draft", "review", "negotiation", "approval",
                 "signed", "active", "renewal", "expired"],
    },
    "summary": _str(),
    "identification": _obj({
        "sowNumber":       _nstr(),
        "parentReference": _nstr(),  # "pursuant to the MSA dated…" — the PARENT pointer
        "projectName":     _nstr(),
        "clientName":      _nstr(),
        "vendorName":      _nstr(),
        "signatureStatus": {"type": "string", "enum": ["signed", "unsigned", "unknown"]},
        "executionDate":   _nstr(),
        "signatories":     _arr(_obj({
            "party": _nstr(), "name": _nstr(), "title": _nstr(), "date": _nstr(),
        })),
    }),

    # ── Scope ────────────────────────────────────────────────────────────
    "scope": _obj({
        "inScope":      _arr(_str()),
        "outOfScope":   _arr(_str()),
        "assumptions":  _arr(_str()),
        "dependencies": _arr(_str()),
    }),

    # ── Deliverables ──────────────────────────────────────────────────────
    "deliverables": _arr(_obj({
        "name":               _str(),
        "description":        _nstr(),
        "dueDate":            _nstr(),
        "acceptanceCriteria": _nstr(),
        "owner":              _nstr(),
        "value":              _nnum(),
    })),

    # ── Timeline & milestones ────────────────────────────────────────────
    "timeline": _obj({
        "startDate":         _nstr(),
        "endDate":           _nstr(),  # the contract/term END date (expiry)
        "renewalDate":       _nstr(),  # next renewal/anniversary date, if stated
        "autoRenews":        _bool(),  # true if the term auto-renews / evergreen
        "renewalNoticeDays": _nnum(),  # opt-out / non-renewal notice window, in days
        "phases":     _arr(_obj({"name": _str(), "start": _nstr(), "end": _nstr()})),
        "milestones": _arr(_obj({
            "name": _str(), "date": _nstr(), "payment": _nnum(), "source": _nstr(),
        })),
    }),

    # ── Commercials (the pricing block) ──────────────────────────────────
    "commercials": _obj({
        "currency":            _nstr(),
        "pricingModel":        {"type": "string", "enum": _PRICING_MODELS},
        "totalContractValue":  _nnum(),   # headline TCV / current total, as STATED
        "baseValue":           _nnum(),   # original SOW fee (null for an amendment unless restated)
        "caps":                _nnum(),   # not-to-exceed ceiling
        "paymentTerms":        _nstr(),   # "Net 45"
        "expenses":            _nstr(),   # reimbursable? capped?
        "latePayment":         _nstr(),   # interest / penalty terms
        "valueSource":         _nstr(),   # verbatim quote the TCV came from (provenance)
        "rateCard":            _arr(_obj({"role": _str(), "rate": _nnum(), "unit": _nstr()})),
        "paymentSchedule":     _arr(_obj({
            "label": _str(), "percent": _nnum(), "amount": _nnum(), "trigger": _nstr(),
        })),
    }),

    # ── Service levels ────────────────────────────────────────────────────
    "slas": _arr(_obj({
        "metric": _str(), "target": _nstr(), "window": _nstr(), "penalty": _nstr(),
    })),

    # ── People & governance ──────────────────────────────────────────────
    "personnel": _arr(_obj({"name": _nstr(), "role": _str(), "keyPerson": _bool()})),
    "governance": _obj({
        "cadence":        _nstr(),
        "escalationPath": _nstr(),
        "reporting":      _nstr(),
    }),

    # ── Findings & clauses ───────────────────────────────────────────────
    "keyFindings": _arr(_obj({
        "label": _str(), "detail": _str(),
        "severity": {"type": "string", "enum": _FINDING_SEVERITY},
    })),
    "clauses": _arr(_obj({
        "number":    _str(),
        "title":     _str(),
        "body":      _str(),
        "category":  {"type": "string", "enum": _CLAUSE_CATEGORIES},
        "riskLevel": {"type": "string", "enum": _RISK_LEVELS},
        "summary":   _str(),
    })),

    # ── Amendment delta (filled only for AMENDMENT docs) ─────────────────
    "amendment": _obj({
        "number":            _nstr(),
        "amendmentType":     {"type": "string", "enum": _AMENDMENT_TYPES},
        "parentReference":   _nstr(),  # the SOW/MSA this amends
        "recitals":          _nstr(),  # the WHEREAS chain naming parent + prior amendments
        "valueDelta":        _nnum(),  # amount THIS amendment adds/removes (e.g. +3000)
        "newTotalValue":     _nnum(),  # restated total, if the amendment states one
        "everythingElseStays": _bool(),
        "changes": _arr(_obj({
            "changeType":    {"type": "string", "enum": _CHANGE_TYPES},
            "category":      {"type": "string", "enum": _CHANGE_CATEGORIES},
            "targetSection": _nstr(),
            "before":        _nstr(),
            "after":         _nstr(),
            "summary":       _str(),
        })),
    }),

    # ── Confidence (routes ambiguous extractions to human review) ────────
    "confidence": _obj({
        "parentFound":     _bool(),
        "scopeClear":      _bool(),
        "financialsClear": _bool(),
        "overall":         {"type": "string", "enum": _CONFIDENCE},
        "issues":          _arr(_str()),
    }),
})

_SYSTEM = """\
You are a senior contracts analyst. You read Statements of Work, Master Service
Agreements, Amendments and NDAs and extract a complete, structured, decision-ready
analysis that mirrors the real anatomy of these documents.

Return ONLY JSON conforming to the provided schema. No prose outside the JSON.
Extract facts EXACTLY as written. NEVER invent, infer, round, or compute a number
that is not in the text — use null when a value is absent.

DOCUMENT-LEVEL
- docType: SOW, MSA, AMENDMENT, NDA, LICENSE, DPA, BAA, COMPLIANCE, or OTHER.
    · LICENSE — a technology/software licence agreement, EULA, or SaaS terms.
    · DPA — a Data Processing Agreement (GDPR / data-protection addendum).
    · BAA — a HIPAA Business Associate Agreement.
    · COMPLIANCE — a compliance attestation or report (SOC 2, VPAT, ISO,
      security/accessibility policy). Extract its stated scope and obligations.
- lifecycle: draft/review/negotiation/approval/signed/active/renewal/expired. If
  unclear use "draft"; if a signature block is signed use "signed"/"active".
- effectiveDate: ISO 8601 (YYYY-MM-DD) or null.
- parties: legal entity names only (not individual signatories).
- summary: 2-4 sentences for a busy executive — what this is, between whom, the
  scope, and what stands out commercially.

IDENTIFICATION
- sowNumber: e.g. "SOW-2024-0042" or "Statement of Work No. 3".
- parentReference: THE most valuable field for lineage. Hunt for "pursuant to",
  "under the Master Agreement dated", "governed by", "Agreement No." — capture the
  verbatim phrase naming the parent agreement. null if none.
- projectName, clientName, vendorName: as written.
- signatories: name + title + party + signature date for each signing block.
- signatureStatus: "signed" if a signature/date is present, else "unsigned"/"unknown".
- executionDate: the date the document was signed (distinct from effectiveDate).

SCOPE — extract in-scope and out-of-scope as SEPARATE lists. Out-of-scope and
exclusions matter most: when a later amendment adds something previously
out-of-scope, that is a flagged, costed scope change. Also list assumptions and
each party's dependencies.

DELIVERABLES — one object each: name, description, due date, acceptance criteria,
owner, and any associated value/milestone payment (number or null).

TIMELINE — project/contract start and END (expiry) date, phase breakdown with
dates, and milestones (many are tied to payments — capture the payment amount and
a verbatim source quote). Also capture the renewal terms, which drive the
obligations view:
- endDate: the date the term/contract ENDS or expires (ISO 8601), or null.
- renewalDate: the next renewal/anniversary date if one is stated or derivable
  from the term (ISO 8601), else null.
- autoRenews: true if the term automatically renews / is evergreen ("shall
  automatically renew for successive periods unless…"), else false.
- renewalNoticeDays: the non-renewal / opt-out notice window in days (convert
  months → days), or null if not stated.

COMMERCIALS — this drives every financial insight, extract it richly and precisely:
- currency (USD/EUR/…), pricingModel (fixed / time_and_materials / milestone /
  retainer / mixed / unknown).
- totalContractValue: the headline/current total contract value as STATED. Put the
  verbatim sentence it came from in valueSource. Do NOT compute it.
- baseValue: the original SOW/base fee. For an AMENDMENT leave baseValue null
  unless the amendment restates the original.
- caps: not-to-exceed / maximum spend ceiling.
- paymentTerms: "Net 30/45/60", "due on receipt" — flag because cash-flow impact.
- expenses (reimbursable? capped?), latePayment (interest/penalty).
- rateCard: roles + hourly/daily rates for T&M (rate number, unit "hour"/"day").
- paymentSchedule: milestone/percentage invoices — {label, percent, amount, trigger}.
  Include amount/percent only if stated.

SERVICE LEVELS — each SLA: metric (uptime, response time), target (e.g. 99.9%),
measurement window, penalty/credit for breach.

PEOPLE & GOVERNANCE — named key personnel (keyPerson=true if "key person" locked),
governance cadence (steering committee/status meetings), escalation path, reporting.

CLAUSES — split the document into numbered clauses. Each needs a number (e.g. "1",
"2.1", "§7.4"), a short title, the VERBATIM body (never paraphrase), and a category.
- riskLevel reflects the COMMERCIAL/LEGAL risk this specific clause poses to the
  receiving party based on its ACTUAL wording: low (standard/balanced),
  medium (worth noting), high (one-sided/costly), critical (serious exposure or a
  dealbreaker). A mutual liability cap is low/medium; an uncapped indemnity is
  critical. Pay special attention to: limitation of liability (cap type),
  indemnification (mutual vs one-sided), IP ownership, auto-renewal (trigger +
  opt-out window), termination (convenience + notice), data protection, insurance.
- summary: one plain-English sentence — what the clause does and why it matters.

LICENSING & COMPLIANCE DOCUMENTS — when the document is a LICENSE, DPA, BAA, or
COMPLIANCE attestation, extract its clauses using these categories in addition to
the general ones, and risk-score them from the receiving party's perspective:
- Licensing: LicenseGrant (what is licensed and on what basis — perpetual vs term,
  exclusive vs non-exclusive), LicenseScope (territory, field of use, named users,
  permitted environments), Restrictions (no reverse-engineering, no transfer, use
  limits — high risk if broad), Royalties (licence fees, usage/true-up, escalation),
  Sublicensing (whether and how rights may be passed on), SourceCodeEscrow,
  AuditRights (the licensor's right to inspect usage — note frequency/notice),
  OpenSource (any open-source components and their obligations).
- Compliance: DataProcessing (roles as controller/processor, purpose, instructions),
  DataResidency (where data is stored/processed), SubProcessors (named third
  parties and approval/notice rights), BreachNotification (the notification window —
  72 hours is the GDPR standard; longer is higher risk), DataRetention (how long
  data is kept and deletion on termination), SecurityControls (encryption, access,
  certifications referenced), Accessibility (WCAG/VPAT conformance level claimed).
Treat uncapped audit rights, broad use restrictions, vague breach windows, and
missing deletion/retention terms as elevated risk.

AMENDMENT (fill the `amendment` object; for non-amendments set amendmentType="none",
number=null, changes=[], everythingElseStays=false):
- An amendment is a DELTA document. Do NOT re-extract the whole contract — focus on
  what changed. Leave scope/deliverables/etc. minimal (only what the amendment text
  itself introduces).
- number ("Amendment No. 2"), amendmentType (amendment/change_order/addendum/
  side_letter), parentReference (the SOW/MSA it amends), recitals (the WHEREAS
  preamble naming the parent and prior amendments — version-chain gold).
- valueDelta: the amount THIS amendment adds or removes (e.g. "increased by
  $25,000" → 25000; a reduction is negative). newTotalValue: the restated total if
  the amendment states one (e.g. "to $505,000" → 505000), else null.
- everythingElseStays: true if it says "all other terms remain in full force".
- changes[]: one per change — changeType (replacement/addition/deletion/
  modification), category (scope/value/timeline/payment/personnel/term/sla/other),
  targetSection, before (often only in the parent — null if not stated here), after,
  and a one-line summary.

CONFIDENCE — be honest so humans can review the risky ones:
- parentFound: true only if a parent agreement reference was located.
- scopeClear / financialsClear: false when scope language is vague or money figures
  are ambiguous/conflicting.
- overall: high/medium/low. issues: short notes on anything uncertain (orphan
  amendment with no parent, ambiguous figures, unrecognized clause types).
"""

# --- Validation agent --------------------------------------------------------

_VALIDATE_SCHEMA: dict[str, Any] = _obj({
    "reconciled":          _bool(),     # base + Σ deltas == stated total (within rounding)
    "currency":            _nstr(),
    "baseValue":           _nnum(),     # corrected original/base value (null for pure amendment)
    "totalContractValue":  _nnum(),     # corrected current/total contract value
    "amendmentDelta":      _nnum(),     # for an amendment: the net amount it changes
    "newTotalValue":       _nnum(),     # restated total in an amendment, if any
    "paymentTerms":        _nstr(),
    "lineItems": _arr(_obj({
        "label":  _str(),
        "amount": _nnum(),
        "source": _str(),               # verbatim quote the figure was read from
    })),
    "issues":     _arr(_str()),
    "confidence": {"type": "string", "enum": _CONFIDENCE},
})

_VALIDATE_SYSTEM = """\
You are a financial QA validator for contract extraction. Another model extracted
commercial figures from a document; your job is to RE-READ the document and return
the corrected, canonical money figures — EXACTLY as written, never computed or
rounded. Every amount you return must have a verbatim `source` quote copied from the
document text. If a figure the first model produced does not actually appear in the
text, drop or correct it and note it in `issues`.

Rules:
- baseValue: the original/base contract or SOW fee (e.g. "Original Website SOW:
  $7,500" → 7500). For a pure AMENDMENT, baseValue is null unless it restates the
  original.
- totalContractValue: the document's stated CURRENT/NEW total. A phrase like
  "New Total Project Cost: $12,300", "Total Project Cost", "Total Contract Value",
  or "not-to-exceed" IS this value — capture it verbatim, never compute it. When a
  document lists an original fee PLUS amendment amounts AND a new total, set
  baseValue = the original fee and totalContractValue = the stated new total.
- amendmentDelta: for an AMENDMENT only, the net amount it adds or removes (a
  reduction is negative); null for a base SOW/MSA. e.g. "Amendment #1 (ATS
  Integration): $3,000" → 3000.
- newTotalValue: an amendment's restated total, if stated.
- lineItems: list EVERY distinct monetary figure in the document with its label and
  verbatim source (the original fee and each amendment amount are separate items).
- reconciled: set true only if the numbers are internally consistent —
  baseValue + Σ(amendment amounts) == totalContractValue / newTotalValue (within $1
  rounding). For the example above: 7500 + 3000 + 1800 == 12300 → reconciled true.
  If they do NOT add up, set false and explain the discrepancy in `issues`.
- confidence: high/medium/low based on how clearly the figures appear in the text.
Return ONLY JSON conforming to the schema.
"""


def _user_prompt(text: str, hints: list[str]) -> str:
    hints_str = "\n".join(f"- {h}" for h in hints) if hints else "(none)"
    return (
        f"Analyze and extract this contract document.\n\n"
        f"Detected headers / party hints:\n{hints_str}\n\n"
        f"Document text:\n<<<DOC\n{text}\nDOC>>>"
    )


def _validate_prompt(text: str, commercials: dict[str, Any], amendment: dict[str, Any]) -> str:
    import orjson
    extracted = {
        "docTypeIsAmendment": (amendment or {}).get("amendmentType", "none") != "none",
        "commercials": {
            "currency":           (commercials or {}).get("currency"),
            "totalContractValue": (commercials or {}).get("totalContractValue"),
            "baseValue":          (commercials or {}).get("baseValue"),
            "paymentTerms":       (commercials or {}).get("paymentTerms"),
        },
        "amendment": {
            "valueDelta":    (amendment or {}).get("valueDelta"),
            "newTotalValue": (amendment or {}).get("newTotalValue"),
        },
    }
    return (
        "Figures the first model extracted (verify and correct against the text):\n"
        f"{orjson.dumps(extracted).decode()}\n\n"
        f"Document text:\n<<<DOC\n{text}\nDOC>>>"
    )


# ---------------------------------------------------------------------------
# Defensive defaults — keep older consumers working if the model omits a field
# ---------------------------------------------------------------------------

def _apply_defaults(result: dict[str, Any]) -> None:
    result.setdefault("parties", [])
    result.setdefault("clauses", [])
    result.setdefault("effectiveDate", None)
    result.setdefault("lifecycle", "draft")
    result.setdefault("summary", "")
    result.setdefault("keyFindings", [])
    result.setdefault("deliverables", [])
    result.setdefault("slas", [])
    result.setdefault("personnel", [])
    result.setdefault("identification", {})
    result.setdefault("scope", {"inScope": [], "outOfScope": [], "assumptions": [], "dependencies": []})
    result.setdefault("timeline", {"startDate": None, "endDate": None, "renewalDate": None,
                                   "autoRenews": False, "renewalNoticeDays": None,
                                   "phases": [], "milestones": []})
    result.setdefault("governance", {"cadence": None, "escalationPath": None, "reporting": None})
    result.setdefault("commercials", {})
    result.setdefault("amendment", {"amendmentType": "none", "changes": []})
    result.setdefault("confidence", {})

    for c in result["clauses"]:
        c.setdefault("riskLevel", "low")
        c.setdefault("summary", "")


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    parsed           = event.get("parsed") or {}

    if not parsed.get("text"):
        raise ValueError("classify: parsed.text is missing from pipeline event")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "CLASSIFYING")

    full_text = parsed["text"]
    text    = truncate_to_tokens(full_text,
                                  max_tokens=settings.classify_max_input_tokens,
                                  model=settings.chat_model)
    input_truncated = len(text) < len(full_text)
    headers = detect_clause_headers(full_text)
    hints   = [f"{n} {t}" for n, t, _ in headers[:50]]

    result = _classify_document(text, hints)

    _apply_defaults(result)

    # If the input had to be truncated to fit the context window, the extraction
    # may be missing middle-of-document content. Flag it honestly so the UI can
    # route the document to human review rather than presenting it as complete.
    if input_truncated:
        conf = result.setdefault("confidence", {})
        conf["overall"] = "low"
        issues = conf.setdefault("issues", [])
        issues.append(
            "Document exceeded the analysis window and was truncated; some "
            "middle-of-document clauses may not have been extracted."
        )

    # ── Validation agent — reconcile the money against the document ──────
    result["validation"] = _validate(text, result)

    # ── Playbook check — surface deviations from the firm's standard positions.
    # Deterministic, runs server-side, persisted into classification.json so the
    # existing read API exposes it. (No separate Step Functions stage required.)
    result["playbook"] = evaluate_clauses(result["clauses"], tenant_id)

    # ── Compliance packs — grade the document against the tenant's enabled
    # frameworks (GDPR/HIPAA/SOC2/…). Deterministic: coverage is computed from the
    # extracted clause categories + the playbook result above.
    result["compliance"] = evaluate_compliance(result["clauses"], result["playbook"], tenant_id=tenant_id)

    # ── Pillar (domain) tagging — Blue-IQ Campus' four entry points. Deterministic:
    # derived from docType + clause categories + renewal terms + the compliance
    # gaps above. Persisted into classification.json for the read API.
    result["domain"] = classify_domain(result)

    result["structuralHash"] = structural_hash(result["clauses"])

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info("classify.done",
             docType=result["docType"],
             clauses=len(result["clauses"]),
             findings=len(result["keyFindings"]),
             tcv=result.get("commercials", {}).get("totalContractValue"),
             reconciled=result["validation"].get("reconciled"),
             playbookDeviations=result["playbook"].get("deviationCount"),
             complianceCoverage=result["compliance"].get("overallCoveragePct"),
             complianceGaps=result["compliance"].get("totalGaps"),
             pillar=result["domain"].get("pillar"),
             pillarRiskFlags=len(result["domain"].get("riskFlags", [])))

    event["classification"] = result
    return event


def _classify_document(text: str, hints: list[str]) -> dict[str, Any]:
    """Run the extraction call, retrying once with a larger output budget if the
    model truncated its JSON (which would drop trailing clauses).

    A truncated structured response is NOT silently accepted: chat_json raises
    OutputTruncatedError on finish_reason == "length". We retry once at the
    model's max completion budget so a long contract with many clauses is fully
    extracted; if it still truncates we re-raise so the pipeline fails loudly
    rather than persisting a document that is missing content.
    """
    try:
        return chat_json(
            system=_SYSTEM,
            user=_user_prompt(text, hints),
            json_schema=_SCHEMA,
            schema_name="ContractIntelligence",
            model=settings.chat_model,
            temperature=0.0,
        )
    except OutputTruncatedError:
        log.warning("classify.output_truncated_retry",
                    retryTokens=settings.chat_max_output_tokens_max)
        return chat_json(
            system=_SYSTEM,
            user=_user_prompt(text, hints),
            json_schema=_SCHEMA,
            schema_name="ContractIntelligence",
            model=settings.chat_model,
            temperature=0.0,
            max_tokens=settings.chat_max_output_tokens_max,
        )


def _validate(text: str, result: dict[str, Any]) -> dict[str, Any]:
    """Second LLM pass: re-read the doc, correct the money figures, reconcile.

    On success the canonical figures are written back into result.commercials /
    result.amendment so downstream consumers (dashboard, value bar) read
    validated numbers. The validation block itself is returned for the UI to
    surface provenance and any reconciliation warnings.
    """
    commercials = result.get("commercials") or {}
    amendment   = result.get("amendment") or {}
    try:
        v = chat_json(
            system=_VALIDATE_SYSTEM,
            user=_validate_prompt(text, commercials, amendment),
            json_schema=_VALIDATE_SCHEMA,
            schema_name="CommercialsValidation",
            model=settings.chat_model,
            temperature=0.0,
        )
    except Exception as exc:
        log.warning("classify.validate_failed", error=str(exc))
        return {"validated": False, "reconciled": None, "lineItems": [],
                "issues": ["validation pass unavailable"], "confidence": "low"}

    # Write the validated, source-backed figures back as the canonical values.
    if v.get("currency"):
        commercials["currency"] = v["currency"]
    if v.get("totalContractValue") is not None:
        commercials["totalContractValue"] = v["totalContractValue"]
    if v.get("baseValue") is not None:
        commercials["baseValue"] = v["baseValue"]
    if v.get("paymentTerms"):
        commercials.setdefault("paymentTerms", v["paymentTerms"])
    if v.get("amendmentDelta") is not None:
        amendment["valueDelta"] = v["amendmentDelta"]
    if v.get("newTotalValue") is not None:
        amendment["newTotalValue"] = v["newTotalValue"]

    # Persist provenance: prefer a line-item source for the total; this is the
    # verbatim quote the dashboard shows so a value never appears without a source.
    line_items = v.get("lineItems") or []
    if not commercials.get("valueSource"):
        src = _source_for_total(line_items, v.get("totalContractValue"))
        if src:
            commercials["valueSource"] = src

    result["commercials"] = commercials
    result["amendment"] = amendment

    # ── Deterministic arithmetic reconciliation ─────────────────────────────
    # Do NOT trust the model's `reconciled` flag blindly: re-check in code that
    # base + Σ(line-item amounts) == stated total (within $1). This is the core
    # money guarantee — an unreconciled or unverifiable figure is flagged, never
    # presented as fact.
    recon = _reconcile(
        base_value=v.get("baseValue"),
        total_value=v.get("totalContractValue"),
        new_total=v.get("newTotalValue"),
        amendment_delta=v.get("amendmentDelta"),
        line_items=line_items,
    )
    issues = list(v.get("issues") or [])
    if recon["computed"] and recon["reconciled"] is False:
        issues.append(recon["explanation"])

    return {
        "validated":         True,
        # In-code arithmetic wins over the model's self-report when we could
        # actually compute it; fall back to the model's flag otherwise.
        "reconciled":        recon["reconciled"] if recon["computed"] else v.get("reconciled"),
        "reconciledByMath":  recon["reconciled"] if recon["computed"] else None,
        "reconciliation":    recon,
        "lineItems":         line_items,
        "valueSource":       commercials.get("valueSource"),
        "issues":            issues,
        "confidence":        v.get("confidence", "medium"),
    }


def _source_for_total(line_items: list[dict[str, Any]], total: float | None) -> str | None:
    """Pick the verbatim source quote that best evidences the total value."""
    if not line_items:
        return None
    if total is not None:
        for li in line_items:
            if li.get("amount") is not None and abs(float(li["amount"]) - float(total)) < 1.0:
                return li.get("source") or None
    # Otherwise the largest line item is the most likely headline figure.
    best = max(
        (li for li in line_items if li.get("amount") is not None),
        key=lambda li: float(li["amount"]),
        default=None,
    )
    return (best or {}).get("source") if best else None


def _reconcile(
    *,
    base_value: float | None,
    total_value: float | None,
    new_total: float | None,
    amendment_delta: float | None,
    line_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recompute base + Σ(deltas) and compare to the stated total, in code.

    Returns {computed, reconciled, expectedTotal, statedTotal, sumOfParts,
    explanation}. ``computed`` is False when there isn't enough numeric evidence
    to check (e.g. only a single figure with no parts) — in that case we don't
    assert reconciliation either way.
    """
    stated = new_total if new_total is not None else total_value
    amounts = [float(li["amount"]) for li in line_items if li.get("amount") is not None]

    # The validator is asked to list the PARTS (original fee + each amendment).
    # If it also lists the total itself as a line item, summing everything would
    # double-count and produce a false mismatch — drop a single part that equals
    # the stated total before summing.
    if stated is not None:
        for i, amt in enumerate(amounts):
            if abs(amt - float(stated)) <= 1.0:
                amounts.pop(i)
                break

    # Path A: we have itemised parts — their sum should equal the stated total.
    if stated is not None and len(amounts) >= 2:
        sum_parts = round(sum(amounts), 2)
        reconciled = abs(sum_parts - float(stated)) <= 1.0
        return {
            "computed":     True,
            "reconciled":   reconciled,
            "expectedTotal": sum_parts,
            "statedTotal":  float(stated),
            "sumOfParts":   sum_parts,
            "explanation":  (
                "" if reconciled else
                f"Line items sum to {sum_parts:g} but the stated total is {float(stated):g} "
                f"(difference {abs(sum_parts - float(stated)):g}). Figures do not reconcile."
            ),
        }

    # Path B: base + amendment delta should equal the stated total.
    if stated is not None and base_value is not None and amendment_delta is not None:
        expected = round(float(base_value) + float(amendment_delta), 2)
        reconciled = abs(expected - float(stated)) <= 1.0
        return {
            "computed":     True,
            "reconciled":   reconciled,
            "expectedTotal": expected,
            "statedTotal":  float(stated),
            "sumOfParts":   expected,
            "explanation":  (
                "" if reconciled else
                f"Base {float(base_value):g} + delta {float(amendment_delta):g} = {expected:g}, "
                f"but the stated total is {float(stated):g}. Figures do not reconcile."
            ),
        }

    # Not enough numeric evidence to check arithmetic deterministically.
    return {
        "computed":     False,
        "reconciled":   None,
        "expectedTotal": None,
        "statedTotal":  float(stated) if stated is not None else None,
        "sumOfParts":   None,
        "explanation":  "",
    }
