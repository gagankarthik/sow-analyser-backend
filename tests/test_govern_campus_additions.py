"""Campus additions: consortium agreements, the use-of-name check and new obligations."""
from shared.govern import matrix


def _grade(atype, ctype, text):
    clause = next(c for c in matrix.default_matrix()["playbooks"][atype]["clauses"] if c["clauseType"] == ctype)
    return matrix._grade(clause, text, "OH", atype)


def test_use_of_name():
    assert _grade("license", "type.use-of-name", "Neither party shall use the name of the other without prior written consent.").tier == "within"
    assert _grade("sponsored_research", "type.use-of-name", "Sponsor may use the name of the University for any purpose.").tier in ("deviates", "unacceptable")
    assert _grade("license", "type.use-of-name", "The parties will cooperate.").tier == "review"


def test_consortium_and_subcontract_types():
    assert matrix.infer_agreement_type({"title": "Research Consortium Agreement"}, {}) == "consortium"
    assert matrix.default_matrix()["playbooks"]["consortium"]["clauses"]
    assert "consortium" in matrix.edition_agreement_types("campus")
    assert "subcontract" in matrix.edition_agreement_types("workforce")


def test_invention_disclosure_and_royalty_audit_obligations():
    cls = {"clauses": [
        {"category": "BackgroundIP", "body": "Licensee shall disclose all subject inventions within 60 days of conception."},
        {"category": "Royalties", "body": "Licensee shall keep books and records for five years, open to audit by Licensor."},
    ]}
    kinds = {o["kind"] for o in matrix.extract_obligations(cls, "license", "2026-07-01T00:00:00Z")}
    assert {"invention_disclosure", "royalty_audit"} <= kinds
