"""The document record: unknown is null, never zero / "low" / a default.

Also: the record is UPDATED (ownership and project membership are not touched),
it becomes READY last, user-edited fields survive re-analysis, and the amendment
delta keeps its sign.
"""
from __future__ import annotations

import pytest

from stages import persist

DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def run(monkeypatch):
    order: list[str] = []
    written: dict = {"fields": None, "remove": None, "version": None, "changes": []}
    state = {"meta": {"docId": DOC, "tenantId": "acme", "title": "upload-name", "ownerSub": "owner-sub",
                      "projectIds": ["proj_1"], "createdAt": "2026-01-01T00:00:00Z"},
             "versions": []}
    monkeypatch.setattr(persist, "update_status", lambda *a, **k: order.append("status"))
    monkeypatch.setattr(persist, "query_doc_versions", lambda _id: state["versions"])
    monkeypatch.setattr(persist, "get_doc_meta", lambda _id: state["meta"])
    monkeypatch.setattr(persist, "put_version", lambda v: (order.append("version"), written.__setitem__("version", v)))
    monkeypatch.setattr(persist, "put_change", lambda c: (order.append("change"), written["changes"].append(c)))

    def update(doc_id, fields, remove=()):
        order.append("meta")
        written.update(fields=fields, remove=list(remove))
    monkeypatch.setattr(persist, "update_doc_fields", update)

    def go(classification, **event):
        persist.run({"docId": DOC, "tenantId": "acme", "rawKey": "k", "classification": classification,
                     "parsed": {"checksum": "c", "extraction_method": "docx"}, **event})
        return written
    go.order, go.state = order, state
    return go


def rated(*levels):
    return [{"number": str(i), "riskLevel": level, "category": "Fees"} for i, level in enumerate(levels, 1)]


# ── Risk ─────────────────────────────────────────────────────────────────────


def test_unrated_clauses_are_counted_separately_never_as_low(run):
    fields = run({"clauses": rated("high", None, "low", None, "critical")})["fields"]
    assert fields["riskCounts"] == {"low": 1, "medium": 0, "high": 1, "critical": 1, "unrated": 2}
    assert fields["unratedClauseCount"] == 2
    assert fields["highRiskCount"] == 2 and fields["overallRisk"] == "critical"
    assert fields["clauseCount"] == 5


def test_a_document_with_no_rated_clause_has_no_overall_risk(run):
    fields = run({"clauses": rated(None, None)})["fields"]
    assert fields["overallRisk"] is None and fields["highRiskCount"] is None
    assert fields["riskCounts"] == {"low": 0, "medium": 0, "high": 0, "critical": 0, "unrated": 2}
    fields = run({"clauses": []})["fields"]
    assert fields["overallRisk"] is None and fields["highRiskCount"] is None and fields["clauseCount"] == 0


def test_all_low_is_a_real_low(run):
    fields = run({"clauses": rated("low", "low")})["fields"]
    assert fields["overallRisk"] == "low" and fields["highRiskCount"] == 0


# ── Every other aggregate ───────────────────────────────────────────────────


def test_unknown_facts_are_null_not_defaults(run):
    fields = run({"clauses": rated("low")})["fields"]
    for key in ("contractValue", "baseValue", "valueDelta", "newTotalValue", "valueCap", "currency",
                "pricingModel", "paymentTerms", "reconciled", "effectiveDate", "termEndDate", "renewalDate",
                "renewalNoticeDays", "startDate", "executionDate", "autoRenews", "parentReference",
                "findingsCount", "playbookDeviations", "playbookReviewCount", "playbookSeverity",
                "complianceCoveragePct", "complianceGaps", "extractionCoverage", "extractionConfidence",
                "lineageStatus", "pageCount", "searchable"):
        assert fields[key] is None, key
    assert fields["parties"] == [] and fields["keyDates"] == [] and fields["complianceFrameworks"] == []
    assert fields["title"] == "upload-name"            # the upload's own name, not "Untitled"
    assert fields["needsReview"] is False


def test_untitled_document_gets_an_empty_title_not_a_placeholder(run):
    run.state["meta"] = {"docId": DOC, "tenantId": "acme"}
    assert run({"clauses": []})["fields"]["title"] == ""


def test_measured_zeroes_stay_zero(run):
    fields = run({
        "clauses": rated("low"), "keyFindings": [],
        "playbook": {"checked": 4, "deviationCount": 0, "reviewCount": 0, "withinCount": 4, "noRuleCount": 2,
                     "overallSeverity": "none", "source": "default"},
        "compliance": {"evaluated": ["gdpr"], "overallCoveragePct": 0, "totalGaps": 7},
        "timeline": {"autoRenews": False, "renewalNoticeDays": 0},
    })["fields"]
    assert fields["findingsCount"] == 0 and fields["playbookDeviations"] == 0
    assert fields["playbookSeverity"] == "none" and fields["playbookNoRuleCount"] == 2
    assert fields["complianceCoveragePct"] == 0 and fields["complianceGaps"] == 7
    assert fields["autoRenews"] is False and fields["renewalNoticeDays"] == 0


def test_no_compliance_framework_means_no_coverage_figure():
    from shared.compliance import evaluate_compliance
    out = evaluate_compliance([{"category": "Fees"}], {}, enabled_ids=[])
    assert out["overallCoveragePct"] is None and out["evaluated"] == []
    out = evaluate_compliance([{"category": "Fees"}], {}, enabled_ids=["gdpr"])
    assert out["overallCoveragePct"] == 0 and out["totalGaps"] == 7


def test_amendment_reduction_keeps_its_sign_everywhere(run):
    fields = run({"clauses": [], "docType": "AMENDMENT",
                  "amendment": {"amendmentType": "amendment", "valueDelta": -12500.0, "newTotalValue": 87500.0},
                  "commercials": {"totalContractValue": 87500.0, "baseValue": 100000.0, "currency": "USD"}})["fields"]
    assert fields["valueDelta"] == -12500.0 and fields["newTotalValue"] == 87500.0
    from shared.dynamodb import _to_ddb
    from api.handler import _conv
    assert _conv(_to_ddb(fields["valueDelta"])) == -12500     # survives DynamoDB and the API as a number


def test_extraction_report_and_review_flags_are_persisted(run):
    out = run({
        "clauses": rated("low", None), "needsReview": True, "reviewReasons": ["1 clause(s) could not be analysed."],
        "confidence": {"overall": "low"},
        "extraction": {"coverageRatio": 0.994, "unclassifiedCount": 1, "segmentation": "headings", "engineVersion": "3"},
        "clauseTypes": [{"key": "fee", "label": "Fees", "category": "Fees", "custom": False, "count": 1},
                        {"key": "non-solicitation", "label": "Non-solicitation", "category": "Other",
                         "custom": True, "count": 1}],
    }, embeddings={"embeddedCount": 9, "searchable": True}, lineage={"status": "unmatched"},
        parsed={"checksum": "c", "extraction_method": "pdfplumber", "stats": {"pages": 12}})
    fields, version = out["fields"], out["version"]
    assert fields["extractionCoverage"] == 0.994 and fields["unclassifiedCount"] == 1
    assert fields["needsReview"] is True and fields["reviewReasons"] == ["1 clause(s) could not be analysed."]
    assert fields["extractionConfidence"] == "low" and fields["customClauseTypeCount"] == 1
    assert fields["clauseTypes"][1]["label"] == "Non-solicitation"
    assert fields["searchable"] is True and fields["indexedChunks"] == 9 and fields["pageCount"] == 12
    assert fields["lineageStatus"] == "unmatched"
    assert (version["extractionCoverage"], version["clauseCount"], version["unclassifiedCount"],
            version["pageCount"], version["segmentation"], version["extractionMethod"]) == (
        0.994, 2, 1, 12, "headings", "pdfplumber")


# ── How the record is written ───────────────────────────────────────────────


def test_ownership_and_project_membership_are_never_overwritten(run):
    fields = run({"clauses": []})["fields"]
    for key in ("ownerSub", "ownerEmail", "projectIds", "tenantId", "docId", "createdAt", "userEdited"):
        assert key not in fields


def test_ready_is_written_last_after_version_and_changes(run):
    run({"clauses": []}, diffs={"changes": [{"changeId": "x", "clauseNumber": "1", "after": "a"}]})
    assert run.order == ["status", "version", "change", "meta"]


def test_a_failed_version_write_never_leaves_a_ready_document(run, monkeypatch):
    def boom(_v):
        raise RuntimeError("ddb throttled")
    monkeypatch.setattr(persist, "put_version", boom)
    with pytest.raises(RuntimeError):
        run({"clauses": []})
    assert "meta" not in run.order


def test_a_previous_failure_is_cleared_on_success(run):
    assert run({"clauses": []})["remove"] == ["errorMessage", "errorCode", "errorStage"]


def test_fields_a_user_edited_survive_reanalysis(run):
    run.state["meta"].update(title="My own title", lifecycle="active", docType="MSA",
                             userEdited=["title", "lifecycle"])
    fields = run({"clauses": [], "title": "Extracted Title", "lifecycle": "draft", "docType": "SOW"})["fields"]
    assert fields["title"] == "My own title" and fields["lifecycle"] == "active"
    assert fields["docType"] == "SOW"                              # not edited → re-extracted


def test_patch_records_which_fields_were_edited(monkeypatch, ddb):
    import json
    from api import handler as api
    from shared.access import Caller
    sub = "11111111-1111-4111-8111-111111111111"
    ddb.items[(f"DOC#{DOC}", "META")] = {"PK": f"DOC#{DOC}", "SK": "META", "docId": DOC, "tenantId": f"u-{sub}",
                                         "ownerSub": sub, "title": "old", "userEdited": ["lifecycle"]}
    resp = api._update_document(DOC, Caller.from_claims({"sub": sub}), {"body": json.dumps({"title": "New"})})
    assert resp["statusCode"] == 200
    assert ddb.doc(DOC)["title"] == "New" and ddb.doc(DOC)["userEdited"] == ["lifecycle", "title"]
    assert ddb.doc(DOC)["ownerSub"] == sub                         # untouched


def test_update_can_set_and_remove_in_one_call(ddb):
    from shared import dynamodb
    ddb.items[(f"DOC#{DOC}", "META")] = {"PK": f"DOC#{DOC}", "SK": "META", "docId": DOC, "status": "FAILED",
                                         "errorMessage": "boom", "ownerSub": "o", "projectIds": ["p"]}
    dynamodb.update_doc_fields(DOC, {"status": "READY", "contractValue": -5.5}, remove=["errorMessage", "errorCode"])
    item = ddb.doc(DOC)
    assert item["status"] == "READY" and "errorMessage" not in item
    assert item["ownerSub"] == "o" and item["projectIds"] == ["p"]
    assert float(item["contractValue"]) == -5.5
