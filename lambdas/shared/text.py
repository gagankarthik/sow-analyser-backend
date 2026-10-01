"""Text normalization, hashing, and chunking helpers."""
from __future__ import annotations

import hashlib
import re
from typing import Iterable

from unidecode import unidecode


_WS_RE = re.compile(r"\s+")
_CLAUSE_HEADER_RE = re.compile(
    r"^\s*(?:§|Section|Article|Clause)?\s*(\d+(?:\.\d+)*)\.?\s+(.{2,200})$",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace."""
    if not text:
        return ""
    return _WS_RE.sub(" ", unidecode(text).strip().lower())


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def structural_hash(clauses: Iterable[dict] | Iterable) -> str:
    """SHA-256 of normalised concatenated clause headers.

    Used by stage 4 to detect amendments that share a parent's structure.
    Order matters — clauses must be in document order.
    """
    parts: list[str] = []
    for c in clauses:
        if isinstance(c, dict):
            num = c.get("number", "")
            title = c.get("title", "")
        else:
            num = getattr(c, "number", "")
            title = getattr(c, "title", "")
        parts.append(f"{normalize(num)}|{normalize(title)}")
    return sha256_hex("\n".join(parts))


def detect_clause_headers(text: str) -> list[tuple[str, str, int]]:
    """Return [(number, title, line_index), ...] for plausible clause headers."""
    out: list[tuple[str, str, int]] = []
    for i, line in enumerate(text.splitlines()):
        m = _CLAUSE_HEADER_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip(), i))
    return out


def truncate_to_tokens(text: str, max_tokens: int, model: str = "gpt-4o-mini") -> str:
    """Token-accurate truncation using tiktoken when available, else char fallback.

    Keeps the head + tail of the document — the brief says "header + first/last
    pages".  We split the budget 60/40 head/tail.
    """
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        toks = enc.encode(text)
        if len(toks) <= max_tokens:
            return text
        head_n = int(max_tokens * 0.6)
        tail_n = max_tokens - head_n - 16  # leave room for separator tokens
        head = enc.decode(toks[:head_n])
        tail = enc.decode(toks[-tail_n:]) if tail_n > 0 else ""
        return f"{head}\n\n[... TRUNCATED ...]\n\n{tail}"
    except Exception:
        # Crude char fallback: assume ~4 chars/token.
        budget = max_tokens * 4
        if len(text) <= budget:
            return text
        head_n = int(budget * 0.6)
        tail_n = budget - head_n - 32
        return f"{text[:head_n]}\n\n[... TRUNCATED ...]\n\n{text[-tail_n:]}"


def chunk_text(text: str, max_chars: int = 2000, overlap: int = 200) -> list[str]:
    """Sliding-window chunking by character count.  Used for clause body splits."""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        out.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return out


def title_similarity(a: str, b: str) -> float:
    """Cheap Jaccard token-set similarity in [0, 1]."""
    a_tokens = set(normalize(a).split())
    b_tokens = set(normalize(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


# ---------------------------------------------------------------------------
# Cleaning, token estimates, coverage and retrieval chunking
# ---------------------------------------------------------------------------

_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
    "\ufb05": "st", "\ufb06": "st",
}
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def clean_text(text: str) -> str:
    """Remove artefacts that carry no content but break matching: NULs and other
    control characters, zero-width characters, soft hyphens, PDF ligature glyphs,
    and Windows/Mac line endings. Never drops visible characters."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00ad", "")
    for lig, plain in _LIGATURES.items():
        if lig in text:
            text = text.replace(lig, plain)
    return _CTRL_RE.sub("", text)


def estimate_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    """Token count via tiktoken when available, else ~4 characters per token."""
    if not text:
        return 0
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return (len(text) + 3) // 4


def word_tokens(text: str) -> list[str]:
    """Lower-cased alphanumeric words — the unit the coverage check counts."""
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def coverage_ratio(source: str, parts: Iterable[str]) -> float:
    """Share of the source document's words that appear in ``parts``.

    A multiset comparison: each source word can be claimed once per occurrence,
    so repeating a clause cannot hide a missing one. 1.0 means every word of the
    parsed document is accounted for; an empty source counts as fully covered.
    """
    from collections import Counter

    need = Counter(word_tokens(source))
    total = sum(need.values())
    if total == 0:
        return 1.0
    have = Counter()
    for part in parts:
        have.update(word_tokens(part))
    matched = sum(min(n, have.get(word, 0)) for word, n in need.items())
    return round(matched / total, 4)


def split_for_retrieval(text: str, max_chars: int = 1800, overlap: int = 200) -> list[str]:
    """Split a clause into retrieval-sized pieces without cutting a word.

    Breaks at a paragraph, sentence or word boundary nearest the limit, and
    repeats ``overlap`` characters so a fact straddling a cut stays findable.
    Every character of ``text`` appears in at least one piece.
    """
    text = text or ""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    overlap = max(0, min(overlap, max_chars // 2))
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            if cut < max_chars // 2:
                cut = max(window.rfind(". "), window.rfind("; "))
                cut = cut + 1 if cut >= 0 else cut
            if cut < max_chars // 2:
                cut = window.rfind(" ")
            if cut >= max_chars // 2:
                end = start + cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        nxt = end - overlap
        # Start the next piece on a word boundary inside the overlap.
        space = text.find(" ", nxt, end)
        start = space + 1 if space != -1 else max(nxt, start + 1)
    return out
