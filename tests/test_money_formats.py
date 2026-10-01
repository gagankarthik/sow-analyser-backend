"""Money and clause-type normalisation — deterministic helpers."""
from __future__ import annotations

import pytest

from shared import clause_types as ct
from shared import money


@pytest.mark.parametrize("text,amount,currency", [
    ("Total fee is $7,500.", 7500.0, "USD"),
    ("USD 1.2 million", 1_200_000.0, "USD"),
    ("fees of $25k per month", 25_000.0, "USD"),
    ("$1.5M", 1_500_000.0, "USD"),
    ("US$ 10,000.00", 10_000.0, "USD"),
    ("1,000 USD", 1000.0, "USD"),
    ("€1.200.000,50", 1_200_000.5, "EUR"),
    ("EUR 12,50", 12.5, "EUR"),
    ("£10,000 fee", 10_000.0, "GBP"),
    ("Rs. 5,00,000", 500_000.0, "INR"),
    ("₹5 lakh", 500_000.0, "INR"),
    ("INR 2 crore", 20_000_000.0, "INR"),
    ("CHF 1'250.00", 1250.0, "CHF"),
    ("£1 500,00", 1500.0, "GBP"),
    ("($1,000)", -1000.0, "USD"),
    ("-$3,000", -3000.0, "USD"),
    ("$0.50 per unit", 0.5, "USD"),
])
def test_amount_formats(text, amount, currency):
    found = money.find_amounts(text)
    assert found and found[0]["amount"] == amount and found[0]["currency"] == currency


@pytest.mark.parametrize("text", ["no money here", "Net 30 days", "for all purposes 5,000", "clause 4.2"])
def test_text_without_a_currency_marker_is_not_money(text):
    assert money.find_amounts(text) == []
    assert money.parse_amount(text) is None


def test_an_ordinary_space_is_not_a_thousands_separator():
    found = money.find_amounts("pay $5,000 120 days after signature and $7,500 + $3,000 later")
    assert [f["amount"] for f in found] == [5000.0, 7500.0, 3000.0]


def test_quote_must_actually_state_the_amount():
    assert money.quote_supports(1_200_000, "USD 1.2 million") is True
    assert money.quote_supports(1.2, "USD 1.2 million") is False
    assert money.quote_supports(12_500, "Milestone 2 | 12,500") is True          # table cell, no symbol
    assert money.quote_supports(-5000, "reduced by $5,000") is True              # sign is checked elsewhere
    assert money.quote_supports(5, "payable on signature") is None               # nothing to check against
    assert money.quote_supports(None, "$5") is None


def test_wording_decides_the_sign_only_when_it_is_unambiguous():
    assert money.implied_sign("the fees are reduced by $5,000") == -1
    assert money.implied_sign("a credit of $1,200 will be applied") == -1
    assert money.implied_sign("the fees are increased by $5,000") == 1
    assert money.implied_sign("an additional $3,000") == 1
    assert money.implied_sign("increased by $5,000 less a $1,000 discount") is None
    assert money.implied_sign("the fee is $5,000") is None


def test_currency_is_only_asserted_when_the_document_is_clear():
    assert money.detect_currency("pay $5 and $6 and $7 and $8 and €1") == "USD"
    assert money.detect_currency("pay $5 and €6") is None            # genuinely mixed → say nothing
    assert money.detect_currency("no amounts at all") is None


# ── Clause types ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,key", [
    ("Non-Solicitation", "non-solicitation"),
    ("non solicitation", "non-solicitation"),
    ("Non-Solicitation Clause", "non-solicitation"),
    ("NON-SOLICITATION PROVISIONS", "non-solicitation"),
    ("Service Credits", "service-credit"),
    ("service credit", "service-credit"),
    ("Entire Agreement", "entire-agreement"),
    ("Data Protection & Privacy", "data-protection-and-privacy"),
    ("Licence Grant", "license-grant"),
    ("Warranties", "warranty"),
])
def test_type_keys_fold_case_punctuation_plurals_and_filler(label, key):
    assert ct.type_key(label) == key


@pytest.mark.parametrize("category,specific,title,expected", [
    # a known category wins and is labelled
    ("AuditRights", None, None, ("AuditRights", "Audit rights", "audit-right", False)),
    ("Liability", "Liability cap", None, ("Liability", "Limitation of liability", "limitation-of-liability", False)),
    # "Other" with a real name keeps it, normalised
    ("Other", "Non-Solicitation of Employees", None, ("Other", "Non-solicitation", "non-solicitation", True)),
    ("Other", "no-hire", None, ("Other", "Non-solicitation", "non-solicitation", True)),
    ("Other", "Publicity", None, ("Other", "Publicity", "publicity", True)),
    ("Other", "Press Releases", None, ("Other", "Publicity", "publicity", True)),
    ("Other", "Service Credits", None, ("Other", "Service credits", "service-credits", True)),
    ("Other", "Key Personnel", None, ("Other", "Personnel", "personnel", True)),
    ("Other", "SLA", None, ("Other", "Service levels", "service-levels", True)),
    # "Other" that is really a known type is promoted, so it meets the playbook
    ("Other", "Insurance", None, ("Insurance", "Insurance", "insurance", False)),
    ("Other", "Subcontracting", None, ("Subcontracting", "Subcontracting", "subcontracting", False)),
    ("Other", "Data Protection", None, ("DataProtection", "Data protection", "data-protection", False)),
    ("Other", "Warranties", None, ("Warranty", "Warranties", "warranty", False)),
    ("Other", "Assignment", None, ("Assignment", "Assignment", "assignment", False)),
    ("Other", "Audit Rights", None, ("AuditRights", "Audit rights", "audit-right", False)),
    # no label: fall back to the clause's own heading
    ("Other", None, "PUBLICITY", ("Other", "Publicity", "publicity", True)),
    ("Other", "other", "Entire Agreement", ("Other", "Entire agreement", "entire-agreement", True)),
    # an invalid category is treated as Other, not trusted
    ("Nonsense", "Exclusivity", None, ("Other", "Exclusivity", "exclusivity", True)),
])
def test_clause_type_resolution(category, specific, title, expected):
    out = ct.normalise_type(category, specific, title)
    assert (out["category"], out["specificType"], out["specificTypeKey"], out["typeIsCustom"]) == expected


def test_a_clause_with_no_usable_type_is_honestly_untyped():
    out = ct.normalise_type("Other", None, None)
    assert out == {"category": "Other", "specificType": None, "specificTypeKey": None, "typeIsCustom": False}
    long_sentence = "The parties agree that all of the following shall apply to this agreement"
    assert ct.normalise_type("Other", None, long_sentence)["specificType"] is None


def test_normalisation_is_idempotent():
    once = ct.normalise_type("Other", "Non-Solicitation of Employees", None)
    twice = ct.normalise_type(once["category"], once["specificType"], None)
    assert once == twice


def test_type_counts_group_spellings_together():
    clauses = [{"category": "Other", **ct.normalise_type("Other", s, None)}
               for s in ("Non-solicitation", "No hire", "non solicitation clause", "Publicity")]
    clauses.append({"category": "Fees", **ct.normalise_type("Fees", None, None)})
    counts = ct.type_counts(clauses)
    assert counts[0] == {"key": "non-solicitation", "label": "Non-solicitation", "category": "Other",
                         "custom": True, "count": 3}
    assert {c["key"] for c in counts} == {"non-solicitation", "publicity", "fee"}
