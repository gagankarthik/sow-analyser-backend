"""The seven stages run in sequence through the real Lambda handler, the way Step
Functions drives them — on an in-memory table, bucket, search index and model.

This is the test that the stages actually fit together: what one writes is what
the next reads, the record ends up READY with everything on it, and the API
serves it back to the owner (and to nobody else).
"""
from __future__ import annotations

import copy
import json
import re

import pytest

import handler as pipeline
from api import handler as api
from shared import dynamodb, opensearch, s3 as shared_s3
from shared.access import Caller
from stages import classify, diff, embed, graph, parse, persist, timeline
from test_classify_pipeline import Model, facts, validation

SUB = "11111111-1111-4111-8111-111111111111"
TENANT = f"u-{SUB}"

SOW = """STATEMENT OF WORK SOW-2026-001
This Statement of Work is made on 1 March 2026 between Acme Ltd ("Client") and Globex Inc ("Supplier").

1. SCOPE
The Supplier shall build the website described in Schedule A.

2. FEES
2.1 Fee. The total fee is USD 100,000 payable in the milestones in Schedule A.
2.2 Payment Terms. Invoices are payable Net 45 from invoice.

3. TERM
This Statement of Work runs for twelve (12) months from the Effective Date and renews automatically
unless either party gives sixty (60) days notice before expiry.

4. NON-SOLICITATION
Neither party shall solicit the other's employees for 12 months.

IN WITNESS WHEREOF the parties have signed on 2 March 2026.

SCHEDULE A - MILESTONES
1. Design
Milestone 1 Design is due 15 April 2026 with a payment of USD 40,000.
2. Build
Milestone 2 Build is due 30 June 2026 with a payment of USD 60,000.
"""

AMENDMENT = """AMENDMENT NO. 1 to Statement of Work SOW-2026-001
This Amendment is effective 1 July 2026 and is made pursuant to Statement of Work SOW-2026-001.

1. FEES
Section 2.1 is replaced: the total fee is reduced by USD 12,500 to USD 87,500.

2. GENERAL
All other terms remain in full force and effect.
"""


class Ctx:
    function_name = "pipeline"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-2:000000000000:function:pipeline"
    aws_request_id = "req-12345678"

    def get_remaining_time_in_millis(self):
        return 600_000


@pytest.fixture
def world(monkeypatch, ddb):
    raw: dict[str, bytes] = {}
    processed: dict[str, object] = {}
    index: dict[str, dict] = {}
    model = Model()

    # S3 — one raw bucket of bytes, one processed bucket of JSON
    def put_json(bucket, key, data):
        processed[key] = json.loads(json.dumps(data, default=str))

    def get_json(bucket, key):
        return copy.deepcopy(processed[key])

    for module in (pipeline, parse, classify, diff, timeline):
        if hasattr(module, "put_json"):
            monkeypatch.setattr(module, "put_json", put_json)
        if hasattr(module, "get_json"):
            monkeypatch.setattr(module, "get_json", get_json)
    monkeypatch.setattr(shared_s3, "get_json", get_json)
    monkeypatch.setattr(pipeline, "delete_object", lambda b, k: processed.pop(k, None))
    monkeypatch.setattr(parse, "get_object", lambda b, k: raw[k])
    monkeypatch.setattr(parse, "head_object", lambda b, k: {"ContentLength": len(raw[k])})

    # the model
    monkeypatch.setattr(classify, "chat_json", model)
    monkeypatch.setattr(diff, "chat_json", lambda **kw: {"score": 70, "rationale": "Fee reduced."})
    monkeypatch.setattr(classify.settings, "classify_reuse_unchanged", True)

    # embeddings + search index
    monkeypatch.setattr(embed, "embed_texts", lambda texts, model=None: [[0.1] * opensearch.VECTOR_DIM for _ in texts])
    monkeypatch.setattr(graph, "embed_texts", lambda texts, model=None: [[0.1] * opensearch.VECTOR_DIM for _ in texts])
    monkeypatch.setattr(embed, "ensure_indices", lambda: {"vectorDimension": opensearch.VECTOR_DIM, "mappingsCurrent": True})
    monkeypatch.setattr(embed, "get_cached_embeddings", lambda hashes, **k: {})
    monkeypatch.setattr(embed, "put_cached_embeddings", lambda vectors, model, **k: len(vectors))

    def index_chunks(records, structural_hash=""):
        for r in records:
            index[r["id"]] = r
        return {"indexed": [r["id"] for r in records], "failed": 0}

    def delete_stale(doc_id, run_id):
        stale = [k for k, r in index.items() if r["docId"] == doc_id and r["runId"] != run_id]
        for k in stale:
            del index[k]
        return len(stale)
    monkeypatch.setattr(embed, "index_chunks", index_chunks)
    monkeypatch.setattr(embed, "delete_stale", delete_stale)

    def search_docs(doc_types=None, exclude_doc_id=None, tenant_id=None, doc_ids=None, **_k):
        return sorted({r["docId"] for r in index.values()
                       if r["docId"] != exclude_doc_id and (not doc_types or r["docType"] in doc_types)
                       and (r["tenantId"] == tenant_id or r["docId"] in (doc_ids or []))})
    monkeypatch.setattr(graph, "hybrid_search", lambda **kw: [{"docId": d, "score": 1.0} for d in search_docs(**kw)])
    monkeypatch.setattr(graph, "bm25_search",
                        lambda **kw: [{"_score": 3.0, "_source": {"docId": d}} for d in search_docs(**kw)])

    def upload(doc_id: str, text: str, name: str = "contract.txt") -> dict:
        key = f"tenants/{TENANT}/uploads/{doc_id}/{name}"
        raw[key] = text.encode("utf-8")
        dynamodb.put_doc_meta({"docId": doc_id, "tenantId": TENANT, "ownerSub": SUB, "ownerEmail": "o@x.com",
                               "projectIds": [], "title": name.rsplit(".", 1)[0], "docType": "OTHER",
                               "lifecycle": "draft", "status": "PENDING", "rawKey": key})
        return {"rawBucket": "raw", "rawKey": key, "processedBucket": "processed", "docId": key}

    def run_pipeline(event: dict) -> dict:
        for stage in ("01_parse", "02_classify", "03_embed", "04_graph", "05_diff", "06_timeline", "07_persist"):
            event = pipeline.handler({**event, "_stage": stage}, Ctx())
            # what travels between states must stay tiny and carry no document text
            assert len(json.dumps(event)) < 2000
        return event

    monkeypatch.setenv("PROCESSED_BUCKET", "processed")
    return {"ddb": ddb, "raw": raw, "processed": processed, "index": index, "model": model,
            "upload": upload, "run": run_pipeline, "monkeypatch": monkeypatch}


def _sow_model(model: Model) -> None:
    model.facts = facts(
        docType="SOW", title="Statement of Work SOW-2026-001", parties=["Acme Ltd", "Globex Inc"],
        effectiveDate="1 March 2026", lifecycle="signed",
        identification={"sowNumber": "SOW-2026-001", "signatureStatus": "signed", "executionDate": "2 March 2026",
                        "clientName": "Acme Ltd", "vendorName": "Globex Inc"},
        timeline={"autoRenews": True, "renewalNoticeDays": 60.0, "milestones": [
            {"name": "Design", "date": "2026-04-15", "payment": 40000.0,
             "source": "Milestone 1 Design is due 15 April 2026 with a payment of USD 40,000."},
            {"name": "Build", "date": "2026-06-30", "payment": 60000.0,
             "source": "Milestone 2 Build is due 30 June 2026 with a payment of USD 60,000."}]},
        commercials={"currency": "USD", "pricingModel": "milestone", "totalContractValue": 100000.0,
                     "paymentTerms": "Net 45",
                     "valueSource": "The total fee is USD 100,000 payable in the milestones in Schedule A."},
        keyDates=[{"kind": "term_end", "label": "Initial term ends", "date": None,
                   "rawText": "twelve (12) months from the Effective Date", "anchor": "the Effective Date",
                   "offsetValue": 12.0, "offsetUnit": "months", "offsetDirection": "after",
                   "recurring": None, "amount": None, "source": "Section 3"}],
    )
    model.validation = validation(
        currency="USD", totalContractValue=100000.0, lineItems=[
            {"label": "Total fee", "amount": 100000.0,
             "source": "The total fee is USD 100,000 payable in the milestones in Schedule A."},
            {"label": "Milestone 1", "amount": 40000.0, "source": "a payment of USD 40,000"},
            {"label": "Milestone 2", "amount": 60000.0, "source": "a payment of USD 60,000"}])


def test_a_document_goes_from_upload_to_ready_with_nothing_lost(world):
    model = world["model"]
    _sow_model(model)
    by_heading = {"Preamble": ("Other", "Recitals"), "1": ("ScopeOfWork", None), "2.1": ("Fees", None),
                  "2.2": ("Payment", None), "3": ("Term", None), "4": ("Other", "Non-Solicitation"),
                  "Signatures": ("Other", "Signatures"), "Schedule A 1": ("Deliverables", None),
                  "Schedule A 2": ("Deliverables", None)}

    def labelled_call(**kw):
        """Label by clause NUMBER, read from the prompt the stage builds."""
        if kw["schema_name"] == "ClauseLabels":
            out = []
            for cid, number in re.findall(r"id: (c\d+)\nnumber: ([^\n]+)", kw["user"]):
                category, specific = by_heading[number]
                out.append({"id": cid, "title": "", "category": category, "specificType": specific,
                            "riskLevel": "medium" if category == "Payment" else "low", "summary": f"About {number}."})
            return {"clauses": out}
        return Model.__call__(model, **kw)
    world["monkeypatch"].setattr(classify, "chat_json", labelled_call)

    doc_id = "doc-sow-0001"
    final = world["run"](world["upload"](doc_id, SOW))
    # The terminal result is the detail of the "Document Analysed" event.
    assert final == {"status": "READY", "docId": doc_id, "tenantId": TENANT, "docType": "SOW", "revisionOf": None}

    meta = world["ddb"].doc(doc_id)
    cls = world["processed"][f"{TENANT}/{doc_id}/classification.json"]

    # ── every part of the document is a clause, and the model saw all of them
    numbers = [c["number"] for c in cls["clauses"]]
    assert numbers == list(by_heading)
    assert cls["extraction"]["coverageRatio"] == 1.0 and cls["extraction"]["unclassifiedCount"] == 0
    assert meta["extractionCoverage"] == 1 and meta["clauseCount"] == 9 and meta["needsReview"] is False

    # ── the record: owner untouched, analysis written, READY, version behind it
    assert meta["status"] == "READY" and meta["ownerSub"] == SUB and meta["tenantId"] == TENANT
    assert meta["GSI1PK"] == f"TENANT#{TENANT}" and meta["latestVersion"] == 1
    assert (f"DOC#{doc_id}", "V#000001") in world["ddb"].items
    assert meta["title"] == "Statement of Work SOW-2026-001" and meta["parties"] == ["Acme Ltd", "Globex Inc"]
    assert meta["contractValue"] == 100000 and meta["currency"] == "USD" and meta["reconciled"] is True

    # ── dates: normalised, derived, agreeing with the legacy fields, cited
    assert meta["effectiveDate"] == "2026-03-01" and meta["executionDate"] == "2026-03-02"
    assert meta["termEndDate"] == "2027-03-01" and meta["termEndDateDerived"] is True
    assert meta["autoRenews"] is True and meta["renewalNoticeDays"] == 60
    dates = {(k["kind"], k["label"]): k for k in cls["keyDates"]}
    assert dates[("effective", "Effective date")]["date"] == "2026-03-01"
    assert dates[("term_end", "Initial term ends")]["date"] == "2027-03-01"
    assert dates[("term_end", "Initial term ends")]["clauseNumber"] == "3"
    assert dates[("notice_deadline", "Last day to give notice of non-renewal")]["date"] == "2026-12-31"
    assert dates[("milestone", "Design")]["amount"] == 40000 and dates[("milestone", "Design")]["clauseNumber"] == "Schedule A 1"
    assert dates[("milestone", "Build")]["date"] == "2026-06-30"
    assert [k["date"] for k in meta["keyDates"]] == [k["date"] for k in cls["keyDates"]]      # same list on the record
    assert meta["keyDateCount"] == len(cls["keyDates"]) and meta["keyDatesTruncated"] is False
    timeline_json = world["processed"][f"{TENANT}/{doc_id}/timeline.json"]
    assert [k["id"] for k in timeline_json["keyDates"]] == [k["id"] for k in cls["keyDates"]]

    # ── money is tied to the clause it came from
    assert cls["commercials"]["valueSourceClause"] == "2.1"
    assert {i["label"]: i["clauseNumber"] for i in cls["validation"]["lineItems"]} == {
        "Total fee": "2.1", "Milestone 1": "Schedule A 1", "Milestone 2": "Schedule A 2"}

    # ── clause types, playbook, risk
    non_sol = next(c for c in cls["clauses"] if c["number"] == "4")
    assert (non_sol["category"], non_sol["specificType"], non_sol["typeIsCustom"]) == ("Other", "Non-solicitation", True)
    assert non_sol["playbook"]["outcome"] == "no_rule"
    pay = next(c for c in cls["clauses"] if c["number"] == "2.2")
    assert pay["playbook"]["outcome"] == "deviates" and pay["playbook"]["found"] == "Net 45"
    assert meta["playbookDeviations"] >= 1 and meta["playbookNoRuleCount"] >= 3
    assert meta["riskCounts"] == {"low": 8, "medium": 1, "high": 0, "critical": 0, "unrated": 0}
    assert meta["overallRisk"] == "medium" and meta["customClauseTypeCount"] >= 2

    # ── search: every clause indexed with its metadata
    indexed = {r["clauseNumber"]: r for r in world["index"].values()}
    assert set(indexed) == set(numbers) and meta["searchable"] is True and meta["indexedChunks"] == 9
    assert indexed["4"]["specificTypeKey"] == "non-solicitation" and indexed["2.1"]["section"] == "2. FEES"

    # ── nothing parked is left behind, and the API serves it to its owner only
    assert not any("_pipeline/" in k for k in world["processed"])
    owner = Caller.from_claims({"sub": SUB})
    doc = json.loads(api._get_document(doc_id, owner)["body"])
    assert doc["document"]["status"] == "READY" and doc["document"]["role"] == "owner"
    assert len(doc["document"]["keyDates"]) == len(cls["keyDates"])
    assert doc["versions"][0]["extractionCoverage"] == 1 and doc["versions"][0]["clauseCount"] == 9
    served = json.loads(api._get_doc_classification(doc_id, owner)["body"])
    assert len(served["clauses"]) == 9 and served["keyDates"]
    stranger = Caller.from_claims({"sub": "22222222-2222-4222-8222-222222222222"})
    assert api._get_doc_classification(doc_id, stranger)["statusCode"] == 404


def _label_everything(model: Model) -> None:
    model.label = lambda cid, text: {"category": "Fees" if "fee" in text.lower() else "Other",
                                     "specificType": None if "fee" in text.lower() else "General",
                                     "riskLevel": "low", "summary": "s", "title": "t"}


def test_amendment_links_to_its_parent_and_reports_a_negative_delta(world):
    model = world["model"]
    _sow_model(model)
    _label_everything(model)
    world["run"](world["upload"]("doc-sow-0001", SOW, "sow.txt"))

    model.facts = facts(
        docType="AMENDMENT", title="Amendment No. 1", effectiveDate="2026-07-01", lifecycle="signed",
        identification={"parentReference": "Statement of Work SOW-2026-001"},
        amendment={"amendmentType": "amendment", "number": "Amendment No. 1", "valueDelta": 12500.0,
                   "newTotalValue": 87500.0, "everythingElseStays": True,
                   "changes": [{"changeType": "replacement", "category": "value", "targetSection": "Section 2.1",
                                "before": None, "after": "The total fee is USD 87,500.", "summary": "Fee reduced by USD 12,500."}]})
    model.validation = validation(
        currency="USD", amendmentDelta=12500.0, newTotalValue=87500.0,
        amendmentDeltaSource="the total fee is reduced by USD 12,500 to USD 87,500",
        lineItems=[{"label": "Reduction", "amount": 12500.0, "source": "reduced by USD 12,500"}])
    world["run"](world["upload"]("doc-amd-0001", AMENDMENT, "amendment.txt"))

    meta = world["ddb"].doc("doc-amd-0001")
    assert meta["status"] == "READY" and meta["parentDocId"] == "doc-sow-0001" and meta["lineageStatus"] == "linked"
    assert meta["valueDelta"] == -12500                         # the model said +12,500; the wording says "reduced"
    assert meta["newTotalValue"] == 87500
    diff_json = world["processed"][f"{TENANT}/doc-amd-0001/diff.json"]
    assert diff_json["valueDelta"] == -12500.0 and diff_json["parentStatus"] == "compared"
    change = diff_json["changes"][0]
    assert change["clauseNumber"] == "2.1" and "USD 100,000" in change["before"] and "87,500" in change["after"]
    tl = world["processed"][f"{TENANT}/doc-amd-0001/timeline.json"]
    assert tl["amendmentChain"] == [{"docId": "doc-amd-0001", "docType": "AMENDMENT", "lifecycle": "signed",
                                     "effectiveDate": "2026-07-01", "title": "Amendment No. 1",
                                     "valueDelta": -12500.0, "inForce": True}]
    assert tl["currentState"]["2.1"]["body"] == "The total fee is USD 87,500."
    assert "USD 100,000" in tl["initialState"]["2.1"]["body"]
    assert len(tl["currentState"]) == len(tl["initialState"])   # every other clause survived
    kinds = {k["kind"] for k in tl["keyDates"]}
    assert "amendment_effective" in kinds
    assert (f"DOC#doc-amd-0001", "LINK#doc-sow-0001") in world["ddb"].items
    assert sum(1 for (pk, sk) in world["ddb"].items if pk == "DOC#doc-amd-0001" and sk.startswith("CHG#")) == 1


def test_reanalysing_an_unchanged_document_makes_no_model_calls_and_leaves_no_stale_search_records(world):
    model = world["model"]
    _sow_model(model)
    _label_everything(model)
    event = world["upload"]("doc-sow-0001", SOW, "sow.txt")
    world["run"](dict(event))
    calls = len(model.calls)
    first_ids = set(world["index"])
    first_runs = {r["runId"] for r in world["index"].values()}

    world["run"](dict(event))                                   # "Re-analyze" on the same file
    assert len(model.calls) == calls                            # classification reused
    meta = world["ddb"].doc("doc-sow-0001")
    assert meta["latestVersion"] == 2 and meta["status"] == "READY"
    assert set(world["index"]) == first_ids                     # same records, rewritten...
    assert {r["runId"] for r in world["index"].values()}.isdisjoint(first_runs)   # ...by the new run only


def test_a_failed_stage_marks_where_it_failed_and_says_something_useful(world):
    key = world["upload"]("doc-empty-01", "   \n  ")["rawKey"]
    with pytest.raises(Exception) as info:
        world["run"]({"rawBucket": "raw", "rawKey": key, "processedBucket": "processed", "docId": key})
    assert "No readable text was found in this file" in str(info.value)
    meta = world["ddb"].doc("doc-empty-01")
    assert meta["errorStage"] == "01_parse" and meta["errorCode"] == "user_error"
    assert meta["ownerSub"] == SUB                              # the record is otherwise intact
