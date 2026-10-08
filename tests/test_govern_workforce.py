"""Workforce edition: SOW, MSA and staffing agreements graded from the client's side."""
from shared.govern import matrix, workforce


def _clause(atype, ctype):
    return next(c for c in matrix.default_matrix()["playbooks"][atype]["clauses"] if c["clauseType"] == ctype)


def grade(atype, ctype, text):
    return matrix._grade(_clause(atype, ctype), text, "OH", atype)


def test_default_matrix_has_the_three_workforce_types():
    types = {k: v["clauses"] for k, v in matrix.default_matrix()["playbooks"].items()}
    for t in ("sow", "msa", "staffing"):
        assert types[t], t
        assert all(c.get("suggestedLanguage") for c in types[t])
    assert {"type.hours-cap", "type.overtime", "IP"} <= {c["clauseType"] for c in types["sow"]}
    assert "type.co-employment" in {c["clauseType"] for c in types["staffing"]}


def test_rate_increases_hourly_caps_and_overtime():
    assert grade("sow", "Fees", "Rates may increase by up to 3% per year on renewal.").tier == "within"
    assert grade("sow", "Fees", "Rates may increase by up to 5% per year.").tier == "fallback"
    assert grade("sow", "Fees", "Vendor may change its rates at any time.").tier == "deviates"
    nte = grade("sow", "Fees", "Fees under this SOW shall not exceed $120,000 at the hourly rates in the Rate Card.")
    assert nte.tier == "within" and nte.beneficial
    assert grade("sow", "type.hours-cap", "Vendor shall not exceed 40 hours per week without Client's prior written approval.").tier == "within"
    assert grade("sow", "type.hours-cap", "Vendor may bill unlimited hours as needed.").tier == "unacceptable"
    assert grade("sow", "type.overtime", "Overtime requires Client's prior written approval and is billed at 1.5 times the standard rate.").tier == "within"
    assert grade("sow", "type.overtime", "Overtime is billed at double time.").tier == "deviates"


def test_work_for_hire_and_co_employment():
    assert grade("sow", "IP", "All Deliverables are works made for hire for Client.").tier == "within"
    assert grade("sow", "IP", "Contractor retains all right, title and interest in the Deliverables.").tier in ("deviates", "unacceptable")
    assert grade("staffing", "type.co-employment", "Workers are employees of Agency, which is solely responsible for wages, benefits and taxes.").tier == "within"
    assert grade("staffing", "type.co-employment", "Client shall be responsible for wages and benefits of assigned workers.").tier in ("deviates", "unacceptable")


def test_inference_and_direction():
    assert matrix.infer_agreement_type({"title": "Statement of Work #4", "docType": "SOW"}, {}) == "sow"
    assert matrix.infer_agreement_type({"title": "Master Services Agreement", "docType": "MSA"}, {}) == "msa"
    assert matrix.infer_agreement_type({"title": "Staffing Services Agreement"}, {}) == "staffing"
    # Research wording never reads as staffing.
    assert matrix.infer_agreement_type({"title": "Sponsored Research Agreement"},
                                       {"summary": "Temporary research staff may be hired by the University."}) == "sponsored_research"
    assert matrix.infer_direction("sow", {}) == "outgoing"
    assert workforce.workforce_type("Exclusive License Agreement", "", "LICENSE") is None
    assert matrix.resolve_agreement_type("Statement of Work") == "sow"
    assert matrix.resolve_agreement_type("Staffing vendor agreement") == "staffing"


def test_editions_offer_their_own_types():
    assert matrix.edition_agreement_types("workforce")[:3] == ["sow", "msa", "staffing"]
    campus = matrix.edition_agreement_types("campus")
    assert campus[:4] == ["sponsored_research", "license", "mta", "grant"] and "sow" not in campus
    assert matrix.edition_agreement_types(None) == campus
