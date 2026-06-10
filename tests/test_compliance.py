"""Compliance packs — server-side framework grading.

Guards the feature that turns a tenant's enabled compliance packs (GDPR, HIPAA,
SOC 2, …) into a deterministic, persisted coverage grade on every analysis. The
grade must be deterministic (same clauses → same coverage) and must read the
enabled-pack set from defaults → env → tenant.
"""
from __future__ import annotations

import orjson
import pytest

from shared import compliance
from stages import classify


def _clauses(*categories: str, risk: str = "low") -> list[dict]:
    return [{"number": str(i), "title": c, "body": "x", "category": c, "riskLevel": risk}
            for i, c in enumerate(categories)]


# ── enabled-pack resolution ──────────────────────────────────────────────────

def test_defaults_are_gdpr_hipaa_soc2(monkeypatch):
    monkeypatch.delenv("COMPLIANCE_PACKS_JSON", raising=False)
    assert compliance.resolve_enabled_packs(None) == ["gdpr", "hipaa", "soc2"]


def test_env_override_replaces_defaults(monkeypatch):
    monkeypatch.setenv("COMPLIANCE_PACKS_JSON", orjson.dumps(["gdpr", "iso27001"]).decode())
    assert compliance.resolve_enabled_packs(None) == ["gdpr", "iso27001"]


def test_env_override_drops_unknown_ids(monkeypatch):
    monkeypatch.setenv("COMPLIANCE_PACKS_JSON", orjson.dumps(["gdpr", "nope"]).decode())
    assert compliance.resolve_enabled_packs(None) == ["gdpr"]


def test_known_pack_ids_are_the_five_frameworks():
    assert set(compliance.KNOWN_PACK_IDS) == {"gdpr", "hipaa", "soc2", "ccpa", "iso27001"}


# ── coverage grading ─────────────────────────────────────────────────────────

def test_full_coverage_is_strong_with_no_gaps():
    # GDPR requires 7 categories; provide them all, all low risk.
    cats = ["DataProcessing", "DataResidency", "SubProcessors", "BreachNotification",
            "DataRetention", "DataProtection", "AuditRights"]
    out = compliance.evaluate_compliance(_clauses(*cats), enabled_ids=["gdpr"])
    fw = out["frameworks"][0]
    assert fw["coveragePct"] == 100
    assert fw["status"] == "strong"
    assert fw["gaps"] == []
    assert out["overallCoveragePct"] == 100
    assert out["totalGaps"] == 0


def test_partial_coverage_lists_gaps():
    out = compliance.evaluate_compliance(
        _clauses("DataProcessing", "DataRetention"), enabled_ids=["gdpr"]
    )
    fw = out["frameworks"][0]
    assert 0 < fw["coveragePct"] < 100
    assert "SubProcessors" in fw["gaps"]
    assert "BreachNotification" in fw["gaps"]
    assert out["totalGaps"] == fw["required"] - fw["covered"]


def test_high_risk_clause_marks_category_weak():
    out = compliance.evaluate_compliance(
        _clauses("BreachNotification", risk="critical"), enabled_ids=["soc2"]
    )
    fw = out["frameworks"][0]
    assert "BreachNotification" in fw["weak"]


def test_playbook_deviation_marks_category_weak():
    clauses = _clauses("BreachNotification")  # low-risk clause...
    playbook = {"deviations": [{"category": "BreachNotification", "status": "material"}]}
    out = compliance.evaluate_compliance(clauses, playbook=playbook, enabled_ids=["gdpr"])
    fw = out["frameworks"][0]
    assert "BreachNotification" in fw["weak"]


def test_review_status_does_not_mark_weak():
    clauses = _clauses("BreachNotification")
    playbook = {"deviations": [{"category": "BreachNotification", "status": "review"}]}
    out = compliance.evaluate_compliance(clauses, playbook=playbook, enabled_ids=["gdpr"])
    assert out["frameworks"][0]["weak"] == []


def test_only_enabled_packs_are_evaluated():
    out = compliance.evaluate_compliance(_clauses("SecurityControls"), enabled_ids=["soc2"])
    assert [f["id"] for f in out["frameworks"]] == ["soc2"]
    assert out["evaluated"] == ["soc2"]


def test_unknown_enabled_id_is_ignored():
    out = compliance.evaluate_compliance(_clauses("SecurityControls"), enabled_ids=["soc2", "bogus"])
    assert [f["id"] for f in out["frameworks"]] == ["soc2"]


def test_deterministic_same_input_same_output():
    cats = ["DataProcessing", "BreachNotification"]
    a = compliance.evaluate_compliance(_clauses(*cats), enabled_ids=["gdpr", "hipaa"])
    b = compliance.evaluate_compliance(_clauses(*cats), enabled_ids=["gdpr", "hipaa"])
    assert a == b


# ── wiring into the classify stage ───────────────────────────────────────────

def test_classify_imports_compliance():
    assert hasattr(classify, "evaluate_compliance")


def test_classify_schema_has_renewal_fields():
    tl = classify._SCHEMA["properties"]["timeline"]["properties"]
    for field in ("endDate", "renewalDate", "autoRenews", "renewalNoticeDays"):
        assert field in tl


def test_classify_defaults_include_renewal_fields():
    result: dict = {}
    classify._apply_defaults(result)
    tl = result["timeline"]
    assert tl["renewalDate"] is None
    assert tl["autoRenews"] is False
    assert "renewalNoticeDays" in tl
