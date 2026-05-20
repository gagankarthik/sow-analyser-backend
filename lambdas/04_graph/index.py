"""Stage 4 — Graph (parent-document detection + adjacency writes).

For AMENDMENT docs we try to identify the parent SOW/MSA.  We combine three
signals into one confidence score in [0,1]:

  * hybrid_search (vector + BM25 over the parent's clauses) — semantic match
  * exact prefix match on `structuralHash` first 8 chars — structural identity
  * Jaccard title similarity — surfaces "Amendment No.3 to <Parent Title>"

If the combined score crosses `settings.parent_match_min_confidence` we write
adjacency edges to DynamoDB and set `lineage.parentDocId`.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.dynamodb import get_doc_meta, put_lineage
from shared.logger import get_logger
from shared.openai_client import embed_texts
from shared.opensearch import bm25_search, hybrid_search
from shared.text import title_similarity

log = get_logger("blue-iq.graph")
tracer = Tracer(service="blue-iq.graph")


WEIGHT_HYBRID = 0.6
WEIGHT_STRUCTURAL = 0.25
WEIGHT_TITLE = 0.15


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("graph.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    classification = event.get("classification") or {}
    doc_type = classification.get("docType", "OTHER")

    log.append_keys(docId=doc_id, tenantId=tenant_id)

    lineage = {"parentDocId": None, "matchConfidence": 0.0, "matchReason": ""}

    if doc_type != "AMENDMENT":
        log.info("graph.no_lineage", reason=f"docType={doc_type}")
        event["lineage"] = lineage
        return event

    parent_id, confidence, reason = _find_parent(
        doc_id=doc_id,
        tenant_id=tenant_id,
        classification=classification,
    )

    if parent_id and confidence >= settings.parent_match_min_confidence:
        lineage = {
            "parentDocId": parent_id,
            "matchConfidence": round(float(confidence), 4),
            "matchReason": reason,
        }
        put_lineage(parent_id=parent_id, child_id=doc_id)
        log.info(
            "graph.parent_linked",
            parentDocId=parent_id,
            confidence=confidence,
            reason=reason,
        )
    else:
        lineage["matchConfidence"] = round(float(confidence), 4) if parent_id else 0.0
        lineage["matchReason"] = (
            f"best candidate {parent_id} below threshold ({confidence:.2f})"
            if parent_id
            else "no candidates"
        )
        log.info("graph.no_parent_match", best=parent_id, confidence=confidence)

    event["lineage"] = lineage
    return event


# ---------------------------------------------------------------------------
# Parent search
# ---------------------------------------------------------------------------


def _find_parent(
    *,
    doc_id: str,
    tenant_id: str,
    classification: dict[str, Any],
) -> tuple[str | None, float, str]:
    """Returns (parent_doc_id, confidence, reason_text)."""
    clauses = classification.get("clauses") or []
    title = classification.get("title", "")
    structural = classification.get("structuralHash", "") or ""
    structural_prefix = structural[:8]

    if not clauses:
        return (None, 0.0, "no clauses to match against")

    # Pick a representative clause for hybrid search.  We prefer the first
    # Definitions/Scope clause if present, else the longest clause body.
    rep_clause = _pick_representative_clause(clauses)
    rep_text = rep_clause["body"][:4000]  # cap input

    # 1. Hybrid search across SOW/MSA in same tenant.
    try:
        [rep_vec] = embed_texts([rep_text], model=settings.embedding_model)
    except Exception as e:
        log.warning("graph.embed_rep_failed", error=str(e))
        rep_vec = []

    hybrid_hits = []
    if rep_vec:
        try:
            hybrid_hits = hybrid_search(
                text=rep_text,
                vector=rep_vec,
                tenant_id=tenant_id,
                k=10,
                doc_types=["SOW", "MSA"],
                exclude_doc_id=doc_id,
                alpha=0.6,
            )
        except Exception as e:
            log.warning("graph.hybrid_search_failed", error=str(e))

    # 2. Structural-hash-prefix BM25.
    structural_doc_ids: set[str] = set()
    if structural_prefix:
        try:
            sh_hits = bm25_search(
                text=title or rep_text,
                tenant_id=tenant_id,
                k=20,
                doc_types=["SOW", "MSA"],
                exclude_doc_id=doc_id,
                structural_hash_prefix=structural_prefix,
            )
            for h in sh_hits:
                src = h.get("_source", {})
                if src.get("docId"):
                    structural_doc_ids.add(src["docId"])
        except Exception as e:
            log.warning("graph.structural_search_failed", error=str(e))

    # 3. Score each candidate.
    candidates: dict[str, dict[str, float]] = {}
    for h in hybrid_hits:
        did = h.get("docId")
        if not did:
            continue
        candidates.setdefault(did, {})["hybrid"] = float(h.get("score", 0.0))
    for did in structural_doc_ids:
        candidates.setdefault(did, {})["structural"] = 1.0

    # Title similarity needs the parent's meta from DDB.
    best: tuple[str | None, float, str] = (None, 0.0, "")
    for did, signals in candidates.items():
        meta = get_doc_meta(did)
        parent_title = (meta or {}).get("title", "") if meta else ""
        title_sim = title_similarity(title, parent_title) if parent_title else 0.0
        signals["title"] = title_sim

        combined = (
            WEIGHT_HYBRID * signals.get("hybrid", 0.0)
            + WEIGHT_STRUCTURAL * signals.get("structural", 0.0)
            + WEIGHT_TITLE * signals["title"]
        )

        reason_bits = []
        if signals.get("hybrid"):
            reason_bits.append(f"vector+BM25 {signals['hybrid']:.2f}")
        if signals.get("structural"):
            reason_bits.append(f"structural hash prefix match")
        if signals.get("title"):
            reason_bits.append(f"title similarity {signals['title']:.2f}")
        reason = " + ".join(reason_bits) or "weak signal"

        if combined > best[1]:
            best = (did, combined, reason)

    return best


def _pick_representative_clause(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose a representative clause for similarity search."""
    preferred = {"ScopeOfWork", "Definitions", "Term", "Fees"}
    for c in clauses:
        if c.get("category") in preferred and c.get("body"):
            return c
    # Fallback: longest body.
    return max(clauses, key=lambda c: len(c.get("body", "")))
