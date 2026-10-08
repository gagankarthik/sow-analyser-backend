"""An untouched built-in matrix follows the built-in default; a matrix a
person saved is never changed behind their back."""
from __future__ import annotations

import copy

from shared.govern import matrix as m
from shared.govern import store

TENANT = "t-upgrade"


def _seed_old_default(gov, created_by=None) -> None:
    """Version 1 as an older build seeded it: system-created, older wording."""
    old = copy.deepcopy(m.default_matrix())
    first = next(iter(old["playbooks"].values()))["clauses"][0]
    first["standard"] = "Older built-in wording"
    store.config._put_matrix(TENANT, {
        "version": 1, "effectiveDate": "2026-01-01", "createdAt": "2026-01-01T00:00:00Z",
        "createdBy": created_by, "note": None, "homeState": "Minnesota", "playbooks": old["playbooks"],
    })
    gov.config.items[(f"T#{TENANT}", "MATRIX#CURRENT")] = {
        "PK": f"T#{TENANT}", "SK": "MATRIX#CURRENT", "entityType": "MATRIX_POINTER", "version": 1,
    }


def test_untouched_old_default_is_upgraded_as_a_new_version(gov):
    _seed_old_default(gov)
    current = store.config.current_matrix(TENANT)
    assert current["version"] == 2
    assert current["createdBy"] is None
    assert current["playbooks"] == m.default_matrix()["playbooks"]
    assert current["homeState"] == "Minnesota"
    # History is kept: version 1 is still there, unchanged.
    v1 = store.config.matrix_version(TENANT, 1)
    assert v1 is not None
    assert next(iter(v1["playbooks"].values()))["clauses"][0]["standard"] == "Older built-in wording"
    # A second read does not upgrade again.
    assert store.config.current_matrix(TENANT)["version"] == 2


def test_a_matrix_a_person_saved_is_never_upgraded(gov):
    _seed_old_default(gov, created_by={"email": "a@example.com", "name": "A"})
    current = store.config.current_matrix(TENANT)
    assert current["version"] == 1
    assert next(iter(current["playbooks"].values()))["clauses"][0]["standard"] == "Older built-in wording"
