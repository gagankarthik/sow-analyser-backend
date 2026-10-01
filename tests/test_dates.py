"""Dates — parsed, normalised, resolved and validated in code.

The model output here is faked (hand-written dicts in the shape the extraction
schema returns). What is tested is everything that happens to those dates
afterwards: formats, ambiguity, relative rules, derived deadlines, validation,
de-duplication, ordering, citation and persistence.
"""
from __future__ import annotations

import pytest

from shared import dates
from shared.keydates import build_key_dates, compact_for_record, normalise_legacy_dates
from stages import persist


# ── Formats ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,iso", [
    ("2026-03-01", "2026-03-01"),
    ("2026/03/01", "2026-03-01"),
    ("2026-03-01T00:00:00Z", "2026-03-01"),
    ("1st March 2026", "2026-03-01"),
    ("1 March 2026", "2026-03-01"),
    ("March 1, 2026", "2026-03-01"),
    ("March 1st 2026", "2026-03-01"),
    ("Mar. 1, 2026", "2026-03-01"),
    ("1-Mar-2026", "2026-03-01"),
    ("01 Mar 26", "2026-03-01"),
    ("this 1st day of March, 2026", "2026-03-01"),
    ("on or before 15 January 2027", "2027-01-15"),
    ("31.12.2025", "2025-12-31"),          # only valid as DD.MM
    ("12/31/2025", "2025-12-31"),          # only valid as MM/DD
    ("13/01/2026", "2026-01-13"),
    ("05/05/2026", "2026-05-05"),          # same either way
])
def test_calendar_dates_normalise_to_iso(raw, iso):
    out = dates.parse_date(raw)
    assert out["date"] == iso and out["precision"] == "day" and not out["ambiguous"] and not out["estimated"]


def test_ambiguous_numeric_date_is_not_guessed():
    out = dates.parse_date("03/01/2026")
    assert out["date"] is None and out["ambiguous"] is True and out["reason"] == "ambiguous_day_month"
    # ...unless the document itself shows which convention it uses
    assert dates.parse_date("03/01/2026", day_first=True)["date"] == "2026-01-03"
    assert dates.parse_date("03/01/2026", day_first=False)["date"] == "2026-03-01"


@pytest.mark.parametrize("text,expected", [
    ("Signed 13/02/2026. Starts 03/04/2026.", True),        # 13 can only be a day
    ("Signed 02/13/2026. Starts 03/04/2026.", False),       # 13 can only be a day → month first
    ("Starts 03/04/2026.", None),                           # no evidence
    ("Signed 13/02/2026 and delivered 02/13/2026.", None),  # contradictory
    ("Dated 1 March 2026; ends 28 February 2027.", True),   # written day-first throughout
    ("Dated March 1, 2026; ends February 28, 2027.", False),
])
def test_day_month_order_is_taken_from_the_document(text, expected):
    assert dates.detect_day_first(text) is expected


@pytest.mark.parametrize("raw,iso,start,precision", [
    ("Q2 2026", "2026-06-30", "2026-04-01", "quarter"),
    ("third quarter of 2026", "2026-09-30", "2026-07-01", "quarter"),
    ("4th quarter 2026", "2026-12-31", "2026-10-01", "quarter"),
    ("H1 2027", "2027-06-30", "2027-01-01", "half"),
    ("March 2026", "2026-03-31", "2026-03-01", "month"),
    ("Feb 2028", "2028-02-29", "2028-02-01", "month"),      # leap year
    ("03/2026", "2026-03-31", "2026-03-01", "month"),
    ("2027", "2027-12-31", "2027-01-01", "year"),
])
def test_periods_resolve_to_their_last_day_and_are_marked_estimated(raw, iso, start, precision):
    out = dates.parse_date(raw)
    assert (out["date"], out["periodStart"], out["precision"], out["estimated"]) == (iso, start, precision, True)


@pytest.mark.parametrize("raw", ["2026-02-30", "31 April 2026", "32/13/2026", "February 30, 2026"])
def test_impossible_dates_are_rejected_not_rolled_over(raw):
    out = dates.parse_date(raw)
    assert out["date"] is None and out["reason"] == "invalid_date"


def test_unparseable_and_empty_input():
    assert dates.parse_date("as soon as practicable")["reason"] == "unparsed"
    assert dates.parse_date(None)["reason"] == "absent" and dates.parse_date("  ")["reason"] == "absent"


# ── Relative dates ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,value,unit,direction,anchor", [
    ("thirty (30) days after the Effective Date", 30, "days", 1, "effective"),
    ("30 days after the Effective Date", 30, "days", 1, "effective"),
    ("12 months from signature", 12, "months", 1, "signature"),
    ("twelve (12) months from the date of execution", 12, "months", 1, "signature"),
    ("Net 30 from invoice", 30, "days", 1, "invoice"),
    ("Net 45", 45, "days", 1, "invoice"),
    ("payment due within 45 days of receipt of invoice", 45, "days", 1, "invoice"),
    ("ninety (90) days prior to the end of the then-current Term", 90, "days", -1, "term_end"),
    ("sixty (60) days' written notice prior to the expiration", 60, "days", -1, "term_end"),
    ("twenty-one days before expiry", 21, "days", -1, "term_end"),
    ("within ten (10) Business Days of acceptance", 10, "business_days", 1, "acceptance"),
    ("one hundred and twenty days following delivery", 120, "days", 1, "delivery"),
    ("two (2) years from the Commencement Date", 2, "years", 1, "start"),
    ("six weeks after kick-off", 6, "weeks", 1, "start"),
])
def test_relative_rules_are_parsed(text, value, unit, direction, anchor):
    rule = dates.parse_offset(text)
    assert (rule["value"], rule["unit"], rule["direction"], rule["anchor"]) == (value, unit, direction, anchor)


def test_text_without_a_duration_is_not_a_rule():
    assert dates.parse_offset("upon acceptance of the final deliverable") is None
    assert dates.parse_offset("") is None


@pytest.mark.parametrize("words,value", [
    ("thirty", 30), ("twenty-one", 21), ("ninety", 90), ("one hundred and twenty", 120),
    ("a hundred", 100), ("three hundred sixty five", 365), ("blue", None),
])
def test_written_out_numbers(words, value):
    assert dates.words_to_int(words) == value


def test_calendar_arithmetic():
    assert dates.add_offset("2026-01-31", 1, "months") == "2026-02-28"       # clamps, no overflow
    assert dates.add_offset("2024-02-29", 1, "years") == "2025-02-28"
    assert dates.add_offset("2026-03-01", 12, "months") == "2027-03-01"
    assert dates.add_offset("2026-03-01", 30, "days", -1) == "2026-01-30"
    assert dates.add_offset("2026-03-06", 10, "business_days") == "2026-03-20"   # skips two weekends
    assert dates.add_offset("2026-03-02", 2, "weeks") == "2026-03-16"
    assert dates.add_offset("not a date", 1, "days") is None


# ── Key dates built from a classification ───────────────────────────────────


def classification(**over):
    base = {
        "docType": "SOW", "effectiveDate": "2026-03-01",
        "identification": {"executionDate": "1st March 2026",
                           "signatories": [{"name": "Jane Doe", "date": "2026-03-02"}]},
        "timeline": {"startDate": None, "endDate": None, "renewalDate": None, "renewalNoticeDays": 60,
                     "autoRenews": True, "phases": [], "milestones": []},
        "commercials": {"currency": "USD", "paymentTerms": "Net 30 from invoice", "paymentSchedule": []},
        "deliverables": [], "keyDatesRaw": [],
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


CLAUSES = [
    {"id": "c001", "number": "Preamble", "title": "Preamble", "category": "Other",
     "body": "This Statement of Work is effective 1 March 2026."},
    {"id": "c002", "number": "3.1", "title": "Milestones", "category": "Deliverables",
     "body": "Milestone 1 Design is due 15 April 2026 with a payment of $10,000."},
    {"id": "c003", "number": "4", "title": "Term", "category": "Term",
     "body": "The term runs for twelve (12) months from the Effective Date and renews automatically "
             "unless notice is given sixty (60) days prior to expiry."},
]


def by_kind(result, kind):
    return [k for k in result["keyDates"] if k["kind"] == kind]


def test_relative_term_end_and_notice_deadline_are_resolved():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "term_end", "label": "Initial term ends", "date": None,
         "rawText": "twelve (12) months from the Effective Date"}]), CLAUSES, "Dated 1 March 2026.")
    end = by_kind(out, "term_end")[0]
    assert end["date"] == "2027-03-01" and end["isDerived"] is True
    assert (end["anchor"], end["offsetValue"], end["offsetUnit"], end["offsetDays"]) == ("effective", 12, "months", 360)
    assert end["clauseNumber"] == "4" and end["clauseId"] == "c003" and end["sectionRef"] == "4 Term"
    notice = by_kind(out, "notice_deadline")[0]
    assert notice["date"] == "2026-12-31" and notice["isDerived"] is True      # term end − 60 days
    assert (notice["anchor"], notice["offsetDays"]) == ("term_end", -60)
    assert out["derived"]["termEndDate"] == "2027-03-01"


def test_rule_is_kept_when_its_anchor_is_unknown():
    out = build_key_dates(classification(effectiveDate=None, identification={"executionDate": None, "signatories": []},
                                         keyDatesRaw=[{"kind": "term_end", "label": "Term ends", "date": None,
                                                       "rawText": "12 months from the Effective Date"}]), CLAUSES)
    end = by_kind(out, "term_end")[0]
    assert end["date"] is None and end["issues"] == ["anchor_unknown"]
    assert (end["anchor"], end["offsetValue"], end["offsetUnit"]) == ("effective", 12, "months")
    assert end["rawText"] == "12 months from the Effective Date"                # the rule is not lost
    notice = by_kind(out, "notice_deadline")[0]
    assert notice["date"] is None and (notice["anchor"], notice["offsetDays"]) == ("term_end", -60)


def test_event_based_payment_rule_is_kept_as_a_rule():
    out = build_key_dates(classification(), CLAUSES)
    pay = by_kind(out, "payment")[0]
    assert pay["date"] is None and pay["recurring"] == "per invoice"
    assert (pay["anchor"], pay["offsetValue"], pay["offsetUnit"]) == ("invoice", 30, "days")
    assert pay["issues"] == ["event_based"]


def test_rule_anchored_on_a_date_in_the_same_sentence():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "deadline", "label": "Insurance certificate", "date": None,
         "rawText": "within 30 days after 1 March 2026"}]), CLAUSES)
    d = by_kind(out, "deadline")[0]
    assert d["date"] == "2026-03-31" and d["isDerived"] is True


def test_ambiguous_and_impossible_dates_are_kept_flagged_and_explained():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "deadline", "label": "Insurance certificate", "date": None, "rawText": "03/04/2026"},
        {"kind": "other", "label": "Review", "date": "2026-02-30", "rawText": "30 February 2026"},
        {"kind": "milestone", "label": "Go-live", "date": None, "rawText": "Q2 2026"},
    ]), CLAUSES, "No other dates here.")
    amb = by_kind(out, "deadline")[0]
    assert amb["date"] is None and amb["ambiguous"] is True and amb["rawText"] == "03/04/2026"
    assert amb["confidence"] == "low"
    bad = by_kind(out, "other")[0]
    assert bad["date"] is None and bad["issues"] == ["invalid_date"] and bad["rawText"] == "30 February 2026"
    q = by_kind(out, "milestone")[0]
    assert (q["date"], q["isEstimated"], q["precision"], q["periodStart"]) == ("2026-06-30", True, "quarter", "2026-04-01")
    assert any("day/month" in i for i in out["issues"]) and any("not a valid calendar date" in i for i in out["issues"])


def test_document_locale_resolves_what_would_otherwise_be_ambiguous():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "deadline", "label": "Certificate", "date": None, "rawText": "03/04/2026"}]),
        CLAUSES, "Signed on 13/02/2026 in London.")
    assert by_kind(out, "deadline")[0]["date"] == "2026-04-03" and out["dayFirst"] is True


def test_term_end_before_effective_date_is_flagged_not_dropped():
    out = build_key_dates(classification(timeline={"endDate": "2025-01-01"}), CLAUSES)
    end = by_kind(out, "term_end")[0]
    assert end["date"] == "2025-01-01" and "before_effective_date" in end["issues"] and end["confidence"] == "low"
    assert any("before the effective date" in i for i in out["issues"])


def test_twenty_five_milestones_none_dropped_sorted_and_cited():
    milestones = [{"name": f"Milestone {i}", "date": f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                   "payment": 1000.0 * i, "source": f"Milestone {i} is due"} for i in range(1, 26)]
    clauses = CLAUSES + [{"id": "c010", "number": "Schedule A 1", "title": "Milestones", "category": "Deliverables",
                          "body": "\n".join(f"Milestone {i} is due on the date stated, paying ${1000 * i:,}."
                                            for i in range(1, 26))}]
    raw = [{"kind": "milestone", "label": m["name"], "date": m["date"], "rawText": m["source"],
            "amount": m["payment"]} for m in milestones]                    # the model lists them too
    out = build_key_dates(classification(timeline={"milestones": milestones}, keyDatesRaw=raw), clauses)
    found = by_kind(out, "milestone")
    assert len(found) == 25                                                # none lost, none doubled
    assert {m["label"] for m in found} == {f"Milestone {i}" for i in range(1, 26)}
    assert all(m["amount"] == 1000.0 * int(m["label"].split()[1]) and m["currency"] == "USD" for m in found)
    assert all(m["clauseNumber"] == "Schedule A 1" for m in found)
    dated = [k["date"] for k in out["keyDates"] if k["date"]]
    assert dated == sorted(dated)                                          # chronological
    assert [k["date"] for k in out["keyDates"]][-1] is None or all(k["date"] for k in out["keyDates"])
    assert len({k["id"] for k in out["keyDates"]}) == len(out["keyDates"])  # stable unique ids


def test_duplicates_are_merged_and_the_structured_value_wins():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "effective", "label": "Agreement effective", "date": "2026-03-01", "rawText": "effective 1 March 2026"},
        {"kind": "signature", "label": "Execution date", "date": "2026-03-01", "rawText": "1st March 2026"},
    ]), CLAUSES)
    assert len(by_kind(out, "effective")) == 1
    eff = by_kind(out, "effective")[0]
    assert eff["rawText"] == "effective 1 March 2026" and eff["clauseNumber"] == "Preamble"
    assert eff["confidence"] == "high"
    assert [k["label"] for k in by_kind(out, "signature")] == ["Execution date", "Signed by Jane Doe"]


def test_amendment_effective_date_kind():
    out = build_key_dates(classification(docType="AMENDMENT", amendment={"amendmentType": "amendment"},
                                         keyDatesRaw=[{"kind": "effective", "label": "Effective", "date": "2026-03-01",
                                                       "rawText": "effective 1 March 2026"}]), CLAUSES)
    assert [k["kind"] for k in out["keyDates"] if k["date"] == "2026-03-01" and k["kind"] != "signature"] == [
        "amendment_effective"]


def test_every_entry_has_the_full_documented_shape():
    out = build_key_dates(classification(keyDatesRaw=[
        {"kind": "deadline", "label": "x", "date": None, "rawText": "30 days after the Effective Date"}]), CLAUSES)
    keys = {"id", "kind", "label", "date", "rawText", "precision", "isEstimated", "isDerived", "ambiguous",
            "anchor", "offsetValue", "offsetUnit", "offsetDays", "recurring", "amount", "currency",
            "clauseId", "clauseNumber", "sectionRef", "confidence", "issues", "origin", "periodStart"}
    assert all(set(k) == keys for k in out["keyDates"])
    assert all("status" not in k for k in out["keyDates"])          # past / upcoming is the reader's job
    assert all(k["id"].startswith("kd-") for k in out["keyDates"])
    # ids are stable from run to run
    again = build_key_dates(classification(keyDatesRaw=[
        {"kind": "deadline", "label": "x", "date": None, "rawText": "30 days after the Effective Date"}]), CLAUSES)
    assert [k["id"] for k in again["keyDates"]] == [k["id"] for k in out["keyDates"]]


def test_legacy_date_fields_are_normalised_only_when_unambiguous():
    result = classification(effectiveDate="1st March 2026",
                            timeline={"endDate": "03/04/2027", "renewalDate": "March 1, 2027"},
                            identification={"executionDate": "Q1 2026"})
    normalise_legacy_dates(result, "No numeric evidence.")
    assert result["effectiveDate"] == "2026-03-01" and result["timeline"]["renewalDate"] == "2027-03-01"
    assert result["timeline"]["endDate"] == "03/04/2027"            # ambiguous → left exactly as it was
    assert result["identification"]["executionDate"] == "Q1 2026"   # not a day → left as it was


# ── Persistence: nothing lost between extraction and the record ─────────────


@pytest.fixture
def persisted(monkeypatch):
    written = {"fields": None, "remove": None, "version": None}
    monkeypatch.setattr(persist, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(persist, "query_doc_versions", lambda _id: [])
    monkeypatch.setattr(persist, "get_doc_meta", lambda _id: {"docId": "d", "tenantId": "acme", "title": "upload"})
    monkeypatch.setattr(persist, "put_version", lambda v: written.__setitem__("version", v))
    monkeypatch.setattr(persist, "put_change", lambda c: None)
    monkeypatch.setattr(persist, "update_doc_fields",
                        lambda doc_id, fields, remove=(): written.update(fields=fields, remove=list(remove)))

    def run(cls):
        persist.run({"docId": "d", "tenantId": "acme", "rawKey": "k", "classification": cls,
                     "parsed": {"checksum": "c", "extraction_method": "docx", "stats": {"pages": 3}}})
        return written
    return run


def test_key_dates_reach_the_document_record_and_agree_with_the_legacy_fields(persisted):
    cls = classification(keyDatesRaw=[{"kind": "term_end", "label": "Term ends", "date": None,
                                       "rawText": "twelve (12) months from the Effective Date"}])
    built = build_key_dates(cls, CLAUSES, "Dated 1 March 2026.")
    cls["keyDates"] = built["keyDates"]
    cls["timeline"]["endDate"], cls["timeline"]["endDateDerived"] = built["derived"]["termEndDate"], True
    fields = persisted(cls)["fields"]
    assert len(fields["keyDates"]) == len(built["keyDates"]) and fields["keyDatesTruncated"] is False
    assert fields["keyDateCount"] == len(built["keyDates"])
    by = {k["kind"]: k for k in fields["keyDates"]}
    assert fields["effectiveDate"] == by["effective"]["date"] == "2026-03-01"
    assert fields["termEndDate"] == by["term_end"]["date"] == "2027-03-01" and fields["termEndDateDerived"] is True
    assert fields["renewalNoticeDays"] == 60 and by["notice_deadline"]["offsetDays"] == -60
    assert fields["autoRenews"] is True


def test_record_copy_is_size_bounded_and_says_when_it_is_cut():
    many = [{"id": f"kd-{i}", "kind": "milestone", "label": f"M{i}", "date": "2026-01-01",
             "rawText": "x" * 300} for i in range(2000)]
    kept, truncated = compact_for_record(many, max_bytes=20_000)
    assert truncated is True and 0 < len(kept) < 2000
    assert all(len(k["rawText"]) <= 160 for k in kept)
    small, cut = compact_for_record(many[:5])
    assert cut is False and len(small) == 5


def test_property_no_input_date_is_lost():
    """Randomised: every date or rule handed to the builder comes out again —
    resolved, or kept with its raw text and a reason. Merging may combine two
    entries for the same event, but never drops the date itself."""
    import random

    random.seed(20261001)
    raws = ["2026-03-01", "1st March 2026", "03/04/2026", "13/04/2026", "Q2 2026", "March 2027", "2026-02-30",
            "30 days after the Effective Date", "twelve (12) months from signature", "Net 45",
            "upon acceptance", "sixty (60) days prior to expiry", "within 10 Business Days of delivery",
            "31 December 2026", "2027", "next Tuesday"]
    kinds = ["effective", "signature", "start", "term_end", "renewal", "notice_deadline", "milestone",
             "deliverable", "payment", "acceptance", "sla_reporting", "deadline", "other"]
    for _ in range(400):
        items = [{"kind": random.choice(kinds), "label": f"Item {i}", "date": None, "rawText": random.choice(raws),
                  "amount": random.choice([None, 100.0 * i])} for i in range(random.randint(1, 30))]
        out = build_key_dates(classification(keyDatesRaw=items, timeline={"renewalNoticeDays": None},
                                             commercials={"paymentTerms": None}), CLAUSES, "Signed 13/02/2026.")
        raw_out = {k["rawText"] for k in out["keyDates"]}
        dates_out = {k["date"] for k in out["keyDates"]}
        for item in items:
            # either the entry is there with its own words, or it was merged into
            # another entry for the SAME calendar date
            same_day = dates.parse_date(item["rawText"], day_first=True)["date"]
            assert item["rawText"] in raw_out or (same_day and same_day in dates_out), (item, out["keyDates"])
        for k in out["keyDates"]:
            assert k["date"] or k["issues"], k                 # undated always says why
            assert k["date"] is None or len(k["date"]) == 10
        dated = [k["date"] for k in out["keyDates"] if k["date"]]
        assert dated == sorted(dated)
