"""Stage 05 diff — amendment-delta diffing vs. full re-version diffing.

The headline bug these tests guard: an amendment is a DELTA document, so diffing
its handful of clauses against the full parent must NOT report every untouched
parent clause as a deletion.
"""
from __future__ import annotations

from stages import diff


def _parent_clauses():
    return [
        {"number": "1", "title": "Scope", "body": "Build the website.", "category": "ScopeOfWork"},
        {"number": "2", "title": "Fees", "body": "Total fee is $7,500.", "category": "Fees"},
        {"number": "3", "title": "Term", "body": "Six months.", "category": "Term"},
        {"number": "4", "title": "Confidentiality", "body": "Mutual NDA.", "category": "Confidentiality"},
    ]


# ── Amendment delta diffing ──────────────────────────────────────────────────


def test_amendment_does_not_delete_untouched_parent_clauses():
    amendment_changes = [
        {
            "changeType": "modification",
            "category": "value",
            "targetSection": "Fees",
            "before": "Total fee is $7,500.",
            "after": "Total fee is $10,500.",
            "summary": "Fee increased by $3,000 for ATS integration.",
        }
    ]
    changes = diff._diff_amendment(amendment_changes, _parent_clauses())

    # Exactly one change, and it targets the Fees clause — no phantom deletions.
    assert len(changes) == 1
    ch = changes[0]
    assert ch["clauseNumber"] == "2"          # matched parent clause by title
    assert ch["after"] == "Total fee is $10,500."
    assert ch["before"] == "Total fee is $7,500."
    # No change has after == "" (which the timeline would treat as a deletion).
    assert all(c["after"] != "" for c in changes)


def test_amendment_addition_keeps_stable_synthetic_key():
    amendment_changes = [
        {
            "changeType": "addition",
            "category": "sla",
            "targetSection": "Service Levels",   # not present in parent
            "before": None,
            "after": "99.9% uptime guarantee.",
            "summary": "Added an SLA.",
        }
    ]
    changes = diff._diff_amendment(amendment_changes, _parent_clauses())
    assert len(changes) == 1
    # Unmatched target → keyed by the target section (or synthetic), never blank,
    # and never an empty 'after' that would delete state.
    assert changes[0]["clauseNumber"]
    assert changes[0]["after"] == "99.9% uptime guarantee."


def test_amendment_deletion_only_when_parent_matched():
    amendment_changes = [
        {
            "changeType": "deletion",
            "category": "other",
            "targetSection": "Confidentiality",
            "before": "Mutual NDA.",
            "after": None,
            "summary": "Removed the confidentiality clause.",
        }
    ]
    changes = diff._diff_amendment(amendment_changes, _parent_clauses())
    assert len(changes) == 1
    # A matched deletion produces after == "" so the timeline pops the clause.
    assert changes[0]["clauseNumber"] == "4"
    assert changes[0]["after"] == ""


# ── Full re-version diffing (the fallback path) ─────────────────────────────


def test_reversion_detects_modification_and_deletion():
    current = [
        {"number": "1", "title": "Scope", "body": "Build the website AND the app.", "category": "ScopeOfWork"},
        {"number": "2", "title": "Fees", "body": "Total fee is $7,500.", "category": "Fees"},
        {"number": "3", "title": "Term", "body": "Six months.", "category": "Term"},
        # clause 4 removed in this complete re-version
    ]
    changes = diff._diff(current, _parent_clauses())
    fields = {(c["clauseNumber"], c["field"]) for c in changes}
    # Clause 1 body changed.
    assert ("1", "body") in fields
    # Clause 4 deleted (after == "").
    deleted = [c for c in changes if c["clauseNumber"] == "4"]
    assert deleted and deleted[0]["after"] == ""


def test_reversion_guard_skips_deletions_for_sparse_extraction():
    # A near-empty "current" (e.g. a half-failed extraction) must NOT wipe the
    # parent by reporting every clause as deleted.
    current = [{"number": "1", "title": "Scope", "body": "Build the website.", "category": "ScopeOfWork"}]
    changes = diff._diff(current, _parent_clauses())
    assert all(c["after"] != "" for c in changes)  # no deletions emitted
