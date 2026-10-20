"""Extraction corpus — realistic clause wordings run through the DETERMINISTIC
extractors (no model call), each with the answer a careful reader would give.

The wordings are modelled on real SOW / MSA / sponsored research / licence /
NDA / SaaS / staffing / government RFQ language, in US and UK/EU styles, with
numbers as digits, words and "thirty (30)", tables flattened to text, ALL-CAPS
headings and OCR noise (double spaces, line breaks mid-sentence).

Rule for the expectations: a phrasing a reader cannot settle (03/04/2026 with
no other evidence, a cap stated only in money) expects the documented
"ambiguous" / None / review answer — never a guess.

Categories (test-name prefix):
  a  dates (effective / signature)      f  parties, preamble, signature blocks
  b  term length / term end             g  governing law / venue / immunity
  c  renewal and notice periods         h  obligations and key dates
  d  payment terms                      i  matrix / playbook clause checks
  e  money values and currency
"""
from __future__ import annotations

import pytest

from shared import dates, document_check, money, parties, playbook
from shared.govern import matrix as m
from shared.govern import software as sw
from shared.govern import workforce as wf
from shared.keydates import build_key_dates
from shared.segment import segment_document


# ═════════════════════════════════════════════════════════════════════════════
# a) Dates — effective dates and signature dates
# ═════════════════════════════════════════════════════════════════════════════

# (text, day_first, expected ISO or None, ambiguous?)
DATE_CASES = [
    ("This Agreement is made and entered into as of the 2nd day of November, 2025", None, "2025-11-02", False),
    ("Effective Date: 11/02/2025", None, None, True),                       # US or EU? not guessed
    ("Effective Date: 11/02/2025", False, "2025-11-02", False),             # document is month-first
    ("Dated: 02/11/2025", True, "2025-11-02", False),                       # document is day-first
    ("The Services commencing 2 November 2025 shall be performed", None, "2025-11-02", False),
    ("Effective Date: 2025-11-02", None, "2025-11-02", False),               # ISO inside a sentence
    ("This Agreement becomes effective on 2025/12/01.", None, "2025-12-01", False),
    ('effective as of November 2, 2025 (the "Effective Date")', None, "2025-11-02", False),
    ("EFFECTIVE DATE:  NOVEMBER 2 ,  2025", None, "2025-11-02", False),       # caps + OCR spacing
    ("entered into this 2nd day of\nNovember, 2025 by and between", None, "2025-11-02", False),
    ("This Agreement is made on 2nd November 2025 between", None, "2025-11-02", False),
    ("Commencement Date: 13.11.2025", None, "2025-11-13", False),            # only valid day-first
    ("dated this twenty-second day of November, 2025", None, "2025-11-22", False),
    ("as of Nov. 2, 2025", None, "2025-11-02", False),
    ("with effect from the first day of January 2026", None, "2026-01-01", False),
    ("Start date: 01-Dec-2025", None, "2025-12-01", False),
    ("Effective 1st December, 2025", None, "2025-12-01", False),
    ("Effective Date: 12/31/2025", None, "2025-12-31", False),               # only valid month-first
    ("Effective Date: 31/12/2025", None, "2025-12-31", False),               # only valid day-first
    ("Signed: 03/04/2026", None, None, True),
    ("effective on the date of the last signature below", None, None, False),
    ("Effective Date: [__________], 2025", None, None, False),               # blank template
    ("Date: November 3, 2025", None, "2025-11-03", False),
    ("Signature: /s/ Jane Doe     Date: 3 Nov 2025", None, "2025-11-03", False),
    ("DATE: 11/3/25", False, "2025-11-03", False),
    ("This SOW is effective September 30th, 2025.", None, "2025-09-30", False),
    ("on or about the 15th of January, 2026", None, "2026-01-15", False),
    ("Dated 30 February 2026", None, None, False),                            # impossible
    ('dated as of this 1st day of October 2025 ("Effective Date") by and between', None, "2025-10-01", False),
    ("Award Date: 2025-09-15T00:00:00Z", None, "2025-09-15", False),
]


@pytest.mark.parametrize("text,day_first,iso,ambiguous", DATE_CASES, ids=[f"a{i:02d}" for i in range(len(DATE_CASES))])
def test_a_effective_and_signature_dates(text, day_first, iso, ambiguous):
    out = dates.parse_date(text, day_first=day_first)
    assert (out["date"], out["ambiguous"]) == (iso, ambiguous)
    if iso:
        assert out["precision"] == "day" and not out["estimated"]


@pytest.mark.parametrize("text,expected", [
    ("Effective Date: 2 November 2025. Signed 13/11/2025.", True),
    ("Effective Date: November 2, 2025. Signed 11/13/2025.", False),
    ("Made on 2nd November 2025; expires 1 November 2027.", True),
    ("Signed 11/03/2025 and 11/04/2025.", None),
], ids=["a-df1", "a-df2", "a-df3", "a-df4"])
def test_a_document_date_convention(text, expected):
    assert dates.detect_day_first(text) is expected


def test_a_effective_date_read_from_the_preamble():
    cls = {"clauses": [{"body": 'This Sponsored Research Agreement is entered into as of the 1st day of '
                                'October, 2025 (the "Effective Date") by and between the Parties.'}]}
    assert m._effective_date(cls) == "2025-10-01"


def test_a_signature_block_dates_are_found_in_order():
    block = ("ACME CORP.\nBy: ______________\nName: Jane Doe\nDate: November 3, 2025\n\n"
             "LAKESHORE LLC\nBy: ______________\nName: John Smith\nDate: 2025-11-04\n")
    assert [d for d, _s, _e in dates.find_dates(block)] == ["2025-11-03", "2025-11-04"]


# ═════════════════════════════════════════════════════════════════════════════
# b) Term length and term end
# ═════════════════════════════════════════════════════════════════════════════

TERM_CASES = [
    ("The initial term of this Agreement shall be three (3) years from the Effective Date.", (3, "years")),
    ("This SOW shall remain in effect for a period of 36 months.", (36, "months")),
    ("This Agreement has a 36-month term.", (36, "months")),
    ("The term of this Agreement is one year from the Effective Date.", (1, "years")),
    ("This Agreement shall commence on the Effective Date and continue for a period of thirty-six (36) months, "
     "unless terminated earlier.", (36, "months")),
    ("This Agreement shall be effective for a period of one (1) year from the Effective Date.", (1, "years")),
    ("Term: Thirty-six (36) months", (36, "months")),
    ("Initial Term | 24 months | Billing | Annual", (24, "months")),                       # flattened table
    ("Period of Performance: twelve (12) months from the date of award.", (12, "months")),
    ("This Agreement shall expire thirty-six (36) months after the Effective Date.", (36, "months")),
    ("This Agreement shall expire on the third anniversary of the Effective Date.", (3, "years")),
    ("THE TERM OF THIS AGREEMENT SHALL BE TWO (2) YEARS.", (2, "years")),
    ("This Agreement shall have an initial term of thirty-six\n(36) months commencing on the Effective Date.",
     (36, "months")),
    ("The initial term is one (1) year and thereafter this Agreement shall automatically renew for successive "
     "one (1) year renewal terms.", (1, "years")),
    ("The Subscription Term is twelve (12) months.", (12, "months")),
    ("This Agreement will be in effect for five years.", (5, "years")),
    ("This Agreement has a three-year term commencing on the Effective Date.", (3, "years")),
    # not the agreement's own length
    ("Upon expiry this Agreement shall renew for an additional term of one (1) year unless either party "
     "gives notice.", None),
    ("This Agreement shall continue in effect until terminated by either party in accordance with Section 9.", None),
    ("The license granted herein is perpetual and irrevocable.", None),
    ("This Agreement shall remain in effect for so long as Licensee uses the Software.", None),
    ("The term of this SOW shall be from November 1, 2025 through October 31, 2026.", None),
    ("Confidentiality obligations shall survive for five (5) years after termination.", None),
    ("Either party may terminate this Agreement on ninety (90) days' written notice.", None),
    ("This Agreement may be extended for two (2) additional one-year option periods.", None),
    ("This Agreement shall continue on a month-to-month basis.", None),
    ("The warranty period is 12 months from acceptance.", None),
    ("Renewal Term: 12 months", None),
    ("Records shall be retained for three (3) years after final payment.", None),
]


@pytest.mark.parametrize("text,expected", TERM_CASES, ids=[f"b{i:02d}" for i in range(len(TERM_CASES))])
def test_b_stated_term(text, expected):
    found = dates.stated_term(text)
    assert (found[:2] if found else None) == expected


@pytest.mark.parametrize("text,iso", [
    ("This Agreement shall remain in force until December 31, 2027.", "2027-12-31"),
    ("This Agreement shall expire on June 30, 2028 unless extended.", "2028-06-30"),
    ("The SOW runs from 1 April 2026 through 31 March 2027.", "2026-04-01"),
    ("Period of Performance: 10/01/2025 - 09/30/2026", "2026-09-30"),
], ids=["b-end1", "b-end2", "b-end3", "b-end4"])
def test_b_end_dates_written_out(text, iso):
    assert iso in [d for d, _s, _e in dates.find_dates(text)]


def _cls(**over):
    base = {"docType": "MSA", "effectiveDate": "2025-11-02",
            "identification": {"executionDate": None, "signatories": []},
            "timeline": {"startDate": None, "endDate": None, "renewalDate": None, "renewalNoticeDays": None,
                         "autoRenews": None, "phases": [], "milestones": []},
            "commercials": {"currency": "USD", "paymentTerms": None, "paymentSchedule": []},
            "deliverables": [], "keyDatesRaw": []}
    base.update(over)
    return base


@pytest.mark.parametrize("text,end", [
    ("The initial term of this Agreement shall be three (3) years.", "2028-11-02"),
    ("This SOW shall remain in effect for a period of 18 months.", "2027-05-02"),
    ("Term: Twelve (12) months", "2026-11-02"),
    ("This Agreement shall continue in effect until terminated by either party.", None),
    ("The license is perpetual.", None),
], ids=["b-kd1", "b-kd2", "b-kd3", "b-kd4", "b-kd5"])
def test_b_term_end_derived_from_the_stated_length(text, end):
    out = build_key_dates(_cls(), [], text)
    assert out["derived"]["termEndDate"] == end


# ═════════════════════════════════════════════════════════════════════════════
# c) Renewal and notice periods
# ═════════════════════════════════════════════════════════════════════════════

RENEWAL = {
    "yr90": "This Agreement shall automatically renew for successive one (1) year periods unless either party "
            "gives ninety (90) days' written notice of non-renewal.",
    "mo60": "Thereafter this Agreement shall automatically renew for successive twelve (12) month periods unless "
            "either party provides written notice of non-renewal at least sixty (60) days prior to expiration.",
    "notifies30": "This Agreement will auto-renew for additional one-year terms unless Customer notifies Vendor "
                  "at least 30 days before the end of the then-current term.",
    "months3": "The term renews automatically for successive 12-month periods unless cancelled with three (3) "
               "months' written notice.",
    "cal45": "The Agreement shall automatically renew unless either party gives notice of non-renewal no less "
             "than forty-five (45) calendar days before the end of the Initial Term.",
    "words90": "This Agreement shall automatically renew for successive one year terms unless either party gives "
               "ninety days notice.",
    "nonrenew60": "Non-renewal notice must be given not less than 60 days prior to the end of the Initial Term.",
    "uk3m": "The Agreement shall automatically renew for further periods of 12 months unless either party serves "
            "written notice of termination not less than three months before the expiry of the then-current period.",
    "2y120": "This Agreement automatically renews for successive renewal terms of two (2) years unless notice of "
             "non-renewal is delivered at least one hundred twenty (120) days prior to the end of the term.",
    "ocr60": "This Agreement shall automatically\nrenew for successive one (1) year terms upon sixty (60)\ndays' "
             "prior written  notice of non-renewal.",
    "caps30": "AUTOMATIC RENEWAL. THIS AGREEMENT SHALL AUTOMATICALLY RENEW FOR SUCCESSIVE ONE (1) YEAR TERMS UNLESS "
              "EITHER PARTY GIVES THIRTY (30) DAYS WRITTEN NOTICE.",
    "none_notice": "This Agreement renews automatically each year.",
    "none_periods": "This Agreement shall automatically renew for successive one (1) year periods.",
    "tfc_only": "Either party may terminate this Agreement for convenience upon thirty (30) days' notice.",
}


@pytest.mark.parametrize("key,days", [
    ("yr90", 90), ("mo60", 60), ("notifies30", 30), ("months3", 90), ("cal45", 45), ("words90", 90),
    ("nonrenew60", 60), ("uk3m", 90), ("2y120", 120), ("ocr60", 60), ("caps30", 30),
    ("none_notice", None), ("none_periods", None), ("tfc_only", None),
], ids=lambda v: f"c-{v}" if isinstance(v, str) else None)
def test_c_non_renewal_notice_days(key, days):
    cls = {"clauses": [{"category": "Term", "body": RENEWAL[key]}]}
    assert m._notice_days({}, cls) == days


@pytest.mark.parametrize("key,tier", [
    ("yr90", "deviates"), ("mo60", "fallback"), ("notifies30", "within"), ("months3", "deviates"),
    ("words90", "deviates"), ("none_notice", "deviates"),
], ids=lambda v: f"c-sw-{v}" if v in RENEWAL else None)
def test_c_software_renewal_window(key, tier):
    assert sw.check_renewal(RENEWAL[key], {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ("This Agreement may be renewed only by mutual written agreement of the parties.", "within"),
    ("This Agreement shall not automatically renew.", "within"),
], ids=["c-sw-mutual", "c-sw-noauto"])
def test_c_software_no_auto_renewal(text, tier):
    assert sw.check_renewal(text, {}).tier == tier


@pytest.mark.parametrize("key,status,found", [
    ("yr90", "moderate", "90 days' notice"),
    ("notifies30", "ok", "30 days' notice"),
    ("mo60", "moderate", "60 days' notice"),
], ids=["c-pb-yr90", "c-pb-n30", "c-pb-mo60"])
def test_c_playbook_auto_renewal_opt_out(key, status, found):
    res = playbook._check_autorenewal(RENEWAL[key])
    assert (res[0], res.found) == (status, found)


@pytest.mark.parametrize("body,status,found", [
    ("Either party may terminate this Agreement for material breach upon thirty (30) days' written notice if the "
     "breach remains uncured. Either party may terminate for convenience upon ninety (90) days' written notice.",
     "ok", "90 days' notice"),
    ("This Agreement has an initial term of three (3) years. Client may terminate for convenience on fifteen (15) "
     "days' notice.", "moderate", "15 days' notice"),
    ("Customer may terminate this Agreement at any time without cause upon sixty (60) days prior written notice.",
     "ok", "60 days' notice"),
], ids=["c-tfc1", "c-tfc2", "c-tfc3"])
def test_c_termination_for_convenience_notice(body, status, found):
    res = playbook._check_termination(body)
    assert (res[0], res.found) == (status, found)


@pytest.mark.parametrize("text,value,direction,anchor", [
    ("ninety (90) days prior to the expiration of the then-current term", 90, -1, "term_end"),
    ("at least 60 days prior to expiration", 60, -1, "term_end"),
    ("no less than thirty (30) days before the end of the Initial Term", 30, -1, "term_end"),
], ids=["c-off1", "c-off2", "c-off3"])
def test_c_notice_rules_parse(text, value, direction, anchor):
    rule = dates.parse_offset(text)
    assert (rule["value"], rule["direction"], rule["anchor"]) == (value, direction, anchor)


# ═════════════════════════════════════════════════════════════════════════════
# d) Payment terms
# ═════════════════════════════════════════════════════════════════════════════

# (text, days the playbook reads, research-matrix tier: within ≤30, fallback ≤45)
PAYMENT_CASES = [
    ("Payment terms: Net 30.", 30, "within"),
    ("Invoices are payable Net 45 from the invoice date.", 45, "fallback"),
    ("Net 60", 60, "deviates"),
    ("Client shall pay each invoice within thirty (30) days of receipt of a correct invoice.", 30, "within"),
    ("Payment shall be made within forty-five (45) days after receipt of a proper invoice.", 45, "fallback"),
    ("Payment terms: net thirty (30) days.", 30, "within"),
    ("Terms: 2/10 net 30", 30, "within"),
    ("Fees are payable monthly in arrears.", None, "review"),
    ("Payment of $50,000 shall be due upon acceptance of Milestone 2.", None, "review"),
    ("Sponsor shall pay fifty percent (50%) of the budget in advance upon execution of this Agreement.", None, "review"),
    ("All amounts are due upon receipt of invoice.", 0, "within"),
    ("Payment Terms | Net 45 days | Currency | USD", 45, "fallback"),
    ("The Customer shall pay each valid invoice within 30 days of the date of receipt.", 30, "within"),
    ("Payment is due thirty (30) days after the invoice date.", 30, "within"),
    ("Payment shall be made in accordance with the Prompt Payment Act, 31 U.S.C. 3901.", None, "review"),
    ("Licensee shall pay royalties within sixty (60) days after the end of each calendar quarter.", 60, "deviates"),
    ("Net-30", 30, "within"),
    ("Invoices are payable within 30 business days of receipt of invoice.", 30, "within"),
    ("Payment terms are 30 days end of month.", 30, "within"),
    ("University will remit payment within forty-five days of receipt of an undisputed invoice.", 45, "fallback"),
    ("Invoices shall be paid net 60 days EOM.", 60, "deviates"),
    ("Milestone payments are payable upon Sponsor's written acceptance of each Deliverable.", None, "review"),
    ("Customer shall pay the annual subscription fee annually in advance.", None, "review"),
    ("PAYMENT. CLIENT SHALL PAY ALL UNDISPUTED INVOICES WITHIN FIFTEEN (15) DAYS.", 15, "within"),
    ("A 2% discount applies if paid within 10 days; otherwise payment is due net 30.", 30, "within"),
    ("Contractor shall invoice monthly and\nCity shall pay within  thirty (30)\ndays of approval.", 30, "within"),
]


@pytest.mark.parametrize("text,days,tier", PAYMENT_CASES, ids=[f"d{i:02d}" for i in range(len(PAYMENT_CASES))])
def test_d_payment_window(text, days, tier):
    assert playbook.parse_net_days(text) == days
    assert m._check_payment(text, {}).tier == tier


@pytest.mark.parametrize("text,days", [
    ("net thirty (30).", 30), ("NET-45", 45), ("net sixty days", 60),
    ("a net one-time fee", None), ("net 3% of sales", None), ("Net Sales (as defined)", None),
], ids=["d-net1", "d-net2", "d-net3", "d-net4", "d-net5", "d-net6"])
def test_d_net_terms_are_not_read_from_other_uses_of_net(text, days):
    assert dates.net_days(text) == days


def test_d_advance_payment_is_beneficial():
    assert m._check_payment(PAYMENT_CASES[9][0], {}).beneficial


@pytest.mark.parametrize("text,value,anchor", [
    ("Net 30", 30, "invoice"),
    ("net thirty (30) days", 30, "invoice"),
    ("2/10 net 30", 30, "invoice"),
    ("within forty-five (45) days of receipt of invoice", 45, "invoice"),
    ("within ten (10) business days of acceptance", 10, "acceptance"),
], ids=["d-off1", "d-off2", "d-off3", "d-off4", "d-off5"])
def test_d_payment_rules_parse(text, value, anchor):
    rule = dates.parse_offset(text)
    assert (rule["value"], rule["anchor"]) == (value, anchor)


# ═════════════════════════════════════════════════════════════════════════════
# e) Money values and currency
# ═════════════════════════════════════════════════════════════════════════════

MONEY_CASES = [
    ("The total fee is $1,250,000.00.", [1250000.0], "USD"),
    ("a total of USD 1.25 million", [1250000.0], "USD"),
    ("one hundred thousand dollars ($100,000)", [100000.0], "USD"),
    ("Fee: €85,000", [85000.0], "EUR"),
    ("Expenses capped at £12k", [12000.0], "GBP"),
    ("a not-to-exceed amount of $250,000", [250000.0], "USD"),
    ("Senior Consultant $150/hour; Analyst $95/hour", [150.0, 95.0], "USD"),
    ("Total: $48,000 (320 hours at $150 per hour)", [48000.0, 150.0], "USD"),
    ("between $100,000 and $150,000", [100000.0, 150000.0], "USD"),
    ("Estimated range: $100,000 - $150,000", [100000.0, 150000.0], "USD"),
    ("Contract price €1.200.000,50", [1200000.5], "EUR"),
    ("EUR 85.000", [85000.0], "EUR"),
    ("CHF 1'250'000", [1250000.0], "CHF"),
    ("1,000,000 USD", [1000000.0], "USD"),
    ("Budget $2.5MM", [2500000.0], "USD"),
    ("Twelve Thousand Five Hundred Dollars ($12,500.00)", [12500.0], "USD"),
    ("Fees are reduced by ($5,000)", [-5000.0], "USD"),                      # accounting negative
    ("Credit: -$3,000", [-3000.0], "USD"),
    ("US$ 10,000", [10000.0], "USD"),
    ("A$50,000 per annum", [50000.0], "AUD"),
    ("GBP 12,500 plus VAT", [12500.0], "GBP"),
    ("commercial general liability insurance of not less than $1,000,000 per occurrence and $2,000,000 "
     "in the aggregate", [1000000.0, 2000000.0], "USD"),
    ("Interest accrues at 1.5% per month", [], None),
    ("within 30 days as set out in Section 3.2", [], None),
    ("£1.5 million", [1500000.0], "GBP"),
    ("$ 75,000", [75000.0], "USD"),
    ("₹5 lakh", [500000.0], "INR"),
    ("Fifty Thousand Euros (€50,000)", [50000.0], "EUR"),
    ("an amount equal to Two Million U.S. Dollars (US$2,000,000)", [2000000.0], "USD"),
    ("USD 1,250,000 (one million two hundred fifty thousand US dollars)", [1250000.0], "USD"),
    ("TOTAL CONTRACT VALUE:  $ 1,250,000", [1250000.0], "USD"),
]


@pytest.mark.parametrize("text,amounts,currency", MONEY_CASES, ids=[f"e{i:02d}" for i in range(len(MONEY_CASES))])
def test_e_amounts_and_currency(text, amounts, currency):
    found = money.find_amounts(text)
    assert [a["amount"] for a in found] == amounts
    assert (found[0]["currency"] if found else None) == currency


@pytest.mark.parametrize("text,currency", [
    ("Fees: $10,000. Expenses: $2,000. Travel: $500.", "USD"),
    ("Fees €10.000; expenses €2.000; travel £300.", None),                 # mixed: not asserted
    ("The fee is EUR 10,000 and the deposit EUR 1,000.", "EUR"),
], ids=["e-cur1", "e-cur2", "e-cur3"])
def test_e_document_currency(text, currency):
    assert money.detect_currency(text) == currency


def test_e_quote_checks_scaled_amounts():
    assert money.quote_supports(1250000, "a total of USD 1.25 million") is True
    assert money.quote_supports(1.25, "a total of USD 1.25 million") is False


# ═════════════════════════════════════════════════════════════════════════════
# f) Parties, preamble, recitals and signature blocks
# ═════════════════════════════════════════════════════════════════════════════

_BODY = ("\n\n1. SERVICES\nVendor shall perform the Services described in Exhibit A.\n\n"
         "2. FEES\nClient shall pay the Fees set out in Exhibit B within thirty (30) days of invoice.\n\n"
         "3. TERM\nThe term of this Agreement is one (1) year.\n\n"
         "IN WITNESS WHEREOF, the parties have executed this Agreement as of the Effective Date.\n\n")

# (preamble, names that must stay in the preamble, signature block, names in the signature block)
PARTY_CASES = [
    ('This Master Services Agreement is made by and between Acme Corp., a Delaware corporation ("Client") '
     'and Lakeshore LLC ("Vendor").', ["Acme Corp.", "Lakeshore LLC"],
     "ACME CORP.\nBy: ____________\nName: Jane Doe\nTitle: CFO", ["Jane Doe"]),
    ('THIS AGREEMENT is entered into as of the 2nd day of November, 2025 by and between THE REGENTS OF THE '
     'UNIVERSITY OF CALIFORNIA, on behalf of its Berkeley campus ("University"), and NOVAGEN THERAPEUTICS, '
     'INC., a Delaware corporation ("Sponsor").', ["REGENTS OF THE UNIVERSITY OF CALIFORNIA", "NOVAGEN"],
     "THE REGENTS OF THE UNIVERSITY OF CALIFORNIA\nBy: ____________\nName: Maria Lopez", ["Maria Lopez"]),
    ('BETWEEN:\nACME HOLDINGS LIMITED\n(company number 01234567) whose registered office is at 1 King Street, '
     'London EC2V 8AU ("Customer"); and\nBLUEWATER SOLUTIONS LTD\nwhose registered office is at 22 Harbour '
     'Road, Bristol BS1 5TY ("Supplier").', ["ACME HOLDINGS LIMITED", "BLUEWATER SOLUTIONS LTD"],
     "Signed by Tom Brown\nfor and on behalf of ACME HOLDINGS LIMITED", ["Tom Brown"]),
    ('STATEMENT OF WORK NO. 4\nThis Statement of Work is entered into between Globex Inc. ("Client") and '
     'Initech Consulting, LLC ("Consultant") pursuant to the Master Services Agreement dated 1 March 2024.',
     ["Globex Inc.", "Initech Consulting, LLC"], "GLOBEX INC.\nBy: ____\nName: Ann Lee\nDate: 3/4/2025", ["Ann Lee"]),
    ('This Software License Agreement ("Agreement") is made between Northwind Software GmbH, Hauptstrasse 5, '
     '10115 Berlin, Germany ("Licensor") and Contoso University ("Licensee").',
     ["Northwind Software GmbH", "Contoso University"], "Northwind Software GmbH\nBy: ____\nName: Klaus Weber",
     ["Klaus Weber"]),
    ('MUTUAL NON-DISCLOSURE AGREEMENT\nThis Mutual Non-Disclosure Agreement is entered into by and between '
     'Fabrikam, Inc., a Washington corporation with offices at 100 Main Street, Redmond, WA 98052, and '
     'Tailspin Toys Corp., a Nevada corporation.', ["Fabrikam, Inc.", "Tailspin Toys Corp."],
     "FABRIKAM, INC.\nBy: ____\nName: Raj Patel", ["Raj Patel"]),
    ('This Sponsored Research Agreement is made by and between The Board of Trustees of the Leland Stanford '
     'Junior University ("Stanford") and BioCure Pharma S.A. ("Sponsor").',
     ["Board of Trustees of the Leland Stanford", "BioCure Pharma S.A."],
     "THE BOARD OF TRUSTEES OF THE LELAND STANFORD JUNIOR UNIVERSITY\nBy: ____\nName: Kim Ng", ["Kim Ng"]),
    ('THIS AGREEMENT is made by and between ACME CORP., a Delaware corporation ("Client"), and LAKESHORE LLC, '
     'an Illinois limited liability company ("Vendor").\n\nWHEREAS, Client desires to obtain certain '
     'services; and\nWHEREAS, Vendor is willing to provide such services;\nNOW, THEREFORE, the parties agree '
     'as follows:', ["ACME CORP.", "LAKESHORE LLC", "WHEREAS"], "LAKESHORE LLC\nBy: ____\nName: Sam Hill",
     ["Sam Hill"]),
    ('This Staffing Services Agreement is entered into by and between Contoso Health System ("Client") and '
     'Proseware Staffing Inc. ("Agency"), effective as of January 1, 2026.',
     ["Contoso Health System", "Proseware Staffing Inc."], "PROSEWARE STAFFING INC.\nBy: ____\nName: Lee Chan",
     ["Lee Chan"]),
    ('RFQ No. 2025-114 — Request for Quotation\nThe City of Springfield ("City") requests quotations from '
     'qualified vendors. Offeror: Litware, Inc., 500 Oak Avenue, Springfield, IL 62701.',
     ["City of Springfield", "Litware, Inc."], "Authorized Signature: ____\nName: Pat Doe", ["Pat Doe"]),
    ('This Data Use Agreement is made between the University of Michigan ("Provider") and Wide World '
     'Importers Ltd ("Recipient").', ["University of Michigan", "Wide World Importers Ltd"],
     "Signed for and on behalf of Wide World Importers Ltd\nName: Ian Gray", ["Ian Gray"]),
    ('This Agreement is made by and between Acme  Corp.,  a Delaware corporation ("Client")\nand Lakeshore\nLLC '
     '("Vendor").', ["Acme  Corp.", "Lakeshore\nLLC"], "ACME CORP.\nBy: ____\nName: Jane Doe", ["Jane Doe"]),
    ('LICENSE AGREEMENT\nThis License Agreement is entered into by the Trustees of Princeton University '
     '("Princeton") and Adatum Corporation ("Licensee").', ["Trustees of Princeton University", "Adatum Corporation"],
     "ADATUM CORPORATION\nBy: ____\nName: Ola Berg", ["Ola Berg"]),
    ('SUBCONTRACT AGREEMENT\nThis Subcontract is entered into by and between Prime Contractor, Inc. '
     '("Contractor") and Small Business Solutions, LLC ("Subcontractor") under Prime Contract No. W91-25-C-0001.',
     ["Prime Contractor, Inc.", "Small Business Solutions, LLC"], "SMALL BUSINESS SOLUTIONS, LLC\nBy: ____\n"
     "Name: Dee Roy", ["Dee Roy"]),
    ('This Consulting Agreement is entered into between Woodgrove Bank, N.A. ("Bank") and Jane Q. Consultant, '
     'an individual ("Consultant").', ["Woodgrove Bank, N.A.", "Jane Q. Consultant"],
     "WOODGROVE BANK, N.A.\nBy: ____\nName: Bo Li", ["Bo Li"]),
    ('This Collaboration Agreement is made between Massachusetts Institute of Technology ("MIT"), 77 '
     'Massachusetts Avenue, Cambridge, MA 02139, and Coho Winery Pty Ltd ("Collaborator").',
     ["Massachusetts Institute of Technology", "Coho Winery Pty Ltd"], "COHO WINERY PTY LTD\nBy: ____\n"
     "Name: Ava Jones", ["Ava Jones"]),
    ('This Order Form is entered into between Customer: Lucerne Publishing, Inc. and Provider: Relecloud '
     'SaaS B.V.', ["Lucerne Publishing, Inc.", "Relecloud SaaS B.V."], "RELECLOUD SAAS B.V.\nBy: ____\n"
     "Name: Jan de Vries", ["Jan de Vries"]),
    ('THIS MATERIAL TRANSFER AGREEMENT is between The Johns Hopkins University ("Provider") and Trey Research '
     'Co. ("Recipient").', ["Johns Hopkins University", "Trey Research"], "TREY RESEARCH CO.\nBy: ____\n"
     "Name: Uma Rao", ["Uma Rao"]),
    ('This Agreement dated 2 November 2025 is made BETWEEN (1) ALPINE SKI HOUSE PLC, a company incorporated in '
     'England and Wales; and (2) MARGIE\'S TRAVEL LIMITED.', ["ALPINE SKI HOUSE PLC", "MARGIE'S TRAVEL LIMITED"],
     "Executed by ALPINE SKI HOUSE PLC acting by a director\nName: Rob Fox", ["Rob Fox"]),
    ('This Grant Agreement is made by the National Science Foundation ("NSF") and the Research Foundation for '
     'the State University of New York ("Recipient").', ["National Science Foundation", "Research Foundation"],
     "Agreed and accepted\nName: Dr. Ella Fitz", ["Ella Fitz"]),
    ('This Clinical Trial Agreement is entered into by and among Duke University ("Institution"), Fourth Coffee '
     'Pharmaceuticals, Inc. ("Sponsor") and Dr. Alan Smith ("Investigator").',
     ["Duke University", "Fourth Coffee", "Alan Smith"], "DUKE UNIVERSITY\nBy: ____\nName: Liz Wu", ["Liz Wu"]),
    ('MASTER SUBSCRIPTION AGREEMENT\nThis Agreement is between Blue Yonder Airlines, Inc. ("Customer") and '
     'Graphic Design Institute LLC ("Supplier").', ["Blue Yonder Airlines, Inc.", "Graphic Design Institute LLC"],
     "GRAPHIC DESIGN INSTITUTE LLC\nBy: ____\nName: Max Gold", ["Max Gold"]),
    ('This Agreement is made between Humongous Insurance Co. ("Client") and School of Fine Art, Inc. ("Vendor"). '
     'Client\'s address: 1 Main St., Suite 200, Dallas, TX 75201.', ["Humongous Insurance Co.", "School of Fine Art, Inc."],
     "HUMONGOUS INSURANCE CO.\nBy: ____\nName: Ken Cho", ["Ken Cho"]),
    ('BY AND BETWEEN\nTHE UNIVERSITY OF TEXAS AT AUSTIN\nand\nVANARSDEL, LTD.', ["UNIVERSITY OF TEXAS AT AUSTIN",
                                                                                   "VANARSDEL, LTD."],
     "VANARSDEL, LTD.\nBy: ____\nName: Ivy Moss", ["Ivy Moss"]),
    ('This Teaming Agreement is entered into as of 11/02/2025 between Northwind Traders, Inc. and Wingtip Toys '
     'L.L.C.', ["Northwind Traders, Inc.", "Wingtip Toys"], "WINGTIP TOYS L.L.C.\nBy: ____\nName: Gus Park",
     ["Gus Park"]),
]


@pytest.mark.parametrize("preamble,names,sig,signers", PARTY_CASES, ids=[f"f{i:02d}" for i in range(len(PARTY_CASES))])
def test_f_parties_stay_in_the_preamble_and_signatures_in_their_block(preamble, names, sig, signers):
    doc = preamble + _BODY + sig + "\n"
    segs = segment_document(doc)["segments"]
    assert segs[0].kind == "preamble"
    for name in names:
        assert name in segs[0].body                                    # the parties are not torn away
    numbered = [s.number for s in segs if s.kind == "clause"]
    assert numbered[:3] == ["1", "2", "3"]                              # no party line became a clause
    signature = [s for s in segs if s.kind == "signature"]
    assert signature and all(any(n in s.body for s in signature) for n in signers)
    assert document_check.check(doc) is None                           # recognised as an agreement


# The parties the deterministic extractor reads from each PARTY_CASES document
# (display form). The RFQ (f09) names no contracting parties: nothing is guessed.
PARTY_NAMES = [
    ["Acme Corp.", "Lakeshore LLC"],
    ["The Regents of the University of California", "Novagen Therapeutics, Inc."],
    ["Acme Holdings Limited", "Bluewater Solutions Ltd"],
    ["Globex Inc.", "Initech Consulting, LLC"],
    ["Northwind Software GmbH", "Contoso University"],
    ["Fabrikam, Inc.", "Tailspin Toys Corp."],
    ["The Board of Trustees of the Leland Stanford Junior University", "BioCure Pharma S.A."],
    ["Acme Corp.", "Lakeshore LLC"],
    ["Contoso Health System", "Proseware Staffing Inc."],
    [],
    ["University of Michigan", "Wide World Importers Ltd"],
    ["Acme Corp.", "Lakeshore LLC"],
    ["Trustees of Princeton University", "Adatum Corporation"],
    ["Prime Contractor, Inc.", "Small Business Solutions, LLC"],
    ["Woodgrove Bank, N.A.", "Jane Q. Consultant"],
    ["Massachusetts Institute of Technology", "Coho Winery Pty Ltd"],
    ["Lucerne Publishing, Inc.", "Relecloud SaaS B.V."],
    ["The Johns Hopkins University", "Trey Research Co."],
    ["Alpine Ski House PLC", "Margie's Travel Limited"],
    ["National Science Foundation", "Research Foundation for the State University of New York"],
    ["Duke University", "Fourth Coffee Pharmaceuticals, Inc.", "Dr. Alan Smith"],
    ["Blue Yonder Airlines, Inc.", "Graphic Design Institute LLC"],
    ["Humongous Insurance Co.", "School of Fine Art, Inc."],
    ["The University of Texas at Austin", "Vanarsdel, Ltd."],
    ["Northwind Traders, Inc.", "Wingtip Toys L.L.C."],
]


@pytest.mark.parametrize("case,expected", list(zip(PARTY_CASES, PARTY_NAMES)),
                         ids=[f"f{i:02d}-rule" for i in range(len(PARTY_CASES))])
def test_f_parties_read_by_rule_from_the_preamble_and_signature_block(case, expected):
    preamble, names, sig, _ = case
    doc = preamble + _BODY + sig + "\n"
    got = parties.extract_parties(doc)
    assert [p["name"] for p in got] == expected
    for p in got:                                                      # every name is in the text as written
        s, e = p["span"]
        assert parties.same_party(doc[s:e], p["name"])
    check = parties.check_parties(expected, doc)
    assert check["notInText"] == [] and check["modelOnly"] == []


def test_f_a_model_party_absent_from_the_document_is_reported():
    preamble, _, sig, _ = PARTY_CASES[0]
    check = parties.check_parties(["Acme Corporation", "Globex Industries Ltd"], preamble + _BODY + sig)
    assert check["matched"] == ["Acme Corporation"] and check["notInText"] == ["Globex Industries Ltd"]
    assert check["textOnly"] == ["Lakeshore LLC"]


@pytest.mark.parametrize("text,count", [
    ('Acme Corp. ("Client") shall pay Lakeshore Inc. within 30 days.', 1),
    ("Only U.S. citizens may perform the work. Sponsor agrees.", 2),
    ("Payment to Contoso Ltd. is due monthly; Client shall pay Globex Co. quarterly.", 2),
], ids=["f-s1", "f-s2", "f-s3"])
def test_f_party_abbreviations_do_not_end_a_sentence(text, count):
    assert len(m._sentences(text)) == count


# ═════════════════════════════════════════════════════════════════════════════
# g) Governing law, venue and sovereign immunity
# ═════════════════════════════════════════════════════════════════════════════

GOV_CASES = [
    ("This Agreement shall be governed by the laws of the State of New York, without regard to its conflict of "
     "laws principles.", "New York", "within"),
    ("This Agreement shall be governed by the laws of the State of New York, without regard to conflict of laws.",
     "Ohio", "deviates"),
    ("This Agreement shall be governed by the laws of the State of New York.", None, "review"),
    ("This Agreement and any dispute arising from it shall be governed by the laws of England and Wales.", None,
     "review"),
    ("This Agreement shall be governed by the laws of the Commonwealth of Massachusetts.", "Massachusetts", "within"),
    ("This Agreement is governed by the laws of the State of Texas. Nothing in this Agreement shall be construed "
     "as a waiver of the sovereign immunity of the State of Texas.", "Texas", "within"),
    ("This Agreement is governed by the laws of the State of Texas. University hereby waives its sovereign "
     "immunity with respect to claims under this Agreement.", "Texas", "unacceptable"),
    ("This Agreement shall be governed by the laws of the State of New York. The parties submit to the exclusive "
     "jurisdiction of the state and federal courts located in Wilmington, Delaware.", "New York", "within"),
    ("This Agreement shall be governed by the federal law of the United States.", None, "review"),
    ("This Agreement shall be governed by the laws of the Province of Ontario.", "Ohio", "review"),
    ("This Agreement is governed by the laws of the Federal Republic of Germany.", None, "review"),
    ("Each party shall comply with all applicable laws.", "Ohio", "fallback"),
    ("This Agreement shall be construed in accordance with the internal laws of the State of California.",
     "California", "within"),
    ("GOVERNING LAW. THIS AGREEMENT SHALL BE GOVERNED BY THE LAWS OF THE STATE OF DELAWARE.", "Delaware", "within"),
    ("This Agreement shall be governed by the laws of the State of\nNew  York.", "New York", "within"),
    ("This Agreement is governed by Ohio law. No provision of this Agreement shall constitute or be construed as "
     "a waiver of the State's sovereign immunity.", "Ohio", "within"),
    ("This Agreement is governed by the laws of the State of Florida. The University does not waive its "
     "sovereign immunity.", "Florida", "within"),
    ("Neither party waives any immunity it may have.", None, "fallback"),
    ("This Agreement between Sponsor and The Regents of the University of Michigan shall be governed by the laws "
     "of the State of Ohio.", "Ohio", "within"),
    ("Delaware law governs this Agreement.", "Delaware", "within"),
    ("Governing Law: Texas", "Texas", "within"),
    ("This Agreement shall be governed by the laws of the State of Washington.", "Washington", "within"),
    ("The laws of Scotland shall apply to this Agreement.", None, "review"),
    ("This Agreement shall be governed by the laws of the State of New York and the federal laws of the United "
     "States of America.", "New York", "within"),
    ("This Agreement is governed by English law and the parties submit to the exclusive jurisdiction of the "
     "courts of England.", None, "review"),
    ("This Agreement is governed by the laws of the State of Georgia, without giving effect to any choice of law "
     "rules.", "Virginia", "deviates"),
]


@pytest.mark.parametrize("text,home,tier", GOV_CASES, ids=[f"g{i:02d}" for i in range(len(GOV_CASES))])
def test_g_governing_law(text, home, tier):
    assert m._check_governing_law(text, {"homeState": home}).tier == tier


def test_g_governing_law_names_the_jurisdiction_as_written():
    assert m._check_governing_law(GOV_CASES[3][0], {}).found == "England and Wales law"


# ═════════════════════════════════════════════════════════════════════════════
# h) Obligations and key dates
# ═════════════════════════════════════════════════════════════════════════════

def _obl(bodies, agreement_type="sponsored_research", signed="2025-11-02", **cls):
    clauses = [{"id": f"c{i}", "number": str(i + 1), "body": b, "category": cat}
               for i, (b, cat) in enumerate((b if isinstance(b, tuple) else (b, "Other")) for b in bodies)]
    return m.extract_obligations({"effectiveDate": "2025-11-02", "clauses": clauses, **cls}, agreement_type, signed)


@pytest.mark.parametrize("bodies,atype,extra,kind,title_part,due", [
    (["University shall submit quarterly progress reports within thirty (30) days after the end of each calendar "
      "quarter."], "sponsored_research", {}, "sponsor_report", "Quarterly", "2026-01-30"),
    (["Sponsor shall receive written progress reports quarterly."], "sponsored_research", {}, "sponsor_report",
     "Quarterly", "2026-02-02"),
    (["Recipient shall provide an annual technical report to the Provider."], "grant", {}, "sponsor_report",
     "Annual", "2026-11-02"),
    (["Consultant shall deliver monthly status reports to the Client."], "sow", {}, "sponsor_report", "Monthly",
     "2025-12-02"),
    (["A final report shall be submitted within ninety (90) days after the expiration of the term."],
     "sponsored_research", {"keyDates": [{"kind": "term_end", "date": "2026-12-31"}]}, "sponsor_report",
     "Final report", "2027-03-31"),
    (["Licensee shall deliver royalty reports within forty-five (45) days after the end of each calendar quarter."],
     "license", {}, "royalty_report", "Royalty report", "2026-02-14"),
    (["Deliverable 1: Requirements Specification due December 15, 2025."], "sow", {}, "deliverable_due",
     "Requirements", "2025-12-15"),
    (["Phase 2 deliverables shall be delivered within sixty (60) days of the Effective Date."], "sow", {},
     "deliverable_due", "Phase 2", "2026-01-01"),
    (["Milestone payments:\n(a) $25,000 upon completion of the beta, expected by March 31, 2026."], "license", {},
     "milestone_payment", "$25,000", "2026-03-31"),
    (["This Agreement shall automatically renew for successive one (1) year periods unless either party gives "
      "sixty (60) days' written notice of non-renewal."], "sow",
     {"keyDates": [{"kind": "term_end", "date": "2026-12-31"}], "timeline": {"autoRenews": True}},
     "renewal_notice", "60 days", "2026-11-01"),
    (["University shall disclose all Subject Inventions to Sponsor within sixty (60) days of conception."],
     "sponsored_research", {}, "invention_disclosure", "within 60 days", None),
    ([("Institution shall submit any proposed publication to Sponsor for review at least sixty (60) days prior to "
       "submission.", "PublicationRights")], "sponsored_research", {}, "publication_review", "60 days", None),
    ([("Sponsor shall have a review period of one hundred thirty-five (135) days to review any proposed "
       "publication.", "PublicationRights")], "sponsored_research", {}, "publication_review", "135 days", None),
], ids=[f"h{i:02d}" for i in range(13)])
def test_h_obligations(bodies, atype, extra, kind, title_part, due):
    out = _obl(bodies, atype, **extra)
    hits = [o for o in out if o["kind"] == kind and title_part in o["title"]]
    assert hits, out
    assert hits[0]["dueDate"] == due


@pytest.mark.parametrize("text,tier,found", [
    ("Institution shall submit any proposed publication to Sponsor at least sixty (60) days prior to submission. "
     "Sponsor may request an additional delay of up to seventy-five (75) days to file patent applications.",
     "deviates", "60 days' review + 75 days' patent delay"),
    ("Sponsor shall have thirty (30) days to review any proposed publication.", "within", "30 days' review"),
    ("Sponsor may review manuscripts for forty-five (45) days before publication.", "fallback", "45 days' review"),
    ("University may publish the results without Sponsor's prior written approval after a 30 day review.",
     "within", "30 days' review"),
    ("University shall not publish without the Sponsor's prior written consent.", "unacceptable", None),
], ids=["h-pub1", "h-pub2", "h-pub3", "h-pub4", "h-pub5"])
def test_h_publication_review(text, tier, found):
    res = m._check_publication(text, {})
    assert res.tier == tier and (found is None or res.found == found)


@pytest.mark.parametrize("text,tier", [
    ("The obligations of confidentiality shall survive for five (5) years after termination.", "within"),
    ("These obligations of confidentiality survive for a period of seven (7) years following expiration.", "fallback"),
    ("The obligations of confidentiality shall survive in perpetuity.", "deviates"),
    ("Recipient shall keep the Confidential Information confidential for ten years.", "deviates"),
    ("The confidentiality obligations last for 36 months from disclosure.", "within"),
    ("Confidentiality obligations continue for five (5) years; provided that obligations regarding trade secrets "
     "continue for so long as the information remains a trade secret.", "within"),
    ("The duty of confidentiality shall survive termination and continue indefinitely.", "deviates"),
], ids=["h-conf1", "h-conf2", "h-conf3", "h-conf4", "h-conf5", "h-conf6", "h-conf7"])
def test_h_confidentiality_survival(text, tier):
    assert m._check_confidentiality(text, {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ("Contractor shall submit monthly status reports.", "fallback"),
    ("Contractor shall submit weekly status reports.", "deviates"),
    ("University shall provide quarterly technical reports.", "within"),
    ("University shall provide an annual report.", "within"),
    ("University shall provide semi-annual progress reports.", "within"),
    ("Recipient shall report on progress each fiscal quarter.", "within"),
    ("Contractor shall provide bi-weekly reports.", "deviates"),
    ("This subaward is subject to the terms and conditions of the prime award, which are incorporated by "
     "reference.", "deviates"),
    ("The flow-down terms of the prime award are listed in Attachment B.", "within"),
], ids=["h-rep1", "h-rep2", "h-rep3", "h-rep4", "h-rep5", "h-rep6", "h-rep7", "h-rep8", "h-rep9"])
def test_h_reporting_cadence(text, tier):
    assert m._check_sponsor_reporting(text, {}).tier == tier


# ═════════════════════════════════════════════════════════════════════════════
# i) Matrix and playbook clause checks
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,status,found", [
    ("In no event shall either party's aggregate liability exceed the fees paid in the twelve (12) months "
     "preceding the claim.", "ok", "fees paid in the preceding 12 months"),
    ("Each party's total liability shall be limited to the fees paid or payable under this SOW in the twelve "
     "months preceding the event.", "ok", "fees paid in the preceding 12 months"),
    ("Supplier's aggregate liability shall not exceed two times (2x) the fees paid under this Agreement.",
     "moderate", "2x fees"),
    ("Total liability shall not exceed two (2) times the annual fees.", "moderate", "2x fees"),
    ("Supplier shall have unlimited liability for breach of this Agreement.", "material", "uncapped"),
    ("Vendor's total liability shall not exceed the amounts paid by Customer under this Agreement.", "ok",
     "fees paid"),
    ("In no event shall either party's aggregate liability exceed $500,000.", "review", None),
    ("Limitation of liability: 3x the annual fees.", "material", "3x fees"),
    ("Neither party shall be liable for indirect, incidental or consequential damages.", "review", None),
    ("Aggregate liability shall not exceed the fees paid in the twenty-four (24) months preceding the claim.",
     "moderate", "2x fees"),
], ids=[f"i-liab{i}" for i in range(10)])
def test_i_liability_cap(text, status, found):
    res = playbook._check_liability(text)
    assert (res[0], res.found) == (status, found)


@pytest.mark.parametrize("text,tier", [
    ("Sponsor shall indemnify, defend and hold harmless University from any claims.", "within"),
    ("Licensee shall, at its own expense, defend, indemnify and hold harmless the University.", "within"),
    ("Company agrees to defend, indemnify and hold harmless the University and its trustees.", "within"),
    ("The University shall indemnify Sponsor against all claims.", "unacceptable"),
    ("To the extent permitted by the laws of the State of Ohio, University shall indemnify Sponsor.", "fallback"),
    ("Each party shall indemnify the other for its negligence.", "unacceptable"),
    ("Each party shall be responsible for its own negligence.", "within"),
], ids=[f"i-ind{i}" for i in range(7)])
def test_i_research_indemnity(text, tier):
    assert m._check_indemnity(text, {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ("Vendor shall defend, indemnify and hold harmless Customer against any claim that the Software infringes "
     "any patent or copyright.", "within"),
    ("Customer shall indemnify Vendor against all claims.", "unacceptable"),
    ("Each party is responsible for its own acts.", "deviates"),
], ids=["i-swind1", "i-swind2", "i-swind3"])
def test_i_buyer_indemnity(text, tier):
    assert sw.check_indemnity(text, {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ('All Deliverables shall be considered "works made for hire" as defined in the U.S. Copyright Act.', "within"),
    ("Contractor hereby assigns to Client all right, title and interest in the Deliverables.", "within"),
    ("Supplier hereby irrevocably assigns to the Customer all Intellectual Property Rights in the Deliverables.",
     "within"),
    ("Intellectual Property Rights in the Deliverables shall vest in the Customer upon creation.", "within"),
    ("Consultant shall own all Deliverables.", "deviates"),
    ("Vendor retains all right, title and interest in the Deliverables.", "deviates"),
    ("Client receives a non-exclusive license to use the Deliverables.", "fallback"),
    ("Each party keeps its own tools.", "review"),
], ids=[f"i-wfh{i}" for i in range(8)])
def test_i_work_product_ownership(text, tier):
    assert wf.check_work_for_hire(text, {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ("All results shall be works made for hire owned by Sponsor.", "unacceptable"),
    ("Each party shall retain ownership of its Background IP.", "within"),
], ids=["i-bip1", "i-bip2"])
def test_i_background_ip(text, tier):
    assert m._check_background_ip(text, {}).tier == tier


@pytest.mark.parametrize("text,tier", [
    ("Each party shall comply with all applicable U.S. export control laws, including the EAR and ITAR.", "within"),
    ("Each party shall comply with the Export Administration Regulations.", "within"),
    ("Only U.S. citizens may work on the project.", "unacceptable"),
    ("Sponsor shall not provide any export-controlled information without prior written notice to University.",
     "within"),
    ("Sponsor may provide ITAR-controlled technical data to University.", "deviates"),
    ("The work under this Agreement is fundamental research.", "within"),
], ids=[f"i-exp{i}" for i in range(6)])
def test_i_export_control(text, tier):
    assert m._check_export_control(text, {}).tier == tier


@pytest.mark.parametrize("text,tier,beneficial", [
    ("Contractor shall maintain commercial general liability insurance with limits of not less than $1,000,000 "
     "per occurrence.", "within", False),
    ("Sponsor shall, at its own expense, maintain product liability insurance.", "within", False),
    ("Licensee will maintain product liability insurance naming University as an additional insured.", "within",
     True),
], ids=["i-ins1", "i-ins2", "i-ins3"])
def test_i_insurance(text, tier, beneficial):
    res = m._check_insurance(text, {})
    assert res.tier == tier and bool(res.beneficial) is beneficial


@pytest.mark.parametrize("text,tier", [
    ("Customer may terminate this Agreement for convenience upon thirty (30) days' notice.", "within"),
    ("University may terminate this Agreement at any time for convenience.", "within"),
    ("Either party may terminate for material breach not cured within thirty (30) days.", "fallback"),
    ("This Agreement may be terminated in the event of non-appropriation of funds.", "within"),
    ("Customer may terminate this Agreement for any reason or no reason upon written notice.", "within"),
], ids=[f"i-term{i}" for i in range(5)])
def test_i_termination_for_convenience(text, tier):
    assert sw.check_termination(text, {}).tier == tier
