"""Stage 03 — Embed: make every clause searchable.

Each clause is cut into retrieval chunks (a short clause is one chunk; a long
one is split at paragraph / sentence boundaries with an overlap, so nothing is
truncated by the embedding model's input limit and a long clause can still be
found by its last paragraph). Every chunk of EVERY clause is embedded and
written to both OpenSearch indices with its metadata (document, tenant, clause
id / number / title, section heading, type, page, chunk position).

Speed and cost
--------------
* cache lookups and writes run in bounded parallel instead of one at a time;
* cache misses are embedded in batches, the batches in bounded parallel;
* identical text is embedded once (per run and, through the cache, across runs
  and documents) — the cache key includes the model and the vector size, so a
  model change can never be served stale vectors;
* indexing is two bulk requests instead of two requests per clause.

Re-runs are idempotent: records are written under deterministic ids, and once
the new records are in, the document's records from earlier runs are deleted
(``delete_stale``) so renumbered or removed clauses do not linger in search.

A vector-size mismatch between the embedding model, EMBEDDING_DIMENSIONS and the
live index is fatal and reported clearly. Other OpenSearch failures are
non-fatal: the document is still analysed, and the result records how many
chunks were indexed so an unsearchable document is visible, not silent.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from shared import openai_client
from shared.concurrency import bounded_map
from shared.config import settings
from shared.dynamodb import get_cached_embeddings, put_cached_embeddings, update_status
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import (
    VECTOR_DIM, _vector_doc_id, assert_vector_dimension, delete_stale, ensure_indices, index_chunks,
)
from shared.text import sha256_hex, split_for_retrieval

log = get_logger("blue-iq.embed")

# Hard ceiling on one embedding input (characters). A chunk is ~1,800 characters;
# this only guards against a mis-set chunk size exceeding the model's input limit.
_MAX_EMBED_CHARS = 24_000


def build_chunks(
    clauses: list[dict[str, Any]], *, doc_id: str, tenant_id: str, doc_type: str, run_id: str,
) -> list[dict[str, Any]]:
    """One record per retrieval chunk, for every clause that has text."""
    records: list[dict[str, Any]] = []
    for i, clause in enumerate(clauses):
        body = (clause.get("body") or "").strip()
        if not body:
            continue
        number = str(clause.get("number") or f"U{i + 1}")
        pieces = split_for_retrieval(body, settings.embed_chunk_chars, settings.embed_chunk_overlap) or [body]
        for j, piece in enumerate(pieces):
            heading = " ".join(x for x in (number, clause.get("title") or "") if x)
            context = "\n".join(x for x in (clause.get("section") or "", heading) if x)
            records.append({
                "id": _vector_doc_id(doc_id, number, j),
                "docId": doc_id,
                "tenantId": tenant_id,
                "clauseId": clause.get("id"),
                "clauseNumber": number,
                "title": clause.get("title") or "",
                "section": clause.get("section") or "",
                "category": clause.get("category") or "Other",
                "specificType": clause.get("specificType"),
                "specificTypeKey": clause.get("specificTypeKey"),
                "docType": doc_type,
                "text": piece,
                "chunkIndex": j,
                "chunkCount": len(pieces),
                "page": clause.get("page"),
                "runId": run_id,
                # what is embedded: the chunk WITH its heading context, so "what is
                # the notice period?" matches a chunk whose heading says Termination
                "_embed": f"{context}\n{piece}"[:_MAX_EMBED_CHARS],
            })
    return records


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id         = event["docId"]
    tenant_id      = event["tenantId"]
    classification = event.get("classification") or {}
    clauses: list[dict[str, Any]] = classification.get("clauses") or []
    structural     = classification.get("structuralHash", "")
    doc_type       = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "EMBEDDING")
    started = time.monotonic()
    openai_client.usage_snapshot(reset=True)
    log.info("embed.start", clauses=len(clauses))

    if not clauses:
        event["embeddings"] = {"clauseVectorIds": [], "embeddedCount": 0, "chunkCount": 0,
                               "clauseCount": 0, "indexFailures": 0, "searchable": False}
        return event

    run_id = uuid.uuid4().hex
    records = build_chunks(clauses, doc_id=doc_id, tenant_id=tenant_id, doc_type=doc_type, run_id=run_id)

    # Best-effort index creation / mapping update; don't fail the pipeline if
    # OpenSearch is slow. The live index's vector size is checked below.
    index_state: dict[str, Any] = {"vectorDimension": None, "mappingsCurrent": False}
    try:
        index_state = ensure_indices() or index_state
    except Exception as exc:
        log.warning("embed.ensure_indices_failed", error_type=type(exc).__name__)

    # ── Resolve embedding cache hits vs. misses ───────────────────────────────
    model = settings.embedding_model
    for r in records:
        r["_hash"] = sha256_hex(f"{model}|{VECTOR_DIM}|{r['_embed']}")
    try:
        vectors = get_cached_embeddings([r["_hash"] for r in records], dimensions=VECTOR_DIM,
                                        max_workers=settings.llm_max_concurrency * 2)
    except Exception as exc:
        log.warning("embed.cache_read_failed", error_type=type(exc).__name__)
        vectors = {}
    cache_hits = sum(1 for r in records if r["_hash"] in vectors)

    # ── Batch-embed the misses (identical text once), batches in parallel ─────
    missing: dict[str, str] = {}
    for r in records:
        if r["_hash"] not in vectors:
            missing.setdefault(r["_hash"], r["_embed"])
    miss_hashes = list(missing)
    size = max(1, settings.embedding_batch_size)
    batches = [miss_hashes[i:i + size] for i in range(0, len(miss_hashes), size)]
    outcomes = bounded_map(
        lambda batch: embed_texts([missing[h] for h in batch], model=model),
        batches, settings.llm_max_concurrency,
    )
    fresh: dict[str, list[float]] = {}
    for batch, (vecs, error) in zip(batches, outcomes):
        if error is not None:
            raise error            # an embedding outage fails the stage, visibly
        for h, vec in zip(batch, vecs or []):
            fresh[h] = vec
    vectors.update(fresh)

    # Fail fast — before any write — if model / configured size / index disagree.
    sample = next(iter(fresh.values()), None) or next(iter(vectors.values()), None)
    if sample is not None:
        assert_vector_dimension(len(sample), index_state.get("vectorDimension"))

    if fresh:
        try:
            put_cached_embeddings(fresh, model, max_workers=settings.llm_max_concurrency * 2)
        except Exception as exc:
            log.warning("embed.cache_write_failed", error_type=type(exc).__name__)

    # ── Index into OpenSearch (two bulk requests; failures are non-fatal) ─────
    to_index = []
    for r in records:
        vec = vectors.get(r["_hash"])
        if vec is None:
            continue
        to_index.append({**{k: v for k, v in r.items() if not k.startswith("_")}, "vector": vec})
    indexed: list[str] = []
    failures = len(records) - len(to_index)
    try:
        outcome = index_chunks(to_index, structural_hash=structural)
        indexed, failures = outcome["indexed"], failures + outcome["failed"]
    except Exception as exc:
        failures = len(records)
        log.warning("embed.index_failed", error_type=type(exc).__name__, chunks=len(to_index))

    # ── Remove what earlier runs left behind (only once the new set is in) ────
    stale_deleted = 0
    if indexed and not failures and index_state.get("mappingsCurrent"):
        try:
            stale_deleted = delete_stale(doc_id, run_id)
        except Exception as exc:
            log.warning("embed.delete_stale_failed", error_type=type(exc).__name__)

    clauses_indexed = len({r["clauseNumber"] for r in records if r["id"] in set(indexed)})
    usage = openai_client.usage_snapshot()
    event["embeddings"] = {
        "clauseVectorIds": indexed,
        "embeddedCount": len(indexed),
        "chunkCount": len(records),
        "clauseCount": len(clauses),
        "clausesIndexed": clauses_indexed,
        "indexFailures": failures,
        "searchable": bool(indexed) and clauses_indexed == len({r["clauseNumber"] for r in records}),
        "runId": run_id,
    }
    log.info("embed.done", clauses=len(clauses), chunks=len(records), indexed=len(indexed),
             clausesIndexed=clauses_indexed, indexFailures=failures, cacheHits=cache_hits,
             embedded=len(fresh), staleDeleted=stale_deleted, embeddingCalls=usage["calls"],
             promptTokens=usage["prompt_tokens"], retries=usage["retries"],
             durationMs=int((time.monotonic() - started) * 1000))
    return event
