"""Deterministic party extraction (shared/parties.py) and its cross-check of
the model's parties in the classify stage. Offline: no model call is made."""
from __future__ import annotations

import re

import pytest

from shared.parties import (
    check_parties, counterparty_fallback, display_name, extract_parties, party_key, party_names, same_party,
)

_BODY = ("\n\n1. SERVICES\nVendor shall perform the Services described in Exhibit A.\n\n"
         "2. FEES\nClient shall pay the Fees within thirty (30) days of invoice.\n\n")


def _names(text: str) -> list[str]:
    return party_names(text + _BODY)


# ── preamble variants: (text, expected names in order, expected role labels or None) ──────────

PREAMBLES = [
    # US — descriptors, role labels, the classic "by and between"
    ('This Master Services Agreement (the "Agreement") is made by and between Acme Widgets, Inc., a Delaware '
     'corporation ("Client"), and Lakeshore Analytics LLC, an Illinois limited liability company ("Vendor").',
     ["Acme Widgets, Inc.", "Lakeshore Analytics LLC"], ["Client", "Vendor"]),
    ('This Agreement is entered into as of March 1, 2026 by and between Globex Corporation, with offices at '
     '500 Market Street, San Francisco, CA 94105 ("Globex") and Initech LLP, located at 1 Main St., Austin, TX.',
     ["Globex Corporation", "Initech LLP"], ["Globex", None]),
    ('THIS SERVICES AGREEMENT is made by and between HOOLI HOLDINGS, INC., A DELAWARE CORPORATION HAVING ITS '
     'PRINCIPAL PLACE OF BUSINESS AT 1 INFINITE LOOP, PALO ALTO ("HOOLI") AND PIED PIPER LLC ("PROVIDER").',
     ["Hooli Holdings, Inc.", "Pied Piper LLC"], ["HOOLI", "PROVIDER"]),
    ('This Consulting Agreement is made between Northwind Software GmbH, Hauptstrasse 5, 10115 Berlin '
     '("Licensor") and Contoso Analytics Ltd ("Licensee").',
     ["Northwind Software GmbH", "Contoso Analytics Ltd"], ["Licensor", "Licensee"]),
    # UK — numbered parties
    ('THIS AGREEMENT is dated 2 November 2025 and is made BETWEEN:\n(1) ACME HOLDINGS LIMITED (company number '
     '01234567) whose registered office is at 1 King Street, London EC2V 8AU ("Customer"); and\n(2) BLUEWATER '
     'SOLUTIONS LTD whose registered office is at 22 Harbour Road, Bristol ("Supplier").',
     ["Acme Holdings Limited", "Bluewater Solutions Ltd"], ["Customer", "Supplier"]),
    ('THIS AGREEMENT is dated 2 November 2025\n\nPARTIES\n\n(1) ALPINE SKI HOUSE PLC incorporated and registered '
     'in England and Wales with company number 987654 whose registered office is at 3 Strand, London '
     '("Client").\n\n(2) MARGIE\'S TRAVEL LIMITED incorporated and registered in Scotland ("Contractor").\n\n'
     'BACKGROUND\n\n(A) The Client wishes to appoint the Contractor.',
     ["Alpine Ski House PLC", "Margie's Travel Limited"], ["Client", "Contractor"]),
    ("This Agreement is made on 4 May 2025 between (1) Fabrikam Consulting LLP; and (2) Tailspin Toys Ltd.",
     ["Fabrikam Consulting LLP", "Tailspin Toys Ltd."], [None, None]),
    # "between A ('the University') and B" — single-quoted labels
    ("This Agreement is made on 1 September 2025 between the University of Leeds ('the University') and "
     "Coho Vineyard Ltd ('the Company').",
     ["University of Leeds", "Coho Vineyard Ltd"], ["University", "Company"]),
    # Universities and boards
    ('This Sponsored Research Agreement is entered into by and between THE REGENTS OF THE UNIVERSITY OF '
     'CALIFORNIA, on behalf of its San Diego campus ("University"), and NovaGen Therapeutics, Inc. ("Sponsor").',
     ["The Regents of the University of California", "NovaGen Therapeutics, Inc."], ["University", "Sponsor"]),
    ('This Agreement is made by and between The Board of Trustees of the Leland Stanford Junior University '
     '("Stanford") and BioCure Pharma S.A. ("Sponsor").',
     ["The Board of Trustees of the Leland Stanford Junior University", "BioCure Pharma S.A."],
     ["Stanford", "Sponsor"]),
    ('BY AND BETWEEN\nTHE BOARD OF REGENTS OF THE UNIVERSITY OF WISCONSIN SYSTEM\nand\nVANARSDEL, LTD.',
     ["The Board of Regents of the University of Wisconsin System", "Vanarsdel, Ltd."], [None, None]),
    ('This License Agreement is entered into by the Trustees of Princeton University ("Princeton") and '
     'Adatum Corporation ("Licensee").', ["Trustees of Princeton University", "Adatum Corporation"],
     ["Princeton", "Licensee"]),
    ('This Collaboration Agreement is made between Massachusetts Institute of Technology ("MIT"), 77 '
     'Massachusetts Avenue, Cambridge, MA 02139, and Coho Winery Pty Ltd ("Collaborator").',
     ["Massachusetts Institute of Technology", "Coho Winery Pty Ltd"], ["MIT", "Collaborator"]),
    # Government agencies
    ('This Interagency Agreement is entered into between the Department of Health and Human Services ("HHS") '
     'and Litware Research, Inc. ("Contractor").',
     ["Department of Health and Human Services", "Litware Research, Inc."], ["HHS", "Contractor"]),
    ('This Contract is entered into between the State of Ohio, acting by and through its Department of '
     'Administrative Services ("State"), and Proseware, Inc. ("Contractor").',
     ["State of Ohio", "Proseware, Inc."], ["State", "Contractor"]),
    ('This Space Act Agreement is made between the National Aeronautics and Space Administration ("NASA") and '
     'Relecloud Orbital Corp. ("Partner").',
     ["National Aeronautics and Space Administration", "Relecloud Orbital Corp."], ["NASA", "Partner"]),
    ('This Grant Agreement is made by the National Science Foundation ("NSF") and the Research Foundation for '
     'the State University of New York ("Recipient").',
     ["National Science Foundation", "Research Foundation for the State University of New York"],
     ["NSF", "Recipient"]),
    ("This Memorandum of Understanding is between the City of Springfield and Springfield Public Library "
     "Foundation.", ["City of Springfield", "Springfield Public Library Foundation"], [None, None]),
    # DBA / doing business as
    ('This Agreement is made by and between Acme Holdings, Inc. d/b/a Acme Health ("Client") and Beta Systems '
     'LLC ("Vendor").', ["Acme Holdings, Inc.", "Beta Systems LLC"], ["Client", "Vendor"]),
    ('This Staffing Agreement is between Contoso Staffing Group LLC doing business as Contoso Nurses '
     '("Agency") and Woodgrove Health System ("Client").',
     ["Contoso Staffing Group LLC", "Woodgrove Health System"], ["Agency", "Client"]),
    # "together the Parties"
    ('This Agreement is entered into between Lucerne Publishing, Inc. ("Lucerne") and Wide World Importers '
     'Ltd ("WWI") (together the "Parties" and each a "Party").',
     ["Lucerne Publishing, Inc.", "Wide World Importers Ltd"], ["Lucerne", "WWI"]),
    ('This Mutual NDA is made between Fourth Coffee Company and Trey Research Co. (each a "Party" and '
     'collectively the "Parties").', ["Fourth Coffee Company", "Trey Research Co."], [None, None]),
    # "A ("Sponsor") and B ("Institution")" with no "between"
    ('CLINICAL TRIAL AGREEMENT\nAdatum Biotech, Inc. ("Sponsor") and Duke University ("Institution") agree as '
     'follows:', ["Adatum Biotech, Inc.", "Duke University"], ["Sponsor", "Institution"]),
    # three parties
    ('This Clinical Trial Agreement is entered into by and among Duke University ("Institution"), Fourth Coffee '
     'Pharmaceuticals, Inc. ("Sponsor") and Dr. Alan Smith ("Investigator").',
     ["Duke University", "Fourth Coffee Pharmaceuticals, Inc.", "Dr. Alan Smith"],
     ["Institution", "Sponsor", "Investigator"]),
    # role written as a label; hereinafter without parentheses
    ("This Order Form is entered into between Customer: Lucerne Publishing, Inc. and Provider: Relecloud SaaS B.V.",
     ["Lucerne Publishing, Inc.", "Relecloud SaaS B.V."], ["Customer", "Provider"]),
    ('This Agreement is made between Graphic Design Institute, Inc., hereinafter referred to as "Company", and '
     'Blue Yonder Airlines LLC, hereinafter "Consultant".',
     ["Graphic Design Institute, Inc.", "Blue Yonder Airlines LLC"], ["Company", "Consultant"]),
    # OCR: line breaks and double spaces in the middle of names
    ('This Agreement is made by and between Acme  Corp.,  a Delaware\ncorporation ("Client")\nand Lakeshore\n'
     'LLC ("Vendor").', ["Acme Corp.", "Lakeshore LLC"], ["Client", "Vendor"]),
    ('THIS AGREEMENT is made by and between WOODGROVE BANK, N.A. ("Bank") and\nNORTHWIND TRADERS,\nINC. '
     '("Vendor").', ["Woodgrove Bank, N.A.", "Northwind Traders, Inc."], ["Bank", "Vendor"]),
    # ALL CAPS with acronyms
    ('THIS AGREEMENT IS MADE BY AND BETWEEN IBM CORPORATION ("IBM") AND NTT DATA, INC. ("NTT").',
     ["IBM Corporation", "NTT Data, Inc."], ["IBM", "NTT"]),
    # no labels, plain "and"
    ("This Teaming Agreement is entered into as of 11/02/2025 between Northwind Traders, Inc. and Wingtip Toys "
     "L.L.C. for the purpose of pursuing a federal contract.", ["Northwind Traders, Inc.", "Wingtip Toys L.L.C."],
     [None, None]),
    ("This Agreement is made between Contoso Widgets and Fabrikam Systems to provide design and build services.",
     ["Contoso Widgets", "Fabrikam Systems"], [None, None]),
    ("This Agreement is between the Department of Health and Human Services and Acme Inc.",
     ["Department of Health and Human Services", "Acme Inc."], [None, None]),
]


@pytest.mark.parametrize("text,names,roles", PREAMBLES, ids=[f"p{i:02d}" for i in range(len(PREAMBLES))])
def test_preamble_parties(text, names, roles):
    doc = text + _BODY
    got = [p for p in extract_parties(doc) if p["source"] == "preamble"]
    assert [p["name"] for p in got] == names
    assert [p["role_label"] for p in got] == roles
    for p in got:                                      # the span points at the name as written
        s, e = p["span"]
        assert same_party(doc[s:e], p["name"])


def test_entity_types_and_dba():
    got = extract_parties(PREAMBLES[18][0] + _BODY)
    assert got[0]["dba"] == "Acme Health" and got[0]["entity_type"] == "corporation"
    assert got[1]["entity_type"] == "llc"
    types = {p["name"]: p["entity_type"] for p in extract_parties(PREAMBLES[0][0] + _BODY)}
    assert types == {"Acme Widgets, Inc.": "corporation", "Lakeshore Analytics LLC": "llc"}
    types = {p["name"]: p["entity_type"] for text, *_ in PREAMBLES[8:17] for p in extract_parties(text + _BODY)}
    assert types["The Regents of the University of California"] == "university"
    assert types["Department of Health and Human Services"] == "government"
    assert types["National Aeronautics and Space Administration"] == "government"
    assert extract_parties(PREAMBLES[19][0] + _BODY)[0]["dba"] == "Contoso Nurses"


# ── signature blocks ─────────────────────────────────────────────────────────

_WITNESS = "IN WITNESS WHEREOF, the parties have executed this Agreement as of the Effective Date.\n\n"


def test_signature_block_company_lines():
    doc = ("SERVICES AGREEMENT" + _BODY + _WITNESS +
           "ACME CORP.\nBy: ____________\nName: Jane Doe\nTitle: CFO\n\n"
           "LAKESHORE LLC\nBy: ____________\nName: Sam Hill\nTitle: CEO\n")
    got = extract_parties(doc)
    assert [(p["name"], p["source"]) for p in got] == [("Acme Corp.", "signature"), ("Lakeshore LLC", "signature")]
    s, e = got[0]["span"]
    assert doc[s:e] == "ACME CORP."


def test_signature_block_labels_columns_and_on_behalf_of():
    doc = ("AGREEMENT" + _BODY + _WITNESS +
           "CLIENT: GLOBEX INC.\nBy: ____\nName: Ann Lee\n\n"
           "Signed by Tom Brown\nfor and on behalf of BLUEWATER SOLUTIONS LTD\n\n"
           "Executed by ALPINE SKI HOUSE PLC acting by a director\nName: Rob Fox\n\n"
           "CONTOSO UNIVERSITY          FABRIKAM, INC.\nBy: ________          By: ________\n")
    got = {p["name"]: p["role_label"] for p in extract_parties(doc)}
    assert got == {"Globex Inc.": "Client", "Bluewater Solutions Ltd": None, "Alpine Ski House PLC": None,
                   "Contoso University": None, "Fabrikam, Inc.": None}


def test_signature_confirms_and_labels_preamble_parties():
    doc = (PREAMBLES[29][0] + _BODY + _WITNESS + "NORTHWIND TRADERS, INC.\nBy: ____\nName: Gus Park\n\n"
           "VENDOR: WINGTIP TOYS L.L.C.\nBy: ____\n")
    got = extract_parties(doc)
    assert [(p["name"], p["source"]) for p in got] == [("Northwind Traders, Inc.", "preamble"),
                                                       ("Wingtip Toys L.L.C.", "preamble")]
    assert got[1]["role_label"] == "Vendor"


def test_signature_people_and_placeholders_are_not_parties():
    doc = ("AGREEMENT" + _BODY + _WITNESS + "[COMPANY NAME]\nBy: ____\nName: ____\n\n"
           "Signed by Tom Brown\nName: Tom Brown\n\nAuthorized Signature: ____\nName: Pat Doe\n")
    assert extract_parties(doc) == []


# ── never guess ──────────────────────────────────────────────────────────────

NEGATIVE = [
    "",
    "1. SERVICES\nThe Contractor shall perform the services.\n\n2. FEES\nFees are due monthly.",
    'This Agreement is made by and between [CLIENT NAME] ("Client") and [VENDOR NAME] ("Vendor").',
    "This Agreement is between the Company and the Contractor.",
    "This Agreement is entered into between the Company and you.",
    "This Agreement is made between ____________ and ____________.",
    'This Agreement is made by and between Acme Corp. ("Client") and <<Supplier Legal Name>> ("Supplier").',
    'This Agreement is made between Client Name ("Client") and Vendor Name ("Vendor").',
    "Disputes between the parties shall be resolved by arbitration in New York.",
    'The City of Springfield ("City") requests quotations from qualified vendors.',
    "This Agreement is made on {{effective_date}} between {{client}} and {{vendor}}.",
]


@pytest.mark.parametrize("text", NEGATIVE, ids=[f"n{i:02d}" for i in range(len(NEGATIVE))])
def test_no_confident_match_returns_nothing(text):
    assert extract_parties(text + _BODY) == []


def test_bad_input_returns_nothing():
    assert extract_parties(None) == [] and extract_parties("   ") == []          # type: ignore[arg-type]


# ── normalisation and comparison ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,shown", [
    ('"ACME CORP."', "Acme Corp."),
    ("THE REGENTS OF THE UNIVERSITY OF CALIFORNIA", "The Regents of the University of California"),
    ("IBM CORPORATION", "IBM Corporation"),
    ("MARGIE'S TRAVEL LIMITED", "Margie's Travel Limited"),
    ("NORTHWIND  SOFTWARE\nGMBH", "Northwind Software GmbH"),
    ("[Lakeshore LLC]", "Lakeshore LLC"),
    ("BioCure Pharma S.A.", "BioCure Pharma S.A."),
    ("AT&T SERVICES, INC.", "AT&T Services, Inc."),
])
def test_display_name(raw, shown):
    assert display_name(raw) == shown


def test_same_party_ignores_suffixes_case_and_punctuation():
    assert party_key("The ACME Corp.") == party_key("Acme Corporation") == "acme"
    assert same_party("Wingtip Toys L.L.C.", "Wingtip Toys LLC")
    assert same_party("Regents of the University of California", "The Regents of the University of California")
    assert same_party("Acme Widgets", "ACME WIDGETS HOLDINGS, INC.")
    assert not same_party("Globex Inc.", "Initech LLC")
    assert not same_party("Co.", "Acme Co.")


def test_check_parties_reports_matched_model_only_and_text_only():
    doc = PREAMBLES[0][0] + _BODY
    out = check_parties(["ACME WIDGETS INC", "Phantom Holdings Ltd"], doc)
    assert out["matched"] == ["ACME WIDGETS INC"]
    assert out["modelOnly"] == ["Phantom Holdings Ltd"] and out["notInText"] == ["Phantom Holdings Ltd"]
    assert out["textOnly"] == ["Lakeshore Analytics LLC"]


def test_counterparty_fallback_skips_the_organization():
    us = re.compile(r"\buniversity\b", re.I)
    text = PREAMBLES[8][0]
    assert counterparty_fallback(text, us) == "NovaGen Therapeutics, Inc."
    assert counterparty_fallback("No parties here.", us) is None


# ── classify wiring ──────────────────────────────────────────────────────────

from test_classify_pipeline import DOC, env, facts  # noqa: E402,F401  (fixture reuse)

_CONTRACT = ('SERVICES AGREEMENT\nThis Agreement is made by and between Acme Widgets, Inc., a Delaware corporation '
             '("Client"), and Lakeshore Analytics LLC ("Vendor").\n\n'
             "1. SERVICES\nVendor shall perform the services.\n\n2. FEES\nClient shall pay the fees.\n")


def test_classify_fills_parties_when_the_model_returned_none(env):
    model, run, _ = env
    model.facts = facts(parties=[])
    out = run(_CONTRACT)
    assert out["parties"] == ["Acme Widgets, Inc.", "Lakeshore Analytics LLC"]
    check = out["extraction"]["partyCheck"]
    assert check["source"] == "rule" and check["filled"] is True
    assert out["extraction"]["partiesSource"] == "rule"
    assert [p["source"] for p in check["extracted"]] == ["preamble", "preamble"]
    assert any("taken from the preamble" in i for i in out["confidence"]["issues"])


def test_classify_flags_a_party_that_is_not_in_the_document(env):
    model, run, _ = env
    model.facts = facts(parties=["Acme Widgets, Inc.", "Phantom Holdings Ltd"])
    out = run(_CONTRACT)
    assert out["parties"] == ["Acme Widgets, Inc.", "Phantom Holdings Ltd"]        # the model's list is kept
    check = out["extraction"]["partyCheck"]
    assert check["matched"] == ["Acme Widgets, Inc."] and check["modelOnly"] == ["Phantom Holdings Ltd"]
    assert check["textOnly"] == ["Lakeshore Analytics LLC"] and check["filled"] is False
    assert 'Party "Phantom Holdings Ltd" not found in the document.' in out["confidence"]["issues"]
    assert "partiesSource" not in out["extraction"]


def test_classify_agreeing_parties_add_no_issue(env):
    model, run, _ = env
    model.facts = facts(parties=["ACME WIDGETS INC", "Lakeshore Analytics, L.L.C."])
    out = run(_CONTRACT)
    check = out["extraction"]["partyCheck"]
    assert len(check["matched"]) == 2 and not check["modelOnly"] and not check["textOnly"]
    assert not any("part" in i.lower() for i in out["confidence"]["issues"])


def test_classify_without_a_rule_result_changes_nothing(env):
    model, run, _ = env
    model.facts = facts(parties=[])
    out = run("Just a short note with no contract terms in it.")
    assert out["parties"] == [] and out["extraction"]["partyCheck"]["extracted"] == []
    assert out["extraction"]["partyCheck"]["filled"] is False


def test_classify_rule_filled_parties_survive_reuse(env):
    model, run, store = env
    model.facts = facts(parties=[])
    first = run(_CONTRACT, checksum="same")
    store["meta"] = {"docId": DOC, "checksum": "same"}
    again = run(_CONTRACT, checksum="same")                                       # same checksum: previous output reused
    assert again["extraction"]["reused"] is True
    assert again["parties"] == first["parties"]
    assert again["extraction"]["partyCheck"]["filled"] is True


# ── workflow counterparty fallback ───────────────────────────────────────────

def test_infer_fields_falls_back_to_the_preamble_for_the_counterparty():
    from shared.govern import workflow
    header = PREAMBLES[8][0]
    out = workflow.infer_fields({"docType": "SOW"}, {"parties": []}, header)
    assert out["counterparty"] == "NovaGen Therapeutics, Inc."
    out = workflow.infer_fields({"docType": "SOW"}, {"parties": ["Globex Inc."]}, header)
    assert out["counterparty"] == "Globex Inc."                    # the analysis still wins
