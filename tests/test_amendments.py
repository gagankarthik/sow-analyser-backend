"""Amendments — parent matching, the diff, value deltas and the timeline.

Guards the cases that used to lose or invent information: an amendment whose
parent is missing or still processing, a target written as "Section 4.2", a
short amendment with no itemised changes, a negative value delta, and an
amendment that has been analysed more than once.
"""
from __future__ import annotations

import pytest

from stages import diff, graph, timeline

DOC, PARENT = "amend-0001", "parent-0001"


def parent_clauses():
    return [
        {"number": "1", "title": "Scope", "body": "Build the website.", "category": "ScopeOfWork"},
        {"number": "4.1", "title": "Fees", "body": "The fee is $100,000.", "category": "Fees"},
        {"number": "4.2", "title": "Payment Terms", "body": "Net 30.", "category": "Payment"},
        {"number": "7", "title": "Term", "body": "One year.", "category": "Term"},
        {"number": "Schedule A 1", "title": "Milestones", "body": "M1 $50,000.", "category": "Deliverables"},
    ]


@pytest.fixture
def env(monkeypatch):
    state = {"parent_clauses": parent_clauses(), "parent_meta": {"docId": PARENT, "status": "READY"}, "saved": None}
    monkeypatch.setattr(diff, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(diff, "_load_parent_clauses", lambda b, t, p: state["parent_clauses"])
    monkeypatch.setattr(diff, "get_doc_meta", lambda _id: state["parent_meta"])
    monkeypatch.setattr(diff, "put_json", lambda b, k, d: state.__setitem__("saved", d))
    monkeypatch.setattr(diff, "chat_json", lambda **kw: {"score": 55, "rationale": "Fee change."})

    def run(classification, parent_id=PARENT):
        event = {"docId": DOC, "tenantId": "acme", "processedBucket": "p",
                 "lineage": {"parentDocId": parent_id}, "classification": classification}
        return diff.run(event)["diffs"]
    return state, run


def amendment(changes, **over):
    return {"docType": "AMENDMENT", "clauses": [{"number": "1", "title": "Amendment", "body": "x"}],
            "amendment": {"amendmentType": "amendment", "changes": changes, **over}}


# ── Matching a change to the parent clause ──────────────────────────────────


@pytest.mark.parametrize("target,number", [
    ("Section 4.2", "4.2"),
    ("Clause 4.2 (Payment Terms)", "4.2"),
    ("§ 4.1", "4.1"),
    ("4.2", "4.2"),
    ("Article 7", "7"),
    ("Section 4.2.1", "4.2"),              # a sub-item lives inside its clause
    ("Schedule A, paragraph 1", "Schedule A 1"),
    ("Payment Terms", "4.2"),              # by title when no number is given
])
def test_target_section_is_resolved_to_the_parent_clause(target, number):
    changes = diff._diff_amendment(
        [{"changeType": "modification", "category": "payment", "targetSection": target,
          "before": None, "after": "New text.", "summary": "s"}], parent_clauses())
    assert changes[0]["clauseNumber"] == number
    assert changes[0]["before"]                                    # the parent's wording was pulled in


def test_unmatched_target_is_an_addition_under_its_own_name():
    changes = diff._diff_amendment(
        [{"changeType": "addition", "category": "sla", "targetSection": "Section 12 Service Levels",
          "before": None, "after": "99.9% uptime.", "summary": "s"}], parent_clauses())
    assert changes[0]["clauseNumber"] == "Section 12 Service Levels" and changes[0]["before"] == ""


# ── What the diff stage reports ──────────────────────────────────────────────


def test_declared_changes_are_diffed_and_the_signed_delta_travels_with_them(env):
    state, run = env
    out = run(amendment([{"changeType": "modification", "category": "value", "targetSection": "Section 4.1",
                          "before": None, "after": "The fee is $87,500.", "summary": "Fee reduced."}],
                        valueDelta=-12500.0, newTotalValue=87500.0))
    assert len(out["changes"]) == 1 and out["changes"][0]["clauseNumber"] == "4.1"
    assert out["changes"][0]["before"] == "The fee is $100,000." and out["changes"][0]["impactScore"] == 55
    assert out["valueDelta"] == -12500.0 and out["newTotalValue"] == 87500.0      # negative stays negative
    assert out["parentStatus"] == "compared" and state["saved"]["valueDelta"] == -12500.0


def test_amendment_with_no_parent_says_so_and_keeps_its_delta(env):
    state, run = env
    out = run(amendment([], valueDelta=3000.0), parent_id=None)
    assert out["changes"] == [] and out["parentStatus"] == "unmatched" and out["valueDelta"] == 3000.0
    assert "No parent document was matched" in out["impactSummary"]
    # a first-version SOW is not an error and has no delta
    out = run({"docType": "SOW", "clauses": []}, parent_id=None)
    assert out["parentStatus"] == "not_applicable" and out["valueDelta"] is None
    assert out["impactSummary"] == "First version — no diff."


def test_parent_still_processing_is_distinguished_from_parent_missing(env):
    state, run = env
    state["parent_clauses"] = []
    state["parent_meta"] = {"docId": PARENT, "status": "CLASSIFYING"}
    out = run(amendment([{"changeType": "modification", "targetSection": "4.1", "after": "x", "summary": "s"}]))
    assert out["parentStatus"] == "pending" and "still being analysed" in out["impactSummary"]
    state["parent_meta"] = {"docId": PARENT, "status": "FAILED"}
    out = run(amendment([]))
    assert out["parentStatus"] == "unavailable" and "Re-analyze the parent" in out["impactSummary"]


def test_a_short_amendment_without_itemised_changes_does_not_invent_a_diff(env):
    """Its clause "1" is not the parent's clause "1"; comparing them by number
    reported changes the amendment never made."""
    state, run = env
    out = run(amendment([]))
    assert out["changes"] == []
    assert "could not be itemised" in out["impactSummary"]


def test_a_full_restatement_is_compared_clause_by_clause(env):
    state, run = env
    restated = [dict(c) for c in parent_clauses()]
    restated[2]["body"] = "Net 60."
    out = run({"docType": "AMENDMENT", "clauses": restated, "amendment": {"amendmentType": "none", "changes": []}})
    assert [(c["clauseNumber"], c["before"], c["after"]) for c in out["changes"]] == [("4.2", "Net 30.", "Net 60.")]


def test_impact_scoring_survives_a_model_failure(env, monkeypatch):
    state, run = env

    def boom(**kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(diff, "chat_json", boom)
    out = run(amendment([{"changeType": "modification", "category": "value", "targetSection": "4.1",
                          "after": "The fee is $87,500.", "summary": "Fee reduced."}]))
    change = out["changes"][0]
    assert change["impactScore"] > 0 and change["impactRationale"] == "Fee reduced."     # heuristic kept


# ── Parent matching scope ────────────────────────────────────────────────────


def test_parent_search_covers_the_workspace_and_shared_project_documents(monkeypatch):
    seen = []
    monkeypatch.setattr(graph, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(graph, "related_doc_ids", lambda doc_id: ["shared-sow"])
    monkeypatch.setattr(graph, "embed_texts", lambda texts, model=None: [[0.1]])
    monkeypatch.setattr(graph, "hybrid_search", lambda **kw: seen.append(kw) or [{"docId": "shared-sow", "score": 1.0}])
    monkeypatch.setattr(graph, "bm25_search", lambda **kw: seen.append(kw) or [
        {"_score": 5.0, "_source": {"docId": "shared-sow"}}])
    monkeypatch.setattr(graph, "get_doc_meta", lambda _id: {"title": "Website SOW"})
    linked = []
    monkeypatch.setattr(graph, "put_lineage", lambda parent_id, child_id: linked.append((parent_id, child_id)))
    event = {"docId": DOC, "tenantId": "u-uploader", "classification": {
        "docType": "AMENDMENT", "title": "Amendment 1 to Website SOW", "structuralHash": "abcdef123",
        "identification": {"parentReference": "the Website SOW dated 1 March 2026"},
        "clauses": [{"number": "1", "body": "The fee is increased.", "category": "Fees"}]}}
    out = graph.run(event)["lineage"]
    assert out["parentDocId"] == "shared-sow" and out["status"] == "linked" and linked == [("shared-sow", DOC)]
    assert all(kw["tenant_id"] == "u-uploader" and kw["doc_ids"] == ["shared-sow"] and kw["any_of"] is True
               for kw in seen)


def test_amendment_without_a_findable_parent_is_marked_unmatched(monkeypatch):
    monkeypatch.setattr(graph, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(graph, "related_doc_ids", lambda doc_id: [])
    monkeypatch.setattr(graph, "embed_texts", lambda texts, model=None: [[0.1]])
    monkeypatch.setattr(graph, "hybrid_search", lambda **kw: [])
    monkeypatch.setattr(graph, "bm25_search", lambda **kw: [])
    event = {"docId": DOC, "tenantId": "acme", "classification": {
        "docType": "AMENDMENT", "title": "Amendment", "clauses": [{"number": "1", "body": "x", "category": None}]}}
    out = graph.run(event)["lineage"]
    assert out["parentDocId"] is None and out["status"] == "unmatched"
    assert graph.run({"docId": DOC, "tenantId": "acme",
                      "classification": {"docType": "SOW"}})["lineage"]["status"] == "not_applicable"


# ── Timeline ─────────────────────────────────────────────────────────────────


def test_only_the_latest_analysis_of_an_amendment_is_replayed():
    rows = [
        {"changeId": "old", "clauseNumber": "4.1", "after": "The fee is $90,000.", "versionNumber": 1},
        {"changeId": "new", "clauseNumber": "4.1", "after": "The fee is $87,500.", "versionNumber": 2},
        {"changeId": "new2", "clauseNumber": "7", "after": "Two years.", "versionNumber": 2},
    ]
    latest = timeline.latest_version_changes(rows)
    assert [r["changeId"] for r in latest] == ["new", "new2"]
    state = timeline._state_from_clauses(parent_clauses())
    timeline._apply(state, latest)
    assert state["4.1"]["body"] == "The fee is $87,500." and state["7"]["body"] == "Two years."
    assert timeline.latest_version_changes([]) == []


def test_chain_is_ordered_deterministically_and_carries_signed_deltas(monkeypatch):
    metas = {
        "a2": {"docType": "AMENDMENT", "lifecycle": "signed", "effectiveDate": "2026-06-01", "title": "A2",
               "valueDelta": -12500, "createdAt": "2026-06-02T00:00:00Z"},
        "a1": {"docType": "AMENDMENT", "lifecycle": "signed", "effectiveDate": "2026-04-01", "title": "A1",
               "valueDelta": 3000, "createdAt": "2026-04-02T00:00:00Z"},
        "a3": {"lifecycle": None, "effectiveDate": None, "title": None, "createdAt": "2026-01-01T00:00:00Z"},
        "gone": None,
    }
    monkeypatch.setattr(timeline, "query_doc_children", lambda root: [{"childId": k} for k in metas])
    monkeypatch.setattr(timeline, "get_doc_meta", lambda did: metas[did])
    rows = timeline._gather_chain(root_id="root", current_doc_id="root", current_doc_type="SOW", event={})
    assert [r["docId"] for r in rows] == ["a1", "a2", "a3"]            # dated first, undated last, deleted skipped
    assert [r["valueDelta"] for r in rows] == [3000.0, -12500.0, None]
    # nothing is invented for a document whose record lacks a field
    assert rows[2]["docType"] is None and rows[2]["lifecycle"] is None and rows[2]["title"] is None


def test_timeline_output_includes_key_dates_and_in_force_flags(monkeypatch):
    saved = {}
    monkeypatch.setattr(timeline, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(timeline, "query_doc_children", lambda root: [])
    monkeypatch.setattr(timeline, "put_json", lambda b, k, d: saved.update(d))
    key_dates = [{"id": "kd-1", "kind": "effective", "date": "2026-03-01"}]
    event = {"docId": DOC, "tenantId": "acme", "processedBucket": "p", "lineage": {},
             "classification": {"docType": "SOW", "clauses": parent_clauses(), "keyDates": key_dates}}
    out = timeline.run(event)["timeline"]
    assert out["keyDates"] == key_dates and saved["keyDates"] == key_dates
    assert set(out) == {"initialState", "currentState", "amendmentChain", "futureState", "keyDates"}
    assert len(out["initialState"]) == 5                               # no clause lost to a number collision
