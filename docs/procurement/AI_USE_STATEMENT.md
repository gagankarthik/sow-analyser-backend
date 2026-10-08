# Blue-IQ Govern: how AI is used

Plain-language statement for the customer Legal Affairs, Privacy, and AI governance reviewers. Prepared 2026-10-08 from the code in `sow-analyser-backend` and `sow-analyzer`.

## In one paragraph

Govern uses an AI model from OpenAI to **read** agreements: to pull out facts (parties, dates, amounts) and to label each clause by type (for example "indemnity" or "publication review"). It does **not** use AI to **decide** anything. Ordinary code checks each labelled clause against the customer's own contract matrix, so the same clause always gets the same result. People make every decision: send back, approve, escalate, sign. The AI never contacts a sponsor, changes contract text on its own, or approves anything.

## Where AI is and is not used

| Step | AI used? | What happens |
|---|---|---|
| Reading text from the file | No (Amazon Textract OCR for scans) | Text is extracted inside AWS |
| Extracting facts (parties, dates, money, scope) | **Yes**, OpenAI | The model returns structured fields. A second pass re-checks amounts against quoted source text |
| Splitting the agreement into clauses | No | Done in code, so no text is skipped (`shared/segment.py`) |
| Labelling each clause (type, risk, short summary) | **Yes**, OpenAI | The model labels clauses that code has already cut out |
| Search ("find similar clauses") | **Yes**, OpenAI embeddings | Clause text becomes vectors stored in the customer's search index |
| **Matrix review: is this clause within the customer terms?** | **No** | Deterministic rules from the the customer matrix version in force (`shared/govern/matrix.py`). Each finding records which rule fired and which matrix version applied |
| Routing, deadlines, reminders, reports | No | Workflow code |
| Approve / send back / escalate / sign | No | A named person acts. Each action goes to the append-only activity log |
| "Ask Sonar" questions about a contract | **Yes**, OpenAI | Answers are restricted to the user's permitted documents and must cite the clause. Personal identifiers are masked before sending |
| SOW drafting (optional feature) | **Yes**, OpenAI | Produces a draft for a person to edit. Same masking |

## Data and training

- **Training.** OpenAI's API terms say API data is not used to train its models unless the customer opts in. Blue-IQ has not opted in. The code will not send data to any AI provider that is not registered as "no training"; it stops instead (`shared/guardrails.py`).
- **Retention by OpenAI.** Today, OpenAI may keep API inputs for up to 30 days for abuse monitoring. Blue-IQ is applying for Zero Data Retention and will sign OpenAI's DPA **before the the customer pilot**. Until both are in place, we will not tell the customer that text is "not retained". See `SUBPROCESSORS.md`.
- **What is masked.** On the question-and-answer and drafting paths, emails, phone numbers, SSNs, card numbers and IP addresses are replaced with placeholders before sending. On the extraction path, text is sent as written, because the model needs to read amounts, dates and parties.
- **Logging.** Every AI call is logged with provider, operation and size, never the text. Logs are kept 30 days.
- **the customer data does not improve Blue-IQ's models.** Blue-IQ does not fine-tune or train any model on customer documents. There is no fine-tuning code in the repository.

## Human in the loop

- The AI labels; code grades; a person decides.
- Every finding shows the contract text it came from and the matrix rule it was graded against. Reviewers can accept, edit or dismiss it with a reason.
- Status changes only through an action by a named person, or a verified external event such as a DocuSign "signed" webhook. Each change is written to the immutable activity log.
- Output carries the notice "decision support, not legal advice" (Terms of Service).

## Known limits

- The model can mislabel a clause. A mislabel can lead to a wrong matrix outcome, or to "missing". Mitigations: "needs check" states, source quotes, and reviewer confirmation. A labelled regression set of about 50 the customer agreements is planned before go-live (`COMPETITIVE_ANALYSIS.md` §7). It is **not yet built**.
- Scanned documents depend on OCR quality. A per-page OCR quality flag is planned, **not yet built**.
- There is no published accuracy benchmark yet.

## Alignment with university AI-governance expectations

| Expectation | Govern position |
|---|---|
| Transparency about where AI is used | This statement; AI-produced fields are identifiable in the product |
| No training on institutional data | Supported by OpenAI API terms and the code's provider allowlist; DPA pending |
| Data minimisation | Only extracted text is sent, never files; identifiers masked on chat/drafting paths |
| Human accountability for decisions | All decisions are by named people and logged |
| Explainability | Deterministic matrix rules; each finding names its rule and matrix version |
| Ability to restrict AI on sensitive data (e.g. export-controlled, CUI) | **Not yet built.** Planned: a classification flag that blocks AI processing of marked agreements |
| Accessibility of AI features | Covered by `ACCESSIBILITY_CONFORMANCE_PLAN.md` |

Contact: privacy@blue-iq.ai
