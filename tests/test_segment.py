"""Clause segmentation — every part of the document lands in a clause.

Covers the numbering schemes, the parts that used to fall through (preamble,
definitions, schedules, signature block, tables of contents), nested numbering,
long clauses, page breaks, and the coverage measure itself.
"""
from __future__ import annotations

import pytest

from shared.segment import paragraph_segments, segment_document, unique_numbers
from shared.text import coverage_ratio

CONTRACT = """MASTER SERVICES AGREEMENT
This Agreement is made on 1 March 2026 between Acme Ltd ("Client") and Globex Inc ("Supplier").

TABLE OF CONTENTS
1. Definitions ........ 2
2. Services ........ 3

1. DEFINITIONS
In this Agreement the following terms apply.
(a) "Affiliate" means any entity controlling a party.
(b) "Fees" means the fees in Schedule A.

2. SERVICES
2.1 Scope. The Supplier shall provide the Services described in
Schedule A.
2.2 The Supplier shall deliver within
30 days of the Effective Date.
2.2.1 Each deliverable is subject to acceptance.

3. FEES
3.1 Payment Terms. Client shall pay USD 1.2 million within thirty (30) days of invoice.
3.2 Late Payment
Interest accrues at 1.5% per month.

4. TERM AND TERMINATION
4.1 This Agreement commences on the Effective Date and continues for 12 months.
4.2 Either party may terminate for convenience on 60 days notice.

5. Reserved

6. GOVERNING LAW
This Agreement is governed by the laws of England.

IN WITNESS WHEREOF the parties have signed.
Signed by: Jane Doe, Director, 1 March 2026

SCHEDULE A - FEE SCHEDULE
1. Milestones
Milestone 1  Design  $10,000
Milestone 2  Build  $20,000
2. Expenses
Expenses are reimbursed at cost.
"""


def _segments(text: str, **kw):
    return segment_document(text, **kw)["segments"]


def _by_number(text: str, **kw):
    return {s.number: s for s in _segments(text, **kw)}


def _coverage(text: str, segments) -> float:
    parts: list[str] = []
    for s in segments:
        parts += [s.heading, s.body, s.section or ""]
    return coverage_ratio(text, parts)


def test_whole_contract_is_covered_and_structured():
    result = segment_document(CONTRACT)
    assert result["method"] == "headings"
    numbers = [s.number for s in result["segments"]]
    assert numbers == ["Preamble", "1", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5", "6",
                       "Signatures", "Schedule A 1", "Schedule A 2"]
    assert _coverage(CONTRACT, result["segments"]) == 1.0


def test_text_before_the_first_heading_is_a_preamble_clause():
    pre = _by_number(CONTRACT)["Preamble"]
    assert pre.kind == "preamble"
    assert "1 March 2026" in pre.body and "Globex Inc" in pre.body      # parties and date survive


def test_table_of_contents_is_not_mistaken_for_clauses():
    by = _by_number(CONTRACT)
    assert "TABLE OF CONTENTS" in by["Preamble"].body
    assert by["1"].title == "DEFINITIONS" and "Affiliate" in by["1"].body   # the real clause 1


def test_table_of_contents_without_dot_leaders():
    text = ("AGREEMENT\n1. Definitions 3\n2. Services 4\n3. Fees 5\n\n"
            "1. Definitions\nTerms are defined here.\n2. Services\nServices are provided.\n3. Fees\nFees are due.\n")
    by = _by_number(text)
    assert list(by) == ["Preamble", "1", "2", "3"]
    assert by["1"].body == "Terms are defined here."
    assert "Definitions 3" in by["Preamble"].body


def test_sub_clauses_are_their_own_clauses_with_a_parent():
    by = _by_number(CONTRACT)
    assert by["2.1"].parent == "2" and by["2.2"].parent == "2"
    assert by["2.1"].section == "2. SERVICES"                 # the heading is kept as context
    assert by["2.1"].title == "Scope"
    assert by["2.1"].body.startswith("The Supplier shall provide")


def test_a_heading_with_no_text_is_not_emitted_as_an_empty_clause():
    segments = _segments(CONTRACT)
    assert all(s.body.strip() for s in segments)
    assert "2" not in {s.number for s in segments}             # "2. SERVICES" lives on as the section
    assert "3" not in {s.number for s in segments}
    reserved = _by_number(CONTRACT)["5"]                       # a leaf heading keeps itself as its body
    assert reserved.body == "5. Reserved"


def test_deeper_numbering_and_lettered_items_are_addressable_inside_their_clause():
    by = _by_number(CONTRACT)
    assert [s["ref"] for s in by["1"].subclauses] == ["1(a)", "1(b)"]
    assert [s["ref"] for s in by["2.2"].subclauses] == ["2.2.1"]
    item = by["1"].subclauses[1]
    assert by["1"].body[item["start"]:item["end"]].startswith('(b) "Fees" means')
    assert item["parent"] == "1"


def test_a_wrapped_line_starting_with_a_number_is_not_a_clause():
    by = _by_number(CONTRACT)
    assert "30" not in by
    assert "30 days of the Effective Date" in by["2.2"].body


def test_schedules_are_captured_and_their_numbering_is_scoped():
    by = _by_number(CONTRACT)
    assert by["Schedule A 1"].title == "Milestones"
    assert "$20,000" in by["Schedule A 1"].body                  # the fee table is not lost
    assert by["Schedule A 1"].section == "SCHEDULE A - FEE SCHEDULE"       # as written
    assert by["Schedule A 2"].parent == "Schedule A"
    # no collision with the main body's clause 1
    assert by["1"].title == "DEFINITIONS"


def test_fee_table_rows_are_not_split_into_clauses():
    text = ("1. Scope\nBuild it.\n2. Fees\nThe fees are:\n3 Deployment $5,000\n4 Support $2,000\n"
            "3. Term\nOne year.\n")
    by = _by_number(text)
    assert list(by) == ["1", "2", "3"]
    assert "3 Deployment $5,000" in by["2"].body and "4 Support $2,000" in by["2"].body


def test_signature_block_is_its_own_clause():
    sig = _by_number(CONTRACT)["Signatures"]
    assert sig.kind == "signature" and "Jane Doe" in sig.body
    assert "Jane Doe" not in _by_number(CONTRACT)["6"].body


@pytest.mark.parametrize("text,expected", [
    ("Article 1 Definitions\nA.\nArticle 2 Services\nB.\nArticle 3 Fees\nC.\n", ["1", "2", "3"]),
    ("Section 1. Definitions\nA.\nSection 1.1 Terms\nB.\nSection 2. Fees\nC.\n", ["1", "1.1", "2"]),
    ("ARTICLE I DEFINITIONS\nA.\nARTICLE II SERVICES\nB.\nARTICLE III FEES\nC.\n", ["I", "II", "III"]),
    ("I. DEFINITIONS\nA.\nII. SERVICES\nB.\nIII. FEES\nC.\n", ["I", "II", "III"]),
    ("§1 Definitions\nA.\n§2 Services\nB.\n", ["1", "2"]),
    ("1) Definitions\nA.\n2) Services\nB.\n", ["1", "2"]),
    ("Clause 1: Definitions\nA.\nClause 2: Services\nB.\n", ["1", "2"]),
])
def test_numbering_schemes(text, expected):
    assert [s.number for s in _segments(text)] == expected


def test_number_alone_on_its_line_takes_the_next_line_as_title():
    text = "1\nDefinitions\nTerms are defined.\n2\nServices\nWork is done.\n"
    by = _by_number(text)
    assert by["1"].title == "Definitions" and by["1"].body == "Terms are defined."
    assert by["2"].title == "Services"


def test_unnumbered_document_uses_caps_and_colon_headings():
    text = ("CONSULTING AGREEMENT\nACME INC.\n\nThis agreement is between Acme Inc. and Bob.\n\n"
            "SCOPE OF WORK\nBob will build the site.\n\nFEES\nAcme will pay $5,000.\n\nTerm:\nSix months.\n")
    segments = _segments(text)
    assert [(s.number, s.title) for s in segments] == [
        ("Preamble", "Preamble"), ("1", "SCOPE OF WORK"), ("2", "FEES"), ("3", "Term")]
    assert all(s.synthetic_number for s in segments)           # the numbers are ours, and say so
    assert "ACME INC." in segments[0].body                      # a party name is not a heading
    assert _coverage(text, segments) == 1.0


def test_word_heading_styles_are_used_as_headings():
    text = "Agreement between A and B.\nScope of Services\nWe build it.\nCommercial Terms\nYou pay.\n"
    without = segment_document(text)
    assert without["method"] == "paragraphs"
    with_hints = segment_document(text, heading_hints=["Scope of Services", "Commercial Terms"])
    assert with_hints["method"] == "headings"
    assert [s.title for s in with_hints["segments"]] == ["Preamble", "Scope of Services", "Commercial Terms"]


def test_document_with_no_structure_falls_back_to_paragraph_blocks():
    text = ("This letter agreement has no headings at all. " * 30 + "\n\n") * 6
    result = segment_document(text)
    assert result["method"] == "paragraphs"
    assert len(result["segments"]) == 6
    assert _coverage(text, result["segments"]) == 1.0
    forced = segment_document(CONTRACT, force_paragraphs=True)
    assert forced["method"] == "paragraphs" and _coverage(CONTRACT, forced["segments"]) == 1.0


def test_a_very_long_clause_is_split_into_parts_not_truncated():
    definitions = "\n".join(f'"Term{i}" means something long and specific, number {i}.' for i in range(600))
    text = f"1. DEFINITIONS\n{definitions}\n2. FEES\nPay on time.\n"
    segments = _segments(text, max_clause_chars=8000)
    parts = [s for s in segments if s.part_of == "1"]
    assert len(parts) >= 4 and all(len(s.body) <= 8000 for s in parts)
    assert parts[0].number == f"1 (part 1 of {len(parts)})" and parts[0].part_count == len(parts)
    joined = "\n".join(s.body for s in parts)
    assert '"Term0" means' in joined and '"Term599" means' in joined       # head AND tail present
    assert all(f'"Term{i}" means' in joined for i in range(0, 600, 37))
    assert _coverage(text, segments) == 1.0


def test_a_single_enormous_line_is_split_too():
    text = "1. Scope\n" + ("The supplier shall do the thing. " * 1000) + "\n2. Fees\nPay.\n"
    segments = _segments(text, max_clause_chars=4000)
    assert all(len(s.body) <= 4000 for s in segments)
    assert _coverage(text, segments) == 1.0


def test_a_clause_spanning_a_page_break_stays_one_clause():
    # parse joins pages with a blank line; the sentence continues on the next page
    page1 = "1. SCOPE\nThe Supplier shall provide the services described in this clause and shall"
    page2 = "continue to provide them until the end of the Term.\n2. FEES\nThe fee is $5,000."
    text = page1 + "\n\n" + page2
    by = _by_number(text)
    assert list(by) == ["1", "2"]
    assert "shall\n\ncontinue to provide them" in by["1"].body


def test_a_reference_to_a_schedule_or_section_in_running_text_is_not_a_heading():
    text = ("1. Scope\nThe services are set out in\nSchedule A.\nThey are also subject to\n"
            "Section 9 of the Master Agreement.\n2. Fees\nSee above.\n")
    assert [s.number for s in _segments(text)] == ["1", "2"]


def test_duplicate_clause_numbers_are_disambiguated():
    text = "1. Scope\nA.\n2. Fees\nB.\nPART B\n1. Scope\nC.\n2. Fees\nD.\n"
    numbers = [s.number for s in _segments(text)]
    assert len(numbers) == len(set(numbers))
    clauses = [{"number": "1"}, {"number": "1"}, {"number": ""}, {"number": "2"}]
    assert unique_numbers(clauses) == 2
    assert [c["number"] for c in clauses] == ["1", "1 (dup 2)", "U3", "2"]


def test_heading_keywords_are_kept_so_they_count_towards_coverage():
    """"ARTICLE I" / "Section 4.1" / "SECTION 2 - FEES": the keyword is part of the
    document. Dropping it made short documents look under-covered and pushed them
    onto the paragraph fallback."""
    text = ("ARTICLE I\nDEFINITIONS\n1.1 Terms mean things.\nARTICLE II\nSERVICES\n2.1 Do the work.\n"
            "Section 3. Fees\nSection 3.1 Payment Terms. Client shall pay.\n")
    segments = _segments(text)
    assert _coverage(text, segments) == 1.0
    by = {s.number: s for s in segments}
    assert by["I"].heading == "ARTICLE I" and by["3.1"].heading == "Section 3.1 Payment Terms."
    assert by["3.1"].title == "Payment Terms" and by["3.1"].body == "Client shall pay."
    assert by["3.1"].section == "Section 3. Fees"
    caps = "SECTION 1 - SCOPE\nwork\nSECTION 2 - FEES\npay\n"
    assert _coverage(caps, _segments(caps)) == 1.0


def test_consecutive_exhibits_are_separate_even_after_a_table_cell():
    text = "1. Scope\nSee Exhibit A for details.\nExhibit A\nPricing table\n$5\nExhibit B\nService levels\n99.9%\n"
    assert [s.number for s in _segments(text)] == ["1", "Exhibit A", "Exhibit B"]


def test_empty_and_whitespace_documents():
    assert segment_document("")["segments"] == []
    assert segment_document("   \n\n ")["method"] == "empty"
    assert paragraph_segments("one paragraph only")[0].body == "one paragraph only"


def test_coverage_ratio_counts_each_word_once():
    assert coverage_ratio("pay the fee pay the fee", ["pay the fee"]) == 0.5
    assert coverage_ratio("pay the fee", ["PAY", "the", "fee, twice: fee"]) == 1.0
    assert coverage_ratio("", ["anything"]) == 1.0
    assert coverage_ratio("alpha beta gamma delta", ["alpha beta"]) == 0.5


def test_one_hundred_clauses_are_all_found():
    text = "\n".join(f"{i}. Heading {i}\nBody of clause {i} with its own obligation." for i in range(1, 101))
    segments = _segments(text)
    assert [s.number for s in segments] == [str(i) for i in range(1, 101)]


def test_property_no_input_loses_text_duplicates_a_number_or_emits_an_empty_clause():
    """Randomised: whatever mixture of headings, numbers, schedule names and
    signature phrases is thrown at it, every word lands in a clause, no clause is
    empty and no two clauses share a number."""
    import random

    random.seed(20261001)
    words = ["shall", "the", "Supplier", "Fees", "1.", "2.1", "(a)", "Schedule A", "ARTICLE II", "$5,000",
             "30 days", "IN WITNESS WHEREOF", "Section 4", "TERM", "Net 30", "\n", "\n\n", "3", "4.2.1",
             "Exhibit B", "Signed by", ":", "Part B", "I.", "DEFINITIONS", "12.5%", "..... 7"]
    for _ in range(1500):
        text = " ".join(random.choice(words) for _ in range(random.randint(1, 120)))
        text = text.replace(" \n ", "\n").replace("\n ", "\n")
        segments = _segments(text, max_clause_chars=random.choice([200, 8000]))
        numbers = [s.number for s in segments]
        assert _coverage(text, segments) == 1.0, text
        assert all(s.body.strip() for s in segments), text
        assert len(numbers) == len(set(numbers)), text
