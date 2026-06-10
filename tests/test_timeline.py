"""Stage 06 timeline — clause-state replay + in-force lifecycle gating."""
from __future__ import annotations

from stages import timeline


def _initial_state():
    clauses = [
        {"number": "1", "title": "Scope", "body": "Build the website.", "category": "ScopeOfWork"},
        {"number": "2", "title": "Fees", "body": "Total fee is $7,500.", "category": "Fees"},
        {"number": "3", "title": "Term", "body": "Six months.", "category": "Term"},
    ]
    return timeline._state_from_clauses(clauses)


def test_apply_modification_preserves_other_clauses():
    state = timeline._clone(_initial_state())
    changes = [
        {"clauseNumber": "2", "field": "body", "after": "Total fee is $10,500."}
    ]
    timeline._apply(state, changes)
    # The modified clause updated...
    assert state["2"]["body"] == "Total fee is $10,500."
    # ...and the untouched clauses survived (the core regression).
    assert "1" in state and "3" in state
    assert state["1"]["body"] == "Build the website."


def test_apply_deletion_pops_clause():
    state = timeline._clone(_initial_state())
    timeline._apply(state, [{"clauseNumber": "3", "field": "body", "after": ""}])
    assert "3" not in state
    assert "1" in state and "2" in state


def test_in_force_lifecycles_include_signed():
    # A signed/executed amendment must count as in force (was: only "active").
    assert "signed" in timeline._IN_FORCE_LIFECYCLES
    assert "active" in timeline._IN_FORCE_LIFECYCLES
    # Pending lifecycles must NOT be in force.
    for pending in ("draft", "review", "negotiation", "approval"):
        assert pending not in timeline._IN_FORCE_LIFECYCLES


def test_norm_clause_number():
    assert timeline._norm(" §7.4 ") == "7.4"
    assert timeline._norm("Section 2") == "section2"


def test_diff_then_timeline_replay_reconstructs_current_state():
    """End-to-end: an amendment delta diffed, then replayed onto the SOW state,
    must yield a current state with the fee updated and ALL other clauses intact."""
    from stages import diff

    parent_clauses = [
        {"number": "1", "title": "Scope", "body": "Build the website.", "category": "ScopeOfWork"},
        {"number": "2", "title": "Fees", "body": "Total fee is $7,500.", "category": "Fees"},
        {"number": "3", "title": "Term", "body": "Six months.", "category": "Term"},
    ]
    amendment_changes = [{
        "changeType": "modification", "category": "value", "targetSection": "Fees",
        "before": "Total fee is $7,500.", "after": "Total fee is $10,500.",
        "summary": "Fee increased by $3,000.",
    }]

    changes = diff._diff_amendment(amendment_changes, parent_clauses)

    state = timeline._clone(timeline._state_from_clauses(parent_clauses))
    timeline._apply(state, changes)

    assert state["2"]["body"] == "Total fee is $10,500."  # updated
    assert state["1"]["body"] == "Build the website."     # preserved
    assert state["3"]["body"] == "Six months."            # preserved
    assert len(state) == 3                                 # nothing deleted
