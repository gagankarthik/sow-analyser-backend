"""Guardrail adapter: no-train allowlist, reversible redaction, streaming
restore, audit, and output validation.

These guardrails are the contractual promise to the client — confidential data
is pseudonymised before it leaves AWS and only no-train providers are ever
called — so they are covered directly.
"""
from __future__ import annotations

import pytest

from shared import guardrails
from shared.guardrails import (
    Provider,
    ProviderNotAllowed,
    Redactor,
    StreamRestorer,
    assert_provider_allowed,
    validate_output,
)


# ── 1. Provider no-train allowlist (fail closed) ─────────────────────────────


def test_allowlisted_provider_returns_provider():
    p = assert_provider_allowed("openai")
    assert p.no_train is True


def test_unknown_provider_fails_closed():
    with pytest.raises(ProviderNotAllowed):
        assert_provider_allowed("some-random-llm")


def test_training_provider_rejected():
    guardrails.register_provider(Provider("trains-on-you", no_train=False))
    with pytest.raises(ProviderNotAllowed):
        assert_provider_allowed("trains-on-you")


# ── 2. Reversible pseudonymisation ───────────────────────────────────────────


def test_email_redacted_and_round_trips():
    r = Redactor(classes=("EMAIL",))
    red = r.redact("Contact jane.doe@acme.com for details.")
    assert "jane.doe@acme.com" not in red.text
    assert "[EMAIL_1]" in red.text
    assert red.counts["EMAIL"] == 1
    assert Redactor.restore(red.text, red.mapping) == "Contact jane.doe@acme.com for details."


def test_repeated_value_gets_stable_placeholder():
    r = Redactor(classes=("EMAIL",))
    red = r.redact("a@x.com talked to a@x.com about b@y.com")
    # Same value -> same token; distinct value -> new token.
    assert red.text.count("[EMAIL_1]") == 2
    assert "[EMAIL_2]" in red.text
    assert red.counts["EMAIL"] == 2


def test_extra_terms_pseudonymise_party_names():
    r = Redactor(classes=(), extra_terms=["Acme Corporation"])
    red = r.redact("This SOW is between Acme Corporation and the vendor.")
    assert "Acme Corporation" not in red.text
    assert Redactor.restore(red.text, red.mapping) == \
        "This SOW is between Acme Corporation and the vendor."


def test_money_not_redacted_by_default_classes():
    # The extraction path must still see real figures.
    r = Redactor(classes=("EMAIL", "PHONE", "SSN", "CREDIT_CARD", "IP"))
    red = r.redact("Total contract value is $12,300.")
    assert "$12,300" in red.text
    assert not red.redacted_any


# ── 3. Streaming restore never splits a placeholder ──────────────────────────


def test_stream_restorer_matches_direct_restore_across_chunks():
    r = Redactor(classes=("EMAIL",))
    red = r.redact("Email jane.doe@acme.com now.")
    model_output = red.text  # model echoes the placeholder form

    restorer = StreamRestorer(red.mapping)
    out: list[str] = []
    # Feed one character at a time — worst case for boundary splitting.
    for ch in model_output:
        out.append(restorer.push(ch))
    out.append(restorer.flush())
    streamed = "".join(out)

    assert streamed == Redactor.restore(model_output, red.mapping)
    assert "jane.doe@acme.com" in streamed
    # No half-restored placeholder ever escaped.
    assert "[EMAIL" not in streamed


def test_stream_restorer_no_mapping_is_passthrough():
    restorer = StreamRestorer({})
    out = restorer.push("hello ") + restorer.push("world") + restorer.flush()
    assert out == "hello world"


# ── 4. Output validation ─────────────────────────────────────────────────────


def test_validate_flags_unrestored_placeholder():
    issues = validate_output("See [EMAIL_9] for contact.", mapping={})
    assert any("unrestored-placeholder" in i for i in issues)


def test_validate_flags_raw_pii_leak():
    issues = validate_output("SSN is 123-45-6789.", mapping={})
    assert any("raw-pii-leak:SSN" in i for i in issues)


def test_validate_clean_answer_has_no_issues():
    assert validate_output("Per [§7.2], the term is 12 months.", mapping={}) == []
