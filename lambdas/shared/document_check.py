"""Is this upload an agreement at all?

Govern reviews contracts and agreements: licenses, research agreements,
NDAs, SOWs, MSAs, amendments, data processing and compliance documents. A
résumé, an invoice or a research paper uploaded by mistake would otherwise go
through the whole analysis and come back as a meaningless "contract".

The check is deterministic and cheap (no model call): it weighs language
every agreement uses (parties, "shall", "hereby", governing law, signatures)
against language that marks another kind of document. It only rejects when
the other kind is clear AND agreement language is scarce, so a real contract
that mentions "education" or "invoice" is never turned away. A person can
still ask for the analysis anyway (``skipAgreementCheck`` on the document).
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Language agreements use. Each pattern counts once, however often it appears.
_AGREEMENT = [
    r"\bagreement\b", r"\bcontract\b", r"\bhereby\b", r"\bherein\b", r"\bhereto\b", r"\bwhereas\b",
    r"\bparties\b", r"\bparty\b", r"\bshall\b", r"\beffective date\b", r"\bterm of this\b",
    r"\bgoverning law\b", r"\bindemnif", r"\bterminat", r"\bconfidential", r"\blicens",
    r"\bin witness whereof\b", r"\bexecuted\b", r"\bsignature\b", r"\bauthori[sz]ed (?:signatory|representative)\b",
    r"\bstatement of work\b", r"\bsponsor\b", r"\bconsideration\b", r"\bobligations?\b", r"\bwarrant",
    r"\bliabilit", r"\bnotices?\b", r"\bamendment\b", r"\bdeliverables?\b", r"\bintellectual property\b",
]

_KINDS: dict[str, tuple[str, list[str]]] = {
    "resume": ("a résumé or CV", [
        r"\bcurriculum vitae\b", r"\bresum[eé]\b", r"\bwork experience\b", r"\bprofessional experience\b",
        r"\bemployment history\b", r"\beducation\b", r"\bskills\b", r"\bcareer (?:objective|summary)\b",
        r"\bprofessional summary\b", r"\breferences available\b", r"linkedin\.com", r"\bgpa\b",
        r"\bcertifications?\b", r"\bbachelor of\b", r"\bmaster of\b", r"\bproficient in\b", r"\bhobbies\b",
    ]),
    "invoice": ("an invoice or receipt", [
        r"\binvoice (?:no|number|#|date)\b", r"\bbill to\b", r"\bamount due\b", r"\bsubtotal\b", r"\btax invoice\b",
        r"\bpayment due\b", r"\bunit price\b", r"\bqty\b", r"\breceipt\b", r"\bbalance due\b",
    ]),
    "paper": ("a research paper or article", [
        r"\babstract\b", r"\bintroduction\b", r"\bmethods?\b", r"\bresults\b", r"\bdiscussion\b",
        r"\bdoi\b", r"\bet al\.", r"\bkeywords\b", r"\bfigure \d", r"\bjournal\b",
    ]),
}

# Filenames that say what the file is.
_NAME_HINTS = {"resume": r"(?:^|[^a-z])(?:resume|résumé|cv|curriculum)(?:[^a-z]|$)", "invoice": r"invoice|receipt"}


class Verdict(NamedTuple):
    kind: str
    label: str
    message: str


def _count(patterns: list[str], text: str) -> int:
    return sum(1 for p in patterns if re.search(p, text))


def check(text: str, filename: str = "") -> Verdict | None:
    """A Verdict when the document is clearly not an agreement, else None."""
    low = (text or "")[:40_000].lower()
    if len(low.strip()) < 200:
        return None  # too little text to judge; the empty-text check handles blanks
    agreement = _count(_AGREEMENT, low)
    name = (filename or "").lower()
    best: tuple[int, str] | None = None
    for kind, (_label, patterns) in _KINDS.items():
        score = _count(patterns, low)
        if re.search(_NAME_HINTS.get(kind, r"$^"), name):
            score += 2
        if best is None or score > best[0]:
            best = (score, kind)
    score, kind = best if best else (0, "")
    # Strong other-document signal with little agreement language.
    if score >= 4 and agreement <= 4:
        label = _KINDS[kind][0]
        return Verdict(kind, label, _message(label))
    # Almost no agreement language at all in a long document.
    if agreement <= 1 and len(low) > 1500:
        return Verdict("other", "not an agreement", _message(None))
    return None


def _message(label: str | None) -> str:
    what = f"This looks like {label}, not an agreement." if label else "This doesn't look like an agreement."
    return (f"{what} Govern reviews contracts such as licenses, research agreements, NDAs, SOWs and MSAs. "
            "Delete it and upload an agreement, or analyze it anyway if it is one.")
