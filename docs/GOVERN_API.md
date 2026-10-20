# Govern API contract (v1)

The single source of truth shared by `govern-api` (Python) and the Next.js client
(`sow-analyzer/lib/govern/*`). All routes need the Cognito JWT except `/webhooks/*`
and `/portal/*` (the counterparty portal: the link token is the credential).
Errors use the existing body `{"error": "<message>", "code": "<reason>"}`
(`bad_request`, `not_found`, `forbidden`, `conflict`, `invalid_transition`,
`feature_disabled` — see "Later" features under Me).
Dates are ISO-8601 strings (`2026-10-08T14:03:00Z`; date-only fields `2026-10-08`).
Money is a plain number plus a `currency` code. `null` always means "unknown", never 0.

## Enumerations

```
AgreementType = "sponsored_research" | "grant" | "license" | "option" | "mta" | "nda" | "collaboration" | "other"
Direction     = "incoming" | "outgoing"         # incoming = sponsor funding, licence fees, royalties
Stage         = "draft" | "review" | "negotiation" | "approval" | "signed" | "active" | "renewal" | "expired"
State         = "intake" | "in_review" | "sent_back" | "escalated" | "ready_to_sign"
              | "out_for_signature" | "signed" | "active" | "rejected" | "closed"
WaitingOnKind = "internal_reviewer" | "internal_office" | "counterparty" | "pi_department" | "signatory" | "nobody"
Office        = "legal_affairs" | "tech_commercialization" | "sponsored_programs" | "export_control" | "risk_management"
Tier          = "within" | "fallback" | "deviates" | "unacceptable" | "review" | "missing"
SlaStatus     = "on_track" | "amber" | "red" | "none"
RejectReason  = "unacceptable_terms" | "sponsor_withdrew" | "pi_withdrew" | "duplicate" | "out_of_scope" | "other"
NextAction    = "approve" | "send_back" | "escalate" | "reject" | "send_for_signature" | "wait" | "assign" | "add_value" | "none"
              | "analyze_anyway" | "reanalyze"
ObligationKind= "sponsor_report" | "milestone_payment" | "royalty_report" | "diligence_milestone"
              | "publication_review" | "term_end" | "closeout" | "other"
IncomeKind    = "upfront" | "milestone" | "royalty" | "equity" | "sublicense" | "sponsor_funding" | "subaward" | "other"
GovernRole    = "admin" | "reviewer" | "leader"
```

Stage ↔ state: intake/in_review/escalated → `review`; sent_back → `negotiation`;
ready_to_sign/out_for_signature → `approval`; signed → `signed`; active → `active`;
rejected → stage unchanged (hidden from the board by default); closed → `expired`.
A new contract whose analysis is still running is `draft` / `intake`.

## Objects

```
Person     { email: string, name: string | null }
WaitingOn  { kind: WaitingOnKind, label: string, office: Office | null, person: Person | null }
           # label is plain words: "Waiting on the customer reviewer (Dana Ruiz)", "Waiting on sponsor", "Waiting on Legal Affairs"

MatrixCounts { within, fallback, deviates, unacceptable, review, missing, beneficial: number }

Blocker {
  id, text, clauseType: string | null, office: Office | null,
  suggestedLanguage: string | null, source: "sonar" | "reviewer",
  status: "open" | "closed", createdAt, createdBy: Person | null, closedAt: string | null, closedBy: Person | null
}

Obligation {
  id, kind: ObligationKind, title, dueDate: string | null, amount: number | null,
  status: "open" | "done", source: "sonar" | "manual", completedAt: string | null
}

IncomeItem {
  id, kind: IncomeKind, description, amount: number | null, pct: number | null,
  expectedDate: string | null, source: "sonar" | "manual"
}

NextStep {
  action: NextAction,
  headline: string,           # one plain sentence: "Send back to the sponsor: 3 clauses need changes."
  detail: string | null,
  office: Office | null,
  clauses: { clauseType, label, tier: Tier, suggestedLanguage: string | null }[]
}

Contract {                           # list + detail
  contractId, currentDocId, versionDocIds: string[], tenantId,
  title, docType, agreementType: AgreementType, direction: Direction,
  counterparty: string | null, sponsor: string | null, piName: string | null,
  department: string | null, college: string | null,
  stage: Stage, state: State, waitingOn: WaitingOn,
  owner: Person | null,
  createdAt, updatedAt, stageEnteredAt, signedAt: string | null,
  daysInStage: number, totalDays: number,        # whole days, computed at read time
  targetDays: number | null, slaStatus: SlaStatus,
  rounds: number,
  analysisStatus: string,                        # documents-table status of currentDocId (READY, CLASSIFYING …)
  analysisError: string | null,                  # the document's errorCode when analysisStatus is FAILED ("not_agreement", …)
  value: number | null,                          # manualValue ?? extractedValue ?? expectedValue
  valueSource: "manual" | "extracted" | "expected" | null,
  extractedValue: number | null, manualValue: number | null, expectedValue: number | null,
  currency: string | null, fiscalYear: number | null, requestedDate: string | null,
  valueBucket: "current" | "potential" | "none",  # signed/active/renewal → current; draft..approval (not rejected) → potential
  matrix: { version: number | null, reviewedAt: string | null, counts: MatrixCounts } | null,
  overallRisk: "low" | "medium" | "high" | "critical" | null,
  openBlockers: number,
  nextStep: NextStep,
  huronRecordId: string | null, workdayRef: string | null,
  workdayMatch: "auto" | "manual" | "unmatched",
  syncConflicts: { field, govern, recordValue, system, at }[],
  routing: { required: Office[], approvals: { office: Office | "reviewer", by: Person, at }[], reasons: string[] },
  rejection: { reasonCode: RejectReason, note: string | null, at, by: Person | null } | null,
  signature: { provider: "docusign" | "manual", envelopeId: string | null, sentAt: string | null,
               signedAt: string | null, signatory: Person | null } | null,
  obligationsDue: number,                        # open obligations due within 30 days or overdue
  captureGaps: CaptureGap[],                     # fields Govern could not capture and nobody entered — never silently empty
  allowedActions: ActionName[],                  # actions valid in the current state for THIS caller (role-aware) — the UI shows only these
  effectiveDate: string | null, termEndDate: string | null,   # extracted, or set by a person (PATCH)
  rev: number
}

ActionName = "assign" | "approve" | "office_approve" | "send_back" | "escalate" | "reject" | "send_for_signature"
           | "mark_signed" | "activate" | "close" | "reopen" | "ask_pi" | "pi_answered" | "comment"

CaptureGap = "value" | "counterparty" | "sponsor" | "piName" | "department" | "requestedDate"
           | "huronRecordId" | "workdayRef" | "effectiveDate" | "termEnd" | "agreementTypeUnsure"
           # which apply depends on agreementType (e.g. sponsor/piName for sponsored_research & grant; huronRecordId always);
           # "agreementTypeUnsure" when the type was inferred as "other" and nobody confirmed it

ContractDetail = Contract + {
  blockers: Blocker[], obligations: Obligation[], licensingIncome: IncomeItem[],
  review: MatrixReview | null,
  activity: ActivityEntry[],              # newest first, up to 200
  versions: { docId, title, createdAt, status, round: number, matrixCounts: MatrixCounts | null }[]
}

MatrixClauseResult {
  clauseType, label, clauseNumber: string | null, clauseId: string | null,
  tier: Tier, beneficial: boolean, beneficialReason: string | null,
  reason: string | null, found: string | null,
  standard: string | null, fallback: string | null,
  escalationOffice: Office | null, suggestedLanguage: string | null,
  required: boolean,                      # the matrix requires this clause type
  escalationRequired: boolean,            # the matrix routes deviations of this clause to its office (escalateOnDeviation)
  quote: string | null                    # up to 600 chars of the clause text
}

MatrixReview {
  docId, agreementType, matrixVersion, matrixEffectiveDate, reviewedAt,
  counts: MatrixCounts, clauses: MatrixClauseResult[]
}

ActivityEntry {
  id, at, actor: Person | null,             # null = Sonar / system
  action: "intake" | "assigned" | "reassigned" | "approved" | "office_approved" | "sent_back" | "escalated"
        | "rejected" | "comment" | "stage_changed" | "blocker_added" | "blocker_closed" | "blocker_edited"
        | "blocker_reopened" | "rescored" | "signature_sent" | "signed" | "activated" | "closed" | "reopened"
        | "revision_received" | "field_updated" | "overdue" | "notification_sent" | "sync" | "conflict"
        | "obligation_added" | "obligation_done" | "pi_requested" | "pi_answered"
        | "automation_notify",               # an automation's alert (see Automations); detail.automation names the rule
  fromStage: Stage | null, toStage: Stage | null,
  summary: string,                          # plain sentence for the log
  detail: object | null
  # field_updated detail: { fields: string[], changes: { [field]: { from: any, to: any } } }
  # send_back detail: { clauses: {clauseType, label, suggestedLanguage}[], note }
  # rescored / intake detail: { matrixVersion, counts: MatrixCounts, deviatingClauseTypes: string[] }
}
```

## Routes

### Contracts
| Method | Path | Role | Body / query | Returns |
|---|---|---|---|---|
| GET | `/contracts` | view | `?includeClosed=true` | `{contracts: Contract[], count, generatedAt}` |
| POST | `/contracts` | edit (on the document) | `{docId, ...any PATCH field}` — intake right after upload: creates the contract at stage `draft`, state `intake` (analysisStatus = the document's status), auto-assigns; idempotent (an existing contract is patched instead). The intake Lambda keeps these fields (user-entered intake fields are never overwritten by inference). | `{contract: ContractDetail}` (201 when created) |
| GET | `/contracts/{id}` | view | | `{contract: ContractDetail}` — `activity` holds the newest 20 entries (plus the latest `sent_back`, when older); `activityCount` is exact up to 200, `activityHasMore` says the log is longer, `activityCursor` continues after the shown entries (null when there are no older ones) |
| GET | `/contracts/{id}/activity` | view | `?before=<cursor>&limit=1-100` (default 50) | `{entries: ActivityEntry[], nextCursor: string \| null}` — newest first, strictly older than `before`; one key-condition query on the contract's log |
| PATCH | `/contracts/{id}` | edit | any of `agreementType, direction, counterparty, sponsor, piName, department, college, expectedValue, manualValue, currency, requestedDate, effectiveDate, termEndDate, huronRecordId, workdayRef, workdayMatch` | `{contract: ContractDetail}` |
| DELETE | `/contracts/{id}` | delete_document (the document owner) | | `{deleted: true, contractId, docIds}` — removes the contract with its blockers, obligations, income and reviews, and takes its counts back out of the trends (the activity log keeps its entries). The app then deletes each of `docIds` with `DELETE /documents/{id}`. |
| POST | `/contracts/{id}/actions` | see below | `{action, ...}` | `{contract: ContractDetail}` |
| POST | `/contracts/{id}/rescore` | edit | `{}` | `{contract: ContractDetail}` — re-grades `currentDocId` against the CURRENT matrix version, regenerates Sonar blockers (reviewer blockers untouched) |
| POST | `/contracts/{id}/blockers` | edit | `{text, clauseType?, office?, suggestedLanguage?}` | `{contract}` |
| PATCH | `/contracts/{id}/blockers/{blockerId}` | edit | `{text?, status?, office?, suggestedLanguage?}` | `{contract}` |
| POST | `/contracts/{id}/obligations` | edit | `{kind, title, dueDate?, amount?}` | `{contract}` |
| PATCH | `/contracts/{id}/obligations/{oblId}` | edit | `{status?, dueDate?, title?, amount?}` | `{contract}` |
| PUT | `/contracts/{id}/income` | edit | `{items: IncomeItem[]}` (ids optional for new) | `{contract}` |
| GET | `/contracts/{id}/revision-upload-url` | edit | `?filename=` | `{uploadUrl, docId}` — new document linked to the contract; intake rescoring it moves the contract back to `in_review`, `rounds` unchanged |
| POST | `/contracts/bulk` | edit, per contract (leaders read-only, as PATCH) | `{contractIds: string[] (1–200, unique), fields?: {...any PATCH field}, owner?: Person}` — at least one of `fields` / `owner`. The body (including `fields`, with PATCH's validation) is checked once up front: a bad one is 400 and nothing changes. Each contract then gets the PATCH (`fields`) and the `assign` action (`owner`; checked before the fields are written); a failure is reported per contract and the rest continue. | `{results: ({contractId, ok: true} \| {contractId, ok: false, error: {code, message}})[], updated: number}` — `code` is `not_found` for a contract the caller cannot see (never leaks), `forbidden`, `invalid_transition` (e.g. assigning a closed contract), `bad_request`, `conflict` |

Actions (`POST /contracts/{id}/actions`), all write an activity entry; invalid ones answer 409 `invalid_transition`:

| action | body | effect |
|---|---|---|
| `assign` | `{owner: Person}` | owner set; waiting on that reviewer |
| `approve` | `{note?}` | records the reviewer approval; if routing still requires offices → `escalated`, waiting on the first pending office; else `ready_to_sign`, waiting on signatory |
| `office_approve` | `{office, note?}` | records that office's approval; then as `approve` |
| `send_back` | `{clauses: {clauseType, label, suggestedLanguage}[], note?}` | `sent_back`, stage negotiation, waiting on counterparty, `rounds += 1` |
| `escalate` | `{office, note?}` | `escalated`, waiting on that office; adds the office to `routing.required` |
| `reject` | `{reasonCode, note?}` | `rejected`, waiting on nobody |
| `send_for_signature` | `{provider: "docusign" | "manual", signatory?: Person}` | `out_for_signature` (only from `ready_to_sign`) |
| `mark_signed` | `{signedAt?}` | `signed`; value moves to current; obligations extracted |
| `activate` | `{}` | `active` |
| `close` | `{note?}` | `closed` (term ended / close-out done) |
| `reopen` | `{note?}` | back to `in_review` |
| `ask_pi` | `{request, note?}` | state unchanged; sets `piRequest {request, at, by}`; waiting on `pi_department` until answered (from `intake`, `in_review`, `escalated`; offered only while no request is open) |
| `pi_answered` | `{note?}` | clears `piRequest`; waiting on returns to the reviewer or office. Approve, send back, escalate, reject, send for signature and reopen also clear it |
| `comment` | `{text}` | comment only (leaders may comment) |

Recommended next step (server-computed; first rule that matches):
1. rejected/closed → `none`; signed → `none` (headline about obligations); active → `none`.
2. analysis FAILED → `analyze_anyway` when the document did not look like an agreement (`not_agreement`), else
   `reanalyze`; the app offers the analysis (POST /documents/{id}/reprocess, `force` for `analyze_anyway`) or removal.
   Analysis not READY otherwise → `wait` ("Sonar is still reading this agreement").
3. no owner → `assign`.
3b. an open `piRequest` → `wait` ("Waiting on {PI or department} to answer").
4. any open blocker tied to an `unacceptable` clause with no fallback → `reject` (or `escalate` to its office if the matrix names one and it has not approved).
5. open blockers needing an office that has not approved → `escalate` (office).
6. open blockers → `send_back` with the clauses and suggested redlines.
7. state `ready_to_sign` → `send_for_signature`; `out_for_signature` → `wait` ("Waiting on signature").
8. value unknown → `add_value` (prompt; still lets approve).
9. otherwise → `approve`.

### Matrix (Requirement 1) — admin for writes
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/matrix` | | `{current: Matrix, versions: MatrixVersionInfo[]}` |
| GET | `/matrix/versions/{n}` | | `{matrix: Matrix}` |
| PUT | `/matrix` | `{playbooks, note?, effectiveDate?, homeState?}` | `{matrix}` — saves a NEW version; `homeState` left out keeps the current one, `null` clears it |
| POST | `/matrix/import` | `{agreementType, rows: ImportRow[], mode: "replace" | "merge", note?}` or `{agreementType, csv: string, mode}` | `{matrix, imported: number, skipped: {row: number, reason}[]}` |

```
Matrix { version, effectiveDate, createdAt, createdBy: Person | null, note: string | null,
         homeState: string | null,   # the institution's home state; the governing-law check needs it
         playbooks: { [AgreementType]: { agreementType, label, clauses: MatrixClause[] } } }
MatrixClause { clauseType, label, standard, fallback: string | null, unacceptable: string[],
               beneficial: string[], escalationOffice: Office | null, suggestedLanguage: string | null,
               thresholds: { [name]: number }, required: boolean }
MatrixVersionInfo { version, effectiveDate, createdAt, createdBy, note, clauseCount }
ImportRow { clauseType, standard, fallback?, unacceptable?, escalationOffice?, beneficial?, suggestedLanguage? }
           # skipped[].row is the 1-based index into the `rows` array sent (CSV: 1-based data row, header excluded)
           # clauseType accepts a category id or its label; unacceptable/beneficial accept ";"-separated text;
           # escalationOffice accepts an id or its label ("Legal Affairs")
```
Clause types (category ids): the existing 12 commercial types plus
`PublicationRights, BackgroundIP, LicenseScope, Royalties, Indemnity, GoverningLaw,
ExportControl, DataRights, SponsorReporting, Diligence` (`LicenseScope`, `Royalties`,
`Indemnity`, `GoverningLaw` already exist as categories; the other six are new).

### Workflow settings — admin for writes
| GET/PUT | `/workflow/settings` | → `{settings: WorkflowSettings}` |
```
WorkflowSettings {
  stageTargetDays: { [Stage]: number | null },  # defaults draft 2, review 5, negotiation 10, approval 3
  redAfterMultiple: number,                      # red when daysInStage > target × this (default 2)
  reviewers: { email, name, offices: Office[], agreementTypes: AgreementType[] }[],
  assignmentRules: { id, agreementType: AgreementType | "*", department: string | "*", reviewer: Person }[],
  routingRules: { id, name, enabled, when: { agreementTypes?: AgreementType[], minValue?: number,
                  anyUnacceptable?: boolean, minRisk?: "high" | "critical", direction?: Direction },
                  route: Office[] }[],           # default: any unacceptable OR value ≥ 500000 → legal_affairs
  # Rule semantics: agreementTypes and direction NARROW (AND) which contracts a rule looks at;
  # minValue, anyUnacceptable and minRisk are TRIGGERS — any one that holds fires the rule (OR).
  # A rule with no triggers fires for every contract it narrows to.
  notifications: { email: boolean, teams: boolean,
                   events: { assigned, sent_back, approved, overdue, escalated: boolean } },
  teamsWebhookConfigured: boolean                # write-only secret: PUT {teamsWebhookUrl} stores it in Secrets Manager
}
```

### Integrations (Requirement 6) — admin
| Method | Path | Returns |
|---|---|---|
| GET | `/integrations` | `{connectors: Connector[]}` |
| PUT | `/integrations/{id}` | `{enabled?, config?, fieldMapping?, credentials?}` → `{connector}` (credentials go to Secrets Manager, never returned) |
| POST | `/integrations/{id}/sync` | `{run: SyncRun}` — runs now; with no credentials it is a logged dry run; 409 `feature_disabled` while `integrations` is off |
| GET | `/integrations/sync-log` | `{runs: SyncRun[]}` newest first, 100 |
| GET | `/integrations/unmatched` | `{contracts: Contract[]}` workdayMatch = unmatched (manual match screen) |
```
Connector { id: "huron" | "workday" | "m365" | "docusign", name, enabled, status: "not_connected" | "connected" | "error",
            direction: "in" | "out" | "both", lastSyncAt, lastError, credentialsConfigured: boolean,
            config: { [k]: string }, fieldMapping: { [governField]: string }, ownsFields: string[] }
SyncRun { id, connectorId, startedAt, finishedAt, trigger: "manual" | "schedule" | "event",
          dryRun: boolean, recordsIn, recordsOut, errors: { record, message }[], status: "ok" | "partial" | "failed" | "dry_run" }
```

### Trends (write-time aggregates — "visibility into data trends across all areas")
| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/reports/trends` | `?granularity=month|week&periods=12` (max 24 months / 26 weeks) | `Trends` |
| GET | `/reports/capture` | | `{gaps: {gap: CaptureGap, count, contractIds: string[]}[], missedDocuments: {docId, title, status, reason}[], lastReconciledAt}` — documents READY/FAILED with no contract, and failed analyses, so capture never misses anything silently |

```
Trends {
  granularity, generatedAt,
  periods: [{
    period: "2026-09" | "2026-W38", start, end,
    received, signed, rejected, sentBack, escalated, overdueEvents, revisions,
    signedValue: { [currency]: number }, receivedValue: { [currency]: number },
    avgCycleDays: number | null,                  # intake → signed, for contracts signed in the period
    avgDaysByStage: { [Stage]: number | null },   # time spent in a stage, for stage exits in the period
    avgRounds: number | null,                     # rounds of contracts signed in the period
    onTimePct: number | null                      # stage exits within target / all stage exits
  }],
  byAgreementType: { [AgreementType]: { received, signed, avgCycleDays: number | null } },   # over the whole window
  clauseDeviations: [{ clauseType, label, total, byPeriod: { [period]: number } }],         # deviates+unacceptable at intake/rescore, top 12
  officeLoad: [{ office, escalations, avgDaysToApprove: number | null }]
}
```
Aggregates are maintained by the activity-stream Lambda with atomic `ADD` on `govern-metrics` items `T#<tenant> / D#<yyyy-mm-dd>`
(counters, value sums per currency, duration sums + counts per stage, clause-type deviation counters, office counters);
the API rolls days up into weeks/months. A one-off backfill (`scripts/backfill_govern_aggregates.py`) rebuilds them from
the activity table. Reading a year is ≤ 366 GetItems in batches — never a scan of contracts or activity.

Connector credential keys (PUT `credentials`, stored in Secrets Manager, never returned):
`huron {clientId, clientSecret}` · `workday {clientId, clientSecret, refreshToken}` ·
`m365 {clientId, clientSecret}` · `docusign {integrationKey, userId, privateKey, connectHmacKey}`
(`connectHmacKey` is the key `/webhooks/docusign` verifies with).

### Me
| GET | `/govern/me` | `{email, name, role: GovernRole, tenantId, isAdmin, isCreator, isLeader, features: GovernFeatures, …}` — `isAdmin` / `isCreator` are for THIS organization (tenant). `role` is `admin` when `isAdmin`, else from the Cognito groups (`govern-leader` → leader, otherwise reviewer). The first call in a tenant with no organization record creates it with the caller as creator and admin (conditional put: exactly one creator), and every call records the caller as a member of the tenant. |
| GET | `/govern/people` | `{people: {email, name: string \| null, sources: ("project" \| "reviewer" \| "owner")[]}[]}` — people the caller can assign or share with: owners and members of the caller's projects (`project`), `settings.reviewers` (`reviewer`), owners of contracts the caller can see (`owner`), and the caller (whose `sources` may be empty). One entry per lower-cased email, sorted by name (unnamed last) then email, at most 500. |

### Organization and admins — any member reads, admins write
"Admin" in this document means an admin of the caller's organization (tenant): its creator plus the admins
they added. Cognito groups never grant admin. A personal workspace (`u-<sub>`) has exactly one admin, its user.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/govern/org` | — | `Organization` |
| POST | `/govern/org/admins` | `{email, rev?}` (admin) | `Organization` — the person must have signed in to Govern in this tenant (a `MEMBER` row), else 404 `not_found` "No one with that email has signed in to this organization yet."; already an admin → unchanged. 400 in a personal workspace. |
| DELETE | `/govern/org/admins/{sub}` | `?rev=` (admin) | `Organization` — 404 if `{sub}` is not an admin of this tenant; 409 `conflict` for the creator or the last admin. |

```
Organization {
  tenantId, name: string | null (settings.organization.name), personal: boolean,
  createdBy: {sub, email, name} | null, createdAt, rev: number,
  admins: {sub, email, name, addedAt, isCreator, addedBy: {email, name} | null}[]   # creator first
  you: {isAdmin, isCreator}
  # admins only:
  members?: {sub, email, name}[]                                 # people who signed in here (the picker)
  activity?: {action: "org_created" | "admin_added" | "admin_removed", at, actor: {email, name}, target: {email, name}}[]  # newest 20
}
```
`rev` is optional on writes; when sent and stale the answer is 409 `conflict` (reload and retry). Non-admins get 403 `forbidden`
on the writes. Admin changes are logged (`govern_org.admin_added` / `govern_org.admin_removed`) and kept in `activity`.

### Automations — any Govern user reads, admins write
Rules that run on their own: **when** a trigger happens, **only if** every condition holds, **then** the actions run.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/automations` | | `{automations: Automation[], count, limit: 50, enabled}` — oldest first; `enabled` = the `automations` feature is on for this deployment and organization |
| POST | `/automations` | `{name, enabled?, trigger, conditions?, actions}` | 201 `{automation}`; 400 `limit` past 50 rules per organization; 409 `feature_disabled` while off |
| PATCH | `/automations/{id}` | any of `{name, enabled, trigger, conditions, actions}` + optional `rev` | `{automation}`; the merged rule is validated whole; a stale `rev` → 409 `conflict`; 409 `feature_disabled` while off |
| DELETE | `/automations/{id}` | | `{deleted: true, id}` |
| POST | `/automations/{id}/test` | `{contractId, rule?}` | `{result: AutomationTest}` — a dry run on a contract the caller can view (404 otherwise): nothing is written. `rule` tests unsaved edits; `{id}` = `new` tests a rule that is not saved (then `rule` is required) |

```
Automation {
  id,                                   # 32 hex characters
  name,                                 # 1–120 characters
  enabled: boolean,
  trigger: AutomationTrigger,
  conditions: AutomationCondition[],    # ≤ 10, all must hold
  actions: AutomationAction[],          # 1–5, run in order
  createdBy: Person, createdAt, updatedBy: Person, updatedAt,
  lastRunAt: string | null, runCount: number, rev: number
}
AutomationTrigger =                     # time-based: evaluated by govern-sweeper every hour
    { type: "in_stage_for", stage: Stage, days: 1–3650 }   # open in that stage for ≥ days (not rejected / closed)
  | { type: "obligation_due_in", days: 0–3650 }           # an open obligation due within days (needs `obligations`)
  | { type: "term_ends_in", days: 0–3650 }                # a signed / active agreement whose term ends within days
                                        # event-based: evaluated by govern-stream on every new activity entry
  | { type: "contract_received" }                          # the intake entry
  | { type: "stage_changed", to?: Stage | null }           # any entry that moves the stage (to that stage)
  | { type: "sent_back" }
  | { type: "blocker_added", tier?: "unacceptable" | "deviates" | null }
                                        # no tier: any new open item (a reviewer's or Sonar's);
                                        # tier: Sonar found new items and the review has that tier ("deviates" = deviates or worse)
  | { type: "signed" }
AutomationCondition =
    { field: "agreementType", op: "in", value: AgreementType[] }
  | { field: "direction", op: "is", value: Direction }
  | { field: "value", op: "gte" | "lte", value: number }   # the contract value (manual, else extracted, else expected); unknown never matches
  | { field: "department", op: "is", value: string }       # case and spacing ignored
  | { field: "owner", op: "set" | "unset" }
  | { field: "counterparty", op: "contains", value: string }  # counterparty or sponsor, case ignored
AutomationAction =
    { type: "notify", to: "owner" | "office" | "emails", office?: Office, emails?: string[], message?: string }
        # office: the reviewers of that office (settings.reviewers); emails: 1–20 addresses; message ≤ 500
  | { type: "escalate", office: Office }                   # the escalate action; skipped when already waiting on it
  | { type: "assign", owner?: Person }                      # no owner: the assignment rules pick one (auto_assignee)
  | { type: "comment", text: string }                       # ≤ 2000
# message / text placeholders: {contract} {stage} {days} {due} {owner} {counterparty} {rule}; any other {…} → 400
AutomationTest {
  ruleId: string | null, contractId, contractTitle, wouldFire: boolean, enabled: boolean, featureEnabled: boolean,
  alreadyRan: boolean,                  # this episode / entry has already fired
  trigger: { matched: boolean, detail: string },   # event triggers look for the newest matching entry in recent activity
  conditions: (AutomationCondition & { matched: boolean, actual: any })[],
  actions: { type, summary: string, wouldRun: boolean, reason: string | null, recipients?: string[] }[]
}
```
Validation (400 `bad_request`): unknown keys anywhere (rule, trigger, condition, action, person, test body),
unknown trigger / field / operator / action types, keys that do not belong to the chosen type, out-of-range days.

How rules run. Every automated change goes through `shared/govern/workflow.py` as the system actor
**"Automation: <rule name>"** and its activity entry carries `detail.automation = {ruleId, ruleName}`; entries so
tagged never trigger a rule, so automations neither loop nor chain. `notify` writes an `automation_notify` entry
(recipients, subject, message) that the notifier delivers by SES / Teams under the channel settings (the rule is its
switch; the `notifications` feature must be on) and then logs `notification_sent`. A time rule fires once per
contract per qualifying episode (key: stage entry `<contractId>#<stageEnteredAt>`, term `#term#<termEndDate>`,
obligation `#obl#<id>#<dueDate>`); an event rule once per activity entry. Each action claims a govern-metrics
marker (`AUTO#<tenant> / SEEN#<ruleId>#<key>#<n>`) before it runs, so a retry never repeats an action; an action
the contract's state does not allow is skipped. `runCount` / `lastRunAt` count completed firings. Stored in
govern-config `T#<tenant>#AUTOMATIONS / A#<id>`.

### Saved views (list views for `contracts` and `obligations`) — any Govern user, leaders included
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/views` | `?module=contracts\|obligations` (required) | `{views: View[]}` — the caller's own views (any visibility), `everyone` views, and `specific` views whose `sharedWith` holds the caller's verified email; own first, then by name |
| POST | `/views` | `{module, name, visibility, sharedWith?, config}` | 201 `{view}`; 400 `limit` past 100 views per creator per module |
| PATCH | `/views/{id}` | any of `{name, visibility, sharedWith, config}` | `{view}` — creator only: 403 `forbidden` if the caller can see the view but did not create it, 404 if they cannot see it |
| DELETE | `/views/{id}` | | `{deleted: true, id}` — same creator rule |

```
View {
  id,                                   # 32 hex characters
  module: "contracts" | "obligations",
  name,                                 # 1–80 characters, trimmed
  visibility: "private" | "everyone" | "specific",
  sharedWith: string[],                 # lower-cased emails, max 50; required (non-empty) for "specific", stored empty otherwise
  createdBy: Person, createdAt, updatedAt,
  canEdit: boolean,                     # the caller created it
  config: ViewConfig
}
ViewConfig {                            # every key optional on write (defaults shown); always complete on read
  base: string | null,                  # null
  filters: { match: "all" | "any", rules: { field: string, op: string, value: any }[] },   # {match: "all", rules: []}
  columns: { order: string[], hidden: string[] },                                          # {order: [], hidden: []}
  groupBy: string | null,               # null
  sort: { key: string, dir: "asc" | "desc" } | null,                                       # null
  density: "comfortable" | "compact" | null                                                # null
}
```
Validation (400 `bad_request`): unknown keys anywhere; `base`, `groupBy`, `field`, `op`, `sort.key` and column names
≤ 64 of `A-Z a-z 0-9 _ . : -`; ≤ 25 rules; ≤ 40 entries in each of `order` / `hidden`; a rule `value` is a string
(≤ 200), finite number, boolean, null, or a list of ≤ 50 of those; the whole `config` ≤ 16 KB as JSON.
Stored in govern-config `T#<tenant>#VIEWS / V#<module>#<id>`.

```
GovernFeatures { routingRules: boolean, docusign: boolean, notifications: boolean,
                 obligations: boolean, exports: boolean, integrations: boolean, automations: boolean }
```

#### "Later" features (`GOVERN_FEATURES`)
Built but not part of this release. A deployment switches them on with the
`GOVERN_FEATURES` environment variable on the Govern Lambdas (Terraform
`govern_features`), a comma list of `routing_rules`, `docusign`,
`notifications`, `obligations`, `exports`, `integrations`, `automations` (camelCase accepted).
Empty (the default) = all off. `features` reports the result; the web app
prefers it over its own build-time list (`NEXT_PUBLIC_GOVERN_FEATURES`) —
when the server says off, the feature is off. While a feature is off:

| Feature | Behaviour while off |
|---|---|
| `routingRules` | No office is required by a routing rule (`routing.required` holds only offices escalated to by hand); `approve` goes straight to `ready_to_sign`. Settings may still be saved. |
| `docusign` | `send_for_signature` with `provider: "docusign"` → 409 `feature_disabled` (manual only). `POST /webhooks/docusign` → 200 `{"status": "disabled"}` and acts on nothing. |
| `notifications` | The notifier sends nothing (logs `feature_disabled`) and claims nothing. |
| `obligations` | Obligations may still be stored, but `obligationsDue` is always 0, the next step does not mention them, and the sweeper writes no due / overdue entries. |
| `exports` | Client-side only (Excel / PDF downloads are shown as "Coming soon"). |
| `integrations` | Scheduled and event connector runs are skipped (logged); `POST /integrations/{id}/sync` → 409 `feature_disabled`. |
| `automations` | No rule runs (sweeper or stream); `POST` / `PATCH /automations` → 409 `feature_disabled`; `GET` still lists rules (`enabled: false`), `DELETE` and the dry run still work. |

### Real-time events (AppSync Events WebSocket, Cognito ID token)

Not a REST route: open pages subscribe to their tenant's channel on the Govern
AppSync Events API (`terraform output govern_realtime_domains`) and re-read
through the routes above when a message arrives (GOVERN_ARCHITECTURE.md §2.3.1).

```
Channel:  /govern/<encoded tenantId>
          tenantId = custom:tenantId claim, else "u-<sub>"; "-" → "-0", "_" → "-1", "." → "-2";
          split into 50-character segments (e.g. "u-1b2c" → /govern/u-01b2c)

ContractChanged {
  type: "contract.changed",
  contractId: string,
  action: string,          // the activity action (approve, comment, assign, rescored, …)
  actor: string | null,    // display name only; "Sonar" for system entries; null when only an email is known
  at: string               // the entry's ISO time
}
```

One message per activity entry; nothing else is ever sent (no contents, values,
comments, details or emails). Subscribing to another tenant's channel, or to a
wildcard, is refused (`subscribe_error`). Browsers cannot publish (IAM only).
Delivery is best effort: clients keep a slow polling safety net.

### Webhooks (no JWT)
| POST | `/webhooks/docusign` | DocuSign Connect JSON; header `X-DocuSign-Signature-1` HMAC-SHA256 (base64) of the raw body with the key in Secrets Manager. `envelope-completed` with custom field `contractId` → `mark_signed`. |

### Counterparty portal

A reviewer shares one contract's open asks with the other side, who reads them
and uploads a revised version without an account (`shared/govern/portal.py`).

```
PortalLink { id, status: "active" | "expired" | "revoked", createdAt, createdBy: Person | null,
             expiresAt, allowUpload: boolean, note: string | null, opens: number,
             lastOpenedAt: string | null, uploads: number, revokedAt: string | null }   # never the token
PortalView { contractTitle, organizationName: string | null, needs: PortalNeed[],
             requestedDate: "YYYY-MM-DD" | null, note: string | null, allowUpload: boolean, expiresAt }
PortalNeed { clauseLabel, tier: "Required change" | "Change requested" | "Missing clause" | "Please confirm" | "Requested",
             title, suggestedLanguage: string | null }
```

Reviewer routes (JWT, **edit** on the contract; viewers and leaders → 403, no access → 404):

| Method | Path | Body → Response |
|---|---|---|
| POST | `/contracts/{id}/portal-links` | `{expiresInDays?: 1-60 (14), allowUpload?: boolean (true), note?: string ≤ 1000}` → 201 `{id, url, expiresAt, link: PortalLink}`. `url` (`APP_BASE_URL/portal/<token>`) is returned **once**. 409 `limit_reached` at 5 active links; 409 `invalid_transition` on a rejected / closed contract. Activity `portal_link_created`. |
| GET | `/contracts/{id}/portal-links` | → `{links: PortalLink[] (newest first, ≤ 50), activeCount, maxActive: 5}` |
| DELETE | `/contracts/{id}/portal-links/{linkId}` | → `{link: PortalLink}` (idempotent). Activity `portal_link_revoked`. |

Public routes (no JWT; govern-portal Lambda; responses carry `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`):

| Method | Path | Body → Response |
|---|---|---|
| GET | `/portal/{token}` | → `PortalView` (exactly these keys). Counts a view; the first view of a UTC day writes activity `portal_opened` ("Counterparty opened the link."). 429 `rate_limited` after 500 views of one link. |
| POST | `/portal/{token}/upload-url` | `{filename: *.pdf | *.docx, size: bytes ≤ 25 MB}` → `{uploadUrl, headers: {"Content-Type"}, expiresIn: 300, maxBytes}`. PUT the file to `uploadUrl` with exactly those headers (type and size are signed). The same revision path as `GET /contracts/{id}/revision-upload-url`: a PENDING document with `revisionOf`, `uploadedVia: "portal"`, filed as the link's creator; intake makes it the next round. Activity `portal_upload` (actor "Counterparty via portal link"). 403 `upload_not_allowed` when the link does not allow uploads or the contract is signed / closed; 400 on type / size; 429 after 20 upload URLs per link. |

An unknown, malformed, expired or revoked token — or one whose contract is gone —
always gets the same 404 `{"error": "This link has expired or isn't valid. Ask your contact for a new one.", "code": "not_found"}`.
