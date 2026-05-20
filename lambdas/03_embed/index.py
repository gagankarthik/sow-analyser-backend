"""Stage 3 — Embed.

For each clause:
  1. hash the body
  2. look up DDB cache (PK = CACHE#<hash>); reuse if present
  3. otherwise queue for OpenAI embedding
Embed missing clauses in batches of `settings.embedding_batch_size`, write
cache entries, and index both vector + BM25 documents into OpenSearch.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.dynamodb import get_cached_embedding, put_cached_embedding
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import (
    ensure_indices,
    index_clause_text,
    index_clause_vector,
)
from shared.text import sha256_hex

log = get_logger("blue-iq.embed")
tracer = Tracer(service="blue-iq.embed")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("embed.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    classification = event.get("classification") or {}
    clauses: list[dict[str, Any]] = classification.get("clauses") or []
    structural = classification.get("structuralHash", "")
    doc_type = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    log.info("embed.start", clauses=len(clauses))

    if not clauses:
        event["embeddings"] = {"clauseVectorIds": [], "embeddedCount": 0}
        return event

    # Ensure indices exist (idempotent — fast no-op on warm starts).
    try:
        ensure_indices()
    except Exception as e:
        # We don't want to fail the entire pipeline if OpenSearch is briefly
        # unavailable for ensure-indices; the index call below will surface
        # any real problem.
        log.warning("embed.ensure_indices_warning", error=str(e))

    # 1. Resolve cache vs. work-to-do.
    hashes: list[str] = []
    cached_vecs: dict[int, list[float]] = {}
    missing_idx: list[int] = []
    missing_texts: list[str] = []

    for i, c in enumerate(clauses):
        body = (c.get("body") or "").strip()
        h = sha256_hex(body) if body else ""
        hashes.append(h)
        cached = get_cached_embedding(h) if h else None
        if cached:
            cached_vecs[i] = cached
        else:
            missing_idx.append(i)
            missing_texts.append(body or " ")

    log.info(
        "embed.cache_summary",
        total=len(clauses),
        hits=len(cached_vecs),
        misses=len(missing_idx),
    )

    # 2. Batch embeddings for misses.
    new_vecs: dict[int, list[float]] = {}
    batch_size = settings.embedding_batch_size
    for start in range(0, len(missing_texts), batch_size):
        batch = missing_texts[start : start + batch_size]
        idxs = missing_idx[start : start + batch_size]
        vecs = embed_texts(batch, model=settings.embedding_model)
        for idx, vec in zip(idxs, vecs):
            new_vecs[idx] = vec
            h = hashes[idx]
            if h:
                try:
                    put_cached_embedding(h, vec, settings.embedding_model)
                except Exception as e:  # cache write must never block ingest
                    log.warning("embed.cache_write_failed", error=str(e))

    # 3. Index every clause to OpenSearch (vector + text).
    vector_ids: list[str] = []
    for i, c in enumerate(clauses):
        vector = cached_vecs.get(i) or new_vecs.get(i)
        if vector is None:
            log.warning("embed.skip_no_vector", clause=c.get("number"))
            continue
        cid = index_clause_vector(
            doc_id=doc_id,
            tenant_id=tenant_id,
            clause_number=c.get("number", str(i)),
            category=c.get("category", "Other"),
            doc_type=doc_type,
            text=c.get("body", ""),
            vector=vector,
        )
        index_clause_text(
            doc_id=doc_id,
            tenant_id=tenant_id,
            clause_number=c.get("number", str(i)),
            category=c.get("category", "Other"),
            doc_type=doc_type,
            title=c.get("title", ""),
            text=c.get("body", ""),
            structural_hash=structural,
        )
        vector_ids.append(cid)

    event["embeddings"] = {
        "clauseVectorIds": vector_ids,
        "embeddedCount": len(vector_ids),
    }
    log.info("embed.done", embedded=len(vector_ids))
    return event
