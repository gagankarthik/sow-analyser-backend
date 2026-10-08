"""An amendment is its own piece of work (reviewed and signed), linked to the
agreement it amends: its value is the change, and signing it updates the
parent, so money is never counted twice."""
from __future__ import annotations

from govern_intake import handler as intake
from govern_support import analysed_event, install_analysis, seed_doc
from shared.govern import store, workflow

DANA = {"email": "dana@northfield.edu", "name": "Dana Ruiz"}


def test_an_amendment_links_to_its_parent_and_counts_only_its_change(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1", value=500_000, termEndDate="2027-06-30")
    intake.handle_event(analysed_event("lic-1"))
    seed_doc(ddb, "amd-1", doc_type="AMENDMENT", parentDocId="lic-1", valueDelta=150_000, newTotalValue=650_000,
             termEndDate="2028-06-30", currency="USD")
    intake.handle_event(analysed_event("amd-1"))

    amd = store.contracts.get("amd-1")
    parent = store.contracts.get("lic-1")
    assert amd["parentContractId"] == "lic-1"
    assert amd["extractedValue"] == 150_000                   # the change, not the restated total
    assert parent["amendmentIds"] == ["amd-1"]
    assert parent["extractedValue"] == 500_000                # unchanged until the amendment is signed

    store.contracts.mutate("amd-1", lambda c: c.update(state="ready_to_sign", stage="approval"))
    workflow.perform_action("amd-1", "mark_signed", {}, DANA)

    parent = store.contracts.get("lic-1")
    assert parent["extractedValue"] == 650_000
    assert parent["termEndDate"] == "2028-06-30"
    assert "amendment_signed" in gov.actions("lic-1")
    api = workflow.to_api(parent)
    assert api["amendmentIds"] == ["amd-1"] and api["parentContractId"] is None
