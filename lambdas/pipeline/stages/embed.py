"""Stage 03 — Embed: generate clause embeddings and index into OpenSearch.

Each clause body is embedded via OpenAI (text-embedding-3-small by default).
Embeddings are cached in DynamoDB by content hash to avoid re-billing identical
clause text (common across amendment chains).

OpenSearch indexing failures are non-fatal: a warning is logged per clause and
the pipeline continues. RAG quality degrades gracefully instead of aborting.
"""
from __future__ import annotations

from typing import Any

from shared.config import settings
from shared.dynamodb import get_cached_embedding, put_cached_embedding, update_status
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import ensure_indices, index_clause_text, index_clause_vector
from shared.text import sha256_hex

log = get_logger("blue-iq.embed")


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id         = event["docId"]
    tenant_id      = event["tenantId"]
    classification = event.get("classification") or {}
    clauses: list[dict[str, Any]] = classification.get("clauses") or []
    structural     = classification.get("structuralHash", "")
    doc_type       = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "EMBEDDING")
    log.info("embed.start", clauses=len(clauses))

    if not clauses:
        event["embeddings"] = {"clauseVectorIds": [], "embeddedCount": 0}
        return event

    # Best-effort index creation; don't fail the pipeline if OpenSearch is slow.
    try:
        ensure_indices()
    except Exception as exc:
        log.warning("embed.ensure_indices_failed", error=str(exc))

    # ── Resolve embedding cache hits vs. misses ───────────────────────────────
    hashes: list[str]              = []
    cached_vecs: dict[int, list[float]] = {}
    miss_idxs: list[int]           = []
    miss_texts: list[str]          = []

    for i, clause in enumerate(clauses):
        body = (clause.get("body") or "").strip()
        h    = sha256_hex(body) if body else ""
        hashes.append(h)
        hit  = get_cached_embedding(h) if h else None
        if hit:
            cached_vecs[i] = hit
        else:
            miss_idxs.append(i)
            miss_texts.append(body or " ")

    log.info("embed.cache", total=len(clauses),
             hits=len(cached_vecs), misses=len(miss_idxs))

    # ── Batch-embed cache misses ──────────────────────────────────────────────
    new_vecs: dict[int, list[float]] = {}
    batch_size = settings.embedding_batch_size
    for start in range(0, len(miss_texts), batch_size):
        batch_texts = miss_texts[start : start + batch_size]
        batch_idxs  = miss_idxs[start : start + batch_size]
        vecs        = embed_texts(batch_texts, model=settings.embedding_model)
        for idx, vec in zip(batch_idxs, vecs):
            new_vecs[idx] = vec
            h = hashes[idx]
            if h:
                try:
                    put_cached_embedding(h, vec, settings.embedding_model)
                except Exception as exc:
                    log.warning("embed.cache_write_failed", error=str(exc))

    # ── Index into OpenSearch (non-fatal per clause) ──────────────────────────
    vector_ids: list[str] = []
    for i, clause in enumerate(clauses):
        vec = cached_vecs.get(i) or new_vecs.get(i)
        if vec is None:
            log.warning("embed.no_vector", clause=clause.get("number"))
            continue
        try:
            cid = index_clause_vector(
                doc_id=doc_id, tenant_id=tenant_id,
                clause_number=clause.get("number", str(i)),
                category=clause.get("category", "Other"),
                doc_type=doc_type,
                text=clause.get("body", ""),
                vector=vec,
            )
            index_clause_text(
                doc_id=doc_id, tenant_id=tenant_id,
                clause_number=clause.get("number", str(i)),
                category=clause.get("category", "Other"),
                doc_type=doc_type,
                title=clause.get("title", ""),
                text=clause.get("body", ""),
                structural_hash=structural,
            )
            vector_ids.append(cid)
        except Exception as exc:
            log.warning("embed.index_failed",
                        clause=clause.get("number"), error=str(exc))

    event["embeddings"] = {"clauseVectorIds": vector_ids, "embeddedCount": len(vector_ids)}
    log.info("embed.done", indexed=len(vector_ids))
    return event
