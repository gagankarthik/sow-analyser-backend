"""Capture completeness — Govern must never miss a contract silently.

Two questions, one module (used by ``GET /reports/capture`` and by the hourly
reconciliation in govern-sweeper):

* ``missed_documents`` — which documents have no contract: an analysed
  (READY) document intake never turned into one, a revision that never got
  linked, an analysis that FAILED, or a run that stalled;
* ``gap_summary`` — which fields are still unknown across contracts
  (``captureGaps`` of each contract, rolled up).

The reconciliation result is kept in govern-sync: ``T#<tenant> / RECONCILE``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import store
from .store import iso, parse_iso
from .workflow import CAPTURE_GAPS, capture_gaps

_IN_FLIGHT = frozenset({"PENDING", "PARSING", "CLASSIFYING", "EMBEDDING", "GRAPHING", "DIFFING",
                        "TIMELINING", "PERSISTING"})
_STALLED_AFTER = timedelta(hours=1)


def covered_doc_ids(contracts: Iterable[dict[str, Any]]) -> set[str]:
    """Every document id a contract accounts for (its own, its versions, a
    revision still being read)."""
    out: set[str] = set()
    for c in contracts:
        out.add(c["contractId"])
        out.update(c.get("versionDocIds") or [])
        if c.get("pendingDocId"):
            out.add(c["pendingDocId"])
    return out


def _public_error(raw: Any) -> str:
    text = str(raw or "").strip()
    start = text.find("{")
    if start != -1:
        import json
        try:
            cause = json.loads(text[start:])
            if isinstance(cause, dict) and cause.get("errorMessage"):
                text = str(cause["errorMessage"])
        except ValueError:
            text = text[:start].rstrip(": ")
    return text[:300]


def missed_documents(docs: Iterable[dict[str, Any]], contracts: Iterable[dict[str, Any]],
                     now: datetime | None = None) -> list[dict[str, Any]]:
    """``{docId, title, status, reason}`` for every document Govern has not
    captured, oldest first."""
    now = now or datetime.now(timezone.utc)
    contracts = list(contracts)
    covered = covered_doc_ids(contracts)
    out = []
    for d in docs:
        doc_id = d.get("docId")
        if not doc_id or doc_id in covered:
            continue
        status = str(d.get("status") or "").upper()
        if status == "READY":
            reason = ("A revised version that was never linked to its contract." if d.get("revisionOf")
                      else "Analysed, but no contract was created for it.")
        elif status == "FAILED":
            message = _public_error(d.get("errorMessage"))
            reason = "The analysis failed" + (f": {message}" if message else ".")
        elif status in _IN_FLIGHT:
            updated = parse_iso(str(d.get("updatedAt") or ""))
            if updated is None or now - updated < _STALLED_AFTER:
                continue
            reason = "The analysis has not finished for over an hour."
        else:
            continue
        out.append({"docId": doc_id, "title": d.get("title") or "", "status": status or "UNKNOWN",
                    "reason": reason, "_createdAt": d.get("createdAt") or ""})
    out.sort(key=lambda m: (m.pop("_createdAt"), m["docId"]))
    return out


def requeue_candidates(docs: Iterable[dict[str, Any]], contracts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """READY documents with no contract — intake can still create (or link) them."""
    covered = covered_doc_ids(contracts)
    return [d for d in docs if d.get("docId") and d["docId"] not in covered
            and str(d.get("status") or "").upper() == "READY"]


def gap_summary(contracts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """``{gap, count, contractIds}`` per CaptureGap present, in the API's order."""
    by_gap: dict[str, list[str]] = defaultdict(list)
    for c in contracts:
        for gap in capture_gaps(c):
            by_gap[gap].append(c["contractId"])
    return [{"gap": g, "count": len(by_gap[g]), "contractIds": by_gap[g]} for g in CAPTURE_GAPS if by_gap.get(g)]


def get_reconcile_state(tenant_id: str) -> dict[str, Any]:
    return store.sync.reconcile_state(tenant_id)


def put_reconcile_state(tenant_id: str, *, requeued: list[str], missed: list[dict[str, Any]],
                        now: datetime | None = None) -> None:
    store.sync.put_reconcile_state(tenant_id, {"lastReconciledAt": iso(now), "requeued": requeued[:200],
                                               "missed": missed[:200]})
