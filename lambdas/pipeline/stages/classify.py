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
from shared.openai_client import chat_json
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
    "Insurance", "Other",
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
    "docType":       {"type": "string", "enum": ["SOW", "MSA", "AMENDMENT", "NDA", "OTHER"]},
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
        "startDate":  _nstr(),
        "endDate":    _nstr(),
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
- docType: SOW, MSA, AMENDMENT, NDA, or OTHER.
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

TIMELINE — project start/end, phase breakdown with dates, and milestones (many
are tied to payments — capture the payment amount and a verbatim source quote).

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
- baseValue: the original/base contract or SOW fee. For an AMENDMENT, baseValue is
  null unless the amendment restates the original.
- totalContractValue: the document's stated total / not-to-exceed / new total.
- amendmentDelta: for an AMENDMENT only, the net amount it adds or removes (a
  reduction is negative); null for a base SOW/MSA.
- newTotalValue: an amendment's restated total, if stated.
- lineItems: list EVERY distinct monetary figure in the document with its label and
  verbatim source.
- reconciled: set true only if the numbers are internally consistent — e.g. for an
  amendment, base + amendmentDelta == newTotalValue (within $1 rounding), or for a
  SOW the line items sum to the stated total. If they do NOT reconcile, set false
  and explain the discrepancy in `issues`.
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
    result.setdefault("timeline", {"startDate": None, "endDate": None, "phases": [], "milestones": []})
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

    text    = truncate_to_tokens(parsed["text"],
                                  max_tokens=settings.classify_max_input_tokens,
                                  model=settings.chat_model)
    headers = detect_clause_headers(parsed["text"])
    hints   = [f"{n} {t}" for n, t, _ in headers[:50]]

    result = chat_json(
        system=_SYSTEM,
        user=_user_prompt(text, hints),
        json_schema=_SCHEMA,
        schema_name="ContractIntelligence",
        model=settings.chat_model,
        temperature=0.0,
    )

    _apply_defaults(result)

    # ── Validation agent — reconcile the money against the document ──────
    result["validation"] = _validate(text, result)

    result["structuralHash"] = structural_hash(result["clauses"])

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info("classify.done",
             docType=result["docType"],
             clauses=len(result["clauses"]),
             findings=len(result["keyFindings"]),
             tcv=result.get("commercials", {}).get("totalContractValue"),
             reconciled=result["validation"].get("reconciled"))

    event["classification"] = result
    return event


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
    result["commercials"] = commercials
    result["amendment"] = amendment

    return {
        "validated":   True,
        "reconciled":  v.get("reconciled"),
        "lineItems":   v.get("lineItems", []),
        "issues":      v.get("issues", []),
        "confidence":  v.get("confidence", "medium"),
    }
