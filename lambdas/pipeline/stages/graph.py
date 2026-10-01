"""Stage 04 — Graph: detect parent document and write lineage edges.

Only applicable to AMENDMENT documents. For all other types, lineage is skipped
and the stage exits immediately — the pipeline continues without a parent link.

Matching uses a combined score across four signals:
  - explicit parent reference named in the amendment (weight 0.25)
  - hybrid (vector + BM25) search in OpenSearch       (weight 0.45)
  - structural hash prefix match                      (weight 0.18)
  - title similarity                                  (weight 0.12)

The parent reference (e.g. "pursuant to SOW-2024-001") is the most reliable
signal when present, so we search for it directly rather than leaning only on
fuzzy clause similarity.
"""
from __future__ import annotations

from typing import Any

from shared.config import settings
from shared.dynamodb import get_doc_meta, put_lineage, related_doc_ids, update_status
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import bm25_search, hybrid_search
from shared.text import title_similarity

log = get_logger("blue-iq.graph")

_W_REFERENCE  = 0.25
_W_HYBRID     = 0.45
_W_STRUCTURAL = 0.18
_W_TITLE      = 0.12


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id         = event["docId"]
    tenant_id      = event["tenantId"]
    classification = event.get("classification") or {}
    doc_type       = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "GRAPHING")

    # status: "not_applicable" (not an amendment) | "linked" | "unmatched" (an
    # amendment whose parent was not found — it may not be uploaded or analysed
    # yet; re-analyze the amendment once it is).
    lineage: dict[str, Any] = {
        "parentDocId": None,
        "matchConfidence": 0.0,
        "matchReason": "",
        "status": "not_applicable",
    }

    if doc_type != "AMENDMENT":
        log.info("graph.skipped", reason=f"docType={doc_type} is not AMENDMENT")
        event["lineage"] = lineage
        return event

    # Candidates: the uploader's own workspace, plus every document that shares a
    # project with this one (a teammate may have uploaded the parent).
    try:
        related = related_doc_ids(doc_id)
    except Exception as exc:
        log.warning("graph.related_docs_failed", error_type=type(exc).__name__)
        related = []

    parent_id, confidence, reason = _find_parent(
        doc_id=doc_id, tenant_id=tenant_id, classification=classification, related=related
    )

    if parent_id and confidence >= settings.parent_match_min_confidence:
        lineage = {
            "parentDocId":      parent_id,
            "matchConfidence":  round(float(confidence), 4),
            "matchReason":      reason,
            "status":           "linked",
        }
        put_lineage(parent_id=parent_id, child_id=doc_id)
        log.info("graph.parent_linked", parentDocId=parent_id, confidence=confidence)
    else:
        lineage["status"] = "unmatched"
        lineage["matchConfidence"] = round(float(confidence), 4) if parent_id else 0.0
        lineage["matchReason"] = (
            f"best candidate {parent_id} below threshold ({confidence:.2f})"
            if parent_id else "no candidates found"
        )
        log.info("graph.no_match", best=parent_id, confidence=confidence)

    event["lineage"] = lineage
    return event


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _find_parent(
    *, doc_id: str, tenant_id: str, classification: dict[str, Any], related: list[str] | None = None,
) -> tuple[str | None, float, str]:
    clauses        = classification.get("clauses") or []
    title          = classification.get("title", "")
    structural     = (classification.get("structuralHash") or "")[:8]
    identification = classification.get("identification") or {}
    parent_ref     = (identification.get("parentReference") or "").strip()

    if not clauses:
        return None, 0.0, "no clauses to match"

    rep_clause = _representative(clauses)
    rep_text   = (rep_clause.get("body") or "")[:4000]
    # tenant OR shared-project documents; an empty `related` is just the tenant.
    scope = {"tenant_id": tenant_id, "doc_ids": related or None, "any_of": bool(related)}

    # Embed representative clause.
    rep_vec: list[float] = []
    try:
        [rep_vec] = embed_texts([rep_text], model=settings.embedding_model)
    except Exception as exc:
        log.warning("graph.embed_failed", error_type=type(exc).__name__)

    # Hybrid search.
    hybrid_hits: list[dict[str, Any]] = []
    if rep_vec:
        try:
            hybrid_hits = hybrid_search(
                text=rep_text, vector=rep_vec, **scope,
                k=10, doc_types=["SOW", "MSA"], exclude_doc_id=doc_id, alpha=0.6,
            )
        except Exception as exc:
            log.warning("graph.hybrid_search_failed", error_type=type(exc).__name__)

    # Structural prefix match.
    structural_ids: set[str] = set()
    if structural:
        try:
            for h in bm25_search(
                text=title or rep_text, **scope, k=20,
                doc_types=["SOW", "MSA"], exclude_doc_id=doc_id,
                structural_hash_prefix=structural,
            ):
                if did := h.get("_source", {}).get("docId"):
                    structural_ids.add(did)
        except Exception as exc:
            log.warning("graph.structural_search_failed", error_type=type(exc).__name__)

    # Explicit parent reference. When the amendment names its parent, search for
    # that reference directly and normalise the BM25 scores to [0, 1].
    reference_scores: dict[str, float] = {}
    if parent_ref:
        try:
            ref_hits = bm25_search(
                text=parent_ref, **scope, k=10,
                doc_types=["SOW", "MSA"], exclude_doc_id=doc_id,
            )
            top = max((h.get("_score", 0.0) for h in ref_hits), default=0.0)
            if top > 0:
                for h in ref_hits:
                    if did := h.get("_source", {}).get("docId"):
                        normed = h.get("_score", 0.0) / top
                        reference_scores[did] = max(reference_scores.get(did, 0.0), normed)
        except Exception as exc:
            log.warning("graph.reference_search_failed", error_type=type(exc).__name__)

    # Combine signals.
    candidates: dict[str, dict[str, float]] = {}
    for h in hybrid_hits:
        if did := h.get("docId"):
            candidates.setdefault(did, {})["hybrid"] = float(h.get("score", 0.0))
    for did in structural_ids:
        candidates.setdefault(did, {})["structural"] = 1.0
    for did, ref_score in reference_scores.items():
        candidates.setdefault(did, {})["reference"] = ref_score

    owner = (get_doc_meta(doc_id) or {}).get("ownerSub") if candidates else None
    shared = set(related or [])
    best: tuple[str | None, float, str] = (None, 0.0, "")
    for did, signals in candidates.items():
        meta         = get_doc_meta(did) or {}
        # Same storage tenant is not enough: the parent must be the uploader's own
        # document or one that shares a project with this one.
        if did not in shared and owner and meta.get("ownerSub") and meta["ownerSub"] != owner:
            continue
        parent_title = meta.get("title", "")
        signals["title"] = title_similarity(title, parent_title) if parent_title else 0.0
        # Fold a direct reference↔title comparison into the reference signal so a
        # named parent still scores even if BM25 keyword recall missed it.
        if parent_ref and parent_title:
            signals["reference"] = max(
                signals.get("reference", 0.0), title_similarity(parent_ref, parent_title)
            )

        score = (
            _W_REFERENCE  * signals.get("reference", 0.0)
            + _W_HYBRID     * signals.get("hybrid", 0.0)
            + _W_STRUCTURAL * signals.get("structural", 0.0)
            + _W_TITLE      * signals.get("title", 0.0)
        )
        parts: list[str] = []
        if signals.get("reference"):   parts.append(f"parent-ref={signals['reference']:.2f}")
        if signals.get("hybrid"):      parts.append(f"hybrid={signals['hybrid']:.2f}")
        if signals.get("structural"):  parts.append("structural-hash")
        if signals.get("title"):       parts.append(f"title-sim={signals['title']:.2f}")
        reason = " + ".join(parts) or "weak signal"

        if score > best[1]:
            best = (did, score, reason)

    return best


def _representative(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    preferred = {"ScopeOfWork", "Definitions", "Term", "Fees"}
    for c in clauses:
        if c.get("category") in preferred and c.get("body"):
            return c
    return max(clauses, key=lambda c: len(c.get("body") or ""))
