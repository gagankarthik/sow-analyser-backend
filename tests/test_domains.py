"""Pillar (domain) tagging — Blue-IQ Campus' four entry points.

Guards shared/domains.classify_domain: it must deterministically pick a pillar
(software_license / contract / ip_venture / grants) from the structured fields
the classify stage already extracted, and only ever raise a risk flag that is
backed by evidence in the document.
"""
from __future__ import annotations

from shared import domains
from stages import classify


def _doc(*, doc_type="OTHER", clauses=None, title="", summary="",
         parties=None, timeline=None, compliance=None, scope=None):
    return {
        "docType": doc_type,
        "title": title,
        "summary": summary,
        "parties": parties or [],
        "scope": scope or {},
        "keyFindings": [],
        "clauses": clauses or [],
        "timeline": timeline or {},
        "compliance": compliance or {},
    }


def _clause(category, risk="low", title="x"):
    return {"number": "1", "title": title, "body": "x", "category": category, "riskLevel": risk}


# ── pillar selection ─────────────────────────────────────────────────────────

def test_known_pillars():
    assert domains.KNOWN_PILLAR_IDS == [
        "software_license", "contract", "ip_venture", "grants",
    ]


def test_empty_doc_defaults_to_contract_low_confidence():
    out = domains.classify_domain(_doc())
    assert out["pillar"] == "contract"
    assert out["confidence"] == "low"
    assert out["label"] == "Contract Governance"


def test_software_license_doc():
    out = domains.classify_domain(_doc(
        doc_type="LICENSE",
        title="SaaS Subscription Agreement",
        summary="End user license for named-user software subscription.",
        clauses=[_clause("LicenseGrant"), _clause("LicenseScope"), _clause("Restrictions")],
    ))
    assert out["pillar"] == "software_license"
    assert out["confidence"] == "high"


def test_grants_doc():
    out = domains.classify_domain(_doc(
        doc_type="OTHER",
        title="Sub-award Agreement under Federal Award",
        summary="Sponsored research sub-award subject to 2 CFR 200 uniform guidance.",
        clauses=[_clause("Compliance"), _clause("Subcontracting")],
    ))
    assert out["pillar"] == "grants"


def test_ip_venture_doc():
    out = domains.classify_domain(_doc(
        title="Patent License & Royalty Agreement",
        summary="Exclusive license of university invention with revenue share to inventor.",
        clauses=[_clause("Royalties"), _clause("Sublicensing"), _clause("IP")],
    ))
    assert out["pillar"] == "ip_venture"


def test_generic_msa_is_contract():
    out = domains.classify_domain(_doc(
        doc_type="MSA",
        title="Master Services Agreement",
        clauses=[_clause("Liability"), _clause("Indemnity"), _clause("Term")],
    ))
    assert out["pillar"] == "contract"


def test_deterministic_same_input_same_output():
    d = _doc(doc_type="LICENSE", title="Software EULA", clauses=[_clause("LicenseGrant")])
    assert domains.classify_domain(d) == domains.classify_domain(d)


def test_scores_cover_every_pillar():
    out = domains.classify_domain(_doc())
    assert set(out["scores"]) == set(domains.KNOWN_PILLAR_IDS)


# ── risk flags (only on evidence) ────────────────────────────────────────────

def test_auto_renewal_trap_flag_for_contract():
    out = domains.classify_domain(_doc(
        doc_type="MSA",
        timeline={"autoRenews": True, "renewalNoticeDays": 90},
    ))
    assert out["pillar"] == "contract"
    assert any(f["id"] == "auto_renewal_trap" for f in out["riskFlags"])


def test_no_auto_renewal_flag_when_not_auto_renewing():
    out = domains.classify_domain(_doc(
        doc_type="MSA",
        timeline={"autoRenews": False},
    ))
    assert not any(f["id"] == "auto_renewal_trap" for f in out["riskFlags"])


def test_software_broad_audit_rights_flag():
    out = domains.classify_domain(_doc(
        doc_type="LICENSE",
        title="Software license",
        summary="software subscription saas",
        clauses=[_clause("LicenseGrant"), _clause("AuditRights", risk="critical")],
    ))
    assert out["pillar"] == "software_license"
    assert any(f["id"] == "broad_audit_rights" for f in out["riskFlags"])


def test_grants_flags_missing_flowdown_and_retention():
    out = domains.classify_domain(_doc(
        title="Sub-award under federal award",
        summary="sponsored research subaward 2 cfr 200",
        clauses=[_clause("Compliance")],
    ))
    assert out["pillar"] == "grants"
    ids = {f["id"] for f in out["riskFlags"]}
    assert "subaward_flowdown_missing" in ids
    assert "retention_missing" in ids


def test_ip_missing_royalty_terms_flag():
    out = domains.classify_domain(_doc(
        title="University invention license to spin-out",
        summary="exclusive license of patent to licensee; technology transfer",
        clauses=[_clause("LicenseGrant"), _clause("IP")],
    ))
    assert out["pillar"] == "ip_venture"
    assert any(f["id"] == "missing_royalty_terms" for f in out["riskFlags"])


# ── wiring into the classify stage ───────────────────────────────────────────

def test_classify_imports_domain_tagger():
    assert hasattr(classify, "classify_domain")
