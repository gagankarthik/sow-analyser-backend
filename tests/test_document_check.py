"""The not-an-agreement check: real agreements pass, other documents are flagged."""
from pathlib import Path

import pytest

from shared import document_check

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "research"

RESUME = """
JANE DOE
Senior Data Analyst | jane.doe@example.com | linkedin.com/in/janedoe

PROFESSIONAL SUMMARY
Data analyst with eight years of experience in healthcare analytics. Proficient in SQL, Python and Tableau.

WORK EXPERIENCE
Senior Data Analyst, Riverbend Health (2019 - present)
- Built dashboards used by 40 clinics; cut reporting time by 30%.
Data Analyst, Lakeshore Labs (2016 - 2019)
- Automated monthly reporting with Python.

EDUCATION
Master of Science in Statistics, State University, GPA 3.8
Bachelor of Arts in Mathematics

SKILLS
SQL, Python, R, Tableau, Excel, stakeholder communication

CERTIFICATIONS
Certified Analytics Professional

References available on request.
""" * 2

INVOICE = """
TAX INVOICE
Invoice number: INV-2026-0412      Invoice date: 1 October 2026
Bill to: Northwind Research, 100 Main Street
Description            Qty   Unit price   Amount
Consulting hours        40     $150.00    $6,000.00
Travel                   1     $420.00      $420.00
Subtotal $6,420.00   Tax $513.60   Amount due $6,933.60
Payment due within 30 days. Balance due $6,933.60.
""" * 4

PAPER = """
Abstract. We study lactate sensing in wearable devices. Keywords: biosensor, lactate.
1. Introduction. Prior work (Smith et al., 2021) reported drift in enzymatic sensors.
2. Methods. We fabricated sensors and measured response. See Figure 1.
3. Results. Sensitivity improved by 40%. See Figure 2.
4. Discussion. These results suggest a path to continuous monitoring.
References. Journal of Sensors, doi:10.1000/xyz123.
""" * 6


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.txt")), ids=lambda p: p.name)
def test_sample_agreements_pass(path):
    assert document_check.check(path.read_text(encoding="utf-8"), path.name) is None


@pytest.mark.parametrize("text,name,kind", [
    (RESUME, "jane_doe_resume.pdf", "resume"),
    (RESUME, "document.pdf", "resume"),
    (INVOICE, "inv-0412.pdf", "invoice"),
    (PAPER, "paper.pdf", "paper"),
])
def test_other_documents_are_flagged(text, name, kind):
    verdict = document_check.check(text, name)
    assert verdict is not None and verdict.kind == kind
    assert "not an agreement" in verdict.message and "analyze it anyway" in verdict.message


def test_an_agreement_that_mentions_education_and_invoices_still_passes():
    text = ("This Sponsored Research Agreement is entered into by the Parties as of the Effective Date. "
            "Whereas the Sponsor wishes to fund research and education, the Parties hereby agree as follows. "
            "The University shall invoice the Sponsor quarterly. Either party may terminate on notice. "
            "Confidential Information shall be protected. Governing law is the law of the home state. "
            "In witness whereof the parties have executed this Agreement. ") * 8
    assert document_check.check(text, "sra.pdf") is None


def test_short_text_is_not_judged():
    assert document_check.check("Resume. Skills: Python.", "cv.pdf") is None
