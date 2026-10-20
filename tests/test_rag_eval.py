"""Offline evaluation of Sonar (RAG over contract clauses), end to end.

Three realistic agreements (a statement of work, the master services agreement
it sits under, and a university sponsored-research agreement) are chunked with
the real ``split_for_retrieval`` / ``embed.build_chunks``, written with the real
``opensearch.index_chunks`` into an in-memory search engine, and ~45 questions
are asked through the real HTTP chat handler: query building, hybrid search,
reciprocal-rank fusion, rerank, context assembly, prompt building and the
answer checks all run unmodified. Only the network is replaced:

* ``_FakeSearch`` interprets the query DSL the code sends (bool / multi_match /
  match / term / terms / prefix / script_score k-NN). BM25 uses Lucene's formula
  (k1=1.2, b=0.75) with per-field statistics and the analyzer the index mapping
  asks for (``english``: stop words + a Porter-style stemmer). An unknown query
  form raises, so the fake cannot silently drift from what the code sends.
* The embedding is a deterministic hashed bag of stemmed words (cosine). It is
  purely lexical, so it UNDER-states what a real semantic embedding gets for
  paraphrased questions: the synonym category is a worst case for the vector
  channel and measures what the keyword channel adds.
* The model is scripted: it answers from the clauses it was actually shown
  (faithful), adds invented clause citations, or invents figures.

Metrics, per category: recall@4 / recall@8 (share of gold clauses that reach
the assembled context), MRR@8, and for the answer checks: invented citations
left in the answer text, fabricated figures flagged, faithful answers wrongly
flagged, and honest "not in the document" answers reported as ungrounded.

Run ``pytest tests/test_rag_eval.py -s -q`` to print the table.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any

import pytest

from rag import handler as rag
from shared import opensearch
from stages import embed

OWNER = "11111111-1111-4111-8111-111111111111"
TENANT = "acme"
SOW, MSA, SRA, PROJECT = "sow-0003", "msa-2024", "sra-2025", "proj-harbor"
TITLES = {SOW: "SOW No. 3 - Data Platform", MSA: "Harbor MSA", SRA: "Cascade Sponsored Research"}
DIM = opensearch.VECTOR_DIM
VERBOSE = True          # with -s: list each question whose gold clause missed the context


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _c(number, title, category, body, section=""):
    return {"id": f"c-{number}", "number": number, "title": title, "category": category,
            "section": section, "body": " ".join(body.split()), "page": 1}


_SOW_CLAUSES = [
    _c("1", "Background", "ScopeOfWork", """This Statement of Work No. 3 ("SOW") is entered into under the
        Master Services Agreement dated January 15, 2024 (the "MSA") between Northwind Analytics, Inc.
        ("Supplier") and Harbor City Transit Authority ("Customer"). Capitalised terms not defined in this
        SOW have the meanings given in the MSA.""", "PART A - GENERAL"),
    _c("2", "Scope of Services", "ScopeOfWork", """Supplier will migrate Customer's ridership,
        fare-collection and maintenance data from the legacy on-premise warehouse to a managed cloud data
        platform, build twelve (12) operational dashboards, and train Customer's analytics staff. The
        Services exclude the replacement of fare-collection hardware, the migration of payroll data and any
        work on systems not listed in Appendix 1.""", "PART A - GENERAL"),
    _c("3", "Deliverables and Milestones", "Deliverables", """Supplier shall provide the following
        Deliverables: (a) a migration plan, due April 15, 2025; (b) the migrated data platform in
        production, due August 29, 2025; (c) the twelve operational dashboards, due October 31, 2025; and
        (d) training materials and two (2) training sessions, due November 28, 2025. Each milestone date
        may be adjusted only through the Change Control procedure in Section 8.""", "PART B - DELIVERY"),
    _c("4", "Acceptance", "Acceptance", """Customer shall review each Deliverable and notify Supplier in
        writing of its acceptance or rejection within ten (10) business days after delivery. A rejection
        notice must describe the non-conformities in reasonable detail. Supplier shall correct the
        non-conformities and resubmit the Deliverable within fifteen (15) business days. A Deliverable is
        deemed accepted if Customer does not respond within the review period.""", "PART B - DELIVERY"),
    _c("5", "Fees", "Fees", """In consideration of the Services, Customer shall pay Supplier a fixed fee
        of $480,000, payable in the milestone installments set out in Appendix 2. Additional work requested
        by Customer and approved through Change Control will be charged at the time-and-materials rates in
        Appendix 3, currently $185 per hour for a senior consultant and $140 per hour for an analyst.""",
       "PART C - COMMERCIAL"),
    _c("6", "Invoicing and Payment", "Payment", """Supplier will invoice each milestone installment upon
        acceptance of the related Deliverable. Customer shall pay each undisputed invoice within forty-five
        (45) days of receipt. Amounts not paid when due accrue interest at one percent (1%) per month or
        the maximum rate permitted by law, whichever is lower. Payments shall be made by ACH transfer to
        the account designated by Supplier.""", "PART C - COMMERCIAL"),
    _c("7", "Expenses", "Fees", """Customer will reimburse reasonable travel and out-of-pocket expenses
        that are pre-approved in writing by Customer's project manager and incurred in accordance with
        Customer's travel policy. Reimbursable expenses shall not exceed eight percent (8%) of the fixed
        fee without Customer's prior written approval.""", "PART C - COMMERCIAL"),
    _c("8", "Change Control", "ChangeControl", """Either party may request a change to the scope,
        schedule or fees of this SOW by submitting a written change request. Supplier will provide an
        impact assessment within five (5) business days. No change is binding until both parties sign a
        change order describing the revised scope, schedule and fees.""", "PART C - COMMERCIAL"),
    _c("9", "Term of this SOW", "Term", """This SOW commences on March 1, 2025 and continues until
        December 31, 2025, unless terminated earlier in accordance with Section 13 or the MSA. The parties
        may extend this SOW only by a signed change order.""", "PART D - TERM"),
    _c("10", "Key Personnel", "Other", """Supplier designates Priya Raman as engagement lead. Supplier
        shall not remove or replace key personnel without Customer's prior written consent, which shall
        not be unreasonably withheld, except in the case of illness, resignation or termination of
        employment.""", "PART D - TERM"),
    _c("11", "Customer Responsibilities", "ScopeOfWork", """Customer shall provide timely access to its
        systems, data, facilities and subject-matter experts, and shall appoint a project manager with
        authority to make decisions on its behalf. Delays caused by Customer's failure to meet these
        responsibilities entitle Supplier to an equitable adjustment of the schedule.""", "PART D - TERM"),
    _c("12", "Order of Precedence", "Other", """This SOW is governed by the terms of the MSA. If this SOW
        conflicts with the MSA, the MSA prevails except where this SOW expressly states that a specific
        provision of the MSA is being modified for purposes of this SOW only.""", "PART E - LEGAL"),
    _c("13", "Termination of this SOW", "Termination", """Customer may terminate this SOW for convenience
        at any time on thirty (30) days' written notice to Supplier. Upon such termination Customer shall
        pay Supplier for Services performed and Deliverables accepted up to the effective date of
        termination, together with approved expenses incurred, and Supplier shall deliver all work in
        progress.""", "PART E - LEGAL"),
    _c("14", "Ownership of Deliverables", "IP", """Upon payment in full of the applicable fees, all
        Deliverables and other materials created by Supplier specifically for Customer under this SOW
        shall belong exclusively to Customer, and Supplier hereby assigns to Customer all right, title and
        interest in them. Supplier retains its pre-existing tools, frameworks and know-how and grants
        Customer a non-exclusive, perpetual, royalty-free licence to use any of them incorporated into the
        Deliverables.""", "PART E - LEGAL"),
]

_MSA_DEFINITIONS = """In this Agreement the following terms have the meanings below. "Affiliate" means any
    entity that directly or indirectly controls, is controlled by or is under common control with a party,
    where control means ownership of more than fifty percent of the voting interests. "Confidential
    Information" means all non-public information disclosed by or on behalf of a party, in any form, that
    is marked as confidential or that a reasonable person would understand to be confidential, including
    business plans, pricing, technical data and Customer Data. "Customer Data" means all data, records and
    information supplied by or on behalf of Customer or its riders, or generated by the Services from such
    data, including personal information. "Deliverables" means the items identified as deliverables in an
    SOW. "Documentation" means the user guides, specifications and other materials Supplier makes
    available in connection with the Services. "Effective Date" means January 15, 2024. "Intellectual
    Property Rights" means patents, copyrights, trade secrets, trademarks, database rights, moral rights
    and all other intellectual and industrial property rights anywhere in the world, whether registered
    or not. "Losses" means damages, fines, penalties, settlements, costs and reasonable attorneys' fees.
    "Personnel" means a party's employees, agents and permitted subcontractors. "Pre-existing Materials"
    means materials owned by or licensed to a party before the Effective Date or developed independently
    of this Agreement. "Security Incident" means any unauthorised access to, or acquisition, disclosure
    or loss of, Customer Data. "Services" means the services described in an SOW. "SOW" means a statement
    of work signed by both parties that references this Agreement. "Supplier Tools" means Supplier's
    proprietary software, methodologies, templates, libraries and accelerators used in performing the
    Services, together with all improvements to them made in the course of the Services. "Work Product"
    means all Deliverables, reports, software, data models, configurations and other materials created by
    or for Supplier specifically for Customer in the performance of the Services, excluding Supplier Tools
    and Pre-existing Materials."""

_MSA_IP = """9.1 Pre-existing Materials. Each party retains all Intellectual Property Rights in its
    Pre-existing Materials. Nothing in this Agreement transfers ownership of a party's Pre-existing
    Materials to the other party, and neither party acquires any implied licence to the other party's
    Pre-existing Materials except as expressly stated in this Section 9. Each party shall keep the other's
    Pre-existing Materials free of liens and encumbrances arising through it, and shall not remove or
    obscure any proprietary notices on them. Where a party provides its Pre-existing Materials to the other
    party for use in connection with the Services, the receiving party may use them only for that purpose
    and only for the duration of the applicable SOW, shall comply with any reasonable written instructions
    of the providing party regarding their use and security, and shall return or destroy them when the SOW
    ends. Any feedback a party gives about the other party's Pre-existing Materials may be used freely by
    the owner of those materials without obligation.
    9.2 Supplier Tools. Supplier owns the Supplier Tools, including all modifications, enhancements and
    derivative works made in the course of the Services, whether made by Supplier alone or with the
    assistance of Customer. To the extent any Supplier Tools are incorporated into a Deliverable, Supplier
    grants Customer and its Affiliates a non-exclusive, worldwide, perpetual, irrevocable, fully paid-up,
    royalty-free licence to use, copy, modify and maintain those Supplier Tools solely as part of that
    Deliverable and for Customer's internal business purposes. Customer shall not sell, sublicense or
    distribute the Supplier Tools separately from the Deliverables, nor reverse engineer them except to
    the extent such restriction is prohibited by law. Supplier shall identify in each SOW any Supplier
    Tools it expects to incorporate into the Deliverables.
    9.3 Work Product. Subject to payment of all undisputed fees, Customer owns all Work Product, and
    Supplier hereby assigns to Customer all right, title and interest in the Work Product, including all
    Intellectual Property Rights in it. Supplier shall obtain from its Personnel written assignments
    sufficient to give effect to this Section 9.3 and, to the extent permitted by law, waivers of moral
    rights. At Customer's request and expense Supplier shall sign any document reasonably needed to
    register or perfect Customer's ownership of the Work Product.
    9.4 Residual Knowledge. Nothing in this Agreement restricts either party from using the general
    skills, ideas, concepts and know-how retained in the unaided memory of its Personnel, provided that
    this does not permit disclosure of Confidential Information or infringement of the other party's
    Intellectual Property Rights."""

_MSA_CLAUSES = [
    _c("1", "Definitions", "Definitions", _MSA_DEFINITIONS, "ARTICLE 1 - INTERPRETATION"),
    _c("2", "Services and Statements of Work", "ScopeOfWork", """Supplier shall perform the Services
        described in each SOW in accordance with this Agreement. Each SOW forms part of this Agreement once
        signed by both parties. Supplier is not obliged to perform any services that are not described in a
        signed SOW.""", "ARTICLE 2 - SERVICES"),
    _c("3.1", "Fees", "Fees", """Customer shall pay the fees set out in each SOW. The fees for an SOW are
        fixed for that SOW and may only change by a signed change order. All fees are stated and payable
        in United States dollars.""", "ARTICLE 3 - FEES AND PAYMENT"),
    _c("3.2", "Invoicing and Payment", "Payment", """Unless an SOW states otherwise, Supplier shall invoice
        monthly in arrears and Customer shall pay each correct invoice within thirty (30) days of the
        invoice date. Invoices must reference the applicable SOW and purchase order number.""",
       "ARTICLE 3 - FEES AND PAYMENT"),
    _c("3.3", "Disputed Invoices", "Payment", """If Customer disputes any part of an invoice in good
        faith, it shall notify Supplier in writing within fifteen (15) days of receipt, stating the
        reasons, and pay the undisputed portion. The parties shall work together to resolve the dispute
        within thirty (30) days, failing which either party may escalate it under Section 17.""",
       "ARTICLE 3 - FEES AND PAYMENT"),
    _c("4", "Taxes", "Fees", """Fees are exclusive of sales, use, value added and similar taxes, which
        Customer shall pay except for taxes on Supplier's net income. Customer may provide a valid
        exemption certificate, in which case Supplier shall not charge the exempted tax.""",
       "ARTICLE 3 - FEES AND PAYMENT"),
    _c("5", "Term and Renewal", "Term", """This Agreement begins on the Effective Date and continues for
        an initial term of three (3) years (the "Initial Term"). It then renews automatically for
        successive one (1) year periods (each a "Renewal Term") unless either party gives written notice
        of non-renewal at least ninety (90) days before the end of the then-current term. Any SOW in effect
        at expiry continues to be governed by this Agreement until it ends.""", "ARTICLE 4 - TERM"),
    _c("6.1", "Termination for Cause", "Termination", """Either party may terminate this Agreement or any
        SOW by written notice if the other party commits a material breach and fails to cure it within
        thirty (30) days after receiving written notice describing the breach, or if the other party
        becomes insolvent, makes an assignment for the benefit of creditors or has a receiver
        appointed.""", "ARTICLE 4 - TERM"),
    _c("6.2", "Termination for Convenience", "Termination", """Customer may terminate this Agreement or
        any SOW for convenience on sixty (60) days' prior written notice. Supplier may terminate this
        Agreement for convenience on one hundred eighty (180) days' notice, but not while any SOW remains
        in effect.""", "ARTICLE 4 - TERM"),
    _c("7", "Effect of Termination", "Termination", """On expiry or termination, Supplier shall, at
        Customer's election, return or securely delete all Customer Data within thirty (30) days and
        certify deletion in writing. Customer shall pay all fees for Services performed up to the date of
        termination. Sections 8, 9, 11, 12 and 16 survive.""", "ARTICLE 4 - TERM"),
    _c("8", "Confidentiality", "Confidentiality", """Each party shall hold the other party's Confidential
        Information in confidence, use it only to perform or receive the Services and disclose it only to
        its Personnel and advisers who need to know it and are bound by obligations no less protective.
        These obligations continue during the term and for five (5) years after expiry or termination,
        except that trade secrets remain protected for as long as they qualify as trade secrets under
        applicable law.""", "ARTICLE 5 - PROTECTION"),
    _c("9", "Intellectual Property", "IP", _MSA_IP, "ARTICLE 5 - PROTECTION"),
    _c("10", "Warranties", "Warranty", """Supplier warrants that the Services will be performed in a
        professional and workmanlike manner by appropriately qualified Personnel, and that each
        Deliverable will conform to its specifications for ninety (90) days after acceptance. Customer's
        exclusive remedy for breach of this warranty is re-performance of the non-conforming Services or,
        if Supplier cannot cure, a refund of the fees paid for them.""", "ARTICLE 6 - RISK"),
    _c("11", "Indemnification", "Indemnity", """Supplier shall defend Customer against any third-party
        claim alleging that the Services or Deliverables infringe or misappropriate that third party's
        intellectual property rights and shall indemnify Customer against all Losses finally awarded or
        agreed in settlement. Customer shall give Supplier prompt notice of the claim, sole control of the
        defence and reasonable cooperation.""", "ARTICLE 6 - RISK"),
    _c("12", "Limitation of Liability", "Liability", """Except for the excluded claims below, each party's
        total aggregate liability arising out of or relating to this Agreement shall not exceed the fees
        paid or payable under this Agreement in the twelve (12) months preceding the event giving rise to
        the claim. Neither party is liable for indirect, incidental, special, consequential or punitive
        damages, or for lost profits or revenue. The cap and exclusion do not apply to a party's breach of
        Section 8 (Confidentiality), Supplier's obligations under Section 11 (Indemnification), or
        liability for gross negligence, wilful misconduct or fraud.""", "ARTICLE 6 - RISK"),
    _c("13", "Insurance", "Insurance", """Supplier shall maintain, at its own cost, commercial general
        liability insurance of at least $2,000,000 per occurrence, professional liability (errors and
        omissions) insurance of at least $5,000,000 per claim, cyber liability insurance of at least
        $5,000,000 per claim, and workers' compensation insurance as required by law, and shall provide
        certificates of insurance on request.""", "ARTICLE 6 - RISK"),
    _c("14", "Data Protection and Security", "DataProtection", """Supplier shall process Customer Data
        only on Customer's documented instructions, maintain administrative, physical and technical
        safeguards consistent with ISO 27001, and notify Customer without undue delay, and in any event
        within seventy-two (72) hours, after becoming aware of a Security Incident affecting Customer
        Data.""", "ARTICLE 5 - PROTECTION"),
    _c("15", "Force Majeure", "ForceMajeure", """Neither party is liable for delay or failure to perform
        caused by events beyond its reasonable control, including natural disasters, epidemics, war,
        terrorism, labour disputes (other than those of the affected party's own workforce) or failures of
        public utilities. If such an event continues for more than sixty (60) days, either party may end
        the affected SOW on written notice.""", "ARTICLE 7 - GENERAL"),
    _c("16", "Governing Law and Venue", "GoverningLaw", """This Agreement and any dispute arising out of it
        are governed by the laws of the State of Washington, without regard to its conflict-of-laws rules.
        The state and federal courts located in King County, Washington have exclusive jurisdiction, and
        each party submits to their personal jurisdiction.""", "ARTICLE 7 - GENERAL"),
    _c("17", "Dispute Resolution", "DisputeResolution", """The parties shall first attempt to resolve any
        dispute through good-faith negotiation between their project managers, then between senior
        executives within fifteen (15) days of escalation. If the dispute is not resolved within forty-five
        (45) days, either party may refer it to non-binding mediation before commencing litigation in
        accordance with Section 16.""", "ARTICLE 7 - GENERAL"),
    _c("18", "Assignment", "Assignment", """Neither party may assign this Agreement without the other
        party's prior written consent, except to an Affiliate or to a successor to substantially all of its
        business or assets that agrees in writing to be bound by this Agreement.""", "ARTICLE 7 - GENERAL"),
    _c("19", "Notices", "Notices", """Notices under this Agreement must be in writing and delivered by
        hand, by courier or by email with confirmation of receipt, to the addresses set out on the
        signature page. A notice is effective on receipt.""", "ARTICLE 7 - GENERAL"),
    _c("20", "Non-Solicitation", "Other", """During the term and for twelve (12) months afterwards,
        neither party shall solicit for employment any employee of the other party who was directly
        involved in the Services, except through general advertisements not targeted at such
        employees.""", "ARTICLE 7 - GENERAL"),
    _c("21", "Entire Agreement", "Other", """This Agreement, together with all SOWs, constitutes the
        entire agreement between the parties on its subject matter and supersedes all prior proposals and
        agreements. Any amendment must be in writing and signed by both parties.""", "ARTICLE 7 - GENERAL"),
]

_SRA_CLAUSES = [
    _c("1", "Definitions", "Definitions", """"Background IP" means intellectual property owned or
        controlled by a party before the Effective Date or developed outside the Research Program.
        "Research Results" means all data, findings and materials generated in the performance of the
        Research Program. "Invention" means any invention or discovery, whether or not patentable,
        conceived or first reduced to practice in the performance of the Research Program. "Confidential
        Information" means information disclosed by one party to the other in writing and marked as
        confidential."""),
    _c("2", "Research Program", "ScopeOfWork", """University shall use reasonable efforts to conduct the
        research program described in Exhibit A (the "Research Program") under the direction of Dr. Elena
        Marsh, Department of Molecular Biosciences (the "Principal Investigator"). If the Principal
        Investigator becomes unable to continue, University will nominate a successor acceptable to
        Sponsor; if none is agreed within sixty (60) days, either party may end this Agreement under
        Section 12."""),
    _c("3", "Term", "Term", """This Agreement takes effect on July 1, 2025 (the "Effective Date") and
        expires on June 30, 2027, unless extended by written amendment signed by both parties or ended
        earlier under Section 12."""),
    _c("4", "Funding and Payment", "Payment", """Sponsor shall provide total funding of $750,000 for the
        Research Program, inclusive of University's indirect costs at its federally negotiated rate of
        fifty-two percent (52%) of modified total direct costs. Sponsor shall pay $250,000 within thirty
        (30) days after the Effective Date and the balance in four (4) equal quarterly installments of
        $125,000, each payable within thirty (30) days of University's invoice. Checks shall be payable to
        the University of Northern Plains."""),
    _c("5", "Publication", "PublicationRights", """University and the Principal Investigator are free to
        publish the Research Results. University shall submit any proposed publication or presentation to
        Sponsor at least sixty (60) days before submission for publication. Sponsor may, within that
        period, require the removal of its Confidential Information or request a delay of up to an
        additional thirty (30) days to permit the filing of patent applications."""),
    _c("6", "Confidentiality", "Confidentiality", """Each party shall use reasonable care to protect the
        other party's Confidential Information and shall not disclose it to third parties for three (3)
        years from the date of disclosure. These obligations do not apply to information that is publicly
        available, already known to the recipient or independently developed."""),
    _c("7", "Inventions and Intellectual Property", "IP", """Title to any Invention made solely by
        University personnel vests in University. Title to any Invention made solely by Sponsor personnel
        vests in Sponsor. Inventions made jointly by University and Sponsor personnel are owned jointly.
        Each party retains all rights in its Background IP, and nothing in this Agreement grants either
        party a licence to the other's Background IP except as expressly stated. University shall
        promptly disclose each Invention to Sponsor in confidence."""),
    _c("8", "Option to License", "LicenseGrant", """University grants Sponsor an exclusive option to
        negotiate an exclusive, royalty-bearing licence to University's rights in each Invention. Sponsor
        may exercise the option by written notice within six (6) months after University's disclosure of
        the Invention. The parties shall then negotiate in good faith for up to ninety (90) days; if no
        licence is signed within that period, University may license the Invention to third parties."""),
    _c("9", "Use of Names", "DataRights", """Neither party shall use the name, logo or trademarks of the
        other party, or the name of any of its employees, in any advertising or publicity without prior
        written consent, except that University may disclose the existence of this Agreement, the identity
        of Sponsor and the amount of funding as required by law or University policy."""),
    _c("10", "Disclaimer of Warranties", "Warranty", """The Research Results and any Inventions are
        experimental and are provided AS IS. UNIVERSITY MAKES NO REPRESENTATIONS OR WARRANTIES OF ANY KIND,
        EXPRESS OR IMPLIED, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE OR
        NON-INFRINGEMENT."""),
    _c("11", "Indemnification", "Indemnity", """Sponsor shall indemnify, defend and hold harmless
        University, its trustees, officers, employees and students from any claims, losses or damages
        arising from Sponsor's use of the Research Results or any product made using them, except to the
        extent caused by University's gross negligence or wilful misconduct."""),
    _c("12", "Termination", "Termination", """Either party may terminate this Agreement on sixty (60)
        days' written notice to the other. Either party may terminate on thirty (30) days' written notice
        if the other party materially breaches this Agreement and does not cure the breach within that
        period. On termination Sponsor shall pay all costs incurred through the termination date and all
        non-cancellable obligations, up to the total funding amount."""),
    _c("13", "Export Control", "ExportControl", """Each party shall comply with all applicable export
        control laws. Sponsor shall not provide University with any export-controlled information without
        prior written notice and University's written agreement to accept it."""),
    _c("14", "Governing Law", "GoverningLaw", """This Agreement is governed by the laws of the State of
        North Dakota. Nothing in this Agreement waives the sovereign immunity of the University or the State
        of North Dakota."""),
    _c("15", "Independent Contractors", "Other", """The parties are independent contractors. Neither party
        is the agent, partner or joint venturer of the other, and neither may bind the other to any
        obligation."""),
    _c("16", "Notices", "Notices", """Notices must be in writing and sent to the Principal Investigator and
        the University's Office of Sponsored Programs, and to Sponsor's legal department, at the addresses
        on the signature page."""),
]

CORPUS = {SOW: ("SOW", _SOW_CLAUSES), MSA: ("MSA", _MSA_CLAUSES), SRA: ("RESEARCH", _SRA_CLAUSES)}


# ---------------------------------------------------------------------------
# Questions: (category, target, question, gold [(doc, clause)], fact in gold[0], history)
# ---------------------------------------------------------------------------

def _h(question: str, answer: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]


QUESTIONS: list[tuple[str, str, str, list[tuple[str, str]], str, list]] = [
    # factual — the question uses the agreement's own words
    ("factual", SOW, "What are the payment terms?", [(SOW, "6")], "within forty-five (45) days of receipt", []),
    ("factual", SOW, "When does this SOW end?", [(SOW, "9")], "continues until December 31, 2025", []),
    ("factual", SOW, "What is the fixed fee?", [(SOW, "5")], "a fixed fee of $480,000", []),
    ("factual", SOW, "How long does the customer have to accept or reject a deliverable?", [(SOW, "4")],
     "within ten (10) business days after delivery", []),
    ("factual", SOW, "Can the customer terminate this SOW for convenience?", [(SOW, "13")],
     "thirty (30) days' written notice", []),
    ("factual", MSA, "What is the governing law?", [(MSA, "16")], "the laws of the State of Washington", []),
    ("factual", MSA, "What is the liability cap?", [(MSA, "12")], "in the twelve (12) months preceding", []),
    ("factual", MSA, "Does the agreement renew automatically?", [(MSA, "5")], "successive one (1) year periods", []),
    ("factual", MSA, "How much notice is needed to terminate for convenience?", [(MSA, "6.2")],
     "sixty (60) days' prior written notice", []),
    ("factual", MSA, "How long do the confidentiality obligations last?", [(MSA, "8")],
     "for five (5) years after expiry or termination", []),
    ("factual", MSA, "What insurance must the supplier carry?", [(MSA, "13")], "at least $2,000,000 per occurrence", []),
    ("factual", MSA, "How quickly must a security incident be reported?", [(MSA, "14")],
     "within seventy-two (72) hours", []),
    ("factual", MSA, "What does section 20 say?", [(MSA, "20")], "for twelve (12) months afterwards", []),
    ("factual", SRA, "What is the total funding?", [(SRA, "4")], "total funding of $750,000", []),
    ("factual", SRA, "When does the research agreement expire?", [(SRA, "3")], "expires on June 30, 2027", []),
    ("factual", SRA, "How long does the sponsor have to exercise its option?", [(SRA, "8")],
     "within six (6) months after", []),
    ("factual", SRA, "Which state's law governs the research agreement?", [(SRA, "14")], "State of North Dakota", []),
    ("factual", SRA, "Who is the principal investigator?", [(SRA, "2")], "Dr. Elena Marsh", []),
    # synonyms — everyday words for what the clause calls something else
    ("synonym", SOW, "When do we get paid?", [(SOW, "6")], "within forty-five (45) days of receipt", []),
    ("synonym", SOW, "How long is the contract?", [(SOW, "9")], "continues until December 31, 2025", []),
    ("synonym", SOW, "Who owns the work product?", [(SOW, "14")], "shall belong exclusively to Customer", []),
    ("synonym", SOW, "Is there a cap on travel costs?", [(SOW, "7")], "eight percent (8%) of the fixed fee", []),
    ("synonym", SOW, "What happens if we want to change what is being delivered?", [(SOW, "8")],
     "submitting a written change request", []),
    ("synonym", MSA, "Who owns the IP?", [(MSA, "9")], "Customer owns all Work Product", []),
    ("synonym", MSA, "Which courts hear disputes?", [(MSA, "16")], "courts located in King County, Washington", []),
    ("synonym", MSA, "What are the limits on damages?", [(MSA, "12")], "in the twelve (12) months preceding", []),
    ("synonym", MSA, "When must invoices be paid?", [(MSA, "3.2")], "within thirty (30) days of the invoice date", []),
    ("synonym", MSA, "Is there an NDA?", [(MSA, "8")], "for five (5) years after expiry or termination", []),
    ("synonym", MSA, "How long is the guarantee on deliverables?", [(MSA, "10")],
     "for ninety (90) days after acceptance", []),
    ("synonym", MSA, "Who pays if we get sued for infringement?", [(MSA, "11")], "shall indemnify Customer", []),
    ("synonym", SRA, "How long is the contract?", [(SRA, "3")], "expires on June 30, 2027", []),
    ("synonym", SRA, "When do we get paid?", [(SRA, "4")], "within thirty (30) days of University's invoice", []),
    ("synonym", SRA, "Can the sponsor cancel?", [(SRA, "12")], "sixty (60) days' written notice", []),
    ("synonym", SRA, "Who owns the work product?", [(SRA, "7")], "vests in University", []),
    # multi-clause — the answer needs more than one clause
    ("multi", MSA, "Compare termination for cause with termination for convenience.", [(MSA, "6.1"), (MSA, "6.2")],
     "within thirty (30) days after receiving written notice", []),
    ("multi", MSA, "What are the payment terms and how are disputed invoices handled?", [(MSA, "3.2"), (MSA, "3.3")],
     "within thirty (30) days of the invoice date", []),
    ("multi", MSA, "What happens to our data when the agreement ends, and how long does confidentiality survive?",
     [(MSA, "7"), (MSA, "8")], "within thirty (30) days and certify deletion", []),
    ("multi", MSA, "What is the term and how can we end it early?", [(MSA, "5"), (MSA, "6.2")],
     "an initial term of three (3) years", []),
    # follow-ups — the question leans on the previous turn
    ("follow-up", MSA, "Are there any exceptions to it?", [(MSA, "12")], "do not apply to a party's breach",
     _h("What does the limitation of liability clause say?", "Liability is capped at twelve months of fees [§12].")),
    ("follow-up", MSA, "And how do we stop it renewing?", [(MSA, "5")], "notice of non-renewal at least ninety (90) days",
     _h("What is the initial term of the MSA?", "Three years from the Effective Date [§5].")),
    ("follow-up", MSA, "What are the amounts?", [(MSA, "13")], "at least $2,000,000 per occurrence",
     _h("What insurance does the supplier need?", "Several kinds of insurance [§13].")),
    ("follow-up", SRA, "How long can the sponsor delay it?", [(SRA, "5")], "an additional thirty (30) days",
     _h("Can the university publish the results?", "Yes, after giving the sponsor notice [§5].")),
    # cross-document — a project question over the SOW and the MSA
    ("cross-doc", PROJECT, "Which agreement gives the customer longer to pay invoices?", [(SOW, "6"), (MSA, "3.2")],
     "within forty-five (45) days of receipt", []),
    ("cross-doc", PROJECT, "Who owns the deliverables under the SOW and the MSA?", [(SOW, "14"), (MSA, "9")],
     "shall belong exclusively to Customer", []),
    # out of scope — the honest answer is "the agreement doesn't say"
    ("out-of-scope", MSA, "Is there a non-compete clause?", [], "", []),
    ("out-of-scope", SRA, "What is the liability cap in this agreement?", [], "", []),
    ("out-of-scope", SOW, "What service level credits apply?", [], "", []),
    ("out-of-scope", MSA, "Is there a most favoured nation pricing clause?", [], "", []),
    ("out-of-scope", SRA, "Does the sponsor get a seat on the university board?", [], "", []),
    ("out-of-scope", SOW, "What's the weather like in Seattle?", [], "", []),
]
# Held out: phrased AFTER the retrieval vocabulary was written and never used to
# tune it, so this row estimates how the expansion generalises.
QUESTIONS += [
    ("held-out", SOW, "By when must Harbor City settle a bill?", [(SOW, "6")], "within forty-five (45) days of receipt", []),
    ("held-out", SOW, "What is the hourly rate for a senior consultant?", [(SOW, "5")], "$185 per hour", []),
    ("held-out", SOW, "Who is the engagement lead?", [(SOW, "10")], "Priya Raman", []),
    ("held-out", MSA, "How many years does the agreement run initially?", [(MSA, "5")], "three (3) years", []),
    ("held-out", MSA, "What happens if one side breaks the contract?", [(MSA, "6.1")], "commits a material breach", []),
    ("held-out", MSA, "Are lost profits recoverable?", [(MSA, "12")], "lost profits or revenue", []),
    ("held-out", MSA, "Where must lawsuits be filed?", [(MSA, "16")], "courts located in King County, Washington", []),
    ("held-out", MSA, "Can the vendor hand the contract to another company?", [(MSA, "18")], "prior written consent", []),
    ("held-out", SRA, "How much is paid upfront?", [(SRA, "4")], "$250,000 within thirty (30) days", []),
    ("held-out", SRA, "Can the sponsor stop the university from publishing?", [(SRA, "5")], "free to publish", []),
    ("held-out", SRA, "Is the research guaranteed to work?", [(SRA, "10")], "provided AS IS", []),
    ("held-out", SRA, "Who owns jointly made inventions?", [(SRA, "7")], "are owned jointly", []),
]
CATEGORIES = ["factual", "synonym", "multi", "follow-up", "cross-doc", "held-out", "out-of-scope"]


# ---------------------------------------------------------------------------
# Analyzer, BM25 and a fake embedding
# ---------------------------------------------------------------------------

_STOP = set("a an and are as at be but by for if in into is it no not of on or such that the their then there "
            "these they this to was will with".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][a-z0-9]+)*")


def _cons(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return False
    return not (ch == "y" and i > 0 and _cons(w, i - 1))


def _fix(w: str) -> str:
    if w.endswith(("at", "bl", "iz")):
        return w + "e"
    if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "lsz" and _cons(w, len(w) - 1):
        return w[:-1]
    return w


def stem(w: str) -> str:
    """A compact Porter-style stemmer: what the ``english`` analyzer does, closely
    enough that "terminate / termination / terminated" and "renews / renewal"
    meet, and "paid" / "payment" do not (as with the real Porter stemmer)."""
    if len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ing") and len(w) > 5 and any(c in "aeiouy" for c in w[:-3]):
        w = _fix(w[:-3])
    elif w.endswith("ed") and len(w) > 4 and any(c in "aeiouy" for c in w[:-2]):
        w = _fix(w[:-2])
    if w.endswith("y") and len(w) > 2 and _cons(w, len(w) - 2):
        w = w[:-1] + "i"
    for suf, rep in (("ational", "ate"), ("tional", "tion"), ("ization", "ize"), ("ation", "ate"),
                     ("ator", "ate"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"),
                     ("aliti", "al"), ("iviti", "ive"), ("biliti", "ble"), ("alli", "al"), ("entli", "ent"),
                     ("ousli", "ous"), ("icate", "ic"), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"),
                     ("ness", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + rep
            break
    for suf in ("ement", "ment", "ance", "ence", "able", "ible", "ant", "ent", "ism", "ate", "iti", "ous",
                "ive", "ize", "ion", "al", "er", "ic"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            base = w[: -len(suf)]
            if suf == "ion" and base[-1] not in "st":
                continue
            w = base
            break
    if w.endswith("e") and len(w) > 5:
        w = w[:-1]
    return w


def analyze(text: str, analyzer: str = "english") -> list[str]:
    text = re.sub(r"'s\b", "", (text or "").lower())
    tokens = _TOKEN_RE.findall(text)
    if analyzer == "standard":
        return tokens
    if analyzer != "english":
        raise NotImplementedError(f"fake search has no analyzer {analyzer!r}")
    return [stem(t) for t in tokens if t not in _STOP]


def fake_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for tok in analyze(text):
        vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _NotFound(Exception):
    """Shaped like opensearchpy.NotFoundError (status 404, index_not_found_exception)."""
    status_code = 404

    def __init__(self, index):
        super().__init__(404, "index_not_found_exception", f"no such index [{index}]")


_NotFound.__name__ = "NotFoundError"


class _FakeSearch:
    """An in-memory OpenSearch: two indices, the query DSL Sonar sends, BM25."""

    K1, B = 1.2, 0.75

    def __init__(self):
        self.store: dict[str, dict[str, dict[str, Any]]] = {}
        self.bodies: dict[str, dict[str, Any]] = {}
        self.indices = self
        self._stats: dict[str, Any] = {}
        self._sparse: dict[tuple[str, str], dict[int, float]] = {}
        self.searches: list[tuple[str, dict]] = []

    # indices API ------------------------------------------------------------
    def exists(self, index):
        return index in self.store

    def create(self, index, body):
        self.store[index] = {}
        self.bodies[index] = body

    def put_mapping(self, index, body):
        self.bodies[index].setdefault("mappings", {}).setdefault("properties", {}).update(body.get("properties", {}))

    def get_mapping(self, index):
        return {index: {"mappings": self.bodies[index].get("mappings", {})}}

    def bulk(self, actions):
        for a in actions:
            src = {k: v for k, v in a.items() if not k.startswith("_")}
            self.store[a["_index"]][a["_id"]] = src
            self._stats.pop(a["_index"], None)
        return len(actions), []

    # analysis -----------------------------------------------------------------
    def _analyzer(self, index: str, field: str) -> str:
        body = self.bodies[index]
        props = body.get("mappings", {}).get("properties", {})
        spec = props.get(field) or {}
        if spec.get("type") == "keyword":
            return "keyword"
        name = spec.get("search_analyzer") or spec.get("analyzer") or "default"
        analyzers = ((body.get("settings") or {}).get("analysis") or {}).get("analyzer") or {}
        if name in analyzers:
            kind = analyzers[name].get("type")
            if kind in ("english", "standard"):
                return kind
            raise NotImplementedError(f"fake search cannot run custom analyzer {name!r}")
        return "standard" if name == "default" else name

    def _field_stats(self, index: str):
        if index not in self._stats:
            docs = self.store[index]
            stats: dict[str, Any] = {}
            for field in ("title", "section", "text"):
                analyzer = self._analyzer(index, field)
                tfs = {i: _counts(analyze(str(src.get(field) or ""), analyzer)) for i, src in docs.items()}
                lengths = {i: sum(c.values()) for i, c in tfs.items()}
                df: dict[str, int] = defaultdict(int)
                for c in tfs.values():
                    for t in c:
                        df[t] += 1
                stats[field] = {"tf": tfs, "len": lengths, "df": df, "n": len(docs),
                                "avg": (sum(lengths.values()) / len(docs)) if docs else 1.0, "an": analyzer}
            self._stats[index] = stats
        return self._stats[index]

    def _bm25(self, index: str, field: str, query: str, doc_id: str) -> float:
        st = self._field_stats(index)[field]
        tf, dl = st["tf"][doc_id], st["len"][doc_id] or 1
        score = 0.0
        for t in analyze(query, st["an"]):
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (st["n"] - st["df"][t] + 0.5) / (st["df"][t] + 0.5))
            score += idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * dl / (st["avg"] or 1)))
        return score

    # query DSL ----------------------------------------------------------------
    def _matches(self, index, q, doc_id, src) -> bool:
        return self._score(index, q, doc_id, src) is not None

    def _score(self, index: str, q: dict, doc_id: str, src: dict) -> float | None:
        (kind, spec), = q.items()
        if kind == "bool":
            must = [self._score(index, c, doc_id, src) for c in _as_list(spec.get("must"))]
            if any(s is None for s in must):
                return None
            if not all(self._matches(index, c, doc_id, src) for c in _as_list(spec.get("filter"))):
                return None
            if any(self._matches(index, c, doc_id, src) for c in _as_list(spec.get("must_not"))):
                return None
            should = [s for s in (self._score(index, c, doc_id, src) for c in _as_list(spec.get("should")))
                      if s is not None]
            # Lucene: with a must or filter clause present, should clauses are optional.
            msm = spec.get("minimum_should_match",
                           0 if (spec.get("must") or spec.get("filter")) else (1 if spec.get("should") else 0))
            if len(should) < int(msm):
                return None
            return (sum(must) + sum(should)) * float(spec.get("boost", 1.0))
        if kind == "multi_match":
            if spec.get("operator", "or") != "or" or spec.get("type", "best_fields") not in ("best_fields", "most_fields"):
                raise NotImplementedError(f"fake multi_match: {spec}")
            scores = []
            for f in spec["fields"]:
                name, _, boost = f.partition("^")
                scores.append(self._bm25(index, name, spec["query"], doc_id) * float(boost or 1))
            if not any(scores):
                return None
            if spec.get("type", "best_fields") == "most_fields":
                total = sum(scores)
            else:
                best = max(scores)
                total = best + float(spec.get("tie_breaker", 0.0)) * (sum(scores) - best)
            return total * float(spec.get("boost", 1.0))
        if kind == "match":
            (field, m), = spec.items()
            m = m if isinstance(m, dict) else {"query": m}
            s = self._bm25(index, field, m["query"], doc_id)
            return s * float(m.get("boost", 1.0)) if s else None
        if kind == "term":
            (field, v), = spec.items()
            v = v if isinstance(v, dict) else {"value": v}
            return float(v.get("boost", 1.0)) if src.get(field) == v["value"] else None
        if kind == "terms":
            boost = float(spec.get("boost", 1.0))
            (field, values), = ((k, v) for k, v in spec.items() if k != "boost")
            return boost if src.get(field) in values else None
        if kind == "prefix":
            (field, v), = spec.items()
            v = v if isinstance(v, dict) else {"value": v}
            return float(v.get("boost", 1.0)) if str(src.get(field) or "").startswith(v["value"]) else None
        if kind == "match_all":
            return 1.0
        if kind == "script_score":
            if self._score(index, spec["query"], doc_id, src) is None:
                return None
            params = spec["script"]["params"]
            return 1.0 + self._cos(index, doc_id, src, params["query_value"])
        if kind == "knn":
            return 1.0 + self._cos(index, doc_id, src, spec["vector"]["vector"])
        raise NotImplementedError(f"fake search has no {kind!r} query")

    def _cos(self, index, doc_id, src, query_vec) -> float:
        key = (index, doc_id)
        if key not in self._sparse:
            self._sparse[key] = {i: v for i, v in enumerate(src.get("vector") or []) if v}
        if len(query_vec) != len(src.get("vector") or []):
            raise RuntimeError("search_phase_execution_exception: query vector dimension mismatch")
        dv = self._sparse[key]
        return sum(v * query_vec[i] for i, v in dv.items())

    def search(self, index, body):
        self.searches.append((index, body))
        if index not in self.store:
            raise _NotFound(index)
        scored = []
        for order, (doc_id, src) in enumerate(self.store[index].items()):
            s = self._score(index, body["query"], doc_id, src)
            if s is not None:
                scored.append((-s, order, doc_id, src))
        scored.sort()
        excludes = set(((body.get("_source") or {}).get("excludes")) or [])
        hits = [{"_id": d, "_score": -s, "_source": {k: v for k, v in src.items() if k not in excludes}}
                for s, _, d, src in scored[: body.get("size", 10)]]
        return {"hits": {"hits": hits}}


def _as_list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def _counts(tokens):
    out: dict[str, int] = defaultdict(int)
    for t in tokens:
        out[t] += 1
    return out


# ---------------------------------------------------------------------------
# The world: index the corpus, wire the handler to the fakes
# ---------------------------------------------------------------------------


_CONTEXT_LABEL_RE = re.compile(r"^\[§([^\]]+)\][^\n]*?(?:\(document: ([^)]*)\))?$", re.MULTILINE)


class _Model:
    """Scripted model: reads the clause labels it was shown and answers from them."""

    def __init__(self):
        self.mode = "faithful"
        self.spec: dict[str, Any] = {}
        self.prompts: list[str] = []

    def shown(self, user: str) -> list[tuple[str | None, str]]:
        doc_by_title = {v: k for k, v in TITLES.items()}
        return [(doc_by_title.get(t) if t else None, n.strip()) for n, t in _CONTEXT_LABEL_RE.findall(user)]

    def __call__(self, **kw) -> str:
        user = kw["user"]
        self.prompts.append(user)
        shown = self.shown(user)
        target = self.spec["target"]
        visible = {(d or target, n) for d, n in shown}
        gold = [g for g in self.spec["gold"] if g in visible]
        first_ctx = shown[0][1] if shown else "1"
        if self.mode == "fabricated":
            cite = gold[0][1] if gold else first_ctx
            return f"It is $9,876,543 with 77 days' notice, starting March 3, 2031 [§{cite}]."
        if not gold:
            return "The agreement doesn't say. None of the clauses provided address this."
        parts = []
        if gold[0] == self.spec["gold"][0]:
            parts.append(f'The agreement says "{self.spec["fact"]}" [§{gold[0][1]}].')
        parts += [f"See also [§{n}]." for _, n in gold[1:]] if parts else [f"See [§{n}]." for _, n in gold]
        answer = " ".join(parts)
        if self.mode == "invented":
            answer += f" Compare [§42.9] and [§{gold[0][1]}, §77]."
        return answer


@pytest.fixture(scope="module")
def world():
    mp = pytest.MonkeyPatch()
    search = _FakeSearch()
    mp.setattr(opensearch, "client", lambda: search)
    mp.setattr(opensearch, "bulk_index", lambda actions: search.bulk(list(actions)))
    opensearch.ensure_indices()
    for doc_id, (doc_type, clauses) in CORPUS.items():
        records = embed.build_chunks(clauses, doc_id=doc_id, tenant_id=TENANT, doc_type=doc_type, run_id="run-1")
        out = opensearch.index_chunks([{**{k: v for k, v in r.items() if not k.startswith("_")},
                                        "vector": fake_embed(r["_embed"])} for r in records])
        assert out["failed"] == 0
    yield search
    mp.undo()


@pytest.fixture
def ask(world, monkeypatch, ddb):
    model = _Model()
    monkeypatch.setattr(rag, "get_doc_meta", lambda d: (
        {"docId": d, "tenantId": TENANT, "ownerSub": OWNER, "title": TITLES[d]} if d in TITLES else None))
    real_scope = rag._scope_for
    monkeypatch.setattr(rag, "_scope_for", lambda target, caller: (
        {"doc_ids": [SOW, MSA]} if target == PROJECT else real_scope(target, caller)))
    monkeypatch.setattr(rag.ddb, "get_docs", lambda ids: [{"docId": d, "title": TITLES[d]} for d in ids if d in TITLES])
    monkeypatch.setattr(rag, "_contract_brief", lambda doc_id, meta: None)
    monkeypatch.setattr(rag, "embed_texts", lambda texts, model=None: [fake_embed(t) for t in texts])
    monkeypatch.setattr(rag, "chat_text", model)

    def run(spec: dict[str, Any], mode: str = "faithful", top_k: int = 8) -> dict[str, Any]:
        model.mode, model.spec = mode, spec
        body = {"question": spec["question"], "topK": top_k, "history": spec["history"]}
        ev = {"pathParameters": {"docId": spec["target"]}, "body": json.dumps(body),
              "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {"sub": OWNER}}}}}
        resp = rag._http_handle(ev)
        assert resp["statusCode"] == 200, resp
        return json.loads(resp["body"])
    run.model = model
    return run


def _spec(row) -> dict[str, Any]:
    category, target, question, gold, fact, history = row
    return {"category": category, "target": target, "question": question, "gold": gold, "fact": fact,
            "history": history}


def _context(out) -> list[tuple[str, str]]:
    return [(c["docId"], c["clauseNumber"]) for c in out["citations"]]


def evaluate(ask) -> dict[str, dict[str, float]]:
    rows: dict[str, list[dict[str, float]]] = defaultdict(list)
    for row in QUESTIONS:
        spec = _spec(row)
        m: dict[str, float] = {}
        if spec["gold"]:
            ctx4, ctx8 = _context(ask(spec, top_k=4)), None
            faithful = ask(spec)
            ctx8 = _context(faithful)
            m["r@4"] = sum(g in ctx4 for g in spec["gold"]) / len(spec["gold"])
            m["r@8"] = sum(g in ctx8 for g in spec["gold"]) / len(spec["gold"])
            ranks = [ctx8.index(g) + 1 for g in spec["gold"] if g in ctx8]
            m["mrr"] = 1.0 / min(ranks) if ranks else 0.0
            m["grounded"] = float(bool(faithful.get("grounded")))
            m["fp"] = float(bool(faithful.get("unverified")))           # faithful answer flagged
            invented = ask(spec, "invented")
            if invented.get("usedCitations"):
                left = sum(ref in invented["answer"] for ref in ("§42.9", "§77"))
                m["invented_left"] = left / 2
                m["cites_ok"] = float(set(invented["usedCitations"]) <= {n for _, n in _context(invented)})
        else:
            honest = ask(spec)
            m["abstain_ok"] = float(honest.get("grounded") is False and not honest.get("unverified"))
        fabricated = ask(spec, "fabricated")
        flagged = set(fabricated.get("unverified") or [])
        m["fabricated_flagged"] = sum(f in flagged for f in ("$9,876,543", "77 days", "March 3, 2031")) / 3
        rows[spec["category"]].append(m)
        if VERBOSE and spec["gold"] and m["r@8"] < 1:
            print(f"  MISS [{spec['category']}] {spec['question']!r}: gold={spec['gold']} ctx@8={ctx8}")

    report: dict[str, dict[str, float]] = {}
    for cat in CATEGORIES + ["ALL"]:
        ms = [m for c, items in rows.items() if cat in (c, "ALL") for m in items]
        keys = sorted({k for m in ms for k in m})
        report[cat] = {k: sum(m[k] for m in ms if k in m) / max(1, sum(k in m for m in ms)) for k in keys}
        report[cat]["n"] = len(ms)
    return report


def _print(report):
    cols = ["n", "r@4", "r@8", "mrr", "grounded", "cites_ok", "invented_left", "fabricated_flagged", "fp",
            "abstain_ok"]
    print("\n" + " | ".join(f"{c:>12}" for c in ["category"] + cols))
    for cat, m in report.items():
        print(" | ".join([f"{cat:>12}"] + [f"{m[c]:>12.2f}" if c in m else f"{'-':>12}" for c in cols]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_fact_is_in_its_gold_clause_and_the_corpus_is_chunked():
    bodies = {(d, c["number"]): c["body"] for d, (_, cl) in CORPUS.items() for c in cl}
    for row in QUESTIONS:
        spec = _spec(row)
        for g in spec["gold"]:
            assert g in bodies, g
        if spec["fact"]:
            assert spec["fact"] in bodies[spec["gold"][0]], spec["question"]
    # the long clauses really are split, and the answer to "who owns the IP" is not in the first chunk
    records = embed.build_chunks(_MSA_CLAUSES, doc_id=MSA, tenant_id=TENANT, doc_type="MSA", run_id="r")
    ip = [r for r in records if r["clauseNumber"] == "9"]
    assert len(ip) >= 2 and "Customer owns all Work Product" not in ip[0]["text"]
    assert len([r for r in records if r["clauseNumber"] == "1"]) >= 2
    assert len(QUESTIONS) >= 40


def test_rag_eval_report(ask, monkeypatch):
    report = evaluate(ask)
    print("\n== full pipeline ==")
    _print(report)
    # Ablations (printed only): what each retrieval change contributes.
    with monkeypatch.context() as m:
        # The retrieval as it was before this harness: literal BM25 over
        # title^2/section/text, plain RRF order, follow-ups without cited clauses.
        m.setattr(opensearch, "_bm25_query", lambda text, expand: {
            "must": [{"multi_match": {"query": text, "fields": ["title^2", "section", "text"]}}]})
        m.setattr(opensearch, "_rerank", lambda ranked, categories, k: ranked[:k])

        def legacy_search_text(question, history):
            prev = next((t["content"] for t in reversed(history) if t["role"] == "user"), "")
            words = len(question.split())
            if prev and (words <= 4 or (words <= 14 and rag._FOLLOW_UP_RE.search(question))):
                return f"{prev}\n{question}"
            return question
        m.setattr(rag, "_search_text", legacy_search_text)
        print("\n== before: legacy retrieval (answer checks still on) ==")
        _print(evaluate(ask))
    with monkeypatch.context() as m:
        m.setattr(opensearch, "_CONCEPTS", ())
        print("\n== without synonym expansion / intent rerank ==")
        _print(evaluate(ask))
    with monkeypatch.context() as m:
        m.setattr(opensearch, "_INTENT_BONUS", 0.0)
        print("\n== without intent rerank ==")
        _print(evaluate(ask))

    assert report["ALL"]["n"] == len(QUESTIONS)
    # Retrieval floor: a gold clause that stops reaching the context is a regression.
    for cat, floor in {"factual": 1.0, "synonym": 0.9, "multi": 0.9, "follow-up": 1.0, "cross-doc": 0.75,
                       "held-out": 0.75}.items():
        assert report[cat]["r@8"] >= floor, (cat, report[cat])
    assert report["ALL"]["mrr"] >= 0.85
    # Answer checks.
    assert report["ALL"]["cites_ok"] == 1.0            # usedCitations never name a clause outside the context
    assert report["ALL"]["invented_left"] == 0.0       # invented citations are stripped from the text
    assert report["ALL"]["fabricated_flagged"] == 1.0  # every invented figure is listed as unverified
    assert report["ALL"]["fp"] == 0.0                  # a faithful quote is never flagged
    assert report["out-of-scope"]["abstain_ok"] == 1.0
