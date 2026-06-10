"""Licensing & compliance document support.

Guards the extension that lets the engine analyse the client's licensing
agreements and compliance documents (DPA / BAA / SOC 2 / VPAT) the same way it
analyses SOWs: new document types, new clause categories, and deterministic
playbook checks for breach-notification, data-retention, and audit-rights.
"""
from __future__ import annotations

from stages import classify
from shared import playbook
from api import handler as api_handler


# ── Document types & clause categories are registered everywhere ─────────────

def test_new_doc_types_in_classify_schema():
    enum = classify._SCHEMA["properties"]["docType"]["enum"]
    for t in ("LICENSE", "DPA", "BAA", "COMPLIANCE"):
        assert t in enum


def test_new_doc_types_accepted_by_api():
    for t in ("LICENSE", "DPA", "BAA", "COMPLIANCE"):
        assert t in api_handler._VALID_DOC_TYPES


def test_new_clause_categories_present():
    cats = set(classify._CLAUSE_CATEGORIES)
    licensing = {"LicenseGrant", "LicenseScope", "Restrictions", "Royalties",
                 "Sublicensing", "SourceCodeEscrow", "AuditRights", "OpenSource"}
    compliance = {"DataProcessing", "DataResidency", "SubProcessors",
                  "BreachNotification", "DataRetention", "SecurityControls", "Accessibility"}
    assert licensing <= cats
    assert compliance <= cats
    # Schema enum and the category list must stay in lock-step.
    schema_enum = set()
    for prop in classify._SCHEMA["properties"]["clauses"]["items"]["properties"].values():
        if isinstance(prop, dict) and prop.get("enum") and "Liability" in prop["enum"]:
            schema_enum = set(prop["enum"])
    assert licensing <= schema_enum and compliance <= schema_enum


# ── Breach-notification window (GDPR 72h) ────────────────────────────────────

def test_breach_notification_within_72h_ok():
    status, _ = playbook._check_breach_notification(
        "Processor shall notify Controller of a personal data breach within 48 hours."
    )
    assert status == "ok"


def test_breach_notification_over_72h_flagged():
    status, text = playbook._check_breach_notification(
        "We will notify you of any security incident within 10 days of discovery."
    )
    assert status == "moderate"
    assert "72" in text


def test_breach_notification_missing_window_is_review():
    status, _ = playbook._check_breach_notification(
        "The parties will cooperate regarding any breach of this Agreement."
    )
    assert status == "review"


# ── Data retention / deletion ────────────────────────────────────────────────

def test_data_retention_with_deletion_ok():
    status, _ = playbook._check_data_retention(
        "On termination, Processor shall delete or return all personal data."
    )
    assert status == "ok"


def test_data_retention_without_deletion_is_review():
    status, _ = playbook._check_data_retention(
        "Processor will process the data to provide the services."
    )
    assert status == "review"


# ── Audit rights ─────────────────────────────────────────────────────────────

def test_audit_without_notice_is_review():
    status, _ = playbook._check_audit_rights(
        "Licensor may audit Licensee's use of the Software."
    )
    assert status == "review"


def test_audit_without_notice_at_any_time_is_moderate():
    status, _ = playbook._check_audit_rights(
        "Licensor may inspect Licensee's records at any time without notice."
    )
    assert status == "moderate"


def test_non_audit_clause_is_ok():
    status, _ = playbook._check_audit_rights("Fees are payable Net 30.")
    assert status == "ok"


# ── Positions are wired into the resolved playbook ───────────────────────────

def test_new_positions_registered():
    positions = playbook.resolve_positions()
    for cat in ("BreachNotification", "DataRetention", "AuditRights"):
        assert cat in positions


def test_evaluate_flags_a_compliance_clause():
    result = playbook.evaluate_clauses([
        {"number": "5.1", "title": "Breach notice", "category": "BreachNotification",
         "body": "Notify of a breach within 30 days."},
    ])
    assert result["checked"] == 1
    assert result["deviationCount"] == 1
    assert result["deviations"][0]["category"] == "BreachNotification"


# ── Licensing positions ──────────────────────────────────────────────────────

def test_exclusive_license_flagged():
    status, text = playbook._check_license_grant(
        "Licensor grants an exclusive licence to use the Software."
    )
    assert status == "moderate"
    assert "exclusive" in text.lower()


def test_non_exclusive_license_ok():
    status, _ = playbook._check_license_grant(
        "Licensor grants a non-exclusive, non-revocable licence to use the Software for the Term."
    )
    assert status == "ok"


def test_royalty_escalation_over_cap_flagged():
    status, _ = playbook._check_royalties("Licence fees increase by 12% each year.")
    assert status == "moderate"


def test_sublicensing_silence_is_review():
    status, _ = playbook._check_sublicensing("Licensee shall pay the fees set out in Schedule A.")
    assert status == "review"


def test_copyleft_without_carveout_flagged():
    status, _ = playbook._check_open_source(
        "The Software includes components licensed under the GPL."
    )
    assert status == "moderate"


def test_subprocessors_without_notice_is_minor():
    status, _ = playbook._check_subprocessors(
        "Processor may engage sub-processors to deliver the services."
    )
    assert status == "minor"


def test_licensing_positions_registered():
    positions = playbook.resolve_positions()
    for cat in ("LicenseGrant", "LicenseScope", "Restrictions", "Royalties",
                "Sublicensing", "OpenSource", "DataResidency", "SubProcessors"):
        assert cat in positions
