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
