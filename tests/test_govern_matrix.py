"""Northfield review matrix: deterministic grading, import, validation and the
post-signature extractors (shared/govern/matrix.py).

The demo story is graded from the sample agreements in ``samples/research``. The
pipeline's classify stage is what normally labels each clause with a category;
these tests are offline, so ``_sample_clauses`` stands in for it: it cuts a
sample at its numbered top-level headings ("3. FEES, ROYALTIES AND EQUITY") and
assigns the category from ``HEADING_CATEGORIES`` — the label the classify prompt
asks the model to give that heading. Text before the first heading becomes the
Preamble clause, as in the pipeline.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from shared import clause_types as ct
from shared.govern import matrix as m
from stages import classify_prompts

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "research"
NOW = "2026-10-08T12:00:00Z"

HEADING_CATEGORIES = {
    "DEFINITIONS": "Definitions", "LICENSE GRANT": "LicenseScope", "FEES, ROYALTIES AND EQUITY": "Royalties",
    "SUBLICENSING": "Sublicensing", "DILIGENCE": "Diligence", "REPORTS AND RECORDS": "SponsorReporting",
    "PATENT PROSECUTION AND OWNERSHIP": "BackgroundIP", "CONFIDENTIALITY": "Confidentiality",
    "USE OF NAMES": "DataRights", "WARRANTIES": "Warranty", "INDEMNIFICATION AND INSURANCE": "Indemnity",
    "EXPORT CONTROL": "ExportControl", "TERM AND TERMINATION": "Termination", "GOVERNING LAW": "GoverningLaw",
    "OPTION GRANT": "LicenseScope", "OPTION PERIOD": "Term", "OPTION FEE AND LICENSE TERMS": "Royalties",
    "OWNERSHIP": "BackgroundIP", "INDEMNIFICATION": "Indemnity", "RESEARCH": "ScopeOfWork", "PAYMENT": "Payment",
    "REPORTS": "SponsorReporting", "PUBLICATION": "PublicationRights", "INTELLECTUAL PROPERTY": "BackgroundIP",
    "CONFIDENTIALITY AND USE OF NAMES": "DataRights", "MATERIAL": "ScopeOfWork", "USE OF MATERIAL": "Restrictions",
    "OWNERSHIP AND INVENTIONS": "BackgroundIP", "LIABILITY AND INDEMNIFICATION": "Indemnity", "TERM": "Term",
    "CONFIDENTIAL INFORMATION": "Definitions", "CONFIDENTIALITY OBLIGATIONS": "Confidentiality",
    "PERIOD OF PERFORMANCE AND FUNDING": "Payment", "REPORTS AND FLOW-DOWN TERMS": "SponsorReporting",
    "EXPORT CONTROL AND FOREIGN NATIONALS": "ExportControl", "LIABILITY": "Indemnity", "GENERAL": "Other",
}
_HEADING_RE = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Z ,&/\-()]+?)\s*$", re.MULTILINE)


def _sample_clauses(name: str) -> list[dict]:
    text = (SAMPLES / name).read_text(encoding="utf-8")
    heads = list(_HEADING_RE.finditer(text))
    assert heads, f"{name} has no numbered headings"
    clauses = [{"id": "c0", "number": "0", "title": "Preamble", "body": text[:heads[0].start()].strip(),
                "category": "Other", "specificTypeKey": "recitals"}]
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        heading = h.group(2)
        assert heading in HEADING_CATEGORIES, f"unmapped heading {heading!r} in {name}"
        clauses.append({"id": f"c{h.group(1)}", "number": h.group(1), "title": heading.title(),
                        "body": text[h.end():end].strip(), "category": HEADING_CATEGORIES[heading]})
    return clauses


def _review(name: str, agreement_type: str, matrix: dict | None = None) -> dict:
    return m.review_document(_sample_clauses(name), agreement_type, matrix or m.default_matrix(), "doc-1", NOW)


def _flagged(review: dict) -> dict[str, str]:
    return {c["clauseType"]: c["tier"] for c in review["clauses"] if c["tier"] in ("deviates", "unacceptable")}


def _tier(review: dict, clause_type: str) -> str:
    return next(c["tier"] for c in review["clauses"] if c["clauseType"] == clause_type)


def _one(clause_type: str, body: str, agreement_type: str = "sponsored_research",
         matrix: dict | None = None) -> dict:
    """Review a single clause and return its result row."""
    review = m.review_document([{"id": "x", "number": "4", "title": "T", "body": body, "category": clause_type}],
                               agreement_type, matrix or m.default_matrix(), "d", NOW)
    return next(c for c in review["clauses"] if c["clauseType"] == clause_type)


# ---------------------------------------------------------------------------
# Demo story
# ---------------------------------------------------------------------------


def test_demo_license_v1_flags_exactly_three_clauses():
    review = _review("01-exclusive-license-v1.txt", "license")
    assert _flagged(review) == {"LicenseScope": "deviates", "Royalties": "deviates", "GoverningLaw": "unacceptable"}
    assert review["counts"]["missing"] == 0 and review["counts"]["review"] == 0
    assert review["matrixVersion"] == 1 and review["matrixEffectiveDate"] == "2026-10-08"
    royalties = next(c for c in review["clauses"] if c["clauseType"] == "Royalties")
    assert "1%" in royalties["reason"] and "sublicense" in royalties["reason"]
    assert royalties["beneficial"] and "equity" in royalties["beneficialReason"].lower()
    assert royalties["clauseNumber"] == "3" and royalties["quote"].startswith("3.1 License Issue Fee")


def test_demo_license_v2_revision_is_clean():
    review = _review("01-exclusive-license-v2-revised.txt", "license")
    assert _flagged(review) == {}
    assert review["counts"]["deviates"] == review["counts"]["unacceptable"] == review["counts"]["missing"] == 0
    assert m.sonar_blockers(review) == []
    diligence = next(c for c in review["clauses"] if c["clauseType"] == "Diligence")
    assert diligence["tier"] == "within" and diligence["beneficial"]


def test_demo_blockers_have_stable_ids_offices_and_redlines():
    blockers = m.sonar_blockers(_review("01-exclusive-license-v1.txt", "license"))
    by_id = {b["id"]: b for b in blockers}
    assert set(by_id) == {"sonar-LicenseScope-2", "sonar-Royalties-3", "sonar-GoverningLaw-14"}
    law = by_id["sonar-GoverningLaw-14"]
    assert law["office"] == "legal_affairs"                 # unacceptable → its escalation office
    assert "home state" in law["suggestedLanguage"] and "sovereign immunity" in law["suggestedLanguage"]
    assert by_id["sonar-Royalties-3"]["office"] is None     # a plain deviation is sent back, not escalated
    assert by_id["sonar-Royalties-3"]["text"].startswith("Running royalty is 1% of net sales")
    assert all(b["source"] == "sonar" and b["status"] == "open" and b["createdAt"] == NOW for b in blockers)


@pytest.mark.parametrize("name,agreement_type,expected", [
    ("02-option-agreement.txt", "option", {}),
    ("03-sponsored-research-agreement.txt", "sponsored_research",
     {"PublicationRights": "deviates", "Indemnity": "unacceptable"}),
    ("04-material-transfer-agreement.txt", "mta", {}),
    ("05-mutual-nda.txt", "nda", {"Confidentiality": "deviates"}),
    ("06-federal-subaward-grant.txt", "grant", {"ExportControl": "unacceptable"}),
])
def test_other_samples_flag_what_they_were_written_to_flag(name, agreement_type, expected):
    review = _review(name, agreement_type)
    assert _flagged(review) == expected
    assert review["counts"]["missing"] == 0


def test_samples_carry_fallbacks_where_intended():
    assert _tier(_review("02-option-agreement.txt", "option"), "Term") == "fallback"          # 15-month option
    mta = _review("04-material-transfer-agreement.txt", "mta")
    assert _tier(mta, "Indemnity") == "fallback"          # only to the extent permitted by Minnesota law
    assert _tier(mta, "GoverningLaw") == "fallback"       # silent on governing law


def test_publication_reason_is_a_plain_sentence():
    review = _review("03-sponsored-research-agreement.txt", "sponsored_research")
    pub = next(c for c in review["clauses"] if c["clauseType"] == "PublicationRights")
    assert pub["reason"] == "Publication review period is 90 days; the matrix allows 30 (fallback 60)."
    blocker = next(b for b in m.sonar_blockers(review) if b["clauseType"] == "PublicationRights")
    assert blocker["text"] == pub["reason"] and blocker["id"] == "sonar-PublicationRights-4"


def test_export_restriction_escalates_to_export_control():
    blockers = m.sonar_blockers(_review("06-federal-subaward-grant.txt", "grant"))
    assert [(b["clauseType"], b["office"]) for b in blockers] == [("ExportControl", "export_control")]


def test_review_is_deterministic():
    a = _review("01-exclusive-license-v1.txt", "license")
    b = _review("01-exclusive-license-v1.txt", "license")
    assert a == b
    assert m.sonar_blockers(a) == m.sonar_blockers(b)


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,tier", [
    ("University may publish the results. Sponsor may review each manuscript for thirty (30) days.", "within"),
    ("University may publish. Sponsor has thirty (30) days to review and may request a delay of an additional "
     "thirty (30) days to file a patent application.", "fallback"),
    ("University may publish. Sponsor shall review each manuscript for ninety (90) days.", "deviates"),
    ("University shall not publish any results without the prior written approval of Sponsor.", "unacceptable"),
])
def test_publication_tiers(body, tier):
    assert _one("PublicationRights", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("Licensee shall pay a running royalty of three percent (3%) of Net Sales and twenty-five percent (25%) of "
     "Sublicense Income.", "within"),
    ("Licensee shall pay a running royalty of two and one-half percent (2.5%) of Net Sales. Licensee shall pay "
     "University twenty percent (20%) of Sublicense Income.", "fallback"),
    ("Licensee shall pay a running royalty of one percent (1%) of Net Sales.", "deviates"),
    ("The license is fully paid-up and royalty-free.", "unacceptable"),
])
def test_royalty_tiers(body, tier):
    assert _one("Royalties", body, "license")["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("Sponsor shall indemnify, defend and hold harmless University from all claims.", "within"),
    ("University shall indemnify Sponsor only to the extent permitted by the laws of the State of Minnesota.", "fallback"),
    ("Each party shall indemnify the other party against third-party claims.", "unacceptable"),
    ("University agrees to indemnify and hold harmless Sponsor from any losses.", "unacceptable"),
])
def test_indemnity_tiers_for_a_public_university(body, tier):
    assert _one("Indemnity", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("This Agreement is governed by the laws of the State of Minnesota. Nothing herein waives the sovereign "
     "immunity of University.", "within"),
    ("Each party bears its own costs of any dispute.", "fallback"),
    ("This Agreement is governed by the laws of the State of New York.", "deviates"),
    ("This Agreement is governed by the laws of the State of Minnesota. University hereby waives its sovereign "
     "immunity.", "unacceptable"),
])
def test_governing_law_tiers(body, tier):
    assert _one("GoverningLaw", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("The Research is fundamental research. University will not accept restrictions on the participation of "
     "foreign nationals.", "within"),
    ("Sponsor may provide export-controlled technical data to the Principal Investigator.", "deviates"),
    ("Only U.S. citizens may work on the Project.", "unacceptable"),
])
def test_export_control_tiers(body, tier):
    assert _one("ExportControl", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("Confidentiality obligations survive for five (5) years.", "within"),
    ("Confidentiality obligations survive for seven (7) years.", "fallback"),
    ("Confidentiality obligations survive for ten (10) years.", "deviates"),
    ("Sponsor may use the name of the University in its marketing.", "deviates"),
    ("All research data shall be the sole property of Sponsor.", "deviates"),
])
def test_data_rights_tiers(body, tier):
    assert _one("DataRights", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("The Principal Investigator shall provide quarterly progress reports.", "within"),
    ("The Principal Investigator shall provide monthly progress reports.", "fallback"),
    ("All terms of the prime award are flowed down to University.", "deviates"),
])
def test_sponsor_reporting_tiers(body, tier):
    assert _one("SponsorReporting", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("Each party retains its background IP. Inventions made by University employees are owned by "
     "University.", "within"),
    ("All inventions made under this Agreement shall be owned by Sponsor.", "unacceptable"),
    ("The Research is a work made for hire.", "unacceptable"),
])
def test_background_ip_tiers(body, tier):
    assert _one("BackgroundIP", body)["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("University grants an exclusive license in the Field of Use. University reserves the right to practise "
     "the Licensed Patents for research and educational purposes.", "within"),
    ("University grants an exclusive license in all fields. University reserves the right to practise the "
     "Licensed Patents for research and educational purposes.", "fallback"),
    ("University grants an exclusive, worldwide license in all fields of use.", "deviates"),
])
def test_license_scope_tiers(body, tier):
    assert _one("LicenseScope", body, "license")["tier"] == tier


@pytest.mark.parametrize("body,tier", [
    ("Licensee shall meet the milestones by June 30, 2027 and June 30, 2028. If Licensee fails to meet a "
     "milestone, University may terminate this Agreement.", "within"),
    ("Licensee shall use commercially reasonable efforts to commercialize Licensed Products.", "deviates"),
])
def test_diligence_tiers(body, tier):
    assert _one("Diligence", body, "license")["tier"] == tier


def test_beneficial_is_independent_of_tier():
    row = _one("Royalties", "Licensee shall pay a running royalty of one percent (1%) of Net Sales. Licensee shall "
                            "issue University shares of common stock equal to five percent (5%).", "license")
    assert row["tier"] == "deviates" and row["beneficial"] is True
    assert "5%" in row["beneficialReason"]


def test_negated_unacceptable_phrase_does_not_count():
    row = _one("GoverningLaw", "Governed by the laws of the State of Minnesota. Nothing in this Agreement waives "
                               "sovereign immunity.")
    assert row["tier"] == "within"


def test_thresholds_come_from_the_matrix():
    matrix = m.default_matrix()
    for clause in matrix["playbooks"]["sponsored_research"]["clauses"]:
        if clause["clauseType"] == "PublicationRights":
            clause["thresholds"] = {"maxReviewDays": 90, "fallbackReviewDays": 120}
    body = "University may publish. Sponsor shall review each manuscript for ninety (90) days."
    assert _one("PublicationRights", body, matrix=matrix)["tier"] == "within"


def test_required_clause_absent_is_missing_and_blocks():
    review = m.review_document([{"id": "a", "number": "1", "title": "Scope", "body": "Research.",
                                 "category": "ScopeOfWork"}], "sponsored_research", m.default_matrix(), "d", NOW)
    missing = {c["clauseType"] for c in review["clauses"] if c["tier"] == "missing"}
    required = {c["clauseType"] for c in m.default_matrix()["playbooks"]["sponsored_research"]["clauses"]
                if c["required"]}
    assert missing == required
    assert review["counts"]["missing"] == len(required)
    blockers = m.sonar_blockers(review)
    assert {b["id"] for b in blockers} == {f"sonar-{t}" for t in required}
    export = next(b for b in blockers if b["clauseType"] == "ExportControl")
    assert export["office"] == "export_control"          # escalateOnDeviation applies to missing too


def test_clause_found_by_heading_when_category_differs():
    clauses = [{"id": "p", "number": "4", "title": "Publication", "body": "Sponsor may review for 30 days.",
                "category": "Other", "specificTypeKey": "academic-freedom"}]
    review = m.review_document(clauses, "sponsored_research", m.default_matrix(), "d", NOW)
    pub = next(c for c in review["clauses"] if c["clauseType"] == "PublicationRights")
    assert pub["tier"] == "within" and pub["clauseId"] == "p"


def test_unknown_agreement_type_uses_other_playbook():
    review = m.review_document([], "something", m.default_matrix(), "d", NOW)
    assert {c["clauseType"] for c in review["clauses"]} == {"Indemnity", "GoverningLaw"}


# ---------------------------------------------------------------------------
# Default matrix and validation
# ---------------------------------------------------------------------------


def test_default_matrix_covers_every_agreement_type():
    matrix = m.default_matrix()
    assert matrix["version"] == 1 and matrix["note"] == "Default research and licensing matrix (edit to match your positions)"
    assert list(matrix["playbooks"]) == m.AGREEMENT_TYPES
    for agreement_type, book in matrix["playbooks"].items():
        assert book["clauses"], agreement_type
        for c in book["clauses"]:
            assert c["standard"] and c["suggestedLanguage"] and c["label"]
            assert c["escalationOffice"] in m.OFFICES
    license_types = [c["clauseType"] for c in matrix["playbooks"]["license"]["clauses"]]
    assert license_types[:4] == ["LicenseScope", "Royalties", "Diligence", "BackgroundIP"]
    clean, err = m.validate_matrix(matrix["playbooks"])
    assert err is None and clean == matrix["playbooks"]


@pytest.mark.parametrize("mutate,message", [
    (lambda p: p.update({"loan": p["license"]}), "Unknown agreement type"),
    (lambda p: p["license"]["clauses"][0].update({"colour": "red"}), "unknown field"),
    (lambda p: p["license"]["clauses"][0].update({"clauseType": "Nonsense"}), "clauseType"),
    (lambda p: p["license"]["clauses"][0].update({"standard": " "}), "standard"),
    (lambda p: p["license"]["clauses"][0].update({"escalationOffice": "dean"}), "escalationOffice"),
    (lambda p: p["license"]["clauses"][1].update({"thresholds": {"bogus": 1}}), "unknown threshold"),
    (lambda p: p["license"]["clauses"][1].update({"thresholds": {"minRoyaltyPct": "3"}}), "must be a number"),
    (lambda p: p["license"]["clauses"][0].update({"unacceptable": ["x" * 300]}), "unacceptable / beneficial"),
    (lambda p: p["license"]["clauses"].append(copy.deepcopy(p["license"]["clauses"][0])), "more than once"),
    (lambda p: p["license"]["clauses"][0].update({"required": "yes"}), "required"),
])
def test_validate_matrix_rejects_junk(mutate, message):
    playbooks = copy.deepcopy(m.default_matrix()["playbooks"])
    mutate(playbooks)
    clean, err = m.validate_matrix(playbooks)
    assert clean is None and message in err


def test_validate_matrix_cleans_a_minimal_playbook():
    clean, err = m.validate_matrix({"nda": {"clauses": [
        {"clauseType": "Confidentiality", "standard": "  Five years.  ", "unacceptable": "in perpetuity; forever",
         "escalationOffice": None}]}})
    assert err is None
    clause = clean["nda"]["clauses"][0]
    assert clause["standard"] == "Five years." and clause["unacceptable"] == ["in perpetuity", "forever"]
    assert clause["label"] == "Confidentiality" and clause["required"] is True
    assert m.validate_matrix({})[1] and m.validate_matrix([])[1]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_csv_with_labels_offices_and_lists():
    csv_text = (
        "Clause type,Standard position,Fallback,Unacceptable terms,Escalation office\n"
        "Publication rights and review period,Publish after 30 days,60 days,sponsor approval; veto,Sponsored Programs\n"
        "export control,Fundamental research,,foreign nationals shall not,OSP\n"
        "Governing Law (Minnesota) and sovereign immunity,Minnesota law,,waives sovereign immunity,Office of Legal Affairs\n"
        "Background IP,Northfield owns its inventions,,,\n"
        "Flux capacitor,Something,,,\n"
        ",,,,\n"
        "Indemnity,Northfield does not indemnify,,,Dean's office\n"
        "Royalties,,,,\n"
    )
    clauses, skipped = m.parse_import("sponsored_research", csv_text=csv_text)
    assert [c["clauseType"] for c in clauses] == ["PublicationRights", "ExportControl", "GoverningLaw", "BackgroundIP"]
    pub = clauses[0]
    assert pub["unacceptable"] == ["sponsor approval", "veto"] and pub["escalationOffice"] == "sponsored_programs"
    assert pub["thresholds"] == {"maxReviewDays": 30, "fallbackReviewDays": 60}
    assert clauses[1]["escalationOffice"] == "sponsored_programs"
    assert clauses[2]["escalationOffice"] == "legal_affairs"
    assert clauses[3]["escalationOffice"] is None
    assert [s["row"] for s in skipped] == [5, 7, 8]
    assert "unknown clause type" in skipped[0]["reason"]
    assert "escalation office" in skipped[1]["reason"]
    assert "standard" in skipped[2]["reason"]


def test_import_rows_and_thresholds_and_duplicates():
    rows = [
        {"clauseType": "PublicationRights", "standard": "45 days", "thresholds": "maxReviewDays=45"},
        {"clauseType": "PublicationRights", "standard": "again"},
        {"clauseType": "Diligence", "standard": "Milestones", "required": "no", "beneficial": "equity"},
        {"clauseType": "Payment", "standard": "x", "thresholds": "net=thirty"},
    ]
    clauses, skipped = m.parse_import("license", rows=rows)
    assert clauses[0]["thresholds"]["maxReviewDays"] == 45 and clauses[0]["thresholds"]["fallbackReviewDays"] == 60
    assert clauses[1]["required"] is False and clauses[1]["beneficial"] == ["equity"]
    assert [s["row"] for s in skipped] == [2, 4]
    with pytest.raises(ValueError):
        m.parse_import("loan", rows=[])


def test_sample_matrix_csv_round_trips_the_default_matrix():
    csv_text = (SAMPLES / "review-matrix.csv").read_text(encoding="utf-8")
    defaults = m.default_matrix()["playbooks"]
    for agreement_type, other in (("license", "sponsored_research"), ("sponsored_research", "license")):
        clauses, skipped = m.parse_import(agreement_type, csv_text=csv_text)
        assert clauses == defaults[agreement_type]["clauses"]
        assert skipped and all(f"'{other}'" in s["reason"] for s in skipped)


def test_merge_and_replace():
    playbooks = m.default_matrix()["playbooks"]
    new_pub = m.parse_import("sponsored_research", rows=[
        {"clauseType": "PublicationRights", "standard": "Publish after 45 days", "thresholds": "maxReviewDays=45"}])[0]
    extra = m.parse_import("sponsored_research", rows=[{"clauseType": "Insurance", "standard": "Sponsor insures"}])[0]
    merged = m.merge_import(playbooks, "sponsored_research", new_pub + extra, "merge")
    types = [c["clauseType"] for c in merged["sponsored_research"]["clauses"]]
    assert types[0] == "PublicationRights" and types[-1] == "Insurance"
    assert len(types) == len(playbooks["sponsored_research"]["clauses"]) + 1
    assert merged["sponsored_research"]["clauses"][0]["standard"] == "Publish after 45 days"
    assert merged["license"] == playbooks["license"]
    assert playbooks["sponsored_research"]["clauses"][0]["standard"] != "Publish after 45 days"   # input untouched
    replaced = m.merge_import(playbooks, "sponsored_research", new_pub, "replace")
    assert [c["clauseType"] for c in replaced["sponsored_research"]["clauses"]] == ["PublicationRights"]
    with pytest.raises(ValueError):
        m.merge_import(playbooks, "license", [], "append")


def test_imported_threshold_change_changes_the_grade():
    matrix = m.default_matrix()
    clauses, _ = m.parse_import("sponsored_research", rows=[
        {"clauseType": "Publication rights", "standard": "90 days is fine",
         "thresholds": "maxReviewDays=90; fallbackReviewDays=120"}])
    matrix["playbooks"] = m.merge_import(matrix["playbooks"], "sponsored_research", clauses, "merge")
    review = _review("03-sponsored-research-agreement.txt", "sponsored_research", matrix)
    assert _tier(review, "PublicationRights") == "within"


# ---------------------------------------------------------------------------
# Agreement type, direction, income and obligations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meta,expected", [
    ({"docType": "LICENSE", "title": "Exclusive Option Agreement - Cedar Robotics"}, "option"),
    ({"docType": "LICENSE", "title": "Exclusive License Agreement"}, "license"),
    ({"docType": "OTHER", "title": "Material Transfer Agreement (incoming)"}, "mta"),
    ({"docType": "OTHER", "title": "Sponsored Research Agreement"}, "sponsored_research"),
    ({"docType": "OTHER", "title": "Federal Subaward Agreement"}, "grant"),
    ({"docType": "OTHER", "title": "Research Collaboration Agreement"}, "collaboration"),
    ({"docType": "NDA", "title": "agreement.pdf"}, "nda"),
    ({"docType": "OTHER", "title": "Mutual Confidential Disclosure Agreement"}, "nda"),
    ({"docType": "OTHER", "title": "scan_0042.pdf", "summary": "A subaward under a prime award from NSF."}, "grant"),
    ({"docType": "MSA", "title": "Master Services Agreement"}, "other"),
])
def test_infer_agreement_type(meta, expected):
    assert m.infer_agreement_type(meta, None) == expected


def test_infer_direction():
    lic = {"clauses": _sample_clauses("01-exclusive-license-v1.txt")}
    sub = {"clauses": _sample_clauses("06-federal-subaward-grant.txt")}
    assert m.infer_direction("license", lic) == "incoming"
    assert m.infer_direction("grant", sub) == "outgoing"
    assert m.infer_direction("sponsored_research", None) == "incoming"


def test_extract_income_from_the_license():
    income = m.extract_income({"clauses": _sample_clauses("01-exclusive-license-v2-revised.txt")}, "license")
    by_kind = {}
    for item in income:
        by_kind.setdefault(item["kind"], []).append(item)
    assert by_kind["upfront"][0]["amount"] == 75000 and by_kind["upfront"][0]["expectedDate"] == "2026-10-31"
    assert [(i["amount"], i["expectedDate"]) for i in by_kind["milestone"]] == [
        (50000, "2027-06-30"), (150000, "2028-12-31"), (250000, "2029-06-30")]
    assert {i["pct"] for i in by_kind["royalty"] if i["pct"]} == {3.5}
    assert any(i["amount"] == 10000 for i in by_kind["royalty"])           # minimum annual royalty
    assert by_kind["equity"][0]["pct"] == 5 and by_kind["sublicense"][0]["pct"] == 25
    assert [i["id"] for i in income] == [f"sonar-income-{n}" for n in range(1, len(income) + 1)]
    assert all(i["source"] == "sonar" for i in income)


def test_extract_income_sponsor_funding_and_subaward():
    sra = m.extract_income({"clauses": _sample_clauses("03-sponsored-research-agreement.txt")}, "sponsored_research")
    assert [(i["kind"], i["amount"]) for i in sra] == [("sponsor_funding", 425000)]
    sub = m.extract_income({"clauses": _sample_clauses("06-federal-subaward-grant.txt")}, "grant")
    assert [(i["kind"], i["amount"]) for i in sub] == [("subaward", 180000)]
    fallback = m.extract_income({"clauses": [], "commercials": {"totalContractValue": 99000}}, "grant")
    assert fallback[0]["amount"] == 99000
    assert m.extract_income(None, "license") == []


def test_extract_obligations_after_signing():
    cls = {"clauses": _sample_clauses("03-sponsored-research-agreement.txt"),
           "timeline": {"endDate": "2028-10-31"}}
    obligations = m.extract_obligations(cls, "sponsored_research", "2026-11-10T15:00:00Z")
    kinds = {(o["kind"], o["dueDate"]) for o in obligations}
    assert ("sponsor_report", "2027-02-10") in kinds                     # quarterly → 3 months after signing
    assert ("sponsor_report", "2029-01-29") in kinds                     # final report: term end + 90 days
    assert ("closeout", "2028-12-30") in kinds                           # final invoice: term end + 60 days
    assert ("publication_review", None) in kinds
    assert obligations[-1]["kind"] == "term_end" and obligations[-1]["dueDate"] == "2028-10-31"
    assert [o["id"] for o in obligations] == [f"sonar-obl-{n}" for n in range(1, len(obligations) + 1)]
    assert all(o["status"] == "open" and o["source"] == "sonar" for o in obligations)


def test_extract_license_obligations():
    obligations = m.extract_obligations({"clauses": _sample_clauses("01-exclusive-license-v2-revised.txt")},
                                        "license", "2026-10-15")
    payments = [o for o in obligations if o["kind"] == "milestone_payment"]
    assert [(o["amount"], o["dueDate"]) for o in payments] == [
        (50000, "2027-06-30"), (150000, "2028-12-31"), (250000, "2029-06-30")]
    diligence = [o["dueDate"] for o in obligations if o["kind"] == "diligence_milestone"]
    assert diligence == ["2027-06-30", "2028-06-30", "2029-06-30"]
    royalty = next(o for o in obligations if o["kind"] == "royalty_report")
    assert royalty["dueDate"] == "2027-02-14"                            # Q4 2026 end + 45 days
    unsigned = m.extract_obligations({"clauses": _sample_clauses("01-exclusive-license-v2-revised.txt")},
                                     "license", None)
    assert next(o for o in unsigned if o["kind"] == "royalty_report")["dueDate"] is None


def test_extractors_are_deterministic():
    cls = {"clauses": _sample_clauses("01-exclusive-license-v1.txt")}
    assert m.extract_income(cls, "license") == m.extract_income(copy.deepcopy(cls), "license")
    assert m.extract_obligations(cls, "license", "2026-10-15") == m.extract_obligations(cls, "license", "2026-10-15")


# ---------------------------------------------------------------------------
# Taxonomy and prompt
# ---------------------------------------------------------------------------


def test_new_categories_labels_and_synonyms():
    for cat in ("PublicationRights", "BackgroundIP", "ExportControl", "DataRights", "SponsorReporting", "Diligence"):
        assert cat in ct.KNOWN_CATEGORIES and cat in classify_prompts.CLAUSE_CATEGORIES
        assert cat in classify_prompts.CLAUSE_SYSTEM and cat in classify_prompts.SYSTEM
        assert ct.normalise_type("Other", ct.known_label(cat))["category"] == cat
    assert ct.known_label("LicenseScope") == "License grant scope"
    assert ct.known_label("Royalties") == "Royalties, milestones, equity and sublicense income"
    for label, cat in (("Export Control", "ExportControl"), ("Publication", "PublicationRights"),
                       ("Background IP", "BackgroundIP"), ("Inventions", "BackgroundIP"), ("ITAR", "ExportControl"),
                       ("Flow-down terms", "SponsorReporting"), ("Commercialization", "Diligence"),
                       ("Sovereign immunity", "GoverningLaw")):
        assert ct.normalise_type("Other", label)["category"] == cat, label
    assert ct.normalise_type("Other", "Publicity")["category"] == "Other"     # kept as a custom type


def test_matrix_clause_labels_and_resolvers():
    assert m.matrix_clause_label("GoverningLaw") == "Governing law and sovereign immunity"
    assert m.matrix_clause_label("Payment") == "Payment terms"
    assert m.matrix_clause_label("type.non-solicitation") == "Non solicitation"
    assert m.resolve_office("Technology Commercialization Office") == ("tech_commercialization", True)
    assert m.resolve_office("") == (None, True) and m.resolve_office("Dean") == (None, False)
    assert m.resolve_clause_type("royalties, milestones, equity and sublicense income") == "Royalties"
    assert m.resolve_clause_type("Use of Northfield name") == "DataRights"
    assert len(m.RESEARCH_CLAUSE_TYPES) == 10 and set(m.OFFICE_LABELS) == set(m.OFFICES)


def _pipeline_clauses(name: str) -> list[dict]:
    """The sample cut by the pipeline's own segmenter (sub-clauses 3.1, 3.2 …),
    each sub-clause labelled with its section's category."""
    from shared.segment import segment_document

    text = (SAMPLES / name).read_text(encoding="utf-8")
    out = []
    for i, seg in enumerate(segment_document(text)["segments"]):
        section = re.sub(r"^\d+\.\s+", "", seg.section or "")
        out.append({"id": f"s{i}", "number": seg.number, "title": seg.title, "body": seg.body,
                    "category": HEADING_CATEGORIES.get(section, "Other"),
                    "specificTypeKey": None if section in HEADING_CATEGORIES else "recitals"})
    return out


def test_demo_story_holds_on_pipeline_sub_clauses():
    v1 = m.review_document(_pipeline_clauses("01-exclusive-license-v1.txt"), "license", m.default_matrix(), "d", NOW)
    assert _flagged(v1) == {"LicenseScope": "deviates", "Royalties": "deviates", "GoverningLaw": "unacceptable"}
    royalties = next(c for c in v1["clauses"] if c["clauseType"] == "Royalties")
    assert royalties["clauseNumber"] == "3.3"            # points at the sub-clause that carries the finding
    assert {b["id"] for b in m.sonar_blockers(v1)} == {
        "sonar-LicenseScope-2.1", "sonar-Royalties-3.3", "sonar-GoverningLaw-14.1"}
    v2 = m.review_document(_pipeline_clauses("01-exclusive-license-v2-revised.txt"), "license",
                           m.default_matrix(), "d", NOW)
    assert _flagged(v2) == {} and v2["counts"]["missing"] == 0
