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

import os
import re
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


def delete_run(doc_id: str, run_id: str) -> int:
    """Remove the records ONE pipeline run wrote for a document — the inverse of
    ``delete_stale``. Used when that run's version is deleted, so the chat can
    no longer cite an analysis the user removed."""
    deleted = 0
    c = client()
    query = {"query": {"bool": {"filter": [{"term": {"docId": doc_id}}, {"term": {"runId": run_id}}]}}}
    for index in (settings.clause_vector_index, settings.clause_text_index):
        resp = c.delete_by_query(index=index, body=query, refresh=True, conflicts="proceed",
                                 ignore_unavailable=True)
        deleted += int(resp.get("deleted", 0))
    log.info("opensearch.delete_run", docId=doc_id, deleted=deleted)
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


# ---------------------------------------------------------------------------
# Query understanding for clause retrieval
# ---------------------------------------------------------------------------
#
# People ask "when do we get paid?" of a clause titled "Invoicing and Payment"
# and "who owns the work product?" of one that says the deliverables "belong
# exclusively to Customer". The text index's ``english`` analyzer stems
# ("terminate" / "termination" meet) but knows no synonyms, and the Porter
# stemmer keeps "paid" and "payment" apart. So the keyword query is expanded
# HERE, at query time, with a small legal vocabulary: it works the same on
# every index ever created (old and new), needs no analyzer change, no index
# close/reopen and no reindex, and the expansion terms go through the very same
# analyzer as the indexed text. The same table names the clause categories a
# question is about; the fused ranking gives those a small lift (``_rerank``).
#
# Each concept: (trigger pattern on the lower-cased question, expansion words,
# categories). Expansion words carry a lower weight than the question's own
# words, so they decide ties and rescue paraphrases without drowning the
# question.

_CONCEPTS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = tuple(
    (re.compile(pattern), words, cats) for pattern, words, cats in (
        (r"\b(pay|paid|paying|payments?|payable|invoic\w*|remit\w*|net \d+|late (?:fee|payment)s?)\b",
         "payment payable invoice invoicing due days receipt", ("Payment", "Fees")),
        (r"\b(fees?|price|pricing|costs?|charges?|compensation|how much|budget|funding|rates?|amount)\b",
         "fees price charges compensation funding rates", ("Fees", "Payment", "Royalties")),
        (r"\b(how long|duration|term|expir\w*|end date|when does .* end|start date|commenc\w*|effective date)\b",
         "term duration expires expiry commences continues until effective date", ("Term",)),
        (r"\b(renew\w*|auto-?renew\w*|extend\w*|extension|non-renewal|roll(?:s|ing)? over)\b",
         "renewal renews automatically extension non-renewal term", ("Term",)),
        (r"\b(terminat\w*|cancel\w*|exit|walk away|get out of|end (?:it|this|the (?:agreement|contract|sow)) early"
         r"|breaks?|broke|breach\w*|default\w*|violat\w*)\b",
         "termination terminate notice convenience cause breach", ("Termination",)),
        (r"\b(liabilit\w*|liable|damages|cap(?:ped|s)?|limitations? of|consequential|exposure)\b",
         "limitation liability damages cap aggregate exceed", ("Liability",)),
        (r"\b(indemn\w*|hold harmless|sued|lawsuits?|third[- ]party claims?|infring\w*)\b",
         "indemnify indemnification defend hold harmless claim infringe", ("Indemnity",)),
        (r"\b(owns?|owned|owner(?:ship)?|intellectual property|ip|work product|copyrights?|patents?|inventions?|"
         r"title to|belongs?)\b",
         "intellectual property ownership owns title assigns belong vests work product deliverables inventions",
         ("IP", "BackgroundIP")),
        (r"\b(licen[cs]\w*|options?)\b", "license licence grant option", ("LicenseGrant", "LicenseScope", "IP")),
        (r"\b(governing law|govern\w*|jurisdiction|venue|courts?|which law|state'?s? law|choice of law"
         r"|forum|(?:sue|lawsuits?|claims?|cases?|actions?) (?:\w+ )?(?:be )?(?:filed|brought|heard))\b",
         "governing law governed jurisdiction venue courts", ("GoverningLaw", "DisputeResolution")),
        (r"\b(disputes?|arbitrat\w*|mediat\w*|escalat\w*|litigat\w*)\b",
         "dispute resolution arbitration mediation escalation", ("DisputeResolution",)),
        (r"\b(confidential\w*|nda|non-?disclosure|secrets?|proprietary information)\b",
         "confidential confidentiality non-disclosure disclose", ("Confidentiality", "DataRights")),
        (r"\b(publish\w*|publication|present(?:ation)?s?)\b", "publication publish", ("PublicationRights",)),
        (r"\b(warrant\w*|guarantee\w*|defects?|workmanlike)\b", "warranty warrants conform remedy", ("Warranty",)),
        (r"\b(insur\w*|coverage)\b", "insurance coverage insured", ("Insurance",)),
        (r"\b(data breach|security incident|breach notification|personal data|privacy|data protection)\b",
         "data protection security incident notify customer data", ("DataProtection", "BreachNotification")),
        (r"\b(accept\w*|reject\w*|sign[- ]off)\b", "acceptance accept reject review", ("Acceptance",)),
        (r"\b(deliverables?|milestones?|what is being delivered)\b", "deliverables milestones",
         ("Deliverables", "ScopeOfWork")),
        (r"\b(change orders?|change requests?|change (?:the )?(?:scope|what)|changes? to the scope|variation)\b",
         "change control change order change request scope", ("ChangeControl",)),
        (r"\b(expenses?|travel|reimburs\w*|out-of-pocket)\b", "expenses travel reimburse", ("Fees",)),
        (r"\b(assign\w*|transfer the (?:agreement|contract)|hand (?:the|this|it|our) (?:agreement|contract)?\s*(?:over )?to"
         r"|another (?:company|party|vendor)|successors?|novat\w*|change of control)\b",
         "assignment assign transfer successor consent", ("Assignment",)),
        (r"\b(force majeure|act of god|beyond (?:its|their|our) (?:reasonable )?control)\b",
         "force majeure beyond reasonable control", ("ForceMajeure",)),
        (r"\b(subcontract\w*)\b", "subcontract subcontractor", ("Subcontracting",)),
        (r"\b(non-?solicit\w*|poach\w*|hire (?:their|our) (?:staff|employees))\b",
         "non-solicitation solicit employment employee", ("Other",)),
    )
)

# "section 7.2", "clause 12", "§ 9", "Article 4": a question naming a clause
# should find that clause first.
_CLAUSE_REF_RE = re.compile(
    r"(?:§\s*|\b(?:section|clause|article|paragraph|para\.?)\s+)(\d{1,3}(?:\.\d{1,3}){0,3})\b", re.IGNORECASE)


def analyze_query(text: str) -> dict[str, Any]:
    """What a retrieval question is about: expansion words for the keyword
    query, the clause categories it targets, and clause numbers it names."""
    low = (text or "").lower()
    words: list[str] = []
    cats: list[str] = []
    for pattern, expansion, categories in _CONCEPTS:
        if pattern.search(low):
            words.extend(w for w in expansion.split() if w not in words)
            cats.extend(c for c in categories if c not in cats)
    refs = list(dict.fromkeys(m.group(1).rstrip(".") for m in _CLAUSE_REF_RE.finditer(text or "")))
    return {"expansion": " ".join(words), "categories": cats, "clause_refs": refs}


# Keyword fields and their weights: a heading match says more than a body match.
_BM25_FIELDS = ["title^3", "section^2", "text"]
_EXPANSION_BOOST = 0.4
_CLAUSE_REF_BOOST = 10.0


def _bm25_query(text: str, expand: bool) -> dict[str, Any]:
    if not expand:
        # the amendment matcher's literal query, unchanged
        return {"must": [{"multi_match": {"query": text, "fields": ["title^2", "section", "text"]}}]}
    main = {"multi_match": {"query": text, "fields": _BM25_FIELDS, "type": "best_fields", "tie_breaker": 0.3}}
    q = analyze_query(text)
    should: list[dict[str, Any]] = [main]
    if q["expansion"]:
        should.append({"multi_match": {"query": q["expansion"], "fields": _BM25_FIELDS, "type": "best_fields",
                                       "tie_breaker": 0.3, "boost": _EXPANSION_BOOST}})
    for ref in q["clause_refs"][:4]:
        should.append({"term": {"clauseNumber": {"value": ref, "boost": _CLAUSE_REF_BOOST}}})
        should.append({"prefix": {"clauseNumber": {"value": f"{ref}.", "boost": _CLAUSE_REF_BOOST / 2}}})
    return {"should": should, "minimum_should_match": 1}


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
    expand: bool = False,
) -> list[dict[str, Any]]:
    """Keyword search over title (x3), section heading (x2) and text.

    ``expand`` (used by the chat) adds the legal-vocabulary expansion and an
    exact match on a clause number the question names (see ``analyze_query``);
    the amendment matcher searches with the literal text only."""
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
        "query": {"bool": {**_bm25_query(text, expand), "filter": filters, "must_not": must_not}},
    }
    resp = client().search(index=settings.clause_text_index, body=query)
    return resp.get("hits", {}).get("hits", [])


class SearchUnavailable(RuntimeError):
    """Every retrieval channel failed (cluster down, timed out, misconfigured)."""


class SearchIndexMissing(SearchUnavailable):
    """The clause indices do not exist yet: nothing has been indexed."""


# Wall-clock bound on one hybrid search (both channels run at once). The chat
# answers over an HTTP API with a 30-second ceiling, so a slow cluster must cost
# a few seconds, not the whole request.
SEARCH_TIMEOUT_S = float(os.environ.get("RAG_SEARCH_TIMEOUT_S", "8") or 8)
# Rerank after fusion: a chunk of a clause in the category the question is about
# gains this much (a rank-1 hit in one channel is worth 1/61 ≈ 0.016).
_INTENT_BONUS = 0.004
# At most this many chunks of one clause in the first pass, so one long clause
# cannot fill every slot (its chunks are merged into one block anyway).
_MAX_CHUNKS_PER_CLAUSE = 2


def _is_missing_index(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "index_not_found" in text or (type(exc).__name__ == "NotFoundError")


def _norm_text(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _rerank(ranked: list[dict[str, Any]], categories: list[str], k: int) -> list[dict[str, Any]]:
    """Light rerank of fused hits: lift chunks of the clause categories the
    question targets, drop near-duplicate chunks of the same document (a stale
    copy of a renumbered clause), and keep clause diversity in the top ``k``."""
    wanted = set(categories)
    for row in ranked:
        src = row["hit"].get("_source") or {}
        if wanted and src.get("category") in wanted:
            row["score"] += _INTENT_BONUS
    ranked.sort(key=lambda r: r["score"], reverse=True)

    seen_text: set[tuple[str, str]] = set()
    per_clause: dict[tuple[str, str], int] = {}
    first, overflow = [], []
    for row in ranked:
        src = row["hit"].get("_source") or {}
        doc = str(src.get("docId") or "")
        key = (doc, str(src.get("clauseNumber") or ""))
        norm = _norm_text(src.get("text") or "")
        if norm and (doc, norm) in seen_text:
            continue
        if norm:
            seen_text.add((doc, norm))
        per_clause[key] = per_clause.get(key, 0) + 1
        (first if per_clause[key] <= _MAX_CHUNKS_PER_CLAUSE else overflow).append(row)
    return (first + overflow)[:k]


def clause_search(
    *,
    text: str,
    vector: list[float] | None,
    tenant_id: str | None = None,
    k: int = 8,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    timeout_s: float | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval for RAG: up to ``k`` clause chunks, best first.

    Semantic (k-NN) and keyword (BM25) results are fused by reciprocal rank
    (RRF, 1 / (60 + rank)) — a chunk found by both channels outranks one found
    by either alone, and an exact term the embedding missed (a clause number, a
    defined term, an amount) is no longer crowded out by k semantic hits. The
    keyword query is expanded with legal synonyms, and the fused list gets a
    light rerank (``_rerank``). Results are de-duplicated by record id.

    Fails soft: a channel that errors or exceeds ``timeout_s`` is dropped and
    the other is used; ``vector=None`` (the embedding service was unavailable)
    runs keyword search alone. Only when EVERY channel failed does it raise —
    ``SearchIndexMissing`` if the indices do not exist (nothing indexed yet),
    ``SearchUnavailable`` otherwise — so "no matching clause" (``[]``) and
    "search is down" are never confused.
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    pool = max(k * 3, 12)
    runs = [("bm25", lambda: bm25_search(text=text, tenant_id=tenant_id, k=pool, doc_id=doc_id,
                                         doc_ids=doc_ids, expand=True))]
    if vector:
        runs.insert(0, ("knn", lambda: knn_search(vector=vector, tenant_id=tenant_id, k=pool,
                                                   doc_id=doc_id, doc_ids=doc_ids)))
    if _scope_filters(tenant_id, doc_ids, doc_id) is None:
        return []
    budget = SEARCH_TIMEOUT_S if timeout_s is None else timeout_s
    channels: list[list[dict[str, Any]]] = []
    errors: list[BaseException] = []
    # Both channels at once: the answer waits on the slower one, not on both —
    # and never longer than the budget (a hung request is abandoned, not awaited).
    ex = ThreadPoolExecutor(max_workers=len(runs))
    try:
        futures = [(name, ex.submit(run)) for name, run in runs]
        wait([f for _, f in futures], timeout=budget)
        for name, fut in futures:
            if not fut.done():
                log.warning("opensearch.search_channel_timeout", channel=name, timeout_s=budget)
                errors.append(TimeoutError(f"{name} search exceeded {budget}s"))
                continue
            exc = fut.exception()
            if exc is None:
                channels.append(fut.result() or [])
                continue
            errors.append(exc)
            msg = str(exc).lower()
            if name == "knn" and "dimension" in msg:
                log.error("opensearch.vector_dimension_mismatch", error_type=type(exc).__name__,
                          vectorSize=len(vector or []))
            else:
                log.warning("opensearch.search_channel_failed", channel=name, error_type=type(exc).__name__)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if not channels:
        if errors and all(_is_missing_index(e) for e in errors):
            raise SearchIndexMissing("the clause search indices do not exist yet")
        raise SearchUnavailable(f"every search channel failed ({len(errors)})")

    fused: dict[str, dict[str, Any]] = {}
    for hits in channels:
        for rank, hit in enumerate(hits, start=1):
            hid = hit.get("_id") or f"{(hit.get('_source') or {}).get('docId')}::{(hit.get('_source') or {}).get('clauseNumber')}"
            row = fused.setdefault(hid, {"hit": hit, "score": 0.0})
            row["score"] += 1.0 / (60 + rank)
    ranked = sorted(fused.values(), key=lambda r: r["score"], reverse=True)
    out = []
    for row in _rerank(ranked, analyze_query(text)["categories"], k):
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
