"""Guardrail / data-protection adapter for every third-party AI call.

Client requirement: confidential and proprietary contract data must not be
shared with an AI provider in a way that could train their models, and sensitive
identifiers must be redacted before any text leaves the AWS boundary.

This module is the single choke point that all AI traffic passes through. It
implements four guardrails:

  1. Provider no-train allowlist (FAIL CLOSED).
     No provider is called unless it is registered here as ``no_train=True``.
     OpenAI API data has not been used for training since 2023-03-01 (policy);
     Zero Data Retention additionally removes the ~30-day abuse-monitoring
     retention. An un-registered or training-enabled provider raises rather than
     silently leaking client data.

  2. Reversible PII pseudonymisation.
     Sensitive entities (emails, phones, SSNs, card/account numbers, IPs and —
     optionally — money/dates/named parties) are replaced with stable
     placeholders such as ``[EMAIL_1]`` *before* the text is sent, then restored
     in the model's response. The provider never sees the literal value.

     MONEY / DATE / PARTY are NOT redacted by default: the extraction stage
     exists precisely to read those figures, so pseudonymising them there would
     break the product. They are opt-in via ``REDACT_CLASSES`` and are applied
     on the RAG chat path where appropriate.

  3. Audit log.
     Every send records provider, no_train / zero_retention flags, byte count
     and per-class redaction counts — compliance evidence for a DPA / SOC 2.

  4. Output validation.
     Model responses are checked for un-restored placeholders and for raw PII
     patterns that should never appear in a grounded answer.

The module has no external dependencies beyond the standard library so it can be
imported from any Lambda without touching ``requirements.txt``.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .logger import get_logger

log = get_logger("blue-iq.guardrails")


# ---------------------------------------------------------------------------
# 1. Provider no-train allowlist (fail closed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    no_train: bool
    zero_retention: bool = False
    notes: str = ""


# Only providers listed here may receive client data. Adding one is an explicit,
# reviewable act — the absence of an entry means "refuse to send".
_PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "openai", no_train=True, zero_retention=False,
        notes="OpenAI API: inputs not used for training since 2023-03-01; "
              "retained <=30d for abuse monitoring unless ZDR is enabled.",
    ),
    "openai-zdr": Provider(
        "openai-zdr", no_train=True, zero_retention=True,
        notes="OpenAI API with Zero Data Retention — no retention, no training.",
    ),
    "bedrock": Provider(
        "bedrock", no_train=True, zero_retention=True,
        notes="AWS Bedrock — inference inside your AWS account; not used for training.",
    ),
}


class ProviderNotAllowed(RuntimeError):
    """Raised when code attempts to send data to a non-allowlisted provider."""


def register_provider(provider: Provider) -> None:
    """Register an additional allowlisted provider (e.g. for tests)."""
    _PROVIDERS[provider.name] = provider


def assert_provider_allowed(name: str) -> Provider:
    """Return the Provider for ``name`` or raise — the fail-closed gate.

    Called once at client construction so a misconfigured provider can never
    receive a single byte of client data.
    """
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise ProviderNotAllowed(
            f"AI provider {name!r} is not on the no-train allowlist; refusing to "
            f"send confidential data (fail-closed). Allowed: {sorted(_PROVIDERS)}."
        )
    if not provider.no_train:
        raise ProviderNotAllowed(
            f"AI provider {name!r} is not marked no_train; refusing to send "
            f"confidential client data."
        )
    return provider


# ---------------------------------------------------------------------------
# 2. Reversible PII pseudonymisation
# ---------------------------------------------------------------------------

# Order matters: more specific / structured patterns first so a looser numeric
# pattern (IP, PHONE, CREDIT_CARD) cannot swallow part of a SSN or email.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "PHONE": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "IP": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    # Opt-in classes — these break the extraction stage, so off by default.
    "MONEY": re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?"),
    "DATE": re.compile(
        r"\b(?:\d{4}-\d{2}-\d{2}"
        r"|\d{1,2}/\d{1,2}/\d{2,4}"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b"
    ),
}

DEFAULT_CLASSES: tuple[str, ...] = ("EMAIL", "PHONE", "SSN", "CREDIT_CARD", "IP")

# A restored/leaked placeholder always matches this shape.
_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")


@dataclass
class Redaction:
    """Result of redacting a piece of text.

    ``mapping`` is placeholder -> original, used to restore the model's reply.
    """
    text: str
    mapping: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted_any(self) -> bool:
        return bool(self.mapping)


class Redactor:
    """Reversible pseudonymiser.

    ``classes`` selects which entity patterns are active. ``extra_terms`` lets a
    caller that already knows the sensitive strings (e.g. counterparty names
    pulled from extraction) pseudonymise them by exact match — more reliable than
    guessing names with a regex.
    """

    def __init__(
        self,
        classes: Iterable[str] = DEFAULT_CLASSES,
        extra_terms: Iterable[str] | None = None,
    ) -> None:
        self.classes = tuple(c for c in classes if c in _PATTERNS)
        # Longest first so "Acme Corp International" is matched before "Acme".
        self.extra_terms = sorted(
            {t for t in (extra_terms or []) if t and t.strip()},
            key=len,
            reverse=True,
        )

    def redact(self, text: str) -> Redaction:
        if not text:
            return Redaction(text=text)

        mapping: dict[str, str] = {}
        counts: dict[str, int] = defaultdict(int)
        # original value -> placeholder, so the same value maps to the same token
        # everywhere in this request (lets the model resolve co-references).
        assigned: dict[str, str] = {}
        counters: dict[str, int] = defaultdict(int)

        def placeholder_for(cls: str, value: str) -> str:
            if value in assigned:
                return assigned[value]
            counters[cls] += 1
            token = f"[{cls}_{counters[cls]}]"
            assigned[value] = token
            mapping[token] = value
            counts[cls] += 1
            return token

        # Caller-supplied exact terms first (most authoritative).
        for term in self.extra_terms:
            if term in text:
                token = placeholder_for("PARTY", term)
                text = text.replace(term, token)

        for cls in self.classes:
            pattern = _PATTERNS[cls]

            def _sub(m: re.Match[str], _cls: str = cls) -> str:
                return placeholder_for(_cls, m.group(0))

            text = pattern.sub(_sub, text)

        return Redaction(text=text, mapping=mapping, counts=dict(counts))

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders with their original values."""
        if not text or not mapping:
            return text
        for token, original in mapping.items():
            if token in text:
                text = text.replace(token, original)
        return text


class StreamRestorer:
    """Restore placeholders in a token stream without splitting a placeholder.

    Holds back the tail of the buffer (>= the longest placeholder) so a
    placeholder straddling a chunk boundary is never emitted half-restored.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self._raw = ""
        self._sent = 0  # count of restored chars already emitted

    def _safe_cut(self, raw: str) -> int:
        """Largest prefix length safe to restore now.

        Placeholders are always ``[...]``. If the buffer ends with an unclosed
        ``[`` the token may still be forming, so we hold back from that ``[``;
        everything before it is either complete tokens or literal text and is
        safe to restore. Each restored prefix is therefore a stable prefix of the
        final result, so emitting its tail never splits a token.
        """
        if not self.mapping:
            return len(raw)
        b = raw.rfind("[")
        if b != -1 and raw.find("]", b) == -1:
            return b
        return len(raw)

    def push(self, delta: str) -> str:
        """Feed a raw delta; return the next restored, safe-to-send slice."""
        self._raw += delta
        restored = Redactor.restore(self._raw[: self._safe_cut(self._raw)], self.mapping)
        if len(restored) <= self._sent:
            return ""
        out = restored[self._sent:]
        self._sent = len(restored)
        return out

    def flush(self) -> str:
        """Return any remaining buffered text, fully restored."""
        restored = Redactor.restore(self._raw, self.mapping)
        out = restored[self._sent:]
        self._sent = len(restored)
        return out


# ---------------------------------------------------------------------------
# 3. Audit log
# ---------------------------------------------------------------------------


def audit_send(
    *,
    provider: Provider,
    op: str,
    byte_count: int,
    redaction: Redaction | None = None,
    **extra: object,
) -> None:
    """Emit a structured record of an outbound AI call (compliance evidence)."""
    log.info(
        "ai.guardrail.send",
        provider=provider.name,
        no_train=provider.no_train,
        zero_retention=provider.zero_retention,
        op=op,
        bytes=byte_count,
        redacted=(redaction.counts if redaction else {}),
        **extra,
    )


# ---------------------------------------------------------------------------
# 4. Output validation
# ---------------------------------------------------------------------------

# PII shapes that must never survive into a grounded answer.
_LEAK_PATTERNS = {k: _PATTERNS[k] for k in ("EMAIL", "SSN", "CREDIT_CARD")}


def validate_output(text: str, mapping: dict[str, str]) -> list[str]:
    """Return a list of guardrail issues found in a model response (empty = ok).

    Flags:
      - placeholders the model emitted that we cannot restore (mapping miss), and
      - raw PII patterns appearing in the answer.
    Issues are logged; the caller decides whether to surface or hard-fail.
    """
    issues: list[str] = []

    for token in set(_PLACEHOLDER_RE.findall(text)):
        if token not in mapping:
            issues.append(f"unrestored-placeholder:{token}")

    for cls, pattern in _LEAK_PATTERNS.items():
        if pattern.search(text):
            issues.append(f"raw-pii-leak:{cls}")

    if issues:
        log.warning("ai.guardrail.output_issues", issues=issues)
    return issues
