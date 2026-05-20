"""Prompt + JSON schema definitions for stage 2 classification."""
from __future__ import annotations

from typing import Any


SYSTEM_PROMPT = """You are a senior contracts analyst. You classify legal documents
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
  (e.g. "1", "2.1", "§7.4"), a short title, a body of full prose, and a
  category from this set:
    Definitions, ScopeOfWork, Deliverables, Fees, Payment, Term,
    Termination, IP, Liability, Indemnity, Warranty, Confidentiality,
    DataProtection, Compliance, ChangeControl, Acceptance, ForceMajeure,
    DisputeResolution, GoverningLaw, Notices, Assignment, Subcontracting,
    Other
- If you cannot find a clause number, fabricate one in document order
  (e.g. "1", "2", "3"). Never leave the number blank.
- Body must be the verbatim clause text — do not paraphrase or summarize.
"""


USER_TEMPLATE = """Classify and extract the following contract document.

If headers or party names were already detected, they are listed here as hints:
{hints}

Document text:
<<<DOC
{text}
DOC>>>
"""


CLAUSE_CATEGORIES = [
    "Definitions",
    "ScopeOfWork",
    "Deliverables",
    "Fees",
    "Payment",
    "Term",
    "Termination",
    "IP",
    "Liability",
    "Indemnity",
    "Warranty",
    "Confidentiality",
    "DataProtection",
    "Compliance",
    "ChangeControl",
    "Acceptance",
    "ForceMajeure",
    "DisputeResolution",
    "GoverningLaw",
    "Notices",
    "Assignment",
    "Subcontracting",
    "Other",
]


def classification_schema() -> dict[str, Any]:
    """JSON schema accepted by OpenAI `response_format=json_schema`.

    Note: `additionalProperties: false` and `required: [...all keys...]` are
    mandatory under OpenAI's "strict" mode.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "docType",
            "title",
            "parties",
            "effectiveDate",
            "lifecycle",
            "clauses",
        ],
        "properties": {
            "docType": {
                "type": "string",
                "enum": ["SOW", "MSA", "AMENDMENT", "NDA", "OTHER"],
            },
            "title": {"type": "string"},
            "parties": {"type": "array", "items": {"type": "string"}},
            "effectiveDate": {"type": ["string", "null"]},
            "lifecycle": {
                "type": "string",
                "enum": [
                    "draft",
                    "review",
                    "negotiation",
                    "approval",
                    "signed",
                    "active",
                    "renewal",
                    "expired",
                ],
            },
            "clauses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["number", "title", "body", "category"],
                    "properties": {
                        "number": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": CLAUSE_CATEGORIES,
                        },
                    },
                },
            },
        },
    }


def build_user_prompt(text: str, header_hints: list[str] | None = None) -> str:
    hints = "\n".join(f"- {h}" for h in (header_hints or [])) or "(none)"
    return USER_TEMPLATE.format(hints=hints, text=text)
