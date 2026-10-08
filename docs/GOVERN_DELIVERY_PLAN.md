# Govern for the customer — agile delivery plan and design decisions

Goal: help the customer make better and faster contract decisions, with visibility into
where every contract is, what it is worth and how the trends are moving, while
capture never misses a document or a field.

## How we work

* **Vertical slices.** Each item ships end to end (API + UI + test) and is
  demonstrable on its own. No layer is built ahead of a screen that uses it.
* **Definition of done.** Contract in `GOVERN_API.md` honoured; offline tests
  green; `tsc`, ESLint and `next build` clean; loading / error / empty states;
  works at 360 px, iPad and laptop; plain-language copy passes the "leader test"
  (a non-technical leader answers her question without help).
* **One source of truth.** Wording in `lib/govern/labels.ts`, maths in
  `lib/govern/metrics.ts`, transitions in `shared/govern/workflow.py`. A figure or
  a word is the same on every screen and in every export.
* **Measure the outcome.** The Trends report itself is the scorecard: cycle time,
  on-time %, rounds per contract and throughput, before and after Govern.

## Backlog (priority order; dates from the requirements doc)

| # | Slice | Requirement | Done when | Target |
|---|---|---|---|---|
| 1 | Editable matrix, Excel/CSV import, versioning | R1 | Admin uploads the Excel matrix, edits one position, rescoring a contract reflects it | Fri Oct 9 |
| 2 | Research & licensing clause types, 4 tiers + beneficial | R1 | Sample license and sponsored research agreement show the new types, tiered, favourable terms tagged | Fri Oct 9 |
| 3 | Owner, days in stage, waiting on, on every card | R2, R3 | All three on every card; days count from the last stage change | Mon Oct 12 |
| 4 | Approve / send back / escalate / reject + activity log | R2 | Each action moves the card and writes a log entry with user and time | Mon Oct 12 |
| 5 | Blockers and recommended next step | R3 | Each unsigned contract lists its open items and one recommended action | Tue Oct 13 |
| 6 | Current / potential / held-up value tiles | R4 | Tile totals match the sample contracts | Tue Oct 13 |
| 7 | Leader home (three questions) + plain-word search | R5 | the general counsel reaches any contract's blockers in ≤ 3 clicks | Tue Oct 13 |
| — | Dry run of the demo story; stable demo build | — | Story runs end to end with no manual fixes | Oct 13–14 |
| 8 | Intake on upload, capture gaps, reconciliation sweep | R2, capture | Every upload is a contract within seconds; nothing missing is silent | Sprint 2 |
| 9 | Bottleneck report, value report, Excel/PDF export | R3, R4 | Every report downloads and matches the screen | Sprint 2 |
| 10 | Trends (write-time aggregates) | decisions | 12 months of throughput, cycle time and deviation trends in < 1 s | Sprint 2 |
| 11 | Routing rules, notifications (SES, Teams), overdue sweeper | R2 | Assign / send back / approve / overdue alerts arrive | Sprint 2 |
| 12 | Obligations, licensing income, renewals and close-out | R2, R4 | Signing extracts obligations with due dates | Sprint 3 |
| 13 | DocuSign webhook, Huron and Workday connectors, sync log, manual match | R6 | Live once the customer security review and credentials are in place | Sprint 3+ |
| 14 | the customer SSO (Cognito SAML) and Govern roles | R6 | Roles carry over from the customer's IdP | Sprint 3+ |

The code for all slices is built now. The order above is the order of
**hardening, demo and rollout**, so the Oct 14 demo leads with slices 1–7 and
nothing later can block it.

## Decisions: do we need it, and how is it optimised?

| Idea | Needed? | How it is optimised |
|---|---|---|
| Separate `govern` table | Yes: workflow state is small, hot and portfolio-wide; documents are large and per-item | Denormalised contract item; the leader view is one GSI query, no S3 or documents fan-out |
| Separate `govern-activity` table | Yes: an audit log must be immutable and is the event source | Put-only IAM; DynamoDB Stream → EventBridge; no dual writes |
| EventBridge bus + SQS | Yes: alerts, Huron push-back and trends must not slow a click, and must not be lost | Each consumer has its own queue + DLQ + alarm; one bus shared by Capture, Spend and Govern |
| Step Functions for the workflow | **No.** Contracts wait for weeks on people; a state machine per contract adds cost and opacity | A transition table in one module, optimistic locking, Scheduler-driven sweeps for time |
| OpenSearch for reports | **No.** 2,500 contracts/yr fits in one paginated query | Reports compute from the contract list; trends come from daily aggregate items |
| Trends | Yes: decisions need direction, not just a snapshot | Write-time atomic counters per day (≤ 366 reads for a year), idempotent via event markers, backfill script |
| Model call for matrix review | **No.** Must be explainable, repeatable and free to re-run | Deterministic rules + phrase lists; the model only labels clause types (already paid for) |
| Gantt / task trees | **No** (the Microsoft Project trap, R5) | Status changes only through actions people already take |
| Drag-and-drop board | **No.** It invites manual status entry | Cards move only through Approve / Send back / Escalate / Reject / signature |
| Live Huron/Workday sync before the demo | **No** (not expected by Oct 14) | Connector framework ships with dry-run sync logs and field mapping; credentials switch it to live |
| Capture safeguards | Yes: "capture should not miss anything" | Idempotent SQS intake + DLQ; hourly reconciliation of analysed documents without a contract; per-contract `captureGaps`; portfolio capture report |

## Risks and mitigations

* **the customer matrix not yet shared.** The default matrix is a proposal, versioned; the
  real one imports from Excel in minutes and old reviews keep their version.
* **Sample data.** research and licensing samples under `samples/research/` until redacted
  agreements arrive through the sandbox.
* **Identity.** Until the customer SSO is wired, Govern roles come from Cognito groups;
  a sandbox runs with `GOVERN_OPEN_ADMIN=true`.
