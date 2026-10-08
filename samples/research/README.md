# Northfield demo agreements

Six Northfield-style sample agreements, a revised licence for the counterparty round, and
the review matrix as an import sheet. The wording is original and modelled on the
shape of public university templates. Every party apart from The Northfield University
University is fictional. Each file has a Huron agreement number and a Workday
reference in its header (Requirement 6).

| File | Agreement type | Matrix result (default matrix v1) |
|---|---|---|
| `01-exclusive-license-v1.txt` | license | **3 flags.** Licence scope (all fields, no reserved research rights) is *deviates*. Royalties (1%, no sublicense share) is *deviates*. Governing law (Delaware law and a waiver of sovereign immunity) is *unacceptable*. Equity, the minimum annual royalty and the dated diligence milestones are tagged beneficial. |
| `01-exclusive-license-v2-revised.txt` | license (revision of v1) | **Clean.** Field of use plus reserved rights, 3.5% royalty plus 25% of sublicense income, Minnesota law with no waiver. |
| `02-option-agreement.txt` | option | No flags. The 15-month option period is a *fallback*. |
| `03-sponsored-research-agreement.txt` | sponsored_research | Sponsor Midwest Advanced Materials Corp., PI Dr. Priya Raman, Materials Science & Engineering, $425,000. The 90-day publication review is *deviates*. Northfield indemnifying the sponsor is *unacceptable*. Quarterly reports are *within*. |
| `04-material-transfer-agreement.txt` | mta (incoming) | No flags. Two *fallbacks*: Northfield indemnity limited by Minnesota law, and silence on governing law. |
| `05-mutual-nda.txt` | nda | The 10-year confidentiality term is *deviates* (the matrix allows 5, fallback 7). |
| `06-federal-subaward-grant.txt` | grant (outgoing, $180,000) | The foreign-national restriction is *unacceptable* and escalates to Export Control. The flow-down clauses are identified, so that clause is *within*. |
| `review-matrix.csv` | — | The default matrix for **License** and **Sponsored research** in the bulk-import format. |

## Demo story (Requirement 1–4)

1. **Load the matrix (admin).** Settings → Review matrix → Import, agreement type
   *License*, upload `review-matrix.csv` (merge or replace). The import keeps
   the 12 License rows. It skips the 10 Sponsored research rows with the reason
   "row is for agreement type 'sponsored_research'". Run the import again with
   agreement type *Sponsored research* to load those rows. Each import saves a
   new dated matrix version.
   * Optional: edit one position, for example change Publication `maxReviewDays`
     from 30 to 90, then rescore sample 03. Its publication clause becomes
     *within*.
2. **A licence arrives.** Upload `01-exclusive-license-v1.txt`. Sonar classifies it
   as a *License*, incoming, with Lakeshore BioSensors, Inc. as licensee. It grades
   the licence against matrix v1 and opens **three blockers**:
   * *License grant scope:* "The licence is exclusive, worldwide, in all fields of
     use with no reserved right for Northfield to use the technology for research and
     education …"
   * *Royalties:* "Running royalty is 1% of net sales; the matrix requires at least
     3% (fallback 2%). No share of sublicense income is stated …"
   * *Governing law:* "Governing law is Delaware and Northfield would waive its sovereign
     immunity …" This one is unacceptable, so it names Legal Affairs.

   Each blocker carries the matrix's suggested redline. The licensing income
   (the $75,000 issue fee, $50k / $150k / $250k milestones with expected dates, 1%
   royalty, 5% equity and the $10,000 minimum annual royalty) counts as
   **potential** value.
3. **Send back.** The reviewer chooses *Send back for amendment* with the three
   clauses and their suggested language. The card moves to Negotiation, waiting
   on the counterparty, and `rounds` becomes 1.
4. **Revision received.** Upload `01-exclusive-license-v2-revised.txt` through
   *Upload revision* on the same contract. It becomes a new version of the same
   contract. Sonar rescores it, finds **0 deviations** and closes the three
   blockers. The contract returns to In review.
5. **Approve → sign.** *Approve for signature* moves the contract to Ready to sign.
   Then *Send for signature* (DocuSign or manual), then *Mark signed*.
6. **Value moves.** Once signed, the contract's value moves from **potential** to
   **current** on the dashboard. Obligations are extracted: three milestone
   payments, three diligence milestones, quarterly royalty reports (the first is
   due 45 days after the first quarter end) and the annual progress report.

The other samples fill the board for the leader view. Sample 03 shows escalation
for the Northfield indemnity. Sample 05 shows a simple send-back. Sample 06 shows an
Export Control escalation and an outgoing subaward in the money report.

## How grading works

`lambdas/shared/govern/matrix.py` grades clauses deterministically, with no model
call. The classify stage labels each clause with a category, including the six
research categories `PublicationRights`, `BackgroundIP`, `ExportControl`,
`DataRights`, `SponsorReporting` and `Diligence`. Built-in checks then read the
numbers that matter: review days, royalty %, years of confidentiality, state of
governing law, foreign-national wording and so on. They use the matrix clause's
`thresholds`, then its unacceptable and beneficial phrase lists. The tests in
`tests/test_govern_matrix.py` grade these exact files.
