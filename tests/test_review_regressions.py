"""Regressions found by an independent review of the extraction code — each is
an input that used to mis-segment a contract, change a correct figure, or merge
two distinct dates."""
from __future__ import annotations

from shared import dates, money
from shared.keydates import build_key_dates
from shared.segment import segment_document
from stages import classify, parse


def numbers(text: str) -> list[str]:
    return [s.number for s in segment_document(text)["segments"]]


def test_a_reference_to_a_schedule_does_not_re_scope_the_rest_of_the_contract():
    text = ("1. Scope\nThe services are described in\nSchedule A (Statement of Work).\n2. Fees\nPay.\n"
            "3. Term\nOne year.\n4. Law\nEngland.\n")
    assert numbers(text) == ["1", "2", "3", "4"]
    toc = ("CONTENTS\n1. Scope ........ 2\n2. Fees ........ 3\nSchedule B - Service Levels ........ 11\n\n"
           "1. Scope\nWork.\n2. Fees\nPay.\nSchedule B - Service Levels\n1. Uptime\n99.9%.\n")
    assert numbers(toc) == ["Preamble", "1", "2", "Schedule B 1"]


def test_a_contents_list_followed_by_the_preamble_is_still_a_contents_list():
    text = ("1. Definitions 3\n2. Services 4\n3. Fees 5\n4. Term 6\n5. Liability 7\n"
            "This Agreement is made on 1 March 2026 between A and B.\n"
            "1. Definitions\nTerms.\n2. Services\nWork.\n3. Fees\nPay.\n4. Term\nOne year.\n5. Liability\nCapped.\n")
    by = {s.number: s for s in segment_document(text)["segments"]}
    assert list(by) == ["Preamble", "1", "2", "3", "4", "5"]
    assert by["5"].body == "Capped." and "This Agreement is made" in by["Preamble"].body


def test_a_wrapped_line_starting_with_a_number_and_a_capital_is_not_a_clause():
    text = ("1. Payment\nInvoices are payable within\n5 Business Days of the invoice date.\n2. Notices\n"
            "Notices go to\n2 Park Avenue, New York.\n3. Date\nDated\n1 January 2024\n4. Term\nOne year.\n5. Law\nX.\n")
    assert numbers(text) == ["1", "2", "3", "4", "5"]


def test_three_decimals_before_a_scale_word_is_a_decimal_not_grouping():
    assert money.find_amounts("USD 1.125 million")[0]["amount"] == 1_125_000.0
    assert classify._fix_scale(1125000, "The fee is USD 1.125 million.") is None      # correct value left alone
    assert classify._fix_scale(1.2, "USD 1.2 million") == 1_200_000.0
    assert classify._fix_scale(1200, "The fee is $1.200 in total") is None            # ambiguous → not touched


def test_a_dash_before_an_amount_is_not_a_minus_sign():
    assert money.find_amounts("Milestone 1 – $10,000")[0]["amount"] == 10000.0
    assert money.find_amounts("Milestone 1 - $10,000")[0]["amount"] == 10000.0
    assert money.find_amounts("a credit of -$3,000")[0]["amount"] == -3000.0
    assert money.find_amounts("CAD$ 5,000")[0]["currency"] == "CAD"


def test_distinct_events_on_the_same_day_are_not_merged():
    cls = {"docType": "SOW", "effectiveDate": None,
           "identification": {"executionDate": None, "signatories": [
               {"name": "Alice", "date": "2026-03-01"}, {"name": "Bob", "date": "2026-03-01"}]},
           "timeline": {"milestones": [
               {"name": "Design sign-off", "date": "2026-06-30", "payment": 5000.0, "source": None},
               {"name": "Build complete", "date": "2026-06-30", "payment": 5000.0, "source": None}]},
           "commercials": {"paymentSchedule": [
               {"label": "Instalment 1", "amount": 2500.0, "trigger": "Upon acceptance"},
               {"label": "Instalment 2", "amount": 2500.0, "trigger": "Upon acceptance"}]},
           "deliverables": [], "keyDatesRaw": []}
    out = build_key_dates(cls, [], "")["keyDates"]
    labels = [k["label"] for k in out]
    assert len(out) == 6
    assert {"Signed by Alice", "Signed by Bob", "Design sign-off", "Build complete",
            "Instalment 1", "Instalment 2"} == set(labels)


def test_clause_numbers_are_not_read_as_dates():
    assert dates.detect_day_first("See clause 13.1.10 and clause 14.2.11. Due 03/04/2026.") is None
    assert dates.parse_date("as set out in Clause 12.3.10")["date"] is None
    assert dates.parse_date("31.12.2025")["date"] == "2025-12-31"                     # a real dotted date
    assert dates.parse_date("April 1, 2024 through 31 March 2025")["date"] == "2024-04-01"
    assert dates.parse_date("the first day of January 2026") == {
        "date": "2026-01-01", "precision": "day", "ambiguous": False, "estimated": False,
        "periodStart": None, "reason": None}
    out = build_key_dates({"docType": "SOW", "effectiveDate": "2026-01-01", "keyDatesRaw": [
        {"kind": "term_end", "label": "End", "date": None, "rawText": "x", "anchor": "the Effective Date",
         "offsetValue": 1.5, "offsetUnit": "years", "offsetDirection": "after"}]}, [], "")
    assert next(k for k in out["keyDates"] if k["kind"] == "term_end")["date"] == "2027-07-01"


def test_numbered_headings_at_the_top_of_pages_are_not_stripped_as_headers():
    pages = [{"page": i, "text": f"Appendix {i}\nContent of appendix {i} {'abcde'[i - 1]}.\nlast {'vwxyz'[i - 1]}",
              "char_count": 0} for i in range(1, 6)]
    assert parse.strip_repeating_lines(pages) == 0
    assert all(p["text"].startswith(f"Appendix {p['page']}") for p in pages)


def test_merging_windows_does_not_turn_unanswered_into_false():
    blank = {"timeline": {"autoRenews": None}, "amendment": {"everythingElseStays": None},
             "confidence": {"parentFound": None}}
    merged = classify._merge_windows([dict(blank), dict(blank)])
    assert merged["timeline"]["autoRenews"] is None
    assert merged["amendment"]["everythingElseStays"] is None and merged["confidence"]["parentFound"] is None
    no = {"timeline": {"autoRenews": False}}
    assert classify._merge_windows([dict(blank), no])["timeline"]["autoRenews"] is False
    assert classify._merge_windows([no, {"timeline": {"autoRenews": True}}])["timeline"]["autoRenews"] is True


def test_a_reduction_reconciles_once_its_sign_is_fixed():
    result = {
        "commercials": {"baseValue": 100000.0, "totalContractValue": None},
        "amendment": {"amendmentType": "amendment", "valueDelta": 20000.0, "newTotalValue": 80000.0},
        "validation": {"validated": True, "lineItems": [], "issues": [], "reconciled": False,
                       "reconciliation": {"explanation": "Base 100000 + delta 20000 = 120000, but the stated total is 80000. Figures do not reconcile."}},
    }
    result["validation"]["issues"] = [result["validation"]["reconciliation"]["explanation"]]
    classify._check_money(result, [], "The fee is reduced from $100,000 to $80,000.")
    assert result["amendment"]["valueDelta"] == -20000.0
    assert result["validation"]["reconciled"] is True
    assert not any("do not reconcile" in i for i in result["validation"]["issues"])
