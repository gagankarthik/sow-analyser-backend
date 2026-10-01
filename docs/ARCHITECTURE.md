# Architecture — analysis, alternatives, open questions

> **Status:** scaffolded. Before deep implementation, please review the questions at the bottom — answers materially change scope and cost.

## 1. Requirements as understood

- **Throughput:** "enterprise level — can handle lots of documents." Assumed steady-state of ~10k docs ingested per month, bursts of 1k/day.
- **Document types:** SOW, MSA, Amendment, plus side letters / NDAs.
- **Pipeline stages (per user spec):**
  1. Parse — Textract for OCR'd PDFs, pdfplumber for born-digital PDFs, python-docx for Word.
  2. Classify — OpenAI structured JSON output: doc-type, lifecycle status, hash, structural keys.
  3. Embed — clause-level OpenAI embeddings; find parent doc.
  4. Build version graph — adjacency / lineage.
  5. Diff — field-level changes + impact score.
  6. Timeline — initial · current · future via amendment replay.
  7. Persist — DynamoDB writes.
- **Storage:** S3 processed (extracted JSON, diff snapshots, audit blobs); DynamoDB (Documents, Versions, Changes); OpenSearch (clause vectors + full-text); optional Neptune (lineage graph).
- **Surface area:** AppSync GraphQL — queries, real-time subscriptions, RAG over clauses with streaming.
- **Frontend:** Next.js dashboards consume versions, diffs, insights.

## 2. Proposed architecture (default) — see README diagram

The design above optimizes for:
- **Cost** — single-table DynamoDB, adjacency-list lineage in DDB instead of Neptune for v1.
- **Latency** — Express Step Functions (≤5 min runtime), presigned uploads direct to S3.
- **Operational simplicity** — one OpenSearch cluster, one DynamoDB table, no Neptune cluster to manage initially.
- **Throughput** — every stage horizontally scalable (Lambda concurrency).

## 3. Where to consider less-optimal alternatives

These are points where the spec choice is reasonable but may not be the best tradeoff. **Bold = my recommended alternative.**

### 3.1 Lineage graph — Neptune vs DynamoDB adjacency

| | Neptune | **DynamoDB adjacency list (recommended for v1)** | Postgres + LTREE |
|---|---|---|---|
| Cost / month at idle | $350+ (smallest cluster) | ~$5 | ~$30 (Aurora Serverless v2) |
| Query: "amendments under SOW X" | Gremlin traversal, fast | DDB Query on PK=DOC#X | Recursive CTE |
| Query: "find all SOWs amended by clause Y" | Excellent | Needs GSI | OK |
| Engineering overhead | High (new query language) | Low (DDB already in use) | Medium |

For SOW/amendment lineage which is mostly **shallow trees** (SOW → 5-10 amendments, rarely deeper), DynamoDB adjacency wins. Promote to Neptune only if you start doing cross-account graph analytics or multi-hop "show me every contract sharing this clause" at portfolio scale.

### 3.2 Vector search — OpenSearch k-NN vs pgvector vs Pinecone/Qdrant Cloud

| | **OpenSearch k-NN (recommended — matches spec)** | pgvector on Aurora | Pinecone Serverless | Qdrant Cloud |
|---|---|---|---|---|
| Cost @ 1M vectors | ~$280/mo (3 × r6g.large) | ~$70/mo (Aurora Serverless v2) | ~$30/mo (serverless) | ~$30/mo |
| BM25 full-text in same query | ✅ same cluster | ❌ separate service or pg_trgm | ❌ vectors only | ✅ |
| Hybrid search (vector + BM25) | ✅ excellent | Requires manual rerank | Requires reranker | ✅ |
| AWS-native | ✅ | ✅ | ❌ | ❌ |
| Embedding dims max | 16k | 2k (HNSW) / 16k (IVF) | 4k | 65k |

OpenSearch is the right call **if** you need BM25 + vector hybrid in one query (you do — clause search benefits from both lexical and semantic). For pure vector retrieval at startup-cost, pgvector wins.

### 3.3 Step Functions — Standard vs Express

| | Standard | **Express (recommended)** |
|---|---|---|
| Max runtime | 1 year | 5 minutes |
| Cost per state transition | $0.025 per 1k | $1.00 per million |
| Execution history | Full | None (use CloudWatch) |
| Use case | Long-running, human-in-loop | Short pipelines, high throughput |

Pipeline takes 30s–2min for a typical SOW. Express is **~10× cheaper at 100k+ executions/month**. Move to Standard only if you need human-in-loop approval gates or want execution replay.

### 3.4 Real-time API — AppSync GraphQL vs REST+SSE

| | **AppSync (recommended — matches spec)** | REST + SSE | tRPC over WebSockets |
|---|---|---|---|
| Subscription pricing | $2 / million message + $0.08/hr | Server cost only | Server cost only |
| Federated resolvers | Native | DIY | DIY |
| Lambda resolver auth | IAM / Cognito / API key | DIY | DIY |
| Frontend type safety | Codegen needed | Codegen needed | Native end-to-end |
| Streaming RAG | HTTP resolver + SSE works | Native | WebSocket native |

AppSync matches spec and gives free real-time. The cost catch: WebSocket connections at scale. Mitigate with subscription filtering at the resolver layer (only fan-out per-document, not per-user).

### 3.5 OCR — Textract vs Tesseract vs PaddleOCR

| | **Textract (recommended for scanned)** | Tesseract on Lambda | PaddleOCR |
|---|---|---|---|
| Accuracy on legal docs | Excellent | Mediocre | Good |
| Cost per page | $0.0015 | Compute only (~$0.0001) | Compute only |
| Tables / forms detection | Native | Hard | OK |
| Cold start | None (managed) | 1-3s | 5-15s (big model) |

For born-digital PDFs use pdfplumber (free, fast). Detect "is OCR needed?" by checking if text extraction produces zero/low chars, then fall back to Textract.

### 3.6 Compute substrate

| | **Lambda (recommended)** | ECS Fargate | EKS |
|---|---|---|---|
| Per-stage cost @ 10k docs/mo | <$5 | ~$30 | ~$100 (cluster + nodes) |
| Cold start on parse stage | 1-3s | None (warm pool) | None |
| Max payload | 6 MB sync, 256 KB async | Unlimited | Unlimited |
| Concurrency | Soft 1000 default | Unlimited | Unlimited |

Lambda wins for the pipeline. **One exception:** if you ingest 200+ page contracts, parse stage can exceed the 15-min limit. Mitigate by either (a) chunking, (b) escalating to Fargate for large docs via a "router" Lambda.

### 3.7 OpenAI — direct vs Bedrock Claude

| | **OpenAI (recommended for spec)** | Bedrock Claude | Both |
|---|---|---|---|
| Structured output JSON mode | Native (`response_format`) | Tool use | — |
| Embedding model | text-embedding-3-large/small | Titan v2 | — |
| Data residency | OpenAI Enterprise tier | AWS region | — |
| Per-call cost (gpt-4o-mini) | $0.15/1M in, $0.60/1M out | $0.25/$1.25 (Sonnet) | — |

Spec says OpenAI — fine. If data residency matters (EU, FedRAMP), Bedrock Claude Sonnet 3.5 is the equivalent. The Lambda handler in `02_classify` is provider-agnostic via a small adapter.

## 4. Cost projection (default architecture)

For **10k docs/month**, assumed ~30 clauses each, ~10 amendments/SOW (300k embeddings):

| Service | Component | Monthly est. |
|---|---|---|
| S3 | Raw + processed @ 500 GB | $12 |
| Lambda | 7 stages × 10k = 70k invocations × ~1s avg | $5 |
| Step Functions Express | 10k executions × 7 transitions | $1 |
| Textract | ~30% of pages need OCR, 10k docs × 20 pages × 0.3 | $90 |
| DynamoDB | On-demand, ~50k writes/day, 200k reads/day | $25 |
| OpenSearch | 3× t3.small.search (dev) → r6g.large.search (prod) | $80–$280 |
| AppSync | Queries + 1M subscription events | $15 |
| OpenAI | 10k classifications + 300k embeddings (small) + RAG | $40–80 |
| **Total** | | **$268–508 / mo** |

At **100k docs/month** the dominant costs shift to Textract (~$900) and OpenSearch (~$700, scaled to m6g.xlarge × 3). Total ~$2.5k–$3.5k/mo.

## 5. Critical open questions

These materially change scope. Please answer before we go deeper.

### Throughput & SLA
1. **What's your actual document volume per month?** (1k, 10k, 100k+?) — drives OpenSearch sizing, Lambda concurrency, and whether we need Fargate for large docs.
2. **What's the acceptable end-to-end latency for a fresh upload to appear in the dashboard?** 30 seconds, 2 minutes, 5 minutes? — drives Standard vs Express Step Functions and whether the RAG resolver pre-warms embeddings.
3. **Maximum document size?** Some contracts run 300+ pages. If yes, the parse Lambda needs Fargate fallback.

### Data residency & compliance
4. **Region requirement?** Single-region or multi-region? — affects S3, DDB Global Tables, OpenSearch cross-region.
5. **SOC2 / ISO / FedRAMP?** — if FedRAMP, Bedrock Claude is preferable to OpenAI direct.
6. **PII handling?** — affects whether we redact before sending to OpenAI, and audit-log requirements.

### Product
7. **Do you want versioning to be "every upload is a version" or "user explicitly versions"?** — affects S3 layout and DDB SK design.
8. **Do you want clause-level diffs, document-level diffs, or both?** Clause-level is much richer but doubles storage.
9. **For the version graph: is it a strict tree (one parent), or can amendments reference multiple parents?** — strict tree allows DDB adjacency; DAG needs Neptune or more careful DDB modeling.
10. **RAG: do answers need to cite a specific clause + version, or just the document?** — citation grain affects the embedding chunking strategy.

### Auth & multi-tenancy
11. **Multi-tenant from day one or single-tenant?** — affects DDB partition key design and OpenSearch index strategy (one index per tenant vs one shared with `tenant_id` filter).
12. **Auth provider?** — Cognito, Auth0, custom OIDC? AppSync supports all but the integration differs.

### Budget
13. **Monthly budget ceiling?** — drives whether we use Neptune (yes if budget permits, no otherwise) and OpenSearch sizing.

### Frontend integration
14. **Is the Next.js app deployed to Vercel/Amplify/self-hosted on AWS?** — affects how it authenticates to AppSync (same VPC vs IAM signing vs Cognito) and presigned-URL minting.
15. **Do you want SSE/WebSocket streaming for RAG responses, or is request-response with `<200ms TTFB` acceptable?**

## 6. What's been scaffolded so far

- `README.md` — high-level overview + diagram
- `docs/ARCHITECTURE.md` — this file
- Directory tree (`infra/`, `lambdas/`, `api/`, `scripts/`, `docs/`, `tests/`)
- `.gitignore`, `.env.example`
- CDK + Python project shells (next)
- Lambda handler stubs (next — agents working)
- AppSync schema skeleton (next — agents working)

## 7. Next steps (after your answers)

1. Implement CDK stacks: Storage → Pipeline → Search → Api
2. Implement Python Lambda handlers (parse → classify → embed → graph → diff → timeline → persist)
3. AppSync schema + resolvers (queries, subscriptions, RAG)
4. Frontend integration: presigned upload, GraphQL client, subscription handlers
5. Observability: CloudWatch dashboards, X-Ray tracing, structured logs, alarms
6. CI/CD: GitHub Actions for `cdk deploy` + Lambda packaging


## 8. Access control (per project, by membership)

**Rule.** Nobody shares a workspace by default. A signed-in user can reach a *project* only if they own
it or are a member, and a *document* only if they uploaded it or it is filed in a project they can see.
Everything else answers `404` (never `403`, so an id cannot be probed). A user who can see something but
may not change it gets `403` with `code: "forbidden"`. The rule, the roles and the permission matrix are
defined once, in `lambdas/shared/access.py`.

**Identity.** Only the JWT the API Gateway authorizer verified: `sub` for ownership, and the `email`
claim — only when `email_verified` is true — for membership. A header, a body field or a client-supplied
`ownerEmail` is never identity. The "workspace" (`tenantId`) is the `custom:tenantId` claim if present,
else the private `u-<sub>`; it is a storage namespace (S3 prefix, search field), not a sharing boundary.
There is no shared or default tenant.

**Roles.**

| | owner | editor | viewer |
|---|---|---|---|
| view project, documents, analysis, chat | yes | yes | yes |
| upload into the project, edit document metadata, re-analyze, add/remove documents | yes | yes | – |
| file an existing document into a project (shares it with that project's members) — needs this on the *document* | yes | – | – |
| delete a document or version | yes | – | – |
| rename / delete the project, invite, remove member, change a role | yes | – | – |

On a document the caller's role is the strongest of: `owner` if they uploaded it, else their role in any
project that lists it. The uploader of a document keeps `owner` rights on it wherever it is filed.

**Storage** (same single table, nothing scans):

| Item | PK | SK | GSI1PK → GSI1SK |
|---|---|---|---|
| Project | `PROJ#<id>` | `META` (name, ownerSub, ownerEmail, `docIds`, `rev`) | – |
| Owner pointer | `PROJ#<id>` | `OWNER` | `USER#<sub>` → `PROJ#<id>` |
| Membership | `PROJ#<id>` | `MEMBER#<email>` (role, status, sub) | `MEMBER#<email>` → `PROJ#<id>` |
| Document | `DOC#<id>` | `META` + `ownerSub`, `ownerEmail`, `projectIds` | `TENANT#<workspace>` → `DOC#<id>` (unchanged) |

* "Which projects can I see?" = two GSI1 partition reads (`USER#<sub>`, `MEMBER#<verified email>`).
* "May I read document D?" = D's `META` (one `GetItem`); if the caller is not the uploader, the projects in
  D's `projectIds` that the caller belongs to are fetched (one `BatchGetItem`) and must **list D in
  `docIds`**. The project record is the source of truth; `projectIds` on the document is only a hint that
  says where to look, so a stale hint can never grant access.
* "List my documents" = own uploads (GSI1 `TENANT#<workspace>`, filtered to `ownerSub`) + `BatchGetItem`
  of the `docIds` of the caller's projects.
* A document id or project id that is guessed grants nothing: every read goes through the check above.
* Project `META` is written with optimistic locking on `rev` — two people editing one project cannot
  overwrite each other.

**Invites across workspaces.** A membership is keyed on the invitee's email. It takes effect whenever a
user with that *verified* address signs in — whether or not the account existed when they were invited,
and whatever their own workspace is — and grants that project's documents only. Removing the row ends
access on the next request (nothing is cached between requests). The Cognito invitation email is
best-effort; no tenant attribute is written to the user.

**Search and chat.** Retrieval is restricted to the documents the caller may read *inside the OpenSearch
query* (`filter: terms docId` / `term docId`), never by filtering results afterwards. No permitted
document → no search and no model call. The timeline and diff of a document are withheld (or reduced)
when part of the amendment chain is a document the caller cannot see, because those artefacts quote the
other document's clause text.

**The pipeline.** The persist stage *updates* the analysis fields of the document record instead of
replacing it, so ownership and project membership set by the API during a run are never undone. An
amendment's parent is searched in the uploader's workspace plus the documents that share a project with it.

### Migrating existing data (one-off)

Existing documents and projects live under the shared `default` tenant with no owner recorded. After the
new backend is deployed they are visible to nobody until `scripts/migrate_access.py` has run — nothing is
lost, it is hidden.

```
# 1. Any time (read-only): see exactly what would happen
python scripts/migrate_access.py --table <table> --user-pool-id <pool> --region us-east-2

# 2. Deploy the new backend.

# 3. Straight after the deploy: write the changes
python scripts/migrate_access.py --table <table> --user-pool-id <pool> --region us-east-2 --apply

# Documents in no project, and projects with no recorded owner, are listed and left alone.
# To give them to someone (the account must already exist in Cognito):
python scripts/migrate_access.py --table <table> --user-pool-id <pool> --orphans-to owner@example.com --apply
```

Keep the gap between steps 2 and 3 short, and ideally have users stay out of the app during it: a
browser that still holds the pre-deploy project list will re-save it, creating empty projects under the
person who happens to save first. The script detects these, leaves them untouched and lists them under
`CONFLICTS` for a manual decision.

The script assigns each project to its recorded `ownerEmail` (resolving the Cognito `sub` when the user
exists; otherwise the project is theirs by email as soon as they sign up with that verified address),
carries members over (the legacy `member` role becomes `viewer`), gives each document the owner of the
project that contains it, and lists it under that owner. It is a dry run unless `--apply` is given, never
deletes anything (the legacy `TENANT#default / PROJECTS` blob stays), is idempotent, and does not move a
document's files or search records (`tenantId`, S3 keys and index records are untouched).
