"""govern-intake (lambdas/govern_intake/handler.py): Document Analysed →
contract, idempotent under re-delivery, revision linking, partial batch
failures."""
from __future__ import annotations

import json

from govern_intake import handler as intake
from govern_support import LICENSE_V2, SRA, analysed_event, install_analysis, lambda_context, seed_doc
from shared.govern import store, workflow


def _sqs(*events):
    return {"Records": [{"messageId": f"m{i}", "body": json.dumps(e)} for i, e in enumerate(events)]}


def test_intake_creates_a_reviewed_auto_assigned_contract(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    store.config.put_settings(f"u-aaaaaaaa-0000-4000-8000-0000000000a1", {"assignmentRules": [
        {"id": "r1", "agreementType": "sponsored_research", "department": "*",
         "reviewer": {"email": "osp@northfield.edu", "name": "Olive Sponsored"}}]})
    seed_doc(ddb, "sra-1", sample=SRA, doc_type="OTHER", value=425000,
             parties=["Northfield University", "Midwest Advanced Materials Corp."])
    assert intake.handle_event(analysed_event("sra-1")) == "created"
    c = store.contracts.get("sra-1")
    assert (c["agreementType"], c["direction"], c["state"], c["stage"]) == ("sponsored_research", "incoming", "in_review", "review")
    assert c["owner"] == {"email": "osp@northfield.edu", "name": "Olive Sponsored"}
    assert (c["sponsor"], c["piName"], c["department"]) == ("Midwest Advanced Materials Corp.", "Dr. Priya Raman",
                                                            "Department of Materials Science & Engineering")
    assert c["matrix"]["version"] == 1 and c["reviewedDocVersion"] == 1
    assert gov.actions("sra-1") == ["intake", "assigned", "rescored"]
    assert gov.activity_for("sra-1")[1]["summary"] == "Sonar assigned this to Olive Sponsored (assignment rule)."
    assert store.sync.find_contract(c["tenantId"], "huron", "AGR00012402") == "sra-1"
    assert store.contracts.income("sra-1"), "sponsor funding is extracted"
    assert ddb.doc("sra-1")["lifecycle"] == "review"                     # mirrored to the documents table


def test_redelivery_and_reanalysis(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    rev = store.contracts.get("lic-1")["rev"]
    assert intake.handle_event(analysed_event("lic-1")) == "unchanged"
    assert store.contracts.get("lic-1")["rev"] == rev and gov.actions("lic-1") == ["intake", "rescored"]
    ddb.items[("DOC#lic-1", "META")]["latestVersion"] = 2
    assert intake.handle_event(analysed_event("lic-1")) == "reanalysed"
    assert gov.actions("lic-1") == ["intake", "rescored", "rescored"]


def test_events_for_missing_or_unfinished_documents_are_skipped(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "busy", status="EMBEDDING")
    assert intake.handle_event(analysed_event("gone")) == "skipped_deleted"
    assert intake.handle_event(analysed_event("busy")) == "skipped_not_ready"
    assert intake.handle_event({"detail": {}}) == "skipped_no_doc"
    assert store.contracts.get("busy") is None


def test_a_revision_is_linked_rescored_and_sent_back_to_review(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    intake.handle_event(analysed_event("lic-1"))
    workflow.perform_action("lic-1", "send_back", {"clauses": []}, {"email": "d@northfield.edu", "name": "D"})
    seed_doc(ddb, "lic-2", sample=LICENSE_V2, revisionOf="lic-1")
    assert intake.handle_event(analysed_event("lic-2")) == "revision"
    c = store.contracts.get("lic-2")
    assert c is None, "a revision never becomes its own contract"
    c = store.contracts.get("lic-1")
    assert (c["currentDocId"], c["state"], c["rounds"], c["versionDocIds"]) == ("lic-2", "intake", 1, ["lic-1", "lic-2"])
    assert gov.actions("lic-1")[-2:] == ["revision_received", "rescored"]
    assert c["versionCounts"]["lic-2"]["unacceptable"] == 0


def test_a_revision_of_an_unknown_contract_becomes_a_contract(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "orphan", sample=LICENSE_V2, revisionOf="deleted-contract")
    assert intake.handle_event(analysed_event("orphan")) == "created"


def test_batch_reports_only_failed_records(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1")
    seed_doc(ddb, "no-analysis")
    ddb.items[("DOC#no-analysis", "META")].pop("sample")
    out = intake.handler(_sqs(analysed_event("lic-1"), analysed_event("no-analysis"), {"not": "an event"}),
                         lambda_context())
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert store.contracts.get("lic-1") is not None
