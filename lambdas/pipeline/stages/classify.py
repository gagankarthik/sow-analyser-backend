"""Stage 02 — Classify: extract structured contract metadata via OpenAI.

Outputs a Classification object (docType, title, parties, effectiveDate,
lifecycle, clauses) and writes it to S3 as classification.json.
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

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["docType", "title", "parties", "effectiveDate", "lifecycle", "clauses"],
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
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["number", "title", "body", "category"],
                "properties": {
                    "number":   {"type": "string"},
                    "title":    {"type": "string"},
                    "body":     {"type": "string"},
                    "category": {"type": "string", "enum": _CLAUSE_CATEGORIES},
                },
            },
        },
    },
}

_SYSTEM = """\
You are a senior contracts analyst. You classify legal documents (SOWs, MSAs,
Amendments, NDAs) and extract their structured contents.

Rules:
- Return ONLY JSON conforming to the provided schema. No prose.
- docType: one of SOW, MSA, AMENDMENT, NDA, OTHER.
- lifecycle: one of draft, review, negotiation, approval, signed, active, renewal, expired.
  If unclear, use "draft".
- effectiveDate: ISO 8601 (YYYY-MM-DD) or null.
- parties: legal entity names only (not signatories).
- Split the document into numbered clauses. Each clause needs a number (e.g. "1",
  "2.1", "§7.4"), short title, verbatim body, and category.
- If no clause number is found, fabricate one in document order.
- Body must be verbatim clause text — do not paraphrase.
"""


def _user_prompt(text: str, hints: list[str]) -> str:
    hints_str = "\n".join(f"- {h}" for h in hints) if hints else "(none)"
    return (
        f"Classify and extract this contract document.\n\n"
        f"Detected headers/party hints:\n{hints_str}\n\n"
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

    result["structuralHash"] = structural_hash(result["clauses"])

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info("classify.done", docType=result["docType"], clauses=len(result["clauses"]))

    event["classification"] = result
    return event
