"""Playbook deviation detection — deterministic standard-position checks.

These guard the "Check playbook — deviations surface instantly" flow step, which
did not exist server-side before. The checks must be deterministic (same input →
same deviations) and must flag, never silently pass, ambiguous clauses.
"""
from __future__ import annotations

from shared import playbook


# ── money / term parsing ─────────────────────────────────────────────────────


def test_parse_money_handles_symbols_and_thousands():
    assert playbook.parse_money("Total fee is $7,500.") == 7500.0
    assert playbook.parse_money("USD 12,300.50 due") == 12300.5
    assert playbook.parse_money("no money here") is None


def test_parse_net_days_variants():
    assert playbook.parse_net_days("Payment due Net 45 from invoice.") == 45
    assert playbook.parse_net_days("Invoices are payable within 60 days.") == 60
    assert playbook.parse_net_days("Amount is due on receipt.") == 0
    assert playbook.parse_net_days("no payment terms") is None


# ── per-category deterministic checks ────────────────────────────────────────


def test_uncapped_liability_is_material():
    status, text = playbook._check_liability(
        "The Provider's liability shall be unlimited and uncapped for all claims."
    )
    assert status == "material"
    assert "cap" in text.lower()


def test_standard_liability_cap_is_ok():
    status, _ = playbook._check_liability(
        "Aggregate liability shall not exceed the fees paid in the preceding 12 months."
    )
    assert status == "ok"


def test_liability_cap_above_1x_is_flagged():
    status, text = playbook._check_liability(
        "Total liability is limited to 3x the annual fees paid under this agreement."
    )
    assert status == "material"
    assert "3" in text


def test_payment_net_30_ok_net_60_moderate():
    assert playbook._check_payment("Payment terms are Net 30.")[0] == "ok"
    assert playbook._check_payment("Payment terms are Net 60.")[0] == "moderate"
    assert playbook._check_payment("Payment terms are Net 90.")[0] == "material"


def test_payment_missing_term_is_review_not_silent_pass():
    status, _ = playbook._check_payment("Fees are payable as agreed.")
    assert status == "review"


def test_autorenewal_long_optout_is_moderate():
    status, text = playbook._check_autorenewal(
        "This agreement will automatically renew unless either party gives 90 days notice."
    )
    assert status == "moderate"
    assert "90" in text


# ── document-level evaluation ────────────────────────────────────────────────


def _clauses():
    return [
        {"number": "1", "title": "Scope", "body": "Build the site.", "category": "ScopeOfWork"},
        {"number": "2", "title": "Payment", "body": "Invoices payable Net 75.", "category": "Payment"},
        {"number": "3", "title": "Liability", "body": "Liability is uncapped.", "category": "Liability"},
        {"number": "4", "title": "Confidentiality", "body": "Confidential for 2 years.", "category": "Confidentiality"},
    ]


def test_evaluate_clauses_surfaces_deviations_with_severity():
    out = playbook.evaluate_clauses(_clauses(), tenant_id="t1")
    assert out["checked"] == 3  # Payment, Liability, Confidentiality (ScopeOfWork has no position)
    # Two real deviations (Net 75 payment + uncapped liability); confidentiality ok.
    cats = {d["category"] for d in out["deviations"]}
    assert "Payment" in cats and "Liability" in cats
    assert out["deviationCount"] >= 2
    # Highest severity present is material (uncapped liability).
    assert out["overallSeverity"] == "material"


def test_evaluate_is_deterministic():
    a = playbook.evaluate_clauses(_clauses(), tenant_id="t1")
    b = playbook.evaluate_clauses(_clauses(), tenant_id="t1")
    assert a == b


def test_each_deviation_carries_standard_and_source():
    out = playbook.evaluate_clauses(_clauses(), tenant_id="t1")
    for d in out["deviations"]:
        assert d["standard"]                 # the firm's standard position is named
        assert d["clauseNumber"]             # which clause
        assert "sourceQuote" in d            # provenance for the UI
        assert d["status"] in ("minor", "moderate", "material", "review")


def test_unknown_category_is_skipped():
    out = playbook.evaluate_clauses(
        [{"number": "1", "title": "X", "body": "anything", "category": "Other"}], tenant_id="t1"
    )
    assert out["checked"] == 0
    assert out["deviations"] == []


def test_env_override_changes_standard_text(monkeypatch):
    monkeypatch.setenv(
        "PLAYBOOK_JSON",
        '{"Payment": {"standard": "Net 15 only", "extra": {"netDays": 15}}}',
    )
    positions = playbook.resolve_positions(tenant_id=None)
    assert positions["Payment"].standard == "Net 15 only"
    # The deterministic check function is preserved from the default.
    assert positions["Payment"].check is playbook._check_payment
