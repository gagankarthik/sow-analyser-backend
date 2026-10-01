"""Clause type taxonomy: the fixed categories plus open, normalised custom types.

``category`` stays one of the KNOWN categories below — the frontend filters,
heat-maps, playbook and compliance packs are keyed on it. A clause that fits
none of them keeps ``category = "Other"`` but is no longer anonymous: it also
carries

* ``specificType``     — a human-readable label ("Non-solicitation"),
* ``specificTypeKey``  — a normalised key ("non-solicitation") so the same type is
  spelled identically across documents, and
* ``typeIsCustom``     — True when the type is not one of the known categories.

Known clauses get the same three fields (label of their category, its key,
``typeIsCustom = False``), so a consumer can group by ``specificTypeKey`` alone.

Normalisation is deterministic: case, punctuation, plurals and filler words
("clause", "provisions") are folded, and obvious synonyms are merged. A custom
label that is really a known category under another name ("Limitation of
Liability", "Insurance requirements") is promoted to that category, so the model
cannot bypass the playbook by choosing "Other".
"""
from __future__ import annotations

import re
from typing import Any

KNOWN_CATEGORIES: list[str] = [
    "Definitions", "ScopeOfWork", "Deliverables", "Fees", "Payment", "Term",
    "Termination", "IP", "Liability", "Indemnity", "Warranty", "Confidentiality",
    "DataProtection", "Compliance", "ChangeControl", "Acceptance", "ForceMajeure",
    "DisputeResolution", "GoverningLaw", "Notices", "Assignment", "Subcontracting",
    "Insurance",
    # Technology / software licensing
    "LicenseGrant", "LicenseScope", "Restrictions", "Royalties", "Sublicensing",
    "SourceCodeEscrow", "AuditRights", "OpenSource",
    # Compliance / data-protection agreements
    "DataProcessing", "DataResidency", "SubProcessors", "BreachNotification",
    "DataRetention", "SecurityControls", "Accessibility",
    "Other",
]

_LABELS: dict[str, str] = {
    "ScopeOfWork": "Scope of work", "IP": "Intellectual property", "ForceMajeure": "Force majeure",
    "DisputeResolution": "Dispute resolution", "GoverningLaw": "Governing law",
    "DataProtection": "Data protection", "ChangeControl": "Change control",
    "LicenseGrant": "License grant", "LicenseScope": "License scope",
    "SourceCodeEscrow": "Source-code escrow", "AuditRights": "Audit rights",
    "OpenSource": "Open source", "DataProcessing": "Data processing",
    "DataResidency": "Data residency", "SubProcessors": "Sub-processors",
    "BreachNotification": "Breach notification", "DataRetention": "Data retention",
    "SecurityControls": "Security controls", "Liability": "Limitation of liability",
    "Indemnity": "Indemnification", "Warranty": "Warranties", "Term": "Term and renewal",
    "Fees": "Fees", "Payment": "Payment terms",
}

_ACRONYMS = {"ip", "sla", "slas", "gdpr", "hipaa", "ccpa", "nda", "kpi", "kpis", "pii", "phi",
             "soc", "iso", "tupe", "esg", "aml", "uk", "us", "eu", "vat", "gst", "it", "msa", "sow"}
_FILLER = {"clause", "clauses", "provision", "provisions", "section", "sections", "terms",
           "the", "a", "an", "general", "miscellaneous"}

# normalised key → known category. Lets a custom label that is plainly a known
# type be promoted, and gives the known categories their synonyms.
_KNOWN_SYNONYMS: dict[str, str] = {
    "definition": "Definitions", "interpretation": "Definitions", "definition-and-interpretation": "Definitions",
    "scope-of-work": "ScopeOfWork", "scope": "ScopeOfWork", "service": "ScopeOfWork", "scope-of-service": "ScopeOfWork",
    "deliverable": "Deliverables", "milestone": "Deliverables",
    "fee": "Fees", "pricing": "Fees", "charge": "Fees", "price": "Fees", "compensation": "Fees",
    "payment": "Payment", "invoicing": "Payment", "payment-term": "Payment", "invoicing-and-payment": "Payment",
    "term": "Term", "term-and-renewal": "Term", "renewal": "Term", "auto-renewal": "Term", "duration": "Term",
    "termination": "Termination", "term-and-termination": "Termination",
    "intellectual-property": "IP", "ip": "IP", "ip-ownership": "IP", "ownership": "IP",
    "intellectual-property-right": "IP", "work-product": "IP",
    "liability": "Liability", "limitation-of-liability": "Liability", "liability-cap": "Liability",
    "indemnity": "Indemnity", "indemnification": "Indemnity", "indemnity-obligation": "Indemnity",
    "warranty": "Warranty", "representation-and-warranty": "Warranty", "warranty-and-representation": "Warranty",
    "disclaimer": "Warranty",
    "confidentiality": "Confidentiality", "non-disclosure": "Confidentiality", "confidential-information": "Confidentiality",
    "data-protection": "DataProtection", "privacy": "DataProtection", "data-privacy": "DataProtection",
    "personal-data": "DataProtection",
    "compliance": "Compliance", "compliance-with-law": "Compliance", "regulatory-compliance": "Compliance",
    "change-control": "ChangeControl", "change-management": "ChangeControl", "change-request": "ChangeControl",
    "scope-change": "ChangeControl", "change-order": "ChangeControl",
    "acceptance": "Acceptance", "acceptance-criteria": "Acceptance", "acceptance-testing": "Acceptance",
    "force-majeure": "ForceMajeure",
    "dispute-resolution": "DisputeResolution", "arbitration": "DisputeResolution", "dispute": "DisputeResolution",
    "governing-law": "GoverningLaw", "jurisdiction": "GoverningLaw", "governing-law-and-jurisdiction": "GoverningLaw",
    "choice-of-law": "GoverningLaw",
    "notice": "Notices",
    "assignment": "Assignment", "assignment-and-transfer": "Assignment",
    "subcontracting": "Subcontracting", "subcontractor": "Subcontracting",
    "insurance": "Insurance", "insurance-requirement": "Insurance",
    "license-grant": "LicenseGrant", "licence-grant": "LicenseGrant", "grant-of-license": "LicenseGrant",
    "license-scope": "LicenseScope", "licence-scope": "LicenseScope",
    "restriction": "Restrictions", "use-restriction": "Restrictions", "license-restriction": "Restrictions",
    "royalty": "Royalties", "license-fee": "Royalties",
    "sublicensing": "Sublicensing", "sub-licensing": "Sublicensing",
    "source-code-escrow": "SourceCodeEscrow", "escrow": "SourceCodeEscrow",
    "audit-right": "AuditRights", "audit": "AuditRights", "audit-and-inspection": "AuditRights",
    "open-source": "OpenSource", "open-source-software": "OpenSource",
    "data-processing": "DataProcessing",
    "data-residency": "DataResidency", "data-location": "DataResidency",
    "sub-processor": "SubProcessors", "subprocessor": "SubProcessors",
    "breach-notification": "BreachNotification", "security-incident": "BreachNotification",
    "data-retention": "DataRetention", "data-deletion": "DataRetention", "record-retention": "DataRetention",
    "security-control": "SecurityControls", "information-security": "SecurityControls", "security": "SecurityControls",
    "accessibility": "Accessibility",
}

# normalised key → canonical custom key (obvious synonyms for types with no
# known category). Labels are derived from the canonical key.
_CUSTOM_SYNONYMS: dict[str, str] = {
    "non-solicit": "non-solicitation", "no-hire": "non-solicitation", "non-solicitation-of-employee": "non-solicitation",
    "non-poaching": "non-solicitation", "non-compete": "non-competition", "noncompete": "non-competition",
    "exclusivity": "exclusivity", "exclusive-dealing": "exclusivity",
    "service-level": "service-levels", "sla": "service-levels", "service-level-agreement": "service-levels",
    "service-credit": "service-credits", "liquidated-damage": "liquidated-damages", "penalty": "service-credits",
    "publicity": "publicity", "press-release": "publicity", "marketing": "publicity", "use-of-name": "publicity",
    "entire-agreement": "entire-agreement", "integration": "entire-agreement", "merger": "entire-agreement",
    "severability": "severability", "waiver": "waiver", "no-waiver": "waiver",
    "counterpart": "counterparts", "electronic-signature": "counterparts",
    "amendment": "amendments", "variation": "amendments", "modification": "amendments",
    "survival": "survival", "relationship-of-the-party": "relationship-of-the-parties",
    "independent-contractor": "relationship-of-the-parties", "relationship-of-party": "relationship-of-the-parties",
    "third-party-right": "third-party-rights", "no-third-party-beneficiary": "third-party-rights",
    "signature": "signatures", "execution": "signatures", "signature-block": "signatures",
    "recital": "recitals", "background": "recitals", "preamble": "recitals", "party": "recitals",
    "key-personnel": "personnel", "personnel": "personnel", "staffing": "personnel",
    "governance": "governance", "project-governance": "governance", "reporting": "governance",
    "expense": "expenses", "travel-and-expense": "expenses", "reimbursable-expense": "expenses",
    "tax": "taxes", "anti-bribery": "anti-bribery", "anti-corruption": "anti-bribery",
    "business-continuity": "business-continuity", "disaster-recovery": "business-continuity",
    "step-in-right": "step-in-rights", "benchmarking": "benchmarking",
    "most-favoured-nation": "most-favoured-customer", "most-favored-nation": "most-favoured-customer",
    "export-control": "export-control", "order-of-precedence": "order-of-precedence",
    "precedence": "order-of-precedence", "conflict-of-interest": "conflict-of-interest",
    "transition": "exit-and-transition", "exit": "exit-and-transition", "transition-assistance": "exit-and-transition",
    "exit-assistance": "exit-and-transition", "client-responsibility": "client-responsibilities",
    "customer-responsibility": "client-responsibilities", "customer-obligation": "client-responsibilities",
    "client-obligation": "client-responsibilities", "assumption": "assumptions", "dependency": "assumptions",
    "non-disparagement": "non-disparagement",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def known_label(category: str) -> str:
    """Human label for a known category ("AuditRights" → "Audit rights")."""
    if category in _LABELS:
        return _LABELS[category]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", category)
    return spaced[:1].upper() + spaced[1:].lower()


def _singular(word: str) -> str:
    if len(word) <= 3 or word in _ACRONYMS:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def type_key(label: str | None) -> str:
    """Normalise a free-text clause type to a stable key.

    "Non-Solicitation of Employees clause" → "non-solicitation-of-employee".
    Case, punctuation, filler words and plural endings are folded so two spellings
    of the same type collapse to one key.
    """
    if not label:
        return ""
    text = label.lower().replace("&", " and ").replace("licence", "license")
    # keep "non-x"/"sub-x"/"anti-x" joined; other punctuation becomes a space
    text = re.sub(r"\b(non|sub|anti|co|re)[\s\-]+(?=[a-z])", r"\1-", text)
    words: list[str] = []
    for chunk in re.split(r"[^a-z0-9\-]+", text):
        chunk = chunk.strip("-")
        if not chunk or chunk in _FILLER:
            continue
        if chunk.startswith(("non-", "sub-", "anti-")):
            head, _, tail = chunk.partition("-")
            words.append(f"{head}-{_singular(tail)}")
        else:
            words.extend(_singular(w) for w in chunk.split("-") if w and w not in _FILLER)
    return "-".join(words)


def _label_from_key(key: str) -> str:
    words = key.split("-")
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("non", "sub", "anti") and i + 1 < len(words):
            w = f"{w}-{words[i + 1]}"
            i += 1
        out.append(w.upper() if w in _ACRONYMS else w)
        i += 1
    text = " ".join(out)
    return text[:1].upper() + text[1:]


def normalise_type(category: str | None, specific: str | None, title: str | None = None) -> dict[str, Any]:
    """Resolve a clause's (category, specificType, specificTypeKey, typeIsCustom).

    * a known category (other than "Other") wins and is labelled;
    * "Other" + a label that is a known category under another name → promoted;
    * "Other" + a genuinely new label → kept as a normalised custom type;
    * "Other" with no usable label falls back to the clause's own title; if that
      is empty too the type is honestly unknown (``specificType`` = None).
    """
    cat = category if category in KNOWN_CATEGORIES else "Other"
    if cat != "Other":
        return {"category": cat, "specificType": known_label(cat),
                "specificTypeKey": type_key(known_label(cat)), "typeIsCustom": False}

    for candidate in (specific, title):
        key = type_key(candidate)
        if not key or key in ("other", "unknown", "n-a", "none", "na"):
            continue
        if key in _KNOWN_SYNONYMS:
            promoted = _KNOWN_SYNONYMS[key]
            return {"category": promoted, "specificType": known_label(promoted),
                    "specificTypeKey": type_key(known_label(promoted)), "typeIsCustom": False}
        if candidate is title and len(key.split("-")) > 6:
            continue                     # a sentence, not a type name
        key = _CUSTOM_SYNONYMS.get(key, key)
        return {"category": "Other", "specificType": _label_from_key(key),
                "specificTypeKey": key, "typeIsCustom": True}

    return {"category": "Other", "specificType": None, "specificTypeKey": None, "typeIsCustom": False}


def type_counts(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-document aggregate: [{key, label, category, custom, count}] by frequency."""
    agg: dict[str, dict[str, Any]] = {}
    for c in clauses or []:
        key = c.get("specificTypeKey")
        if not key:
            continue
        row = agg.setdefault(key, {
            "key": key, "label": c.get("specificType"), "category": c.get("category") or "Other",
            "custom": bool(c.get("typeIsCustom")), "count": 0,
        })
        row["count"] += 1
    return sorted(agg.values(), key=lambda r: (-r["count"], r["key"]))
