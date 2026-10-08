# Blue-IQ — SOW Analyser Backend

Enterprise contract-intelligence backend that ingests Statements of Work, Master Service Agreements, and Amendments; runs them through a 7-stage AI pipeline; and serves versions, diffs, lineage, and semantic search to the Next.js frontend.

## Architecture at a glance

```
                        ┌────────────────────────┐
   Next.js app  ──┐     │  S3  · raw uploads     │  ──► EventBridge ──┐
   (presigned    │ ──►  │      · processed JSON  │                    │
    upload)      │     │      · diff snapshots  │                    ▼
                 │     │      · audit blobs     │           Step Functions
                 │     └────────────────────────┘            (7 stages)
                 │                                                 │
                 ▼                                                 ▼
          DynamoDB (single-table)                           Per-stage Lambdas
          · Documents (PK=DOC#<id>)                        ┌────────────────┐
          · Versions  (PK=DOC#<id>, SK=V#<n>)              │ 1. Parse        │ pdfplumber+
          · Changes   (PK=DOC#<id>, SK=CHG#<id>)           │                 │ Textract+docx
          · Lineage   (PK=DOC#<id>, SK=LINK#<parent>)      ├────────────────┤
                                                          │ 2. Classify     │ OpenAI structured
                                                          ├────────────────┤
                                                          │ 3. Embed        │ OpenAI embeddings
                                                          ├────────────────┤
                                                          │ 4. Graph build  │ adjacency + edges
                                                          ├────────────────┤
                                                          │ 5. Diff         │ field-level
                                                          ├────────────────┤
                                                          │ 6. Timeline     │ replay amendments
                                                          ├────────────────┤
                                                          │ 7. Persist      │ DynamoDB writes
                                                          └────────────────┘
                                                                 │
                  ┌──────────────────────────────────────────────┤
                  ▼                                              ▼
          OpenSearch                                      AppSync GraphQL
          · clause-vectors (k-NN)                         · queries (versions, diffs, search)
          · clause-text  (BM25)                           · subscriptions (status updates)
                                                          · RAG resolver (streaming via SSE)
```

## Key design choices

1. **Single-table DynamoDB** with composite keys instead of separate tables. Cheaper, faster, supports adjacency lists for parent/child amendment chains.
2. **OpenSearch managed cluster** for both k-NN vector and BM25 full-text. One service, two indices.
3. **Adjacency-list lineage in DynamoDB** instead of Neptune for v1 — saves $400+/mo. Can promote to Neptune later if graph queries get complex.
4. **Standard Step Functions** for the ingest pipeline. (Was Express, but Express kills an execution at 5 minutes while a single stage may run up to 10 — the document then stayed on "processing" forever. Bulky stage output is parked in S3 between stages, since state is capped at 256 KB.)
5. **Server-Sent Events for RAG streaming** through AppSync HTTP resolvers (or Lambda Function URLs) rather than WebSockets.
6. **Python 3.12 Lambdas** with layers for shared deps (boto3, pdfplumber, openai). Pinned versions in `requirements.txt`.
7. **Presigned S3 uploads** — frontend uploads direct to S3, never proxies through Lambda. EventBridge triggers pipeline on `ObjectCreated`.

See `docs/ARCHITECTURE.md` for the full analysis, alternatives, and open questions.

## Govern: contract workflow (OSU)

Govern moves a contract between people: matrix review, owner, days in stage, waiting on,
approve / send back / escalate / reject, signature, obligations, value and trends. It is its own
bounded context on top of the pipeline (`docs/GOVERN_ARCHITECTURE.md`; REST contract
`docs/GOVERN_API.md`; operations `docs/GOVERN_RUNBOOK.md`).

```
Step Functions ── Persist ──► PutEvents "Document Analysed" ──► platform bus
platform bus ─► SQS ─► govern-intake     (contract, matrix review, revisions, auto-assign)
             ─► SQS ─► govern-notifier   (SES email, Teams card)
             ─► SQS ─► govern-connectors (Huron push-back; daily Huron / Workday pulls)
govern-activity stream ─► govern-stream  (Govern.* events + trend aggregates)
Scheduler hourly ─► govern-sweeper       (SLA overdue, obligations due, capture reconciliation)
API Gateway ─► govern-api  (JWT)  ·  POST /webhooks/{provider} ─► govern-webhooks (HMAC, no JWT)

DynamoDB: govern-contracts · govern-activity (append-only, stream) · govern-config ·
          govern-sync · govern-metrics       (KMS CMK; PITR on contracts / activity / config)
```

Every state change goes through `lambdas/shared/govern/workflow.py` (one module for the API, intake,
webhooks and the sweeper); storage is one repository per table in `shared/govern/store.py`; the matrix
grading is deterministic (`shared/govern/matrix.py`, no model call).

| Routes (all JWT except webhooks) | |
|---|---|
| `GET /govern/me` | caller's Govern role |
| `GET·POST /contracts`, `GET·PATCH /contracts/{id}` | board, intake at upload, detail, fields |
| `POST /contracts/{id}/actions` · `/rescore` · `/blockers` · `/obligations`; `PATCH …/blockers/{id}` · `…/obligations/{id}`; `PUT …/income`; `GET …/revision-upload-url` | workflow |
| `GET·PUT /matrix`, `POST /matrix/import`, `GET /matrix/versions/{n}` | review matrix (admin writes) |
| `GET·PUT /workflow/settings` | SLA targets, reviewers, assignment and routing rules, alerts (admin writes) |
| `GET /integrations`, `PUT /integrations/{id}`, `POST /integrations/{id}/sync`, `GET /integrations/sync-log`, `GET /integrations/unmatched` | connectors (admin) |
| `GET /reports/trends`, `GET /reports/capture` | write-time trend aggregates; capture gaps and missed documents |
| `POST /webhooks/docusign` | DocuSign Connect (HMAC-SHA256, 5-minute replay window) |

## Layout

```
terraform/            All infrastructure (API Gateway, Lambdas, Step Functions, DynamoDB, S3, IAM)
lambdas/
  api/                HTTP API handler (documents, projects, team, playbook)
  govern_*/           Govern Lambdas: api, intake, stream, notifier, sweeper, connectors, webhooks
  rag/                Sonar chat (retrieval + answer, access-filtered)
  pipeline/           Step Functions entry point and the stages:
    stages/           parse, classify, embed, graph, diff, timeline, persist
  shared/             Access control, segmentation, dates, money, playbook, clients
    govern/           Govern: workflow, store (5 repositories), matrix, connectors, aggregates, capture
samples/osu/          OSU-style sample agreements and review matrix (demo + tests)
scripts/              Operational scripts (migrate_access.py, backfill_govern_aggregates.py)
docs/                 ARCHITECTURE.md, ENGINE_AUDIT.md, COSTS.md, security overview
tests/                Offline unit and regression tests (AWS and OpenAI are faked)
```

## Getting started

```bash
# Prereqs: Python 3.12, Terraform 1.9+, AWS credentials for the target account

# Run the tests (offline: AWS and OpenAI are faked)
python -m venv .venv_test
.venv_test/Scripts/python -m pip install -r tests/requirements.txt   # Windows
.venv_test/Scripts/python -m pytest tests -q

# Deploy: push to main. The GitHub workflow runs the tests, builds the shared
# layer and applies terraform/ to the selected stage (dev by default).
```

Configuration is passed as Terraform variables and GitHub secrets (see `terraform/variables.tf`
and `.env.example`). Never commit `.env` or a saved plan file.

## Who can see what (access control)

Nobody shares a workspace by default. A signed-in user reaches a **project** only as its owner or a
member, and a **document** only if they uploaded it or it is filed in a project they can see. Anything
else answers `404`. Roles are `owner` / `editor` / `viewer`; the permission matrix lives in one place,
`lambdas/shared/access.py`, and is enforced on every route in `lambdas/api/handler.py` and in the chat
Lambda. Identity is the verified JWT only (`sub`, and `email` when `email_verified`). There is no shared
or default tenant and no setting that creates one. Design, key schema and the migration of existing data:
`docs/ARCHITECTURE.md` → "Access control".

## The playbook

Every clause is graded against a playbook of standard positions (`lambdas/shared/playbook.py`), with no
model call: built-in defaults for 20 clause types, optionally overridden for the whole deployment
(`PLAYBOOK_JSON`) and by each workspace's own rules (`GET /playbook`, `PUT` / `DELETE /playbook/rules/{ruleId}`).
A clause's outcome is one of `within`, `deviates`, `flagged` (a rule applies but could not decide),
`no_rule` (the playbook has no position for that clause type — nothing was checked) or `unclassified`.
Outcomes are in `classification.json` under `playbook.clauseResults` and on each clause as `clause.playbook`.
A document is graded against the playbook of the workspace it was uploaded into; a changed rule applies
from the document's next analysis or re-analysis (re-analysing an unchanged file re-grades it without
calling the model again).

## Models and accuracy settings

Every model name is a setting; nothing is hardcoded beyond the two defaults the deployment already used.
Terraform variables (→ Lambda environment variables) in `terraform/variables.tf`:

| Task | Env var | Terraform variable | Falls back to |
|---|---|---|---|
| Document-level extraction (parties, dates, money, scope) | `EXTRACTION_MODEL` | `extraction_model` | `CHAT_MODEL` |
| Per-clause labelling (type, risk, summary) | `CLAUSE_MODEL` | `clause_model` | `EXTRACTION_MODEL` |
| Money re-check | `VALIDATION_MODEL` | `validation_model` | `EXTRACTION_MODEL` |
| Sonar chat answers | `RAG_MODEL` | `rag_model` | `CHAT_MODEL` |
| Embeddings (search) | `EMBEDDING_MODEL` | `embedding_model` | — |

What to raise for higher accuracy, in order of effect per unit of cost:

1. **`EXTRACTION_MODEL`** — one or two calls per document read the whole contract for facts. This is
   where a stronger model pays off most (dates, money, amendment deltas). `VALIDATION_MODEL` follows it.
2. **`CLAUSE_MODEL`** — many small calls; it decides clause type and risk. Raise it second. Keeping it on
   the cheaper model keeps most of the cost down.
3. **`RAG_MODEL`** — affects chat answers only, not stored analysis.
4. **`CHAT_MAX_OUTPUT_TOKENS` / `CHAT_MAX_OUTPUT_TOKENS_MAX`** (16000 / 32000) — must not exceed the
   extraction model's completion limit. A reply that hits the limit is never accepted; it is retried once
   at the `_MAX` value.
5. **`CLASSIFY_MAX_INPUT_TOKENS`** (60000) — the largest slice of a document sent in one request. A longer
   document is read in overlapping windows and merged, never truncated. Raise it (if the model's context
   allows) so more documents are read in one piece; lower it for a small-context model.
6. **`MIN_COVERAGE_RATIO`** (0.98) — below this share of the document's words inside its clauses, the
   safer paragraph segmentation is used and the document is flagged for review.

Changing models safely:

* **Embedding model.** The vector index has a fixed vector size (`EMBEDDING_DIMENSIONS`, default 1536).
  The embed stage refuses to write if the model's output, the setting and the live index disagree — it
  fails with a clear message instead of leaving documents unsearchable. To move to a model with a
  different size: set `EMBEDDING_DIMENSIONS`, point `CLAUSE_VECTOR_INDEX` at a **new** index name, deploy,
  re-analyze the documents. For a model that accepts a `dimensions` parameter you can instead keep the
  1536-wide index and set `EMBEDDING_SEND_DIMENSIONS=true`.
* **Chat / extraction model.** The client adapts to models that reject `max_tokens`, a temperature, or
  strict `json_schema` output (it re-sends in the accepted form and checks the reply against the schema in
  code). `STRUCTURED_OUTPUT_MODE=json_object` forces the plain-JSON form.
* Any model change makes previous analyses non-reusable automatically (the engine fingerprint includes the
  model names), so "re-analyze" really re-analyzes.

Cost / speed knobs: `LLM_MAX_CONCURRENCY` (4 — simultaneous OpenAI requests per invocation; lower it if the
account hits rate limits), `OPENAI_TIMEOUT_S` (180), `OPENAI_MAX_ATTEMPTS` (4), `CLASSIFY_BATCH_CLAUSES` (16),
`CLASSIFY_REUSE_UNCHANGED` (true), `EMBEDDING_BATCH_SIZE` (100). All defaults are in `lambdas/shared/config.py`.

## Tests

```
python -m venv .venv_test
.venv_test\Scripts\python -m pip install -r tests/requirements.txt
.venv_test\Scripts\python -m pytest tests -q
```

Everything runs offline: AWS and OpenAI are replaced by in-memory fakes (`tests/fakes.py`), and
`tests/conftest.py` fails any test that tries to open a real AWS session or build a real OpenAI client.

## What to read first

1. `docs/ARCHITECTURE.md` — full design + tradeoffs + open questions, and the access-control design
2. `docs/ENGINE_AUDIT.md` — what the extraction engine guarantees, pass by pass
3. `docs/COSTS.md` — cost estimate for 10k / 100k / 1M docs
