# Blue-IQ Govern — target architecture (the customer workflow, matrix, reporting, integrations)

Status: adopted October 2026. Source: "Blue IQ Govern: the customer Workflow and Reporting
Requirements" (Requirements 1–6). The API contract is in `GOVERN_API.md`.

## 1. What changed and why

The existing platform reads, scores and values a contract well. It is built as one
ingest pipeline (S3 → EventBridge → Step Functions, 7 stages), one HTTP Lambda that
routes every REST call, one single-table DynamoDB table and one OpenSearch domain.

the customer adds a second, very different workload: **moving a contract between people**.

| Concern | Ingest (today) | Govern (new) |
|---|---|---|
| Write pattern | a few large writes per document, once | many small writes per contract, over weeks |
| Read pattern | per document | portfolio-wide (board, leader home, bottleneck, value) |
| Consistency need | eventual | strong for state transitions (two reviewers acting at once) |
| Audit | file hashes per version | every assignment, action, comment, stage change — immutable |
| Side effects | none | email / Teams alerts, DocuSign, Huron push-back, Workday pull |
| Time-driven work | none | SLA amber/red, obligations due, connector polling |

Putting this into the current monolith would (a) make the leader view fan out to
DynamoDB + S3 per contract, (b) mix audit records with mutable state, and (c) put
side effects inside request latency. So Govern becomes its own bounded context with
its own storage, compute and events, reusing the identity, document access rules
and analysis output that already exist.

## 2. Target architecture

```
                         ┌──────────────── Cognito (JWT; SAML federation to the customer IdP, groups → roles)
                         ▼
Next.js ──► API Gateway HTTP API ──┬─► api Lambda          /documents /projects /playbook   (unchanged)
                                   ├─► rag Lambda          /documents/{id}/chat              (unchanged)
                                   ├─► govern-api Lambda   /contracts /matrix /workflow /integrations /reports
                                   └─► webhooks Lambda     /webhooks/{provider}   (no JWT; HMAC-verified)
                                                               │
S3 raw ─► EventBridge ─► Step Functions (Parse … Persist) ─► PutEvents "Document Analysed"
                                                               │
                         ┌──────── EventBridge bus  blue-iq-<stage>-platform ◄──────────────┐
                         │                                                                  │
      rule: Document Analysed ──► SQS govern-intake ──► govern-intake Lambda               │
                         │          (matrix review, intake, auto-assign, revision linking) │
      rule: Govern.* events ──► SQS notify ──► notifier Lambda (SES email, Teams webhook)  │
      rule: Govern.* events ──► SQS connector-out ──► connectors Lambda (Huron push-back)  │
                         │                                                                  │
EventBridge Scheduler ── hourly ──► govern-sweeper Lambda (SLA amber/red, obligations due, │
                         daily   ──► connectors Lambda (Huron pull, Workday spend pull)  ──┘

DynamoDB  documents table (existing single table, unchanged)
DynamoDB  govern-contracts  contract aggregate (contract, blockers, obligations, income, review)
DynamoDB  govern-activity   append-only audit log  ── Streams ──► activity-stream Lambda ──► EventBridge (Govern.*)
DynamoDB  govern-config     matrix versions, workflow settings, connectors
DynamoDB  govern-sync       sync runs, external-id map
DynamoDB  govern-metrics    daily trend aggregates (atomic counters)
Secrets Manager            DocuSign HMAC key, Teams webhook URL, Huron/Workday credentials
SES                        email alerts        KMS  table + bucket + secret encryption
```

### 2.1 Storage — one table per access pattern

Govern data is split by **who writes it, how often, and how it is read**, so each
table has one job, its own capacity, retention and IAM, and no table mixes hot
counters with configuration or audit with mutable state.

| Table | Holds | Written by | Read pattern | Notes |
|---|---|---|---|---|
| `govern-contracts` | the contract aggregate: contract item + its blockers, obligations, income items, latest matrix review | govern-api, intake, webhooks, sweeper | one contract = one Query on its partition; portfolio = one GSI query | optimistic locking on `rev`; denormalised for the leader view |
| `govern-activity` | append-only audit log | everyone (PutItem only) | newest-first per contract | Stream → EventBridge; PITR; never updated or deleted |
| `govern-config` | per-tenant configuration: matrix versions + current pointer, workflow settings, connector definitions & field mapping | govern-api (admins) | by key | low volume, versioned, admin-only writes |
| `govern-sync` | connector sync runs and the external-id map (Huron / Workday id → contract) | connectors, webhooks | sync log newest-first; lookup by external id | TTL on sync runs (400 days) |
| `govern-metrics` | daily aggregates for trends + idempotency markers for the stream | activity-stream Lambda (atomic `ADD`) | ≤ 366 BatchGets for a year | hot counters isolated; markers expire by TTL |

Keys:

| Table | PK | SK | Indexes |
|---|---|---|---|
| `govern-contracts` | `CON#<contractId>` | `META` · `BLK#<id>` · `OBL#<id>` · `INC#<id>` · `REVIEW#<docId>` | GSI1 board `T#<tenant>` / `<stage>#<stageEnteredAt>` (META only) · GSI2 owner queue `OWN#<email>` / `<stageEnteredAt>` · GSI3 obligations due `T#<tenant>#OBL` / `<dueDate>` |
| `govern-activity` | `CON#<contractId>` | `<iso>#<eventId>` | — |
| `govern-config` | `T#<tenant>` | `MATRIX#<v:06d>` · `MATRIX#CURRENT` · `SETTINGS` · `CONN#<id>` | — |
| `govern-sync` | `T#<tenant>` | `RUN#<iso>#<runId>` | GSI1 external id `EXT#<tenant>#<system>#<extId>` / `CON#<id>` (items `PK=CON#<id>`, `SK=EXT#<system>`) |
| `govern-metrics` | `T#<tenant>` | `D#<yyyy-mm-dd>` · `SEEN#<eventId>` (TTL) | — |

`contractId` is the docId of the first version of the agreement. Counterparty
redlines are later documents (`versionDocIds`), so one contract keeps one record,
one clock and one log across rounds.

The existing **documents table** is unchanged; Govern reads document META rows and
the S3 classification (for matrix review), and writes only `lifecycle`, which
mirrors the contract stage so existing pages (Library, Dashboard) stay right.

### 2.2 Compute

| Function | Trigger | Does |
|---|---|---|
| `govern-api` | HTTP API (JWT) | all Govern REST routes; state transitions; rescoring on demand |
| `govern-intake` | SQS ← EventBridge `Document Analysed` | matrix review of a READY document; create / update the contract; auto-assign; link a revision to its contract and rescore it; extract obligations & licensing income; Sonar blockers |
| `activity-stream` | DynamoDB Stream of `govern-activity` | republish each entry to EventBridge as `Govern.<action>` |
| `notifier` | SQS ← EventBridge `Govern.assigned|sent_back|approved|overdue|escalated` | SES email + Teams card per workflow settings; best-effort, DLQ on failure |
| `govern-sweeper` | EventBridge Scheduler (hourly) | SLA amber/red transitions → `overdue` activity (once per stage visit); obligations due in 14 days / overdue |
| `connectors` | Scheduler (daily) + SQS ← `Govern.*` | Huron pull of agreements (→ S3 raw, no re-upload), Huron push-back of findings, Workday award/cost-centre/spend pull; every run writes a sync log |
| `webhooks` | HTTP API `POST /webhooks/{provider}` (no JWT) | verifies HMAC (DocuSign Connect) and writes a `signed` action through the same transition code |

All Python 3.12 on the existing shared layer. Transition logic lives in one module
(`shared/govern/workflow.py`) used by `govern-api`, `govern-intake`, `webhooks` and
`govern-sweeper`, so a DocuSign webhook and a reviewer's click take exactly the same
path.

### 2.3 Events — one connector framework for Capture, Spend and Govern

A custom EventBridge bus `blue-iq-<stage>-platform` carries:

* `blue-iq.pipeline / Document Analysed` — `{docId, tenantId, docType, status}` from Step Functions;
* `blue-iq.govern / Govern.<action>` — every activity entry (from the stream).

Connectors subscribe by rule; adding Capture or Spend later is a new rule, not new
plumbing. Field mapping per client is data (`CONN#` items), so a new university or
Workday tenant needs configuration only. **System of record wins**: a connector
never overwrites a field a system of record owns; when values disagree Govern
keeps the record's value and writes a `conflict` activity entry.

### 2.4 Matrix review (Requirement 1) — deterministic, versioned

`shared/govern/matrix.py`. A matrix is a dated, immutable version holding one
playbook per agreement type (sponsored research, grant, license, option, MTA, NDA,
collaboration, other). Each clause rule: standard position, acceptable fallback,
unacceptable terms, beneficial terms, escalation office, numeric thresholds,
suggested redline language. Grading needs no model call: built-in checks for the
ten research/licensing types (publication review period, background IP, licence
scope, royalties, indemnity for a public university, home-state law / sovereign immunity,
export control, data rights / use of name, sponsor reporting / flow-down,
diligence) plus phrase lists. Outcomes: `within`, `fallback`, `deviates`,
`unacceptable`, `review`, `missing`; independently `beneficial`. The version used
is stored on the contract and on every review.

The classify prompt gains the six new clause categories so the model labels them.

### 2.5 Reads for leaders (Requirement 5) and scale (Requirement 6)

`GET /contracts` is one GSI1 query per tenant (≈ 2,500 new contracts a year plus a
backlog: tens of thousands of small items, paginated) followed by an access filter.
Days in stage, SLA colour and the recommended next step are computed in the API from
the item, never stored, so they are always current. Reports and exports are computed
from the same list. Batch backload uses the existing upload path (200 per call) and
the SQS-buffered intake, so a burst never slows the leader view.

### 2.6 Security

* JWT on every route except `/webhooks/*`, which verifies an HMAC with a key in
  Secrets Manager and rejects replays older than 5 minutes.
* Roles: Cognito groups `govern-admin` (matrix, routing, settings, integrations),
  `govern-reviewer` (actions), `govern-leader` (view, comment, approve when routed).
  When the user pool has no Govern groups (`GOVERN_OPEN_ADMIN=true`, the default for
  a sandbox) every signed-in user is an admin.
* Least privilege per function; activity writers are put-only; KMS at rest; TLS only.
* Contract visibility = document visibility (the existing per-project access rule).

### 2.7 Cost (2,500 contracts / year)

DynamoDB on-demand, Lambda, SQS, EventBridge, Scheduler and SES at this volume are
well under $25/month combined; the dominant cost remains OpenSearch and model calls,
which Govern does not add to (matrix review is deterministic).
