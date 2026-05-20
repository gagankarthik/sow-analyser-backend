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
4. **Express Step Functions** for ingest pipeline — cheaper, faster, fits sub-5-minute work.
5. **Server-Sent Events for RAG streaming** through AppSync HTTP resolvers (or Lambda Function URLs) rather than WebSockets.
6. **Python 3.12 Lambdas** with layers for shared deps (boto3, pdfplumber, openai). Pinned versions in `requirements.txt`.
7. **Presigned S3 uploads** — frontend uploads direct to S3, never proxies through Lambda. EventBridge triggers pipeline on `ObjectCreated`.

See `docs/ARCHITECTURE.md` for the full analysis, alternatives, and open questions.

## Layout

```
infra/                AWS CDK (TypeScript) — all infrastructure as code
  bin/                CDK entrypoint
  lib/                Stacks (Storage, Pipeline, Search, Api)
lambdas/              Python Lambda handlers
  shared/             Shared utilities (logging, s3, ddb, prompt templates)
  01_parse/           Textract + pdfplumber + python-docx
  02_classify/        OpenAI structured JSON classification
  03_embed/           OpenAI clause embeddings → OpenSearch
  04_graph/           Parent-doc detection + adjacency-list writes
  05_diff/            Field-level diff + impact score
  06_timeline/        Replay amendments into initial/current state
  07_persist/         Final DynamoDB write + audit blob
api/                  AppSync schema + resolvers
scripts/              Local dev, seed, deploy helpers
docs/                 ARCHITECTURE.md, RUNBOOK.md, COSTS.md
tests/                Unit + integration
```

## Getting started

```bash
# Prereqs: Node 20, Python 3.12, AWS CDK CLI, Docker (for Lambda bundling)

# 1. Install CDK dependencies
cd infra
npm install

# 2. Bootstrap your AWS account (one-time)
npx cdk bootstrap aws://<ACCOUNT>/<REGION>

# 3. Deploy storage stack first (creates S3 buckets + DynamoDB)
npx cdk deploy BlueIQ-Storage

# 4. Deploy pipeline + search + api
npx cdk deploy --all
```

Set environment variables in `.env` (see `.env.example`):
- `OPENAI_API_KEY`
- `AWS_REGION`
- `EMBEDDING_MODEL` (default: `text-embedding-3-small`)
- `CHAT_MODEL` (default: `gpt-4o-mini`)

## What to read first

1. `docs/ARCHITECTURE.md` — full design + tradeoffs + open questions
2. `docs/COSTS.md` — cost estimate for 10k / 100k / 1M docs
3. `docs/RUNBOOK.md` — how to deploy, debug, replay failed docs

## Status

Scaffolded. Awaiting clarifications from product owner before deep implementation (see questions in `docs/ARCHITECTURE.md`).
