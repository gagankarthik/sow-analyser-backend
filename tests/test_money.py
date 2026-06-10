"""Money validation & reconciliation — the highest-priority correctness path.

Guards that:
  - the cross-amendment rollup reconciles in CODE (not just the LLM's say-so):
    base + Σ(amendment deltas) == stated total (the canonical $7,500 + $3,000 +
    $1,800 == $12,300 example),
  - a figure that does NOT add up is flagged (reconciled=False + an issue),
  - a verbatim source quote is persisted for the headline value,
  - currency edge cases parse correctly,
  - the validation write-back still overwrites first-pass commercials.
"""
from __future__ import annotations

from stages import classify
from shared import playbook


# ── deterministic reconciliation core ────────────────────────────────────────


def test_canonical_rollup_reconciles():
    recon = classify._reconcile(
        base_value=7500, total_value=12300, new_total=None, amendment_delta=None,
        line_items=[
            {"label": "Original SOW", "amount": 7500, "source": "Original Website SOW: $7,500"},
            {"label": "Amendment #1", "amount": 3000, "source": "Amendment #1 (ATS): $3,000"},
            {"label": "Amendment #2", "amount": 1800, "source": "Amendment #2: $1,800"},
        ],
    )
    assert recon["computed"] is True
    assert recon["reconciled"] is True
    assert recon["expectedTotal"] == 12300.0
    assert recon["statedTotal"] == 12300.0


def test_rollup_that_does_not_add_up_is_flagged():
    recon = classify._reconcile(
        base_value=7500, total_value=99999, new_total=None, amendment_delta=None,
        line_items=[
            {"label": "Original SOW", "amount": 7500, "source": "x"},
            {"label": "Amendment #1", "amount": 3000, "source": "y"},
            {"label": "Amendment #2", "amount": 1800, "source": "z"},
        ],
    )
    assert recon["computed"] is True
    assert recon["reconciled"] is False
    assert "do not reconcile" in recon["explanation"]


def test_base_plus_delta_reconciliation_for_amendment():
    recon = classify._reconcile(
        base_value=7500, total_value=None, new_total=10500, amendment_delta=3000,
        line_items=[],
    )
    assert recon["computed"] is True
    assert recon["reconciled"] is True
    assert recon["expectedTotal"] == 10500.0


def test_reconcile_uncomputable_when_no_parts():
    # A single figure with no parts can't be checked arithmetically — we must NOT
    # claim it reconciles or fails.
    recon = classify._reconcile(
        base_value=None, total_value=50000, new_total=None, amendment_delta=None,
        line_items=[{"label": "TCV", "amount": 50000, "source": "Total: $50,000"}],
    )
    assert recon["computed"] is False
    assert recon["reconciled"] is None


def test_dollar_rounding_within_one_dollar_reconciles():
    recon = classify._reconcile(
        base_value=None, total_value=100.00, new_total=None, amendment_delta=None,
        line_items=[
            {"label": "a", "amount": 33.33, "source": "s"},
            {"label": "b", "amount": 33.33, "source": "s"},
            {"label": "c", "amount": 33.34, "source": "s"},
        ],
    )
    assert recon["reconciled"] is True  # 100.00 vs 100.00


# ── validation write-back + in-code reconciliation override ──────────────────


def _fake_validation(**fields):
    base = {
        "currency": "USD", "totalContractValue": None, "baseValue": None,
        "amendmentDelta": None, "newTotalValue": None, "paymentTerms": "Net 30",
        "reconciled": True, "lineItems": [], "issues": [], "confidence": "high",
    }
    base.update(fields)
    return base


def test_validate_reports_unreconciled_when_math_disagrees(monkeypatch):
    # The model claims reconciled=True but the figures don't add up. The in-code
    # check must override it to False and add an issue.
    fake = _fake_validation(
        baseValue=7500, totalContractValue=99999, reconciled=True,
        lineItems=[
            {"label": "Original", "amount": 7500, "source": "Original SOW: $7,500"},
            {"label": "Amd1", "amount": 3000, "source": "Amendment 1: $3,000"},
            {"label": "Amd2", "amount": 1800, "source": "Amendment 2: $1,800"},
        ],
    )
    monkeypatch.setattr(classify, "chat_json", lambda **kw: fake)
    result = {"commercials": {}, "amendment": {}}
    out = classify._validate("doc text", result)
    assert out["validated"] is True
    assert out["reconciled"] is False           # in-code math wins over the model
    assert out["reconciledByMath"] is False
    assert any("reconcile" in i for i in out["issues"])


def test_validate_persists_value_source(monkeypatch):
    # A line item whose amount equals the stated total carries the headline
    # source quote — that quote is persisted as commercials.valueSource so a
    # value never appears in the UI without provenance.
    fake = _fake_validation(
        baseValue=None, totalContractValue=12300, reconciled=True,
        lineItems=[
            {"label": "Total", "amount": 12300, "source": "New Total Project Cost: $12,300"},
        ],
    )
    monkeypatch.setattr(classify, "chat_json", lambda **kw: fake)
    result = {"commercials": {}, "amendment": {}}
    out = classify._validate("doc text", result)
    assert result["commercials"]["valueSource"] == "New Total Project Cost: $12,300"
    assert out["valueSource"] == "New Total Project Cost: $12,300"


def test_validate_writeback_still_updates_commercials(monkeypatch):
    # Regression: the original write-back behaviour is preserved.
    fake = _fake_validation(totalContractValue=12300, baseValue=7500)
    monkeypatch.setattr(classify, "chat_json", lambda **kw: fake)
    result = {
        "commercials": {"totalContractValue": 4800, "baseValue": None, "currency": None},
        "amendment": {"amendmentType": "none", "valueDelta": None, "newTotalValue": None},
    }
    out = classify._validate("doc text", result)
    assert result["commercials"]["totalContractValue"] == 12300
    assert result["commercials"]["baseValue"] == 7500
    assert result["commercials"]["currency"] == "USD"


# ── currency edge cases via the playbook money parser (shared) ───────────────


def test_currency_symbols_parse():
    assert playbook.parse_money("£10,000 fee") == 10000.0
    assert playbook.parse_money("€2,500.75 total") == 2500.75
    assert playbook.parse_money("US$ 1,000") == 1000.0
