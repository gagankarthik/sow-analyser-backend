"""Risk aggregation, hybrid-search normalisation, and classify defaults."""
from __future__ import annotations

from stages import persist, classify
from shared import opensearch


# ── persist risk aggregation ────────────────────────────────────────────────


def test_overall_risk_picks_highest_present():
    assert persist._overall_risk({"low": 3, "medium": 1, "high": 0, "critical": 0}) == "medium"
    assert persist._overall_risk({"low": 0, "medium": 0, "high": 0, "critical": 2}) == "critical"
    assert persist._overall_risk({"low": 0, "medium": 0, "high": 0, "critical": 0}) == "low"


def test_risk_counts_are_case_insensitive_and_default_low():
    clauses = [
        {"riskLevel": "HIGH"}, {"riskLevel": "high"}, {"riskLevel": None}, {},
        {"riskLevel": "critical"},
    ]
    counts = persist._risk_counts(clauses)
    assert counts == {"low": 2, "medium": 0, "high": 2, "critical": 1}


# ── hybrid search min-max normalisation (degenerate span) ───────────────────


class _FakeOS:
    """Minimal OpenSearch stub: returns a fixed hit list per index."""

    def __init__(self, vector_hits, text_hits):
        self._by_index = {
            opensearch.settings.clause_vector_index: vector_hits,
            opensearch.settings.clause_text_index: text_hits,
        }

    def search(self, index, body):
        return {"hits": {"hits": self._by_index.get(index, [])}}


def test_hybrid_norm_single_hit_is_not_dropped(monkeypatch):
    # One vector candidate => span 0; the fix must keep it (score > 0), where the
    # old min-max scaling collapsed it to 0.0 and silently discarded the match.
    vector_hits = [{"_score": 12.3, "_source": {"docId": "doc-A", "clauseNumber": "1"}}]
    monkeypatch.setattr(opensearch, "client", lambda: _FakeOS(vector_hits, []))

    out = opensearch.hybrid_search(
        text="anything", vector=[0.0] * 3, tenant_id="t1", k=5, alpha=0.6
    )
    assert len(out) == 1
    assert out[0]["docId"] == "doc-A"
    assert out[0]["score"] > 0.0  # NOT dropped


def test_hybrid_combines_two_channels(monkeypatch):
    vector_hits = [
        {"_score": 10.0, "_source": {"docId": "A", "clauseNumber": "1"}},
        {"_score": 5.0, "_source": {"docId": "B", "clauseNumber": "1"}},
    ]
    text_hits = [
        {"_score": 8.0, "_source": {"docId": "B", "clauseNumber": "2"}},
        {"_score": 2.0, "_source": {"docId": "A", "clauseNumber": "2"}},
    ]
    monkeypatch.setattr(opensearch, "client", lambda: _FakeOS(vector_hits, text_hits))
    out = opensearch.hybrid_search(
        text="q", vector=[0.0] * 3, tenant_id="t1", k=5, alpha=0.5
    )
    ids = {h["docId"] for h in out}
    assert ids == {"A", "B"}
    # Every doc gets a blended score in [0, 1].
    assert all(0.0 <= h["score"] <= 1.0 for h in out)


# ── classify defensive defaults ─────────────────────────────────────────────


def test_apply_defaults_fills_missing_blocks():
    result = {"docType": "SOW", "title": "X", "clauses": [{"number": "1", "title": "a", "body": "b", "category": "Fees"}]}
    classify._apply_defaults(result)
    assert result["scope"] == {"inScope": [], "outOfScope": [], "assumptions": [], "dependencies": []}
    assert result["amendment"]["amendmentType"] == "none"
    assert result["commercials"] == {}
    # Clause defaults applied.
    assert result["clauses"][0]["riskLevel"] == "low"
    assert result["clauses"][0]["summary"] == ""


def test_validate_writeback_updates_commercials(monkeypatch):
    # When the validation agent returns corrected figures, they overwrite the
    # first-pass commercials/amendment values.
    fake = {
        "currency": "USD", "totalContractValue": 12300, "baseValue": 7500,
        "amendmentDelta": None, "newTotalValue": None, "paymentTerms": "Net 30",
        "reconciled": True, "lineItems": [], "issues": [], "confidence": "high",
    }
    monkeypatch.setattr(classify, "chat_json", lambda **kw: fake)
    result = {
        "commercials": {"totalContractValue": 4800, "baseValue": None, "currency": None},
        "amendment": {"amendmentType": "none", "valueDelta": None, "newTotalValue": None},
    }
    out = classify._validate("doc text", result)
    assert out["validated"] is True
    assert out["reconciled"] is True
    assert result["commercials"]["totalContractValue"] == 12300
    assert result["commercials"]["baseValue"] == 7500
    assert result["commercials"]["currency"] == "USD"


def test_validate_failure_returns_low_confidence_stub(monkeypatch):
    def boom(**kw):
        raise RuntimeError("openai down")

    monkeypatch.setattr(classify, "chat_json", boom)
    result = {"commercials": {}, "amendment": {}}
    out = classify._validate("doc text", result)
    assert out["validated"] is False
    assert out["confidence"] == "low"
