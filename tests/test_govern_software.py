"""Software and SaaS purchases are graded from the buyer's side: the university
is the customer, the vendor the other party."""
from __future__ import annotations

from shared.govern import matrix as m
from shared.govern import software as sw


def _cls(*clauses, summary="Master subscription agreement for a cloud SaaS platform for named users."):
    return {
        "summary": summary,
        "docType": "LICENSE",
        "timeline": {"endDate": "2027-06-30", "autoRenews": True, "renewalNoticeDays": 90},
        "clauses": [{"id": f"c{i}", "category": cat, "title": title, "body": body} for i, (cat, title, body) in enumerate(clauses)],
    }


SAAS = _cls(
    ("Warranty", "Warranties", "THE SERVICE IS PROVIDED AS IS. VENDOR DISCLAIMS ALL WARRANTIES."),
    ("Indemnity", "Indemnification", "Customer shall indemnify and hold harmless Vendor from any claims arising from its use."),
    ("Term", "Term and renewal", "This Agreement automatically renews for successive one-year terms unless either party "
                                 "gives notice of non-renewal at least ninety (90) days before the end of the term."),
    ("Fees", "Fees", "Fees for each renewal term may increase by up to seven percent (7%)."),
    ("LicenseScope", "License grant", "Vendor grants Customer a non-exclusive licence for 250 named users."),
)


def test_a_saas_contract_is_recognised_as_a_software_purchase():
    assert m.infer_agreement_type({"title": "Acme Cloud Master Subscription Agreement", "docType": "LICENSE"}, SAAS) == "software"
    assert m.infer_direction("software", SAAS) == "outgoing"


def test_a_patent_licence_the_university_grants_is_not_a_software_purchase():
    tech = _cls(("Royalties", "Royalties", "Licensee shall pay a royalty of 3% of Net Sales of Licensed Products."),
                summary="Exclusive licence to Licensed Patents for SaaS software products.")
    assert m.infer_agreement_type({"title": "Exclusive License Agreement", "docType": "LICENSE"}, tech) == "license"


def test_buyer_side_grades_reverse_the_seller_reading():
    review = m.review_document(SAAS["clauses"], "software", m.default_matrix(), doc_id="d1")
    tier = {r["clauseType"]: r["tier"] for r in review["clauses"]}
    assert tier["Warranty"] == "deviates"          # "as is" is bad news for a buyer
    assert tier["Indemnity"] == "unacceptable"     # a public university cannot indemnify the vendor
    assert tier["Term"] == "deviates"              # 90 days' notice to stop renewal; matrix allows 30 (60)
    assert tier["Fees"] == "deviates"              # 7% a year; matrix allows 3% (5%)
    assert tier["LicenseScope"] == "fallback"      # 250 named users


def test_good_vendor_terms_are_within():
    assert sw.check_indemnity("Vendor shall defend and indemnify Customer against any claim that the Service infringes "
                              "a third party's intellectual property rights.", {}).tier == "within"
    assert sw.check_warranty("Vendor warrants that the Service will perform materially in accordance with the "
                             "Documentation and contains no malicious code.", {}).tier == "within"
    assert sw.check_sla("Vendor will make the Service available 99.9% of the time each month; if not, Customer receives "
                        "service credits.", sw.THRESHOLDS["type.service-levels"]).tier == "within"
    assert sw.check_accessibility("The Service conforms to WCAG 2.1 AA and Vendor provides a VPAT.", {}).tier == "within"
    assert sw.check_data("Vendor will notify Customer of a security breach within 48 hours and will return and delete "
                         "Customer Data on termination.", sw.THRESHOLDS["DataProcessing"]).tier == "within"


def test_auto_renewal_creates_a_notice_deadline_obligation():
    obs = m.extract_obligations(SAAS, "software", "2026-07-01")
    by_kind = {o["kind"]: o for o in obs}
    assert by_kind["renewal_notice"]["dueDate"] == "2027-04-01"   # 90 days before 30 June 2027
    assert by_kind["data_return"]["dueDate"] == "2027-06-30"
    assert by_kind["term_end"]["dueDate"] == "2027-06-30"


def test_software_playbook_validates_and_saves():
    playbooks = m.default_matrix()["playbooks"]
    assert "software" in playbooks and playbooks["software"]["clauses"]
    clean, err = m.validate_matrix(playbooks)
    assert err is None
    offices = {c["escalationOffice"] for c in playbooks["software"]["clauses"]}
    assert {"procurement", "it_security"} <= offices


def test_new_research_types_are_recognised():
    assert m.infer_agreement_type({"title": "Clinical Trial Agreement - Phase II"}, {}) == "clinical_trial"
    assert m.infer_agreement_type({"title": "Data Use Agreement with State Health Dept"}, {}) == "data_use"
    assert m.resolve_agreement_type("Clinical trial") == "clinical_trial"
    assert m.resolve_agreement_type("SaaS subscription") == "software"
