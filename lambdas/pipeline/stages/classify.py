"""Stage 02 — Classify: call OpenAI to extract clauses and doc metadata."""
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
# OpenAI prompt + schema
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a senior contracts analyst. You classify legal documents
(Statements of Work, Master Service Agreements, Amendments, NDAs) and extract
their structured contents.

Rules:
- Return ONLY JSON that conforms to the provided schema. No prose.
- `docType` MUST be one of: SOW, MSA, AMENDMENT, NDA, OTHER.
- `lifecycle` MUST be one of: draft, review, negotiation, approval, signed,
  active, renewal, expired. If unclear, use "draft".
- `effectiveDate` MUST be an ISO 8601 date (YYYY-MM-DD) or null.
- `parties` is the legal entity names (not signatories).
- Split the document into numbered clauses. Each clause has a number
  (e.g. "1", "2.1", "§7.4"), a short title, verbatim body, and a category from:
    Definitions, ScopeOfWork, Deliverables, Fees, Payment, Term,
    Termination, IP, Liability, Indemnity, Warranty, Confidentiality,
    DataProtection, Compliance, ChangeControl, Acceptance, ForceMajeure,
    DisputeResolution, GoverningLaw, Notices, Assignment, Subcontracting, Other
- If no clause number found, fabricate one in document order. Never leave blank.
- Body must be verbatim clause text — do not paraphrase.
"""

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
            "enum": ["draft", "review", "negotiation", "approval", "signed", "active", "renewal", "expired"],
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


def _build_user_prompt(text: str, header_hints: list[str]) -> str:
    hints = "\n".join(f"- {h}" for h in header_hints) or "(none)"
    return (
        f"Classify and extract the following contract document.\n\n"
        f"If headers or party names were already detected, they are listed here as hints:\n{hints}\n\n"
        f"Document text:\n<<<DOC\n{text}\nDOC>>>"
    )


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict) -> dict:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    processed_bucket = event["processedBucket"]
    parsed           = event.get("parsed") or {}

    if not parsed.get("text"):
        raise ValueError("classify: pipeline event missing parsed.text")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "CLASSIFYING")

    text    = truncate_to_tokens(parsed["text"], max_tokens=settings.classify_max_input_tokens, model=settings.chat_model)
    headers = detect_clause_headers(parsed["text"])
    hints   = [f"{n} {t}" for n, t, _ in headers[:50]]

    result = chat_json(
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(text, hints),
        json_schema=_SCHEMA,
        schema_name="ContractClassification",
        model=settings.chat_model,
        temperature=0.0,
    )
    result.setdefault("parties", [])
    result.setdefault("clauses", [])
    result.setdefault("effectiveDate", None)
    result.setdefault("lifecycle", "draft")

    s_hash = structural_hash(result["clauses"])
    result["structuralHash"] = s_hash

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info("classify.done", docType=result["docType"], clauses=len(result["clauses"]))

    event["classification"] = result
    return event
