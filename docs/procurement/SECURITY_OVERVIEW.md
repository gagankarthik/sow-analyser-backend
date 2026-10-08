# Blue-IQ Govern: security overview for The Ohio State University

Prepared 2026-10-08. Audience: OSU Office of Technology and Digital Innovation (OTDI), Legal Affairs, Privacy, Procurement.

**Evidence rule.** Every statement here comes from the code or Terraform in `sow-analyser-backend` and `sow-analyzer` as of this date. File references are given. Anything we could not verify from the repository is marked **To confirm**. Gaps are stated as gaps.

---

## 1. System summary

Blue-IQ Govern is a serverless application on AWS. Users upload agreements. A pipeline extracts text, labels clauses with an AI model (OpenAI) and stores the results. Govern then grades each clause against the institution's contract matrix, using deterministic code with no model call, and routes the contract between reviewers.

| Layer | Implementation | Evidence |
|---|---|---|
| Web app | Next.js 16 / React 19, hosted on AWS Amplify | `sow-analyzer/amplify.yml`, `package.json` |
| API | Amazon API Gateway HTTP API, Cognito JWT authorizer on every route | `terraform/lambda.tf` |
| Compute | AWS Lambda (Python 3.12, arm64): `pipeline`, `api`, `rag`; Govern adds `govern-api` and workers | `terraform/lambda.tf`, `lambdas/govern_api/` |
| Orchestration | AWS Step Functions (STANDARD), 7 stages | `terraform/step_functions.tf` |
| Storage | S3 (raw, processed), DynamoDB, Amazon OpenSearch Service | `terraform/storage.tf` |
| Identity | Amazon Cognito user pool, ID token (JWT) | `terraform/variables.tf` (pool is created outside Terraform) |
| AI | OpenAI API: chat (`gpt-4.1-mini` by default) and embeddings (`text-embedding-3-small`) | `terraform/variables.tf`, `lambdas/shared/config.py` |
| OCR | Amazon Textract (scanned PDFs only) | `terraform/iam.tf` (parse policy) |

## 2. Where OSU data lives

| Data | Store | Region | Encryption at rest | Notes |
|---|---|---|---|---|
| Original uploaded files | S3 `<prefix>-raw-<account>` | `us-east-2` by default (`var.aws_region`); production region set by CI secret: **To confirm** | SSE-S3, AES-256 (AWS-owned keys) | Versioned; public access blocked; TLS-only bucket policy |
| Extracted text, clause JSON, diffs, timelines | S3 `<prefix>-processed-<account>` | same | SSE-S3, AES-256 | Public access blocked; TLS-only policy. **Not versioned** in Terraform (older docs say it is) |
| Document metadata, projects, memberships, embedding cache | DynamoDB `<prefix>-main` | same | DynamoDB server-side encryption with the AWS managed key | PITR on; deletion protection in `prod` |
| Clause text and vectors (search) | OpenSearch domain `<prefix>-search` | same | Encryption at rest and node-to-node | HTTPS enforced, TLS 1.2 minimum |
| Govern contracts, activity log, config, sync, metrics | DynamoDB `govern-*` tables (5) | same | Customer-managed KMS key (rotation on) | `terraform/govern.tf`; PITR on contracts, activity, config; TTL on sync, metrics |
| Integration secrets (Teams, DocuSign, Huron, Workday, M365) | AWS Secrets Manager | same | KMS (service default) | `lambdas/shared/govern/secrets.py`; values never logged or returned |
| Application logs | CloudWatch Logs | same | CloudWatch default | 30-day retention (`var.log_retention_days`) |
| Failed pipeline events | SQS DLQ | same | SQS-managed SSE | 14-day retention; TLS-only policy |

**Customer-managed KMS keys (CMK):** none are configured. All encryption uses AWS-owned or AWS-managed keys. CMKs are a configuration change (S3 `aws:kms`, DynamoDB `kms_key_arn`, OpenSearch `kms_key_id`) and can be offered for OSU.

## 3. Encryption in transit

| Path | Control | Evidence |
|---|---|---|
| Browser to API | HTTPS only (API Gateway HTTP APIs do not serve plain HTTP) | AWS service behaviour |
| Browser to S3 (upload) | Presigned PUT over HTTPS; bucket policy denies `aws:SecureTransport = false` | `storage.tf` `raw_tls_only` |
| Lambda to OpenSearch | `enforce_https = true`, `Policy-Min-TLS-1-2-2019-07` | `storage.tf` |
| Lambda to OpenAI | HTTPS (OpenAI SDK over httpx) | `lambdas/shared/openai_client.py` |
| Web app | HSTS, CSP with `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy` | `sow-analyzer/next.config.ts` |

The public website says "TLS 1.3". The minimum enforced in our configuration is TLS 1.2. The site wording should be corrected (see README action list).

## 4. What goes to OpenAI, and what does not

| Sent to OpenAI | Purpose | Redaction before sending |
|---|---|---|
| Document text, in windows of up to 60,000 tokens | Document-level extraction (parties, dates, amounts, scope) | **None by default.** The extraction step must read amounts, dates and parties |
| Clause text, in batches | Clause labelling (type, risk, summary) | None by default |
| Clause text chunks | Embeddings for search | None by default |
| Field changes between versions (up to 10 calls per document) | Impact rationale in the diff stage | None by default |
| Retrieved clauses plus the user's question | "Ask Sonar" chat answers | Emails, phone numbers, SSNs, card numbers and IP addresses are replaced by placeholders, then restored in the answer (`lambdas/rag/handler.py`) |
| SOW drafting questionnaire (frontend feature) | Draft generation | Same placeholder redaction (`sow-analyzer/lib/sow/guardrails.ts`) |

**Not sent to OpenAI:**
- Govern matrix review (grading clauses against the OSU matrix). It is deterministic code (`lambdas/shared/govern/matrix.py`). No Govern module imports the OpenAI client.
- Workflow, assignments, comments, approvals and the activity log.
- User identities and account data (other than any that appear inside a document).
- Original files. Only extracted text is sent.

**Controls on every AI call** (`lambdas/shared/guardrails.py`, `openai_client.py`):
1. Provider allowlist, fail-closed. The client is not built unless `AI_PROVIDER` is registered as `no_train=True`. Registered: `openai`, `openai-zdr`, `bedrock`.
2. An audit log line for each call: provider, no-train and zero-retention flags, operation, byte count, redaction counts. These go to CloudWatch (30-day retention).
3. Output validation on the chat path (unrestored placeholders, raw PII patterns).

**Current configuration:** Terraform does not set `AI_PROVIDER`, so the default `openai` applies (`zero_retention=False`). Under OpenAI's API terms, API inputs are not used for training by default. They may be kept for up to 30 days for abuse monitoring unless Zero Data Retention (ZDR) is approved for the account. **ZDR is not yet enabled, and a DPA with OpenAI is not yet signed.** See `SUBPROCESSORS.md`.

**Option for OSU:** the allowlist already includes `bedrock`. That would keep inference inside the AWS account. A Bedrock client is **not implemented** today: this is a roadmap option, not a switch.

## 5. Identity, authentication and authorisation

| Control | Status | Evidence |
|---|---|---|
| Every API route requires a valid Cognito ID token (signature, issuer, audience) | Implemented | `aws_apigatewayv2_authorizer.cognito`, `authorization_type = "JWT"` on each route |
| Identity taken only from verified claims (`sub`; `email` only when `email_verified`) | Implemented | `lambdas/shared/access.py`, `shared/auth.py` |
| Per-project, per-document access: owner / editor / viewer; unknown ids return 404 | Implemented | `access.py` permission matrix |
| Govern roles from Cognito groups `govern-admin`, `govern-reviewer`, `govern-leader` | Implemented in code | `lambdas/govern_api/handler.py` |
| `GOVERN_OPEN_ADMIN` defaults to `false`; only the `dev` stage may enable it | Resolved | `shared/config.py`, `terraform/govern.tf` |
| SAML SSO (Shibboleth/InCommon) | **Not yet implemented** | Planned; see `SSO_SHIBBOLETH.md` |
| MFA | **To confirm.** The user pool is managed outside Terraform | `variables.tf` |
| Webhooks (`/webhooks/*`) verified by HMAC with 5-minute replay window | Designed and coded | `GOVERN_ARCHITECTURE.md` §2.6, `config.py` `webhook_max_age_s` |
| API throttling (50 rps default; lower on chat, reprocess, upload, invite) | Implemented | `aws_apigatewayv2_stage.default` |

## 6. Tenant and project isolation

- The workspace (`tenantId`) is the verified `custom:tenantId` claim, or else a private `u-<sub>`. A malformed claim is rejected. No shared or default tenant exists (`shared/auth.py`, `variables.tf` comment).
- Within a workspace, nobody sees a document unless they uploaded it or are a member of a project that lists it. The project record is the source of truth (`access.py`).
- Search and chat apply the permitted-document filter *inside* the OpenSearch query, not after it. With no permitted documents, no search runs and no model call is made (`docs/ARCHITECTURE.md` §8).
- Govern contract visibility equals document visibility (`govern_api/handler.py` docstring).
- Isolation is logical (shared tables, shared index, partitioned by key and filter). A dedicated single-tenant deployment for OSU, in its own AWS account, is possible with the same Terraform. **To confirm** commercially.

## 7. IAM least privilege

From `terraform/iam.tf` and `lambda.tf`:
- **Separate roles** for the internet-facing `api` and `rag` functions and for the pipeline. The API no longer holds Textract or pipeline rights.
- **Resource-scoped** policies: named bucket ARNs, the one table and its indexes, the one OpenSearch domain, and the one Cognito pool (`AdminCreateUser`/`AdminGetUser` only).
- **OpenSearch** domain policy allows only the three roles, data-plane `es:ESHttp*` only.
- **Step Functions** can invoke only the pipeline Lambda and `UpdateItem` on the table.
- **Known broad grants (to tighten):** Textract `Resource = "*"` (required by the API); the `rag` role has `appsync:GraphQL` on `apis/*`, but no AppSync API exists in Terraform, so this grant should be removed; the pipeline stages share one role, so stage-level separation is by policy name only.
- Govern functions: "least privilege per function; activity writers are put-only" is implemented in `terraform/govern.tf`: one role per Govern function, scoped to the tables it uses; activity writers have `dynamodb:PutItem` only.

## 8. Logging and audit

| Log | Content | Retention |
|---|---|---|
| `govern-activity` DynamoDB table | Every assignment, action, comment and stage change for a contract | Indefinite by design. Writes use `PutItem` with `attribute_not_exists(PK)`, so no entry can be overwritten (`shared/govern/store.py`). The code never updates or deletes entries. IAM put-only and PITR are **to confirm** |
| CloudWatch Lambda logs | Structured JSON; AI audit line per call; no document text, no secrets | 30 days |
| API Gateway access log | request id, status, route, integration error | 30 days. Does **not** include caller identity: add `$context.authorizer.claims.sub` |
| Step Functions | Execution logging and X-Ray tracing | 30 days |
| AWS CloudTrail, GuardDuty, Security Hub, AWS Config | **Not defined in Terraform. To confirm** at account level |

## 9. Retention and deletion

- **Retained** while the customer account is active. There is no automatic expiry of current documents.
- **Delete a document** (owner only) removes: the raw file, every processed artefact under the document's prefix, its OpenSearch records, its project links, and its DynamoDB rows (`lambdas/api/handler.py` `_remove_everywhere`).
- **Residual copies after deletion:**

| Residual | Duration |
|---|---|
| S3 non-current version of the raw file | expires after 30 days (`storage.tf` lifecycle) |
| DynamoDB point-in-time recovery | up to 35 days (AWS PITR window) |
| OpenSearch automated snapshots | AWS service default. **To confirm** |
| CloudWatch logs (metadata only) | 30 days |
| OpenAI abuse-monitoring copy | up to 30 days, unless ZDR is enabled |
| Embedding cache (`CACHE#<sha256>`: vector and model name, no text) | not removed on delete. **Gap**, to fix |

- **Govern records** when the underlying document is deleted: **to confirm.** The public-records and retention schedule settings described in `COMPETITIVE_ANALYSIS.md` §8.2 are not yet built.
- **Export at exit:** Govern reports and exports exist (Excel, PDF, JSON per the competitive analysis). A documented full-tenant export procedure is **not yet written**.

## 10. Backups and resilience

- DynamoDB PITR (35 days) on the main table. Deletion protection in `prod`.
- S3 versioning on the raw bucket.
- OpenSearch is a **single node** (`instance_count = 1`, `t3.small.search` default). It is rebuildable from S3 and DynamoDB by re-running analysis, but has no high availability. For production: 2–3 nodes across AZs.
- All services are regional and managed. There is no multi-region DR. RPO/RTO targets are **not yet defined**.
- Pipeline failures go to a DLQ and mark the document `FAILED`. Transient Lambda faults are retried; model calls are not retried twice.

## 11. Network

- No VPC. Lambdas reach OpenSearch, DynamoDB and S3 over AWS public endpoints with IAM SigV4. OpenSearch has a public endpoint gated by IAM. Moving OpenSearch and Lambdas into a private VPC with VPC endpoints is a roadmap item.
- CORS limited to configured origins (`localhost:3000`, `https://govern.blue-iq.ai`). The localhost origin should be removed in `prod`.
- No AWS WAF in front of the API. **Gap.**

## 12. Secure development and change management

- All infrastructure is Terraform. Deploys run through GitHub Actions (`.github/workflows/deploy.yml`): offline unit and security regression tests must pass, then `terraform validate`, `plan`, and `apply`.
- Tests fail if any test opens a real AWS session or OpenAI client (`tests/conftest.py`).
- **Gaps:** CI authenticates with long-lived AWS access keys (move to GitHub OIDC); `apply -auto-approve` runs on every push to `main` (add a manual approval gate for `prod`); no SAST, dependency or container scanning step; no frontend test or accessibility step.
- **Secrets:** the OpenAI API key is passed as a Terraform variable into Lambda environment variables. It therefore sits in Terraform state and in Lambda configuration. The code already supports `OPENAI_SECRET_ARN` (Secrets Manager) but Terraform does not use it. **Gap: move to Secrets Manager.**

## 13. Vulnerability management

| Item | Status |
|---|---|
| Responsible disclosure address | `security@blue-iq.ai` (published on `/security`) |
| Third-party penetration test | **Not yet performed.** To be commissioned before the OSU pilot |
| Dependency scanning (Dependabot / pip-audit / npm audit) | **Not configured** in the repo |
| Patch cadence | Managed runtimes (Lambda, OpenSearch service updates). Written SLA **not yet defined** |
| Proposed SLA | Critical 7 days, High 30 days, Medium 90 days, from confirmation |

## 14. Incident response (outline)

No written incident response plan exists in the repository. This outline is proposed:

1. **Detect:** CloudWatch alarms on errors and DLQ depth; GuardDuty (to enable); reports to `security@blue-iq.ai`.
2. **Triage** within 4 business hours. Severity S1–S4. Named incident lead.
3. **Contain:** revoke Cognito sessions, rotate the OpenAI key and integration secrets, disable affected routes, and snapshot evidence.
4. **Notify:** OSU contact without undue delay and within 72 hours of awareness (the commitment in our published DPA summary). Align to the OSU contract if it is stricter.
5. **Recover** from PITR and S3 versions, then re-run analysis to rebuild the search index.
6. **Review:** written post-incident report to OSU within 10 business days.

Owner: Blue-IQ security lead. The plan must be written, approved and tested (tabletop) before the pilot.

## 15. Certifications

Blue-IQ holds **no** SOC 2, ISO 27001 or other certification today, and no audit report exists. AWS's own certifications cover the infrastructure layer only. A SOC 2 Type I readiness plan is proposed in `README.md`.
