"""Search index and chat retrieval — every clause indexed, re-runs clean,
scope in the query, hybrid ranking, labelled context, cited answers.

OpenSearch, the embedding API and DynamoDB are in-memory fakes.
"""
from __future__ import annotations

import json

import pytest

from rag import handler as rag
from shared import opensearch
from shared.text import split_for_retrieval
from stages import embed

DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DIM = opensearch.VECTOR_DIM


def clause(i: int, body: str | None = None, **extra):
    return {"id": f"c{i:03d}", "number": str(i), "title": f"Heading {i}", "section": None,
            "category": "Fees", "specificType": "Fees", "specificTypeKey": "fee", "page": i,
            "body": body or f"Body of clause {i} about fee number {i}.", **extra}


@pytest.fixture
def env(monkeypatch):
    state = {"cache": {}, "embedded": [], "indexed": [], "stale": [], "cache_writes": {},
             "index_dim": DIM, "mappings": True, "vector_size": DIM, "bulk_fail_ids": set()}
    monkeypatch.setattr(embed, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(embed, "ensure_indices",
                        lambda: {"vectorDimension": state["index_dim"], "mappingsCurrent": state["mappings"]})
    monkeypatch.setattr(embed, "get_cached_embeddings",
                        lambda hashes, dimensions=None, max_workers=8: {h: state["cache"][h] for h in hashes if h in state["cache"]})
    monkeypatch.setattr(embed, "put_cached_embeddings",
                        lambda vectors, model, max_workers=8: state["cache_writes"].update(vectors) or len(vectors))

    def embed_texts(texts, model=None):
        state["embedded"].append(list(texts))
        return [[0.5] * state["vector_size"] for _ in texts]
    monkeypatch.setattr(embed, "embed_texts", embed_texts)

    def index_chunks(records, structural_hash=""):
        ok = [r for r in records if r["id"] not in state["bulk_fail_ids"]]
        state["indexed"].extend(ok)
        return {"indexed": [r["id"] for r in ok], "failed": len(records) - len(ok)}
    monkeypatch.setattr(embed, "index_chunks", index_chunks)
    monkeypatch.setattr(embed, "delete_stale", lambda doc_id, run_id: state["stale"].append((doc_id, run_id)) or 3)
    monkeypatch.setattr(embed.settings, "embedding_batch_size", 100)
    monkeypatch.setattr(embed.settings, "embed_chunk_chars", 1800)
    monkeypatch.setattr(embed.settings, "embed_chunk_overlap", 200)

    def run(clauses, doc_type="SOW"):
        event = {"docId": DOC, "tenantId": "acme",
                 "classification": {"clauses": clauses, "structuralHash": "abc", "docType": doc_type}}
        return embed.run(event)["embeddings"]
    return state, run


# ── Indexing ─────────────────────────────────────────────────────────────────


def test_every_clause_is_indexed_not_a_capped_subset(env):
    state, run = env
    out = run([clause(i) for i in range(1, 251)])
    assert out["embeddedCount"] == 250 and out["clausesIndexed"] == 250 and out["searchable"] is True
    assert {r["clauseNumber"] for r in state["indexed"]} == {str(i) for i in range(1, 251)}
    assert [len(b) for b in state["embedded"]] == [100, 100, 50]          # batched, not one call each


def test_each_record_carries_its_metadata(env):
    state, run = env
    run([clause(7, section="3. FEES › 3.1 Payment")])
    r = state["indexed"][0]
    assert r["id"] == f"{DOC}::7"                                         # stable, pre-existing id shape
    for key, value in {"docId": DOC, "tenantId": "acme", "clauseId": "c007", "clauseNumber": "7",
                       "title": "Heading 7", "section": "3. FEES › 3.1 Payment", "category": "Fees",
                       "specificType": "Fees", "specificTypeKey": "fee", "docType": "SOW",
                       "chunkIndex": 0, "chunkCount": 1, "page": 7}.items():
        assert r[key] == value
    assert r["runId"] and len(r["vector"]) == DIM
    assert not any(k.startswith("_") for k in r)                          # no internals in the index


def test_a_long_clause_is_chunked_with_overlap_and_nothing_is_cut(env):
    state, run = env
    body = " ".join(f"Sentence {i} of the long definitions clause." for i in range(400))
    out = run([clause(1, body)])
    chunks = sorted((r for r in state["indexed"]), key=lambda r: r["chunkIndex"])
    assert len(chunks) > 5 and out["chunkCount"] == len(chunks) and out["clausesIndexed"] == 1
    assert chunks[0]["id"] == f"{DOC}::1" and chunks[1]["id"] == f"{DOC}::1::c1"
    assert all(len(c["text"]) <= 1800 and c["chunkCount"] == len(chunks) for c in chunks)
    joined = " ".join(c["text"] for c in chunks)
    assert all(f"Sentence {i} of" in joined for i in range(400))          # first, last and all between
    assert chunks[0]["text"][-80:] in chunks[0]["text"] and chunks[1]["text"][:40] in chunks[0]["text"]  # overlap
    # the heading is part of what is embedded, so a question about "fees" finds a chunk that never says it
    assert all("1 Heading 1" in text for batch in state["embedded"] for text in batch)


def test_split_for_retrieval_never_cuts_a_word_and_covers_the_text():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 60
    pieces = split_for_retrieval(text, max_chars=200, overlap=40)
    words = set(text.split())
    assert all(set(p.split()) <= words for p in pieces)
    assert all(len(p) <= 200 for p in pieces)
    assert split_for_retrieval("short", 200, 40) == ["short"] and split_for_retrieval("   ", 200, 40) == []


def test_unchanged_text_is_not_embedded_again(env):
    state, run = env
    clauses = [clause(i) for i in range(1, 6)]
    run(clauses)
    state["cache"].update(state["cache_writes"])                          # what the first run cached
    state["embedded"].clear()
    out = run(clauses)                                                    # re-analysis of the same document
    assert state["embedded"] == [] and out["embeddedCount"] == 5
    clauses[2]["body"] = "This clause was genuinely changed."
    run(clauses)
    assert [len(b) for b in state["embedded"]] == [1]                     # only the changed clause


def test_identical_clauses_are_embedded_once(env):
    state, run = env
    same = "Each party shall keep the other's information confidential."
    run([clause(1, same, title="Confidentiality"), clause(2, same, title="Confidentiality")])
    assert len(state["indexed"]) == 2
    assert sum(len(b) for b in state["embedded"]) == 2        # different headings → different text
    state["embedded"].clear()
    state["indexed"].clear()
    run([dict(clause(1, same), number="1"), dict(clause(1, same), number="1")])
    assert sum(len(b) for b in state["embedded"]) == 1


def test_cache_key_depends_on_the_model(env, monkeypatch):
    state, run = env
    run([clause(1)])
    first = set(state["cache_writes"])
    state["cache_writes"].clear()
    monkeypatch.setattr(embed.settings, "embedding_model", "another-embedding-model")
    run([clause(1)])
    assert set(state["cache_writes"]) and not (set(state["cache_writes"]) & first)


def test_rerun_removes_the_previous_runs_records_only_after_writing_the_new_ones(env):
    state, run = env
    out = run([clause(1), clause(2)])
    assert state["stale"] == [(DOC, out["runId"])]
    assert all(r["runId"] == out["runId"] for r in state["indexed"])
    # If indexing failed, the old records are the only ones there are — keep them.
    state["stale"].clear()
    state["bulk_fail_ids"] = {f"{DOC}::1"}
    out = run([clause(1), clause(2)])
    assert state["stale"] == [] and out["indexFailures"] == 1 and out["searchable"] is False
    # Nor when the index cannot filter on runId yet.
    state["bulk_fail_ids"] = set()
    state["mappings"] = False
    run([clause(1)])
    assert state["stale"] == []


def test_model_and_index_dimension_mismatch_fails_before_anything_is_written(env):
    state, run = env
    state["vector_size"] = 3072                                           # a different embedding model
    with pytest.raises(opensearch.VectorDimensionMismatch, match="returned vectors of size 3072"):
        run([clause(1)])
    assert state["indexed"] == [] and state["cache_writes"] == {}
    state["vector_size"] = DIM
    state["index_dim"] = 768                                              # the live index is another size
    with pytest.raises(opensearch.VectorDimensionMismatch, match="was created for vectors of size 768"):
        run([clause(1)])
    assert state["indexed"] == []


def test_search_outage_is_recorded_not_fatal_and_not_silent(env, monkeypatch):
    state, run = env

    def down(records, structural_hash=""):
        raise ConnectionError("opensearch unreachable")
    monkeypatch.setattr(embed, "index_chunks", down)
    out = run([clause(1), clause(2)])
    assert out["embeddedCount"] == 0 and out["indexFailures"] == 2 and out["searchable"] is False


def test_embedding_outage_fails_the_stage(env, monkeypatch):
    state, run = env

    def down(texts, model=None):
        raise TimeoutError("embeddings unavailable")
    monkeypatch.setattr(embed, "embed_texts", down)
    with pytest.raises(TimeoutError):
        run([clause(1)])


def test_document_with_no_clauses(env):
    state, run = env
    out = run([])
    assert len(out.pop("runId")) == 32                      # the version row still records its run
    assert out == {"clauseVectorIds": [], "embeddedCount": 0, "chunkCount": 0, "clauseCount": 0,
                   "indexFailures": 0, "searchable": False}


# ── The OpenSearch layer itself ─────────────────────────────────────────────


class _OS:
    """Records every request; returns scripted hits per index."""

    def __init__(self, vector_hits=None, text_hits=None, fail_exact=False):
        self.vector_hits, self.text_hits, self.fail_exact = vector_hits or [], text_hits or [], fail_exact
        self.searches, self.deletes, self.mappings = [], [], []
        self.indices = self

    def search(self, index, body):
        self.searches.append((index, body))
        if self.fail_exact and "script_score" in body["query"]:
            raise RuntimeError("no knn scoring script")
        return {"hits": {"hits": self.vector_hits if index == opensearch.settings.clause_vector_index else self.text_hits}}

    def delete_by_query(self, index, body, **kw):
        self.deletes.append((index, body))
        return {"deleted": 2}

    # indices API
    def exists(self, index):
        return True

    def put_mapping(self, index, body):
        self.mappings.append((index, body))

    def get_mapping(self, index):
        return {index: {"mappings": {"properties": {"vector": {"type": "knn_vector", "dimension": 1536}}}}}


def test_document_scoped_vector_search_filters_before_it_ranks(monkeypatch):
    """The approximate index picks neighbours first and filters afterwards, so a
    doc-scoped question could come back empty when other documents are closer.
    Scoped searches score every record that passes the filter instead."""
    os_ = _OS()
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    opensearch.knn_search(vector=[0.1], tenant_id="acme", doc_id=DOC, k=8)
    body = os_.searches[0][1]
    script = body["query"]["script_score"]
    assert script["query"]["bool"]["filter"] == [{"term": {"tenantId": "acme"}}, {"term": {"docId": DOC}}]
    assert script["script"]["lang"] == "knn" and script["script"]["params"]["space_type"] == "cosinesimil"
    assert body["_source"] == {"excludes": ["vector"]}
    # tenant-wide: approximate search, still filtered in the query, over-fetched
    os_.searches.clear()
    opensearch.knn_search(vector=[0.1], tenant_id="acme", k=8)
    knn = os_.searches[0][1]["query"]["bool"]
    assert knn["filter"] == [{"term": {"tenantId": "acme"}}] and knn["must"][0]["knn"]["vector"]["k"] >= 40


def test_exact_search_falls_back_to_the_filtered_approximate_query(monkeypatch):
    os_ = _OS(vector_hits=[{"_id": "x", "_source": {"docId": DOC}}], fail_exact=True)
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    hits = opensearch.knn_search(vector=[0.1], tenant_id="acme", doc_id=DOC, k=8)
    assert hits and len(os_.searches) == 2
    assert {"term": {"docId": DOC}} in os_.searches[1][1]["query"]["bool"]["filter"]


def test_hybrid_ranking_rewards_agreement_and_keeps_keyword_only_hits(monkeypatch):
    def hit(i):
        return {"_id": f"{DOC}::{i}", "_source": {"docId": DOC, "clauseNumber": str(i)}}
    semantic = [hit(i) for i in (1, 2, 3, 4, 5, 6, 7, 8)]       # 8 semantic hits: used to fill every slot
    keyword = [hit(9), hit(3)]                                  # an exact-term match the embedding missed
    monkeypatch.setattr(opensearch, "client", lambda: _OS(semantic, keyword))
    out = opensearch.clause_search(text="clause 9", vector=[0.1], tenant_id="acme", doc_id=DOC, k=8)
    numbers = [h["_source"]["clauseNumber"] for h in out]
    assert numbers[0] == "3"                                    # found by both channels → first
    assert "9" in numbers                                       # keyword-only hit is not crowded out
    assert len(numbers) == len(set(numbers)) == 8               # de-duplicated


def test_one_search_channel_failing_still_answers(monkeypatch):
    class _Half(_OS):
        def search(self, index, body):
            if index == opensearch.settings.clause_vector_index:
                raise RuntimeError("knn plugin down")
            return {"hits": {"hits": [{"_id": "a", "_source": {"docId": DOC, "clauseNumber": "1"}}]}}
    monkeypatch.setattr(opensearch, "client", lambda: _Half())
    assert len(opensearch.clause_search(text="x", vector=[0.1], tenant_id="acme", doc_id=DOC, k=4)) == 1


def test_stale_delete_targets_only_older_runs_of_that_document(monkeypatch):
    os_ = _OS()
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    assert opensearch.delete_stale(DOC, "run-2") == 4
    for _, body in os_.deletes:
        assert body["query"]["bool"] == {"filter": [{"term": {"docId": DOC}}],
                                         "must_not": [{"term": {"runId": "run-2"}}]}


def test_existing_index_gets_the_new_metadata_fields_and_its_dimension_is_read(monkeypatch):
    os_ = _OS()
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    state = opensearch.ensure_indices()
    assert state == {"vectorDimension": 1536, "mappingsCurrent": True}
    assert len(os_.mappings) == 2
    assert {"runId", "clauseId", "specificTypeKey", "section", "chunkIndex"} <= set(os_.mappings[0][1]["properties"])
    opensearch.assert_vector_dimension(opensearch.VECTOR_DIM, 1536 if opensearch.VECTOR_DIM == 1536 else None)


def test_bulk_indexing_reports_which_records_failed(monkeypatch):
    sent = []

    def bulk(actions):
        actions = list(actions)
        sent.append(actions)
        if actions[0]["_index"] == opensearch.settings.clause_text_index:
            return len(actions) - 1, [{"index": {"_id": "b", "status": 400}}]
        return len(actions), []
    monkeypatch.setattr(opensearch, "bulk_index", bulk)
    out = opensearch.index_chunks([{"id": "a", "vector": [0.1], "docId": DOC, "text": "x"},
                                   {"id": "b", "vector": [0.2], "docId": DOC, "text": "y"}], structural_hash="h")
    assert out == {"indexed": ["a"], "failed": 1}               # "b" is only half-indexed → not searchable
    assert len(sent) == 2 and sent[0][0]["vector"] == [0.1] and "vector" not in sent[1][0]
    assert sent[1][0]["structuralHash"] == "h"


# ── Context assembly and citations ──────────────────────────────────────────


def _hit(number, text, chunk=0, **src):
    return {"_id": f"{DOC}::{number}::{chunk}", "_source": {
        "docId": DOC, "clauseNumber": number, "text": text, "chunkIndex": chunk, "category": "Fees", **src}}


def test_context_is_labelled_merged_and_ordered():
    hits = [
        _hit("7.2", "second part of the payment clause.", chunk=1, title="Payment", section="7. FEES"),
        _hit("3", "The term is one year.", title="Term", clauseId="c004", page=2),
        _hit("7.2", "First part: fees are due net 30;", chunk=0, title="Payment", section="7. FEES"),
    ]
    blocks, citations = rag._assemble(hits, multi_doc=False)
    assert len(blocks) == 2
    assert blocks[0].startswith("[§7.2] Payment — under 7. FEES\n")
    # chunks of one clause are merged in document order, not retrieval order
    assert blocks[0].index("First part") < blocks[0].index("second part")
    assert blocks[1].startswith("[§3] Term\n")
    assert citations[0] == {"clauseNumber": "7.2", "docId": DOC, "category": "Fees", "title": "Payment",
                            "clauseId": None, "specificType": None, "section": "7. FEES", "page": None,
                            "snippet": "First part: fees are due net 30;"}
    assert {"clauseNumber", "docId", "category"} <= set(citations[1])     # the shape the frontend reads
    assert citations[1]["clauseId"] == "c004" and citations[1]["page"] == 2


def test_multi_document_context_names_the_document():
    blocks, _ = rag._assemble([_hit("1", "Text.", title="Scope")], multi_doc=True, titles={DOC: "Acme MSA"})
    assert blocks[0].startswith("[§1] Scope (document: Acme MSA)")


def test_only_citations_the_answer_really_uses_are_reported():
    citations = [{"clauseNumber": "7.2"}, {"clauseNumber": "3"}, {"clauseNumber": "Schedule A 1"}]
    answer = "Fees are due net 30 [§7.2] and the schedule lists them [§ Schedule A 1]. See also [§99]."
    assert rag._used(answer, citations) == ["7.2", "Schedule A 1"]        # §99 was never in the context
    assert rag._used("The document does not state a governing law.", citations) == []


def test_chat_answer_carries_citations_and_grounding(monkeypatch, ddb):
    owner = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(rag, "get_doc_meta", lambda _id: {"docId": DOC, "tenantId": "acme", "ownerSub": owner})
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [[0.0]])
    monkeypatch.setattr(rag, "clause_search", lambda **kw: [
        _hit("7.2", "Fees are due net 30.", title="Payment"), _hit("3", "One year.", title="Term")])
    sent = {}

    def chat(**kw):
        sent.update(kw)
        return "The document does not state a governing law."
    monkeypatch.setattr(rag, "chat_text", chat)
    ev = {"pathParameters": {"docId": DOC}, "body": json.dumps({"question": "Which law governs?"}),
          "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {"sub": owner}}}}}
    out = json.loads(rag._http_handle(ev)["body"])
    assert [c["clauseNumber"] for c in out["citations"]] == ["7.2", "3"]
    assert out["usedCitations"] == [] and out["grounded"] is False        # honest "not in the document"
    assert "[§7.2] Payment" in sent["user"] and sent["temperature"] == 0.0
    assert sent["model"] == rag.settings.model_for("rag")


def test_sonar_adds_the_review_and_remembers_the_conversation(monkeypatch, ddb):
    owner = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(rag, "get_doc_meta", lambda _id: {"docId": DOC, "tenantId": "acme", "ownerSub": owner})
    searched = {}

    def embed(texts, model=None):
        searched["text"] = texts[0]
        return [[0.0]]
    monkeypatch.setattr(rag, "embed_texts", embed)
    monkeypatch.setattr(rag, "clause_search", lambda **kw: [_hit("9", "Licensee shall indemnify.", title="Indemnity")])
    monkeypatch.setattr(rag, "_contract_brief", lambda doc_id, meta: (
        "Recommended next step: Send back\n- Indemnification [§9]: needs changes\n  Suggested language: Each party's liability is capped."))
    sent = {}
    monkeypatch.setattr(rag, "chat_text", lambda **kw: sent.update(kw) or "Change §9 [§9].\nNext step: send back.")
    history = [{"role": "user", "content": "What needs to change before signing?"},
               {"role": "assistant", "content": "Indemnity needs changes [§9]."},
               {"role": "system", "content": "ignored"}]
    ev = {"pathParameters": {"docId": DOC}, "body": json.dumps({"question": "And the wording?", "history": history}),
          "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {"sub": owner}}}}}
    out = json.loads(rag._http_handle(ev)["body"])
    assert out["usedCitations"] == ["9"]
    user = sent["user"]
    assert "<review>" in user and "Suggested language: Each party's liability is capped." in user
    assert "<conversation>" in user and "User: What needs to change before signing?" in user and "ignored" not in user
    # A short follow-up searches with the previous question too.
    assert searched["text"].startswith("What needs to change before signing?")
    assert "You are Sonar" in sent["system"] and "Next step:" in sent["system"]


def test_history_is_trimmed_and_typed():
    long = [{"role": "user", "content": "x" * 5000}] * 10
    out = rag._history(long)
    assert len(out) == rag.MAX_HISTORY_TURNS and all(len(t["content"]) == rag.MAX_HISTORY_CHARS for t in out)
    assert rag._history("nope") == [] and rag._history([{"role": "tool", "content": "x"}]) == []



def test_one_bracket_may_cite_several_clauses():
    citations = [{"clauseNumber": "7.2"}, {"clauseNumber": "8.1"}, {"clauseNumber": "3"}]
    assert rag._used("Both apply [§7.2, §8.1]; see also [§3; §99].", citations) == ["7.2", "8.1", "3"]


@pytest.mark.parametrize("question,follow_up", [
    ("And the payment terms?", True),
    ("Is it capped?", True),
    ("Why?", True),
    ("What is the governing law of this agreement?", False),    # stands on its own
    ("Who owns the intellectual property created under the agreement?", False),
])
def test_only_a_follow_up_is_searched_with_the_previous_question(question, follow_up):
    history = [{"role": "user", "content": "What does the indemnity say?"}]
    assert rag._search_text(question, history).startswith("What does the indemnity say?") is follow_up
    assert rag._search_text(question, []) == question


def _chat_event(owner, question):
    return {"pathParameters": {"docId": DOC}, "body": json.dumps({"question": question}),
            "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {"sub": owner}}}}}


def test_the_review_answers_when_no_clause_matches(monkeypatch, ddb):
    owner = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(rag, "get_doc_meta", lambda _id: {"docId": DOC, "tenantId": "acme", "ownerSub": owner})
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [[0.0]])
    monkeypatch.setattr(rag, "clause_search", lambda **kw: [])
    monkeypatch.setattr(rag, "_contract_brief", lambda doc_id, meta: "Recommended next step: Assign a reviewer")
    sent = {}
    monkeypatch.setattr(rag, "chat_text", lambda **kw: sent.update(kw) or "Assign a reviewer.\nNext step: assign one.")
    out = json.loads(rag._http_handle(_chat_event(owner, "What should I do next?"))["body"])
    assert out["answer"].startswith("Assign a reviewer") and out["citations"] == []
    assert "no clause of the agreement matched" in sent["user"] and "<review>" in sent["user"]


def test_with_no_clause_and_no_review_sonar_says_so_without_a_model_call(monkeypatch, ddb):
    owner = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(rag, "get_doc_meta", lambda _id: {"docId": DOC, "tenantId": "acme", "ownerSub": owner})
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [[0.0]])
    monkeypatch.setattr(rag, "clause_search", lambda **kw: [])
    monkeypatch.setattr(rag, "_contract_brief", lambda doc_id, meta: None)
    monkeypatch.setattr(rag, "chat_text", lambda **kw: pytest.fail("no model call without any source"))
    out = json.loads(rag._http_handle(_chat_event(owner, "Which law governs?"))["body"])
    assert out["answer"] == rag._NO_HITS_ANSWER


def test_the_brief_lists_only_open_blockers(monkeypatch):
    from shared.govern import store, workflow

    monkeypatch.setattr(rag.settings, "contracts_table", "govern-contracts")
    c = {"contractId": DOC, "tenantId": "acme", "title": "MSA", "state": "in_review", "stage": "review"}
    monkeypatch.setattr(store.contracts, "get", lambda cid: c)
    monkeypatch.setattr(store.contracts, "get_review", lambda cid, did: {
        "counts": {"missing": 1}, "matrixVersion": 3,
        "clauses": [{"label": "Payment terms", "tier": "missing", "clauseNumber": None, "standard": "Net 30."}]})
    monkeypatch.setattr(store.contracts, "blockers", lambda cid: [
        {"status": "open", "text": "Cap the indemnity"}, {"status": "closed", "text": "Fix the notice address"}])
    monkeypatch.setattr(workflow, "to_api", lambda contract: {"title": "MSA", "owner": {"name": "Dana Ruiz"}})
    brief = rag._contract_brief(DOC, {})
    assert "Cap the indemnity" in brief and "Fix the notice address" not in brief
    assert "Owner: Dana Ruiz" in brief
    assert "- Payment terms: missing" in brief and "[§-]" not in brief      # no clause → no fake citation


# ── Streaming pushes reuse one AppSync connection ───────────────────────────


class _Conn:
    opened: list = []  # noqa: RUF012 — reset by the test; records every connection made

    def __init__(self, host, timeout=None):
        self.host, self.sent, self.closed = host, [], False
        self.fail_next: BaseException | None = None
        _Conn.opened.append(self)

    def request(self, method, path, body=None, headers=None):
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.sent.append((method, path, body))

    def getresponse(self):
        return type("R", (), {"read": lambda self: b"{}"})()

    def close(self):
        self.closed = True


def test_appsync_pushes_share_one_connection_and_survive_a_stale_one(monkeypatch):
    import http.client
    _Conn.opened = []
    monkeypatch.setattr(rag.http.client, "HTTPSConnection", _Conn)
    monkeypatch.setattr(rag, "_appsync_conn", None)
    url = "https://abc.appsync-api.us-east-2.amazonaws.com/graphql"
    for token in (b"a", b"b", b"c"):
        rag._post_appsync(url, token, {"Content-Type": "application/json"})
    assert len(_Conn.opened) == 1 and _Conn.opened[0].host == "abc.appsync-api.us-east-2.amazonaws.com"
    assert [s[2] for s in _Conn.opened[0].sent] == [b"a", b"b", b"c"] and _Conn.opened[0].sent[0][1] == "/graphql"
    # The server closed the idle connection: reconnect and send once more.
    _Conn.opened[0].fail_next = http.client.RemoteDisconnected("closed")
    rag._post_appsync(url, b"d", {})
    assert len(_Conn.opened) == 2 and _Conn.opened[0].closed and _Conn.opened[1].sent[0][2] == b"d"
    # Any other failure is not retried (it may have been delivered) and drops the connection.
    _Conn.opened[1].fail_next = TimeoutError("slow")
    with pytest.raises(TimeoutError):
        rag._post_appsync(url, b"e", {})
    assert _Conn.opened[1].closed and rag._appsync_conn is None


def test_boto_clients_have_a_pool_sized_for_the_stage_fan_out():
    from shared import aws
    cfg = aws._BOTO_CONFIG
    assert cfg.max_pool_connections == max(10, aws.settings.llm_max_concurrency * 2 + 4)
    assert cfg.tcp_keepalive is True


# ── Query building: synonyms, clause references, rerank ─────────────────────


def test_question_vocabulary_is_expanded_and_clause_references_are_found():
    q = opensearch.analyze_query("When do we get paid?")
    assert "invoice" in q["expansion"] and "Payment" in q["categories"]
    q = opensearch.analyze_query("Who owns the work product?")
    assert "ownership" in q["expansion"] and q["categories"][0] == "IP"
    assert opensearch.analyze_query("How long is the contract?")["categories"] == ["Term"]
    assert opensearch.analyze_query("Is there an NDA?")["categories"][0] == "Confidentiality"
    assert opensearch.analyze_query("What does section 7.2 say, and § 9?")["clause_refs"] == ["7.2", "9"]
    assert opensearch.analyze_query("Tell me a joke") == {"expansion": "", "categories": [], "clause_refs": []}


def test_chat_keyword_query_searches_headings_with_synonyms_and_named_clauses(monkeypatch):
    os_ = _OS()
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    opensearch.bm25_search(text="What does section 12 say about damages?", tenant_id="acme", doc_id=DOC, k=5,
                            expand=True)
    q = os_.searches[0][1]["query"]["bool"]
    assert q["minimum_should_match"] == 1 and q["filter"] == [{"term": {"tenantId": "acme"}}, {"term": {"docId": DOC}}]
    main, synonyms = q["should"][0]["multi_match"], q["should"][1]["multi_match"]
    assert main["fields"] == ["title^3", "section^2", "text"] and main["type"] == "best_fields"
    assert "liability" in synonyms["query"] and synonyms["boost"] < 1
    assert {"term": {"clauseNumber": {"value": "12", "boost": 10.0}}} in q["should"]
    # The amendment matcher's literal query is unchanged.
    os_.searches.clear()
    opensearch.bm25_search(text="Master Services Agreement", tenant_id="acme", k=5)
    assert os_.searches[0][1]["query"]["bool"]["must"] == [
        {"multi_match": {"query": "Master Services Agreement", "fields": ["title^2", "section", "text"]}}]


def _row(number, text, score, category="Other", chunk=0, doc=DOC):
    return {"hit": {"_id": f"{doc}::{number}::{chunk}", "_source": {
        "docId": doc, "clauseNumber": number, "text": text, "category": category, "chunkIndex": chunk}},
        "score": score}


def test_rerank_lifts_the_asked_about_category_dedupes_and_keeps_clauses_diverse():
    ranked = [_row("1", f"Definitions part {i}.", 0.030 - i * 0.001, chunk=i) for i in range(4)]
    ranked += [_row("9", "Old copy of the payment clause.", 0.020),
               _row("9b", "Old copy of the payment clause.", 0.019),       # same text, renumbered: dropped
               _row("7", "Fees are due net 30.", 0.027, category="Payment")]
    out = opensearch._rerank(ranked, ["Payment"], k=5)
    numbers = [r["hit"]["_source"]["clauseNumber"] for r in out]
    assert numbers[0] == "7"                                    # intent bonus
    assert numbers[:4] == ["7", "1", "1", "9"]                  # at most 2 chunks of one clause first
    assert "9b" not in numbers                                  # near-identical copy dropped
    assert len(out) == 5 and numbers[-1] == "1"                 # overflow chunks fill what is left


# ── Fail-soft search ─────────────────────────────────────────────────────────


class _Failing(_OS):
    def __init__(self, vector_exc=None, text_exc=None, delay=0.0, **kw):
        super().__init__(**kw)
        self.vector_exc, self.text_exc, self.delay = vector_exc, text_exc, delay

    def search(self, index, body):
        import time
        if self.delay:
            time.sleep(self.delay)
        vec = index == opensearch.settings.clause_vector_index
        exc = self.vector_exc if vec else self.text_exc
        if exc:
            raise exc
        return super().search(index, body)


class NotFoundError(Exception):
    pass


def test_no_vector_means_keyword_search_alone(monkeypatch):
    os_ = _OS(text_hits=[{"_id": "a", "_source": {"docId": DOC, "clauseNumber": "1"}}])
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    assert len(opensearch.clause_search(text="fees", vector=None, tenant_id="acme", doc_id=DOC, k=4)) == 1
    assert [i for i, _ in os_.searches] == [opensearch.settings.clause_text_index]


def test_every_channel_failing_raises_instead_of_looking_like_no_match(monkeypatch):
    monkeypatch.setattr(opensearch, "client", lambda: _Failing(RuntimeError("down"), RuntimeError("down")))
    with pytest.raises(opensearch.SearchUnavailable):
        opensearch.clause_search(text="x", vector=[0.1], tenant_id="acme", doc_id=DOC, k=4)
    missing = NotFoundError(404, "index_not_found_exception")
    monkeypatch.setattr(opensearch, "client", lambda: _Failing(missing, missing))
    with pytest.raises(opensearch.SearchIndexMissing):
        opensearch.clause_search(text="x", vector=[0.1], tenant_id="acme", doc_id=DOC, k=4)


def test_a_vector_of_the_wrong_size_falls_back_to_keywords(monkeypatch):
    hit = {"_id": "a", "_source": {"docId": DOC, "clauseNumber": "1"}}
    os_ = _Failing(vector_exc=RuntimeError("query vector has invalid dimension: 3072. Dimension should be: 1536"),
                   text_hits=[hit])
    monkeypatch.setattr(opensearch, "client", lambda: os_)
    out = opensearch.clause_search(text="fees", vector=[0.1] * 3072, tenant_id="acme", doc_id=DOC, k=4)
    assert [h["_id"] for h in out] == ["a"]


def test_a_hung_search_is_abandoned_within_the_budget(monkeypatch):
    import time
    monkeypatch.setattr(opensearch, "client", lambda: _Failing(delay=1.0))
    started = time.monotonic()
    with pytest.raises(opensearch.SearchUnavailable):
        opensearch.clause_search(text="x", vector=[0.1], tenant_id="acme", doc_id=DOC, k=4, timeout_s=0.05)
    assert time.monotonic() - started < 0.8


# ── The chat endpoint's failure paths ────────────────────────────────────────

OWNER = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def chat_env(monkeypatch, ddb):
    st = {"search": [], "chat": [], "embed": 0, "brief": None,
          "hits": [_hit("7.2", "Fees of $12,500 are due within thirty (30) days of invoice.", title="Payment")],
          "answer": "Fees are due within 30 days [§7.2]."}
    monkeypatch.setattr(rag, "get_doc_meta", lambda _id: (
        {"docId": DOC, "tenantId": "acme", "ownerSub": OWNER} if _id == DOC else None))
    monkeypatch.setattr(rag, "_contract_brief", lambda doc_id, meta: st["brief"])

    def embed_texts(texts, model=None):
        st["embed"] += 1
        if isinstance(st.get("embed_fail"), BaseException):
            raise st["embed_fail"]
        return [[0.1]]
    monkeypatch.setattr(rag, "embed_texts", embed_texts)

    def search(**kw):
        st["search"].append(kw)
        if isinstance(st.get("search_fail"), BaseException):
            raise st["search_fail"]
        return st["hits"]
    monkeypatch.setattr(rag, "clause_search", search)

    def chat(**kw):
        st["chat"].append(kw)
        if isinstance(st.get("chat_fail"), BaseException):
            raise st["chat_fail"]
        return st["answer"]
    monkeypatch.setattr(rag, "chat_text", chat)

    def ask(question="When are fees due?", **body):
        ev = _chat_event(OWNER, question)
        ev["body"] = json.dumps({"question": question, **body})
        resp = rag._http_handle(ev)
        return resp["statusCode"], json.loads(resp["body"])
    return st, ask


def test_embedding_outage_still_answers_from_keyword_search(chat_env):
    st, ask = chat_env
    st["embed_fail"] = TimeoutError("embeddings timed out")
    status, out = ask()
    assert status == 200 and out["usedCitations"] == ["7.2"]
    assert st["search"][0]["vector"] is None


def test_a_slow_embedding_is_abandoned(chat_env, monkeypatch):
    import time
    st, ask = chat_env
    monkeypatch.setattr(rag, "EMBED_TIMEOUT_S", 0.05)
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: time.sleep(0.5) or [[0.1]])
    started = time.monotonic()
    status, out = ask()
    assert status == 200 and st["search"][0]["vector"] is None and time.monotonic() - started < 0.45


def test_search_outage_is_a_clear_503_not_a_false_no_match(chat_env):
    st, ask = chat_env
    st["search_fail"] = opensearch.SearchUnavailable("down")
    status, out = ask()
    assert status == 503 and out["code"] == "search_unavailable" and "try again" in out["error"].lower()
    assert st["chat"] == []
    # ...but the Govern review can still answer on its own.
    st["brief"] = "Recommended next step: Assign a reviewer"
    st["answer"] = "Assign a reviewer.\nNext step: assign one."
    status, out = ask("What should I do next?")
    assert status == 200 and out["answer"].startswith("Assign a reviewer") and out["citations"] == []


def test_unindexed_document_says_so(chat_env):
    st, ask = chat_env
    st["search_fail"] = opensearch.SearchIndexMissing("no index")
    status, out = ask()
    assert status == 200 and out["answer"] == rag._NOT_INDEXED_ANSWER and out["grounded"] is False
    assert st["chat"] == []


def test_model_outage_or_timeout_is_a_clear_503(chat_env):
    st, ask = chat_env
    st["chat_fail"] = TimeoutError("read timeout")
    status, out = ask()
    assert status == 503 and out["code"] == "sonar_unavailable" and "try again" in out["error"].lower()
    # the chat call is bounded: an explicit timeout and the request deadline
    assert st["chat"][0]["timeout"] == rag.CHAT_TIMEOUT_S
    assert rag.CHAT_TIMEOUT_S < rag.REQUEST_BUDGET_S < 30


def test_an_empty_model_reply_is_not_shown_as_an_answer(chat_env):
    st, ask = chat_env
    st["answer"] = "   "
    status, out = ask()
    assert status == 200 and out["answer"] == rag._EMPTY_ANSWER and out["grounded"] is False


def test_hits_outside_the_scope_never_reach_the_prompt(chat_env):
    st, ask = chat_env
    st["hits"] = [_hit("7.2", "Ours.", title="Payment"),
                  {"_id": "x", "_source": {"docId": "someone-elses-doc", "clauseNumber": "1", "text": "Theirs."}}]
    status, out = ask()
    assert [c["docId"] for c in out["citations"]] == [DOC]
    assert "Theirs." not in st["chat"][0]["user"]


def test_a_deleted_project_document_is_not_cited(chat_env, monkeypatch):
    st, ask = chat_env
    monkeypatch.setattr(rag, "_scope_for", lambda target, caller: {"doc_ids": [DOC, "deleted-doc"]})
    monkeypatch.setattr(rag.ddb, "get_docs", lambda ids: [{"docId": DOC, "title": "MSA"}])
    st["hits"] = [{"_id": "d", "_source": {"docId": "deleted-doc", "clauseNumber": "3", "text": "Gone."}},
                  _hit("7.2", "Kept.", title="Payment")]
    status, out = ask()
    assert [c["docId"] for c in out["citations"]] == [DOC]


@pytest.mark.parametrize("body,status", [({"question": "   "}, 400), ({"question": "x" * 2001}, 400),
                                         ({"question": "fees?", "history": "not a list"}, 200)])
def test_empty_huge_and_malformed_input(chat_env, body, status):
    st, ask = chat_env
    ev = _chat_event(OWNER, "x")
    ev["body"] = json.dumps(body)
    assert rag._http_handle(ev)["statusCode"] == status


# ── Answer checks ────────────────────────────────────────────────────────────


def test_invented_citations_are_removed_from_the_answer(chat_env):
    st, ask = chat_env
    st["answer"] = "Fees are due within 30 days [§7.2, §99]. See also [§42]."
    status, out = ask()
    assert out["answer"] == "Fees are due within 30 days [§7.2]. See also."
    assert out["usedCitations"] == ["7.2"] and out["grounded"] is True


def test_review_clause_numbers_are_kept_in_the_text():
    citations = [{"clauseNumber": "3"}]
    text, removed = rag._clean_citations("Fix the indemnity [§9] and the term [§3].", citations, {"9"})
    assert text == "Fix the indemnity [§9] and the term [§3]." and removed == []


def test_figures_the_cited_clause_does_not_contain_are_flagged(chat_env):
    st, ask = chat_env
    st["answer"] = "Fees of $12,500 are due within 45 days, from March 3, 2031 [§7.2]."
    status, out = ask()
    assert out["unverified"] == ["March 3, 2031", "45 days"]
    st["answer"] = "Fees of $12,500 are due within thirty (30) days [§7.2]."
    assert ask()[1]["unverified"] == []


@pytest.mark.parametrize("answer,source,flagged", [
    ("Capped at $1.5 million [§12].", "liability capped at $1,500,000", []),
    ("Notice of 60 days [§6].", "on sixty days' notice", []),
    ("Interest of 1.5% per month.", "one and a half percent (1.5%) per month", []),
    ("It ends on 2025-12-31.", "continues until December 31, 2025", []),
    ("It ends on 31 December 2026.", "continues until December 31, 2025", ["31 December 2026"]),
    ("See Section 14.3 and Appendix 12; ISO 27001 applies.", "nothing numeric here", []),
    ("There are 2 findings.", "", []),
    ("A fee of €9,000 applies.", "a fee of €8,000", ["€9,000"]),
    ("Pay within 90 days.", "within ninety (90) days", []),
])
def test_unverified_figure_check(answer, source, flagged):
    assert rag._unverified(answer, [source]) == flagged


def test_context_is_held_to_a_token_budget(monkeypatch):
    monkeypatch.setattr(rag, "MAX_CONTEXT_TOKENS", 1000)
    blocks = ["x" * 2400] * 8                              # ~600 tokens each
    citations = [{"clauseNumber": str(i)} for i in range(8)]
    b, c, _ = rag._fit_context(blocks, citations, blocks, top_k=8)
    assert len(b) == len(c) == 1
    monkeypatch.setattr(rag, "MAX_CONTEXT_TOKENS", 10)
    assert len(rag._fit_context(blocks, citations, blocks, top_k=8)[0]) == 1    # never empty


def test_a_follow_up_also_searches_the_clauses_the_last_answer_cited():
    history = [{"role": "user", "content": "What does the limitation of liability clause say?"},
               {"role": "assistant", "content": "It is capped at 12 months of fees [§12; §12.2]."}]
    text = rag._search_text("Are there exceptions to it?", history)
    assert text.startswith("What does the limitation") and text.endswith("§12 §12.2")
    assert opensearch.analyze_query(text)["clause_refs"] == ["12", "12.2"]


# ── AppSync streaming failure paths ─────────────────────────────────────────


@pytest.fixture
def stream_env(monkeypatch):
    from shared.access import Caller
    pushed = []
    monkeypatch.setattr(rag, "_push_token", lambda sid, token, final: pushed.append((token, final)))
    monkeypatch.setattr(Caller, "visible_doc_ids", lambda self: [DOC])
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [[0.0]])
    monkeypatch.setattr(rag, "clause_search", lambda **kw: [_hit("4", "Fee $5.", title="Fees")])
    monkeypatch.setattr(rag.ddb, "get_docs", lambda ids: [{"docId": DOC, "title": "SOW"}])
    ev = {"arguments": {"input": {"question": "fees?"}}, "identity": {"claims": {"sub": OWNER}}}
    return pushed, ev


def _chunk(text, finish=None):
    delta = type("D", (), {"content": text})()
    return type("C", (), {"choices": [type("Ch", (), {"delta": delta, "finish_reason": finish})()]})()


def test_stream_that_fails_before_any_token_ends_with_a_clear_message(stream_env, monkeypatch):
    pushed, ev = stream_env

    def broken(*a, **k):
        raise TimeoutError("no stream")
        yield  # pragma: no cover
    monkeypatch.setattr(rag, "chat_stream", broken)
    out = rag._handle(ev)
    assert out["answer"] == rag._MODEL_DOWN_ERROR and pushed[-1] == (rag._MODEL_DOWN_ERROR, True)


def test_stream_interrupted_midway_says_so_and_still_finishes(stream_env, monkeypatch):
    pushed, ev = stream_env

    def flaky(*a, **k):
        yield _chunk("The fee is $5 ")
        raise ConnectionError("reset")
    monkeypatch.setattr(rag, "chat_stream", flaky)
    out = rag._handle(ev)
    assert out["answer"].startswith("The fee is $5") and out["answer"].endswith(rag._INTERRUPTED_NOTE)
    assert pushed[-1] == ("", True)


def test_stream_search_outage_and_missing_index(stream_env, monkeypatch):
    pushed, ev = stream_env

    def down(**kw):
        raise opensearch.SearchUnavailable("down")
    monkeypatch.setattr(rag, "clause_search", down)
    assert rag._handle(ev)["answer"] == rag._SEARCH_DOWN_ERROR and pushed[-1][1] is True

    def missing(**kw):
        raise opensearch.SearchIndexMissing("none")
    monkeypatch.setattr(rag, "clause_search", missing)
    assert rag._handle(ev)["answer"] == rag._NOT_INDEXED_ANSWER
