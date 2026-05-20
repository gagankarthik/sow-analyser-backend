"""Stage 02 — Classify: extract structured contract intelligence via OpenAI.

This stage does the heavy lifting of *understanding* the document. It returns a
Classification object containing:
  - document metadata (docType, title, parties, effectiveDate, lifecycle)
  - an executive summary of the whole contract
  - keyFindings — the important things a reviewer must know (risks, unusual
    terms, key dates, financial exposure)
  - clauses — each with verbatim body, a category, an LLM-assessed risk level,
    and a plain-English one-line summary

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
    "Other",
]

_RISK_LEVELS = ["low", "medium", "high", "critical"]
_FINDING_SEVERITY = ["info", "low", "medium", "high", "critical"]

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "docType", "title", "parties", "effectiveDate", "lifecycle",
        "summary", "keyFindings", "clauses",
    ],
    "properties": {
        "docType":       {"type": "string", "enum": ["SOW", "MSA", "AMENDMENT", "NDA", "OTHER"]},
        "title":         {"type": "string"},
        "parties":       {"type": "array", "items": {"type": "string"}},
        "effectiveDate": {"type": ["string", "null"]},
        "lifecycle": {
            "type": "string",
            "enum": ["draft", "review", "negotiation", "approval",
                     "signed", "active", "renewal", "expired"],
        },
        "summary": {"type": "string"},
        "keyFindings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "detail", "severity"],
                "properties": {
                    "label":    {"type": "string"},
                    "detail":   {"type": "string"},
                    "severity": {"type": "string", "enum": _FINDING_SEVERITY},
                },
            },
        },
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["number", "title", "body", "category", "riskLevel", "summary"],
                "properties": {
                    "number":    {"type": "string"},
                    "title":     {"type": "string"},
                    "body":      {"type": "string"},
                    "category":  {"type": "string", "enum": _CLAUSE_CATEGORIES},
                    "riskLevel": {"type": "string", "enum": _RISK_LEVELS},
                    "summary":   {"type": "string"},
                },
            },
        },
    },
}

_SYSTEM = """\
You are a senior contracts analyst. You read legal documents (SOWs, MSAs,
Amendments, NDAs) and extract a complete, structured, decision-ready analysis.

Return ONLY JSON conforming to the provided schema. No prose outside the JSON.

DOCUMENT-LEVEL FIELDS
- docType: one of SOW, MSA, AMENDMENT, NDA, OTHER.
- lifecycle: one of draft, review, negotiation, approval, signed, active,
  renewal, expired. If unclear, use "draft".
- effectiveDate: ISO 8601 (YYYY-MM-DD) or null.
- parties: legal entity names only (not individual signatories).
- summary: 2-4 sentences. What is this contract, between whom, for what scope,
  and what stands out commercially. Write for a busy executive.
- keyFindings: the 3-8 things a reviewer MUST know before signing. Each finding
  has a short label, a one-sentence detail, and a severity (info/low/medium/
  high/critical). Surface unusual terms, one-sided liability, missing caps,
  auto-renewals, aggressive termination rights, payment risk, IP assignment,
  data/privacy obligations, and notable dates or dollar amounts.

CLAUSE EXTRACTION
- Split the document into numbered clauses. Each needs a number (e.g. "1",
  "2.1", "§7.4"), a short title, the verbatim body, and a category.
- If no clause number exists, fabricate one in document order.
- body MUST be verbatim clause text — never paraphrase the body.
- category: choose the single best fit from the allowed list.
- riskLevel: assess the COMMERCIAL/LEGAL risk this specific clause poses to the
  receiving party, based on its ACTUAL wording — not just its category. A
  standard mutual liability cap is low/medium; an uncapped indemnity is critical.
  Use: low (standard, balanced), medium (worth noting), high (one-sided or
  costly), critical (serious exposure or a dealbreaker).
- summary: one plain-English sentence explaining what the clause does and why it
  matters. No legalese.
"""


def _user_prompt(text: str, hints: list[str]) -> str:
    hints_str = "\n".join(f"- {h}" for h in hints) if hints else "(none)"
    return (
        f"Analyze and extract this contract document.\n\n"
        f"Detected headers / party hints:\n{hints_str}\n\n"
        f"Document text:\n<<<DOC\n{text}\nDOC>>>"
    )


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
        schema_name="ContractClassification",
        model=settings.chat_model,
        temperature=0.0,
    )

    # Defensive defaults for optional fields.
    result.setdefault("parties", [])
    result.setdefault("clauses", [])
    result.setdefault("effectiveDate", None)
    result.setdefault("lifecycle", "draft")
    result.setdefault("summary", "")
    result.setdefault("keyFindings", [])

    # Defensive per-clause defaults — keep older consumers working even if the
    # model omits a field on an edge case.
    for c in result["clauses"]:
        c.setdefault("riskLevel", "low")
        c.setdefault("summary", "")

    result["structuralHash"] = structural_hash(result["clauses"])

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info("classify.done",
             docType=result["docType"],
             clauses=len(result["clauses"]),
             findings=len(result["keyFindings"]))

    event["classification"] = result
    return event
