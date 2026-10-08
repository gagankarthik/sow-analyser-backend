# Govern API contract (v1)

The single source of truth shared by `govern-api` (Python) and the Next.js client
(`sow-analyzer/lib/govern/*`). All routes need the Cognito JWT except `/webhooks/*`.
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
        | "obligation_added" | "obligation_done" | "pi_requested" | "pi_answered",
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
| GET | `/contracts/{id}` | view | | `{contract: ContractDetail}` |
| PATCH | `/contracts/{id}` | edit | any of `agreementType, direction, counterparty, sponsor, piName, department, college, expectedValue, manualValue, currency, requestedDate, effectiveDate, termEndDate, huronRecordId, workdayRef, workdayMatch` | `{contract: ContractDetail}` |
| POST | `/contracts/{id}/actions` | see below | `{action, ...}` | `{contract: ContractDetail}` |
| POST | `/contracts/{id}/rescore` | edit | `{}` | `{contract: ContractDetail}` — re-grades `currentDocId` against the CURRENT matrix version, regenerates Sonar blockers (reviewer blockers untouched) |
| POST | `/contracts/{id}/blockers` | edit | `{text, clauseType?, office?, suggestedLanguage?}` | `{contract}` |
| PATCH | `/contracts/{id}/blockers/{blockerId}` | edit | `{text?, status?, office?, suggestedLanguage?}` | `{contract}` |
| POST | `/contracts/{id}/obligations` | edit | `{kind, title, dueDate?, amount?}` | `{contract}` |
| PATCH | `/contracts/{id}/obligations/{oblId}` | edit | `{status?, dueDate?, title?, amount?}` | `{contract}` |
| PUT | `/contracts/{id}/income` | edit | `{items: IncomeItem[]}` (ids optional for new) | `{contract}` |
| GET | `/contracts/{id}/revision-upload-url` | edit | `?filename=` | `{uploadUrl, docId}` — new document linked to the contract; intake rescoring it moves the contract back to `in_review`, `rounds` unchanged |

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
2. analysis not READY → `wait` ("Sonar is still reading this agreement").
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
| GET | `/govern/me` | `{email, name, role: GovernRole, tenantId, features: GovernFeatures}` |

```
GovernFeatures { routingRules: boolean, docusign: boolean, notifications: boolean,
                 obligations: boolean, exports: boolean, integrations: boolean }
```

#### "Later" features (`GOVERN_FEATURES`)
Built but not part of this release. A deployment switches them on with the
`GOVERN_FEATURES` environment variable on the Govern Lambdas (Terraform
`govern_features`), a comma list of `routing_rules`, `docusign`,
`notifications`, `obligations`, `exports`, `integrations` (camelCase accepted).
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

### Webhooks (no JWT)
| POST | `/webhooks/docusign` | DocuSign Connect JSON; header `X-DocuSign-Signature-1` HMAC-SHA256 (base64) of the raw body with the key in Secrets Manager. `envelope-completed` with custom field `contractId` → `mark_signed`. |
