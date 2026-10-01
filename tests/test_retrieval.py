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
    assert run([]) == {"clauseVectorIds": [], "embeddedCount": 0, "chunkCount": 0, "clauseCount": 0,
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
