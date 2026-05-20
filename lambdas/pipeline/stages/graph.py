"""Stage 04 — Graph: detect parent document and write lineage edges.

Only applicable to AMENDMENT documents. For all other types, lineage is skipped
and the stage exits immediately — the pipeline continues without a parent link.

Matching uses a combined score across three signals:
  - hybrid (vector + BM25) search in OpenSearch  (weight 0.60)
  - structural hash prefix match                  (weight 0.25)
  - title similarity                              (weight 0.15)
"""
from __future__ import annotations

from typing import Any

from shared.config import settings
from shared.dynamodb import get_doc_meta, put_lineage, update_status
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import bm25_search, hybrid_search
from shared.text import title_similarity

log = get_logger("blue-iq.graph")

_W_HYBRID     = 0.60
_W_STRUCTURAL = 0.25
_W_TITLE      = 0.15


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id         = event["docId"]
    tenant_id      = event["tenantId"]
    classification = event.get("classification") or {}
    doc_type       = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "GRAPHING")

    lineage: dict[str, Any] = {
        "parentDocId": None,
        "matchConfidence": 0.0,
        "matchReason": "",
    }

    if doc_type != "AMENDMENT":
        log.info("graph.skipped", reason=f"docType={doc_type} is not AMENDMENT")
        event["lineage"] = lineage
        return event

    parent_id, confidence, reason = _find_parent(
        doc_id=doc_id, tenant_id=tenant_id, classification=classification
    )

    if parent_id and confidence >= settings.parent_match_min_confidence:
        lineage = {
            "parentDocId":      parent_id,
            "matchConfidence":  round(float(confidence), 4),
            "matchReason":      reason,
        }
        put_lineage(parent_id=parent_id, child_id=doc_id)
        log.info("graph.parent_linked", parentDocId=parent_id, confidence=confidence)
    else:
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
    *, doc_id: str, tenant_id: str, classification: dict[str, Any]
) -> tuple[str | None, float, str]:
    clauses    = classification.get("clauses") or []
    title      = classification.get("title", "")
    structural = (classification.get("structuralHash") or "")[:8]

    if not clauses:
        return None, 0.0, "no clauses to match"

    rep_clause = _representative(clauses)
    rep_text   = rep_clause["body"][:4000]

    # Embed representative clause.
    rep_vec: list[float] = []
    try:
        [rep_vec] = embed_texts([rep_text], model=settings.embedding_model)
    except Exception as exc:
        log.warning("graph.embed_failed", error=str(exc))

    # Hybrid search.
    hybrid_hits: list[dict[str, Any]] = []
    if rep_vec:
        try:
            hybrid_hits = hybrid_search(
                text=rep_text, vector=rep_vec, tenant_id=tenant_id,
                k=10, doc_types=["SOW", "MSA"], exclude_doc_id=doc_id, alpha=0.6,
            )
        except Exception as exc:
            log.warning("graph.hybrid_search_failed", error=str(exc))

    # Structural prefix match.
    structural_ids: set[str] = set()
    if structural:
        try:
            for h in bm25_search(
                text=title or rep_text, tenant_id=tenant_id, k=20,
                doc_types=["SOW", "MSA"], exclude_doc_id=doc_id,
                structural_hash_prefix=structural,
            ):
                if did := h.get("_source", {}).get("docId"):
                    structural_ids.add(did)
        except Exception as exc:
            log.warning("graph.structural_search_failed", error=str(exc))

    # Combine signals.
    candidates: dict[str, dict[str, float]] = {}
    for h in hybrid_hits:
        if did := h.get("docId"):
            candidates.setdefault(did, {})["hybrid"] = float(h.get("score", 0.0))
    for did in structural_ids:
        candidates.setdefault(did, {})["structural"] = 1.0

    best: tuple[str | None, float, str] = (None, 0.0, "")
    for did, signals in candidates.items():
        meta         = get_doc_meta(did) or {}
        parent_title = meta.get("title", "")
        signals["title"] = title_similarity(title, parent_title) if parent_title else 0.0

        score = (
            _W_HYBRID     * signals.get("hybrid", 0.0)
            + _W_STRUCTURAL * signals.get("structural", 0.0)
            + _W_TITLE      * signals.get("title", 0.0)
        )
        parts: list[str] = []
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
    return max(clauses, key=lambda c: len(c.get("body", "")))
