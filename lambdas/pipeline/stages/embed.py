"""Stage 03 — Embed: compute and cache clause embeddings, index into OpenSearch."""
from __future__ import annotations

from typing import Any

from shared.config import settings
from shared.dynamodb import get_cached_embedding, put_cached_embedding, update_status
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import ensure_indices, index_clause_text, index_clause_vector
from shared.text import sha256_hex

log = get_logger("blue-iq.embed")


def run(event: dict) -> dict:
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

    try:
        ensure_indices()
    except Exception as exc:
        log.warning("embed.ensure_indices_warning", error=str(exc))

    # Resolve cache hits vs. misses.
    hashes:       list[str]          = []
    cached_vecs:  dict[int, list[float]] = {}
    missing_idx:  list[int]          = []
    missing_texts: list[str]         = []

    for i, c in enumerate(clauses):
        body = (c.get("body") or "").strip()
        h    = sha256_hex(body) if body else ""
        hashes.append(h)
        cached = get_cached_embedding(h) if h else None
        if cached:
            cached_vecs[i] = cached
        else:
            missing_idx.append(i)
            missing_texts.append(body or " ")

    log.info("embed.cache_summary", total=len(clauses), hits=len(cached_vecs), misses=len(missing_idx))

    # Batch-embed misses.
    new_vecs: dict[int, list[float]] = {}
    for start in range(0, len(missing_texts), settings.embedding_batch_size):
        batch = missing_texts[start : start + settings.embedding_batch_size]
        idxs  = missing_idx[start : start + settings.embedding_batch_size]
        vecs  = embed_texts(batch, model=settings.embedding_model)
        for idx, vec in zip(idxs, vecs):
            new_vecs[idx] = vec
            h = hashes[idx]
            if h:
                try:
                    put_cached_embedding(h, vec, settings.embedding_model)
                except Exception as exc:
                    log.warning("embed.cache_write_failed", error=str(exc))

    # Index all clauses into OpenSearch.  Each clause is wrapped independently
    # so a single bad document or a transient OpenSearch hiccup doesn't kill
    # the entire pipeline — RAG quality degrades gracefully instead of failing.
    vector_ids: list[str] = []
    for i, c in enumerate(clauses):
        vector = cached_vecs.get(i) or new_vecs.get(i)
        if vector is None:
            log.warning("embed.skip_no_vector", clause=c.get("number"))
            continue
        try:
            cid = index_clause_vector(
                doc_id=doc_id, tenant_id=tenant_id,
                clause_number=c.get("number", str(i)),
                category=c.get("category", "Other"), doc_type=doc_type,
                text=c.get("body", ""), vector=vector,
            )
            index_clause_text(
                doc_id=doc_id, tenant_id=tenant_id,
                clause_number=c.get("number", str(i)),
                category=c.get("category", "Other"), doc_type=doc_type,
                title=c.get("title", ""), text=c.get("body", ""),
                structural_hash=structural,
            )
            vector_ids.append(cid)
        except Exception as exc:
            log.warning("embed.clause_index_failed", clause=c.get("number"), error=str(exc))

    event["embeddings"] = {"clauseVectorIds": vector_ids, "embeddedCount": len(vector_ids)}
    log.info("embed.done", embedded=len(vector_ids))
    return event
