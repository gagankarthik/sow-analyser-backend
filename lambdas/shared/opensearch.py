"""OpenSearch client + helpers.

Auth via SigV4 (IAM) using `requests-aws4auth` + the credentials from boto3.
Both `opensearch-py` and `requests-aws4auth` are listed in
`shared/requirements.txt`'s sibling layer — see infra/CDK for the deploy-time
packaging.  If they are not present (local unit tests), this module still
imports — callers get an informative error only when they try to make a call.
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


_VECTOR_MAPPING = {
    "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 100}},
    "mappings": {
        "properties": {
            "docId": {"type": "keyword"},
            "tenantId": {"type": "keyword"},
            "clauseNumber": {"type": "keyword"},
            "category": {"type": "keyword"},
            "docType": {"type": "keyword"},
            "text": {"type": "text"},
            "vector": {
                "type": "knn_vector",
                "dimension": 1536,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "nmslib",
                    "parameters": {"ef_construction": 256, "m": 16},
                },
            },
            "createdAt": {"type": "date"},
        }
    },
}


_TEXT_MAPPING = {
    "settings": {"analysis": {"analyzer": {"default": {"type": "english"}}}},
    "mappings": {
        "properties": {
            "docId": {"type": "keyword"},
            "tenantId": {"type": "keyword"},
            "clauseNumber": {"type": "keyword"},
            "category": {"type": "keyword"},
            "docType": {"type": "keyword"},
            "title": {"type": "text"},
            "text": {"type": "text"},
            "structuralHash": {"type": "keyword"},
            "createdAt": {"type": "date"},
        }
    },
}


def ensure_indices() -> None:
    """Idempotently create both indices if they don't already exist."""
    c = client()
    for name, body in (
        (settings.clause_vector_index, _VECTOR_MAPPING),
        (settings.clause_text_index, _TEXT_MAPPING),
    ):
        if not c.indices.exists(index=name):
            log.info("opensearch.create_index", index=name)
            c.indices.create(index=name, body=body)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def _vector_doc_id(doc_id: str, clause_number: str) -> str:
    safe = clause_number.replace(" ", "_").replace("/", "_")
    return f"{doc_id}::{safe}"


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


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def knn_search(
    *,
    vector: list[float],
    tenant_id: str,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
) -> list[dict[str, Any]]:
    must_not: list[dict] = []
    if exclude_doc_id:
        must_not.append({"term": {"docId": exclude_doc_id}})
    filters: list[dict] = [{"term": {"tenantId": tenant_id}}]
    if doc_types:
        filters.append({"terms": {"docType": doc_types}})
    query = {
        "size": k,
        "query": {
            "bool": {
                "must": [
                    {"knn": {"vector": {"vector": vector, "k": k}}},
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
    tenant_id: str,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
    structural_hash_prefix: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[dict] = [{"term": {"tenantId": tenant_id}}]
    if doc_types:
        filters.append({"terms": {"docType": doc_types}})
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
                    {"multi_match": {"query": text, "fields": ["title^2", "text"]}}
                ],
                "filter": filters,
                "must_not": must_not,
            }
        },
    }
    resp = client().search(index=settings.clause_text_index, body=query)
    return resp.get("hits", {}).get("hits", [])


def hybrid_search(
    *,
    text: str,
    vector: list[float],
    tenant_id: str,
    k: int = 10,
    doc_types: list[str] | None = None,
    exclude_doc_id: str | None = None,
    alpha: float = 0.5,
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
    )
    bm25_hits = bm25_search(
        text=text,
        tenant_id=tenant_id,
        k=k * 2,
        doc_types=doc_types,
        exclude_doc_id=exclude_doc_id,
    )

    def _norm(hits: list[dict]) -> tuple[dict[str, float], dict[str, dict]]:
        """Returns (normalised_scores_by_docId, best_source_by_docId)."""
        if not hits:
            return {}, {}
        scores = [h.get("_score", 0.0) for h in hits]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        norm_scores: dict[str, float] = {}
        best_source: dict[str, dict] = {}
        for h in hits:
            src = h.get("_source", {})
            did = src.get("docId")
            if not did:
                continue
            normed = (h.get("_score", 0.0) - lo) / span
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
