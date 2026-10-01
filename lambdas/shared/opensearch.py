"""OpenSearch client + helpers.

Auth via SigV4 (IAM) using `requests-aws4auth` + the credentials from boto3.
Both `opensearch-py` and `requests-aws4auth` are listed in
`shared/requirements.txt`'s sibling layer — see infra/CDK for the deploy-time
packaging.  If they are not present (local unit tests), this module still
imports — callers get an informative error only when they try to make a call.

What is indexed
---------------
One record per retrieval CHUNK of a clause (a short clause is a single chunk),
written to both indices under the same id:

* ``clause-vectors`` — the embedding (k-NN) plus the metadata below
* ``clause-text``    — the same text for BM25

Metadata on every record: docId, tenantId, clauseId, clauseNumber, title,
section (the heading it sits under), category, specificType / specificTypeKey,
docType, chunkIndex / chunkCount, page, runId. ``runId`` identifies the pipeline
run that wrote the record, so a re-run can delete exactly the records it did
not rewrite (see ``delete_stale``).

Scoping
-------
Every search takes its scope as arguments and applies it INSIDE the query
(``filter``), never by post-filtering results: ``tenant_id`` (a storage
namespace) and/or ``doc_ids`` (the exact documents the caller is allowed to
see). ``doc_ids=[]`` means "nothing is permitted" and returns no hits without
querying.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

from .aws import get_credentials
from .config import settings
from .logger import get_logger

log = get_logger("blue-iq.opensearch")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def client():
    try:
        from opensearchpy import OpenSearch, RequestsHttpConnection
        from requests_aws4auth import AWS4Auth
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "opensearch-py and requests-aws4auth must be installed in this layer"
        ) from e

    creds = get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials available for OpenSearch SigV4")
    frozen = creds.get_frozen_credentials()
    auth = AWS4Auth(
        frozen.access_key,
        frozen.secret_key,
        settings.aws_region,
        "es",
        session_token=frozen.token,
    )
    endpoint = settings.opensearch_endpoint
    if not endpoint:
        raise RuntimeError("OPENSEARCH_ENDPOINT env var not set")
    # Strip protocol if user passed https://...
    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------


# Dimension of the clause vectors. It is a property of the INDEX: it must match
# the output size of settings.embedding_model (text-embedding-3-small = 1536).
# Set with EMBEDDING_DIMENSIONS; the embed stage refuses to write a vector of
# any other size and refuses to write into an index created with another size.
VECTOR_DIM = settings.embedding_dimensions

# Fields added after the first release. They are put onto an existing index with
# put_mapping (adding fields is always allowed), so no reindex is needed.
_COMMON_FIELDS: dict[str, Any] = {
    "docId": {"type": "keyword"},
    "tenantId": {"type": "keyword"},
    "clauseId": {"type": "keyword"},
    "clauseNumber": {"type": "keyword"},
    "category": {"type": "keyword"},
    "specificType": {"type": "keyword"},
    "specificTypeKey": {"type": "keyword"},
    "docType": {"type": "keyword"},
    "title": {"type": "text"},
    "section": {"type": "text"},
    "text": {"type": "text"},
    "chunkIndex": {"type": "integer"},
    "chunkCount": {"type": "integer"},
    "page": {"type": "integer"},
    "runId": {"type": "keyword"},
    "createdAt": {"type": "date"},
}

_VECTOR_MAPPING = {
    "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 100}},
    "mappings": {
        "properties": {
            **_COMMON_FIELDS,
            "vector": {
                "type": "knn_vector",
                "dimension": VECTOR_DIM,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "nmslib",
                    "parameters": {"ef_construction": 256, "m": 16},
                },
            },
        }
    },
}


_TEXT_MAPPING = {
    "settings": {"analysis": {"analyzer": {"default": {"type": "english"}}}},
    "mappings": {
        "properties": {
            **_COMMON_FIELDS,
            "structuralHash": {"type": "keyword"},
        }
    },
}


class VectorDimensionMismatch(RuntimeError):
    """The embedding size configured for this deployment is not the size the
    live vector index was created with."""


def ensure_indices() -> dict[str, Any]:
    """Idempotently create both indices, and add any newer metadata fields to an
    index that already exists.

    Returns ``{"vectorDimension": <int|None>, "mappingsCurrent": <bool>}`` — the
    dimension of the live vector index (None if it could not be read) and
    whether the metadata fields this code filters on are mapped.
    """
    c = client()
    current = True
    for name, body in (
        (settings.clause_vector_index, _VECTOR_MAPPING),
        (settings.clause_text_index, _TEXT_MAPPING),
    ):
        if not c.indices.exists(index=name):
            log.info("opensearch.create_index", index=name)
            c.indices.create(index=name, body=body)
            continue
        try:
            c.indices.put_mapping(index=name, body={"properties": _COMMON_FIELDS})
        except Exception as exc:  # an old field mapped differently — keep going
            current = False
            log.warning("opensearch.put_mapping_failed", index=name, error_type=type(exc).__name__)
    return {"vectorDimension": index_vector_dimension(), "mappingsCurrent": current}


def index_vector_dimension() -> int | None:
    """Dimension the live vector index was created with (None if unreadable)."""
    try:
        mapping = client().indices.get_mapping(index=settings.clause_vector_index)
    except Exception as exc:
        log.warning("opensearch.get_mapping_failed", error_type=type(exc).__name__)
        return None
    for body in (mapping or {}).values():
        dim = (((body or {}).get("mappings") or {}).get("properties") or {}).get("vector", {}).get("dimension")
        if isinstance(dim, int):
            return dim
    return None


def assert_vector_dimension(vector_size: int, index_dimension: int | None = None) -> None:
    """Fail clearly — before anything is written — when the embedding model, the
    configured dimension and the live index disagree. Writing a wrong-sized
    vector is rejected by OpenSearch per record, which used to surface only as
    warnings and leave the document silently unsearchable."""
    if vector_size != VECTOR_DIM:
        raise VectorDimensionMismatch(
            f"Embedding model {settings.embedding_model} returned vectors of size {vector_size}, but "
            f"EMBEDDING_DIMENSIONS is {VECTOR_DIM}. Set EMBEDDING_DIMENSIONS to the model's size (and "
            f"create a new vector index), or set EMBEDDING_SEND_DIMENSIONS=true for a model that can "
            f"return {VECTOR_DIM}-wide vectors."
        )
    if index_dimension is not None and index_dimension != vector_size:
        raise VectorDimensionMismatch(
            f"The vector index '{settings.clause_vector_index}' was created for vectors of size "
            f"{index_dimension}, but the configured embedding size is {vector_size}. Point "
            f"CLAUSE_VECTOR_INDEX at a new index name and re-analyze the documents; the existing "
            f"index has not been modified."
        )


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def _vector_doc_id(doc_id: str, clause_number: str, chunk_index: int = 0) -> str:
    """Record id. The first chunk of a clause keeps the original
    ``<docId>::<clauseNumber>`` id, so ``get_clause_vector`` and anything written
    before chunking existed keep working."""
    safe = str(clause_number).replace(" ", "_").replace("/", "_")
    base = f"{doc_id}::{safe}"
    return base if chunk_index <= 0 else f"{base}::c{chunk_index}"


def index_clause_vector(
    *,
    doc_id: str,
    tenant_id: str,
    clause_number: str,
    category: str,
    doc_type: str,
    text: str,
    vector: list[float],
) -> str:
    cid = _vector_doc_id(doc_id, clause_number)
    client().index(
        index=settings.clause_vector_index,
        id=cid,
        body={
            "docId": doc_id,
            "tenantId": tenant_id,
            "clauseNumber": clause_number,
            "category": category,
            "docType": doc_type,
            "text": text,
            "vector": vector,
        },
        refresh=False,
    )
    return cid


def get_clause_vector(doc_id: str, clause_number: str) -> list[float] | None:
    """Fetch a clause's stored embedding vector (used to find similar clauses).
    Returns None when the clause isn't indexed (older doc, not yet embedded) or
    OpenSearch is unavailable — callers degrade to an empty result, never error."""
    cid = _vector_doc_id(doc_id, clause_number)
    try:
        resp = client().get(index=settings.clause_vector_index, id=cid)
    except Exception as exc:  # noqa: BLE001 — missing doc/index is non-fatal
        log.info("opensearch.get_clause_vector_miss", error_type=type(exc).__name__)
        return None
    vec = (resp.get("_source") or {}).get("vector")
    return vec if isinstance(vec, list) and vec else None


def index_clause_text(
    *,
    doc_id: str,
    tenant_id: str,
    clause_number: str,
    category: str,
    doc_type: str,
    title: str,
    text: str,
    structural_hash: str,
) -> str:
    cid = _vector_doc_id(doc_id, clause_number)
    client().index(
        index=settings.clause_text_index,
        id=cid,
        body={
            "docId": doc_id,
            "tenantId": tenant_id,
            "clauseNumber": clause_number,
            "category": category,
            "docType": doc_type,
            "title": title,
            "text": text,
            "structuralHash": structural_hash,
        },
        refresh=False,
    )
    return cid


def bulk_index(actions: Iterable[dict[str, Any]]) -> tuple[int, list[dict]]:
    """Wrapper around `opensearchpy.helpers.bulk`.  Returns (ok_count, errors)."""
    from opensearchpy.helpers import bulk as _bulk

    actions = list(actions)
    if not actions:
        return (0, [])
    ok, errors = _bulk(client(), actions, raise_on_error=False, stats_only=False)
    return (ok, errors or [])


def index_chunks(records: list[dict[str, Any]], *, structural_hash: str = "") -> dict[str, Any]:
    """Write retrieval chunks to both indices in two bulk requests.

    Each record: ``{"id", "vector", <metadata fields>}``. Returns
    ``{"indexed": [ids written to BOTH indices], "failed": <int>}`` — a record
    only counts as searchable when its vector and its text both landed.
    """
    if not records:
        return {"indexed": [], "failed": 0}
    vector_actions, text_actions = [], []
    for r in records:
        meta = {k: v for k, v in r.items() if k not in ("id", "vector")}
        vector_actions.append({"_op_type": "index", "_index": settings.clause_vector_index,
                               "_id": r["id"], **meta, "vector": r["vector"]})
        text_actions.append({"_op_type": "index", "_index": settings.clause_text_index,
                             "_id": r["id"], **meta, "structuralHash": structural_hash})
    failed_ids: set[str] = set()
    for actions in (vector_actions, text_actions):
        _, errors = bulk_index(actions)
        for err in errors:
            detail = next(iter(err.values()), {}) if isinstance(err, dict) else {}
            if detail.get("_id"):
                failed_ids.add(detail["_id"])
            else:
                failed_ids.add(f"?{len(failed_ids)}")
    indexed = [r["id"] for r in records if r["id"] not in failed_ids]
    return {"indexed": indexed, "failed": len(records) - len(indexed)}


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_doc(doc_id: str) -> int:
    """Remove every indexed clause (vector + text) for a document.

    Best-effort: returns the number of documents deleted across both indices.
    A missing index or unconfigured OpenSearch is logged, not raised, so a
    document delete never fails just because its search index is already gone.
    """
    deleted = 0
    c = client()
    for index in (settings.clause_vector_index, settings.clause_text_index):
        try:
            resp = c.delete_by_query(
                index=index,
                body={"query": {"term": {"docId": doc_id}}},
                refresh=True,
                conflicts="proceed",
                ignore_unavailable=True,
            )
            deleted += int(resp.get("deleted", 0))
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("opensearch.delete_doc_failed", index=index, docId=doc_id, error_type=type(exc).__name__)
    log.info("opensearch.delete_doc", docId=doc_id, deleted=deleted)
    return deleted


def delete_stale(doc_id: str, run_id: str) -> int:
    """After a re-run has written its records, remove the document's records from
    EARLIER runs (anything for this docId that does not carry this runId).

    Without this, a clause that was renumbered, split differently or removed in
    the new analysis would stay searchable forever and be cited by the chat.
    Called only after the new records are in, so a failed run never leaves the
    document with nothing indexed.
    """
    deleted = 0
    c = client()
    query = {"query": {"bool": {
        "filter": [{"term": {"docId": doc_id}}],
        "must_not": [{"term": {"runId": run_id}}],
    }}}
    for index in (settings.clause_vector_index, settings.clause_text_index):
        resp = c.delete_by_query(index=index, body=query, refresh=True, conflicts="proceed",
                                 ignore_unavailable=True)
        deleted += int(resp.get("deleted", 0))
    log.info("opensearch.delete_stale", docId=doc_id, deleted=deleted)
    return deleted


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _scope_filters(
    tenant_id: str | None,
    doc_ids: list[str] | None,
    doc_id: str | None = None,
    doc_types: list[str] | None = None,
    any_of: bool = False,
) -> list[dict] | None:
    """Build the access scope as query filters. Returns None when the scope is
    empty (no tenant and no permitted documents) — the caller must then return
    no hits. ``any_of`` widens tenant AND doc_ids into tenant OR doc_ids."""
    filters: list[dict] = []
    ids = [d for d in dict.fromkeys(doc_ids)] if doc_ids is not None else None
    if ids is not None and not ids and not (any_of and tenant_id):
        return None
    if tenant_id and ids and any_of:
        filters.append({"bool": {"should": [{"term": {"tenantId": tenant_id}}, {"terms": {"docId": ids}}],
                                 "minimum_should_match": 1}})
    else:
        if tenant_id:
            filters.append({"term": {"tenantId": tenant_id}})
        if ids:
            filters.append({"terms": {"docId": ids}})
    if not filters:
        return None                      # refuse an unscoped search outright
    if doc_id:
        filters.append({"term": {"docId": doc_id}})
    if doc_types:
        filters.append({"terms": {"docType": doc_types}})
    return filters


def knn_search(
    *,
    vector: list[float],
    tenant_id: str | None = None,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    any_of: bool = False,
    exact: bool | None = None,
) -> list[dict[str, Any]]:
    """Nearest clause chunks to ``vector`` inside the given scope.

    Two query forms:

    * ``exact`` (default whenever the scope names specific documents): a
      ``script_score`` query that scores EVERY record matching the filter. The
      filter runs first, so recall inside one contract is perfect.
    * approximate (tenant-wide): the HNSW graph. With this index's engine the
      filter is applied AFTER the nearest neighbours are picked, so ``k`` is
      over-fetched to keep enough in-scope hits.
    """
    filters = _scope_filters(tenant_id, doc_ids, doc_id, doc_types, any_of)
    if filters is None:
        return []
    must_not: list[dict] = [{"term": {"docId": exclude_doc_id}}] if exclude_doc_id else []
    use_exact = exact if exact is not None else bool(doc_id or doc_ids)

    if use_exact:
        query = {
            "size": k,
            "_source": {"excludes": ["vector"]},
            "query": {"script_score": {
                "query": {"bool": {"filter": filters, "must_not": must_not}},
                "script": {"source": "knn_score", "lang": "knn",
                           "params": {"field": "vector", "query_value": vector, "space_type": "cosinesimil"}},
            }},
        }
        try:
            resp = client().search(index=settings.clause_vector_index, body=query)
            return resp.get("hits", {}).get("hits", [])
        except Exception as exc:
            # e.g. a cluster without the k-NN scoring script: fall back to the
            # approximate query (still filtered) rather than returning nothing.
            log.warning("opensearch.exact_knn_failed", error_type=type(exc).__name__)

    # Over-fetch: the filter below is applied to the neighbours the graph returns,
    # so asking for exactly k would return fewer than k in-scope hits (or none)
    # whenever other tenants' / documents' vectors are closer.
    ann_k = min(max(k * (20 if use_exact else 5), 50), 1000)
    query = {
        "size": k,
        "_source": {"excludes": ["vector"]},
        "query": {
            "bool": {
                "must": [
                    {"knn": {"vector": {"vector": vector, "k": ann_k}}},
                ],
                "filter": filters,
                "must_not": must_not,
            }
        },
    }
    resp = client().search(index=settings.clause_vector_index, body=query)
    return resp.get("hits", {}).get("hits", [])


def bm25_search(
    *,
    text: str,
    tenant_id: str | None = None,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
    structural_hash_prefix: str | None = None,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    any_of: bool = False,
) -> list[dict[str, Any]]:
    filters = _scope_filters(tenant_id, doc_ids, doc_id, doc_types, any_of)
    if filters is None:
        return []
    if structural_hash_prefix:
        filters.append({"prefix": {"structuralHash": structural_hash_prefix}})
    must_not = []
    if exclude_doc_id:
        must_not.append({"term": {"docId": exclude_doc_id}})
    query = {
        "size": k,
        "query": {
            "bool": {
                "must": [
                    {"multi_match": {"query": text, "fields": ["title^2", "section", "text"]}}
                ],
                "filter": filters,
                "must_not": must_not,
            }
        },
    }
    resp = client().search(index=settings.clause_text_index, body=query)
    return resp.get("hits", {}).get("hits", [])


def clause_search(
    *,
    text: str,
    vector: list[float],
    tenant_id: str | None = None,
    k: int = 8,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval for RAG: up to ``k`` clause chunks, best first.

    Semantic (k-NN) and keyword (BM25) results are fused by reciprocal rank
    (RRF, 1 / (60 + rank)) — a chunk found by both channels outranks one found
    by either alone, and an exact term the embedding missed (a clause number, a
    defined term, an amount) is no longer crowded out by k semantic hits.
    Results are de-duplicated by record id. If one channel fails (index missing,
    cluster busy) the other is still used.
    """
    pool = max(k * 3, 12)
    channels: list[list[dict[str, Any]]] = []
    for name, run in (
        ("knn", lambda: knn_search(vector=vector, tenant_id=tenant_id, k=pool, doc_id=doc_id, doc_ids=doc_ids)),
        ("bm25", lambda: bm25_search(text=text, tenant_id=tenant_id, k=pool, doc_id=doc_id, doc_ids=doc_ids)),
    ):
        try:
            channels.append(run())
        except Exception as exc:
            log.warning("opensearch.search_channel_failed", channel=name, error_type=type(exc).__name__)
            channels.append([])
    if not any(channels):
        return []

    fused: dict[str, dict[str, Any]] = {}
    for hits in channels:
        for rank, hit in enumerate(hits, start=1):
            hid = hit.get("_id") or f"{(hit.get('_source') or {}).get('docId')}::{(hit.get('_source') or {}).get('clauseNumber')}"
            row = fused.setdefault(hid, {"hit": hit, "score": 0.0})
            row["score"] += 1.0 / (60 + rank)
    ranked = sorted(fused.values(), key=lambda r: r["score"], reverse=True)
    out = []
    for row in ranked[:k]:
        hit = dict(row["hit"])
        hit["_rrf"] = round(row["score"], 6)
        out.append(hit)
    return out


def hybrid_search(
    *,
    text: str,
    vector: list[float],
    tenant_id: str | None = None,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
    alpha: float = 0.5,
    doc_ids: list[str] | None = None,
    any_of: bool = False,
) -> list[dict[str, Any]]:
    """Linear combination of normalised BM25 + KNN scores.

    `alpha` weights the vector channel (`1 - alpha` weights BM25).
    Hits are grouped by `docId`; the doc's best clause hit is used.
    """
    knn_hits = knn_search(
        vector=vector,
        tenant_id=tenant_id,
        k=k * 2,
        doc_types=doc_types,
        exclude_doc_id=exclude_doc_id,
        doc_ids=doc_ids,
        any_of=any_of,
    )
    bm25_hits = bm25_search(
        text=text,
        tenant_id=tenant_id,
        k=k * 2,
        doc_types=doc_types,
        exclude_doc_id=exclude_doc_id,
        doc_ids=doc_ids,
        any_of=any_of,
    )

    def _norm(hits: list[dict]) -> tuple[dict[str, float], dict[str, dict]]:
        """Returns (normalised_scores_by_docId, best_source_by_docId)."""
        if not hits:
            return {}, {}
        scores = [h.get("_score", 0.0) for h in hits]
        lo, hi = min(scores), max(scores)
        span = hi - lo
        # When every hit shares the same score (a single candidate, or a uniform
        # result set), min-max scaling would map them all to 0.0 and silently
        # drop an otherwise strong match. Treat that degenerate case as a tie at
        # the top of the channel (1.0) instead.
        degenerate = span <= 1e-9
        norm_scores: dict[str, float] = {}
        best_source: dict[str, dict] = {}
        for h in hits:
            src = h.get("_source", {})
            did = src.get("docId")
            if not did:
                continue
            normed = 1.0 if degenerate else (h.get("_score", 0.0) - lo) / span
            # keep the highest-scoring clause per doc
            if did not in norm_scores or normed > norm_scores[did]:
                norm_scores[did] = normed
                best_source[did] = src
        return norm_scores, best_source

    knn_scores, knn_sources = _norm(knn_hits)
    bm25_scores, bm25_sources = _norm(bm25_hits)
    docs = set(knn_scores) | set(bm25_scores)
    combined = []
    for d in docs:
        # Prefer the KNN source (vector match) as the representative clause;
        # fall back to BM25 source when the doc only appeared via BM25.
        src = knn_sources.get(d) or bm25_sources.get(d) or {}
        combined.append(
            {
                "docId": d,
                "clauseNumber": src.get("clauseNumber", ""),
                "title": src.get("title", ""),
                "text": src.get("text", ""),
                "category": src.get("category", ""),
                "docType": src.get("docType", ""),
                "score": alpha * knn_scores.get(d, 0.0)
                + (1 - alpha) * bm25_scores.get(d, 0.0),
                "knn_score": knn_scores.get(d, 0.0),
                "bm25_score": bm25_scores.get(d, 0.0),
            }
        )
    combined.sort(key=lambda x: x["score"], reverse=True)
    return combined[:k]
