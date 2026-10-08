# Blue-IQ Govern: subprocessors and third-party services

Prepared 2026-10-08. Derived from `terraform/*.tf`, `lambdas/shared/*`, `sow-analyzer/amplify.yml` and `docs/GOVERN_ARCHITECTURE.md`. Items not in Terraform are marked.

Two kinds of third party are listed:
- **A. Subprocessors** process OSU data on Blue-IQ's behalf.
- **B. Customer-authorised integrations** are OSU's own systems or vendors. Govern connects to them only if OSU enables the connection and supplies credentials. They are OSU's processors, not Blue-IQ's.

## A. Subprocessors

### A1. Amazon Web Services (AWS)

Entity: Amazon Web Services, Inc. Region: `us-east-2` (Ohio) by default (`var.aws_region`). Production region: **to confirm** (set by a CI secret). Terms: AWS Customer Agreement and AWS GDPR DPA (incorporated into the AWS Service Terms).

| Service | Used for | OSU data it handles | In Terraform? |
|---|---|---|---|
| Amazon S3 | Raw uploads; processed artefacts; Lambda layer | Full document content | Yes |
| Amazon DynamoDB | Documents, projects, memberships, embedding cache; Govern tables | Metadata, extracted fields, workflow and audit records | Main table yes; Govern tables **no** |
| Amazon OpenSearch Service | Clause search (text and vectors) | Clause text | Yes |
| AWS Lambda | All compute | All, transiently | Yes |
| AWS Step Functions | Pipeline orchestration | Document ids and stage state (bulky state goes to S3) | Yes |
| Amazon API Gateway | HTTP API | Requests and responses | Yes |
| Amazon Cognito | Sign-in, tokens, groups | Name, email, group membership | Referenced (pool managed outside Terraform) |
| Amazon Textract | OCR of scanned PDFs | Page images of scanned documents | IAM only |
| Amazon EventBridge | Pipeline trigger; Govern event bus | Ids and event metadata | Default bus yes; Govern bus **no** |
| Amazon SQS | Pipeline DLQ; Govern queues | Event payloads | DLQ yes; Govern queues **no** |
| Amazon SES | Email alerts (Govern) | Recipient email, contract title, link | **No** |
| AWS Secrets Manager | Integration credentials | Credentials only | **No** |
| Amazon CloudWatch / AWS X-Ray | Logs, metrics, traces | Operational metadata. Code avoids logging document text | Yes |
| AWS Amplify Hosting | Web front end, server-side routes (incl. SOW drafting) | Browser traffic; SOW drafting prompts | `amplify.yml` |

**Action, Textract:** under AWS Service Terms, Amazon Textract may use content to improve the service unless the account opts out through an AWS Organizations AI services opt-out policy. **Apply the opt-out policy** for Textract (and all AI services) on the production account. Record the evidence for the HECVAT. Status: **not yet confirmed.**

### A2. OpenAI

| Item | Detail |
|---|---|
| Entity | OpenAI, L.L.C. (API platform) |
| Purpose | (1) document and clause extraction and labelling; (2) embeddings for search; (3) chat answers ("Ask Sonar"); (4) SOW drafting. **Not** used for Govern matrix review, workflow or reporting |
| Models | `gpt-4.1-mini` (chat), `text-embedding-3-small` (embeddings), configurable per task (`terraform/variables.tf`) |
| Data sent | Extracted document text and clause text. On the chat and drafting paths, emails, phones, SSNs, card numbers and IPs are pseudonymised first. On the extraction path, no redaction by default. See `SECURITY_OVERVIEW.md` §4 |
| Data not sent | Original files, user accounts, workflow and audit records, matrix grading |
| Transport | HTTPS from Lambda (`lambdas/shared/openai_client.py`) and from the Next.js server (`lib/sow/openai.ts`) |
| Training | OpenAI's API terms state that API inputs and outputs are not used to train models unless the customer opts in. Blue-IQ has not opted in. The code refuses to call any provider not registered as no-train (`guardrails.py`) |
| Retention | Up to 30 days for abuse monitoring under the default API terms. **Zero Data Retention is not yet enabled** (`AI_PROVIDER` defaults to `openai`, not `openai-zdr`) |
| Processing location | **To confirm.** No data-residency setting is configured. Ask OpenAI whether US data residency is available for the project and enable it if so |
| Contract | **No DPA signed yet.** API usage is under OpenAI's standard Business Terms |
| Account type | API platform (not ChatGPT). Tier and organisation verification: **to confirm** |

**Vendor actions before the OSU pilot (blocking):**
1. Sign OpenAI's Data Processing Addendum and file the executed copy.
2. Apply for Zero Data Retention on the API organisation/project used for production. Once approved, set `AI_PROVIDER=openai-zdr` (and `OPENAI_BASE_URL` if a dedicated endpoint is issued) in Terraform and Amplify.
3. Ask about US data residency and record the answer.
4. Move the API key from a Lambda environment variable to Secrets Manager (`OPENAI_SECRET_ARN` is already supported in code).
5. Correct the website text (`/security`, `/legal/privacy`, `/legal/subprocessors`). It now says text is "not retained by the provider" under "an enterprise data-handling agreement". That is true only after steps 1 and 2.

**Alternative for OSU:** if OSU policy rules out an external AI processor for some agreement classes, the options are: (a) exclude those agreements from AI processing by a classification flag (not yet built); (b) a Bedrock-hosted model in the same AWS account (allowlisted in `guardrails.py`, but the client is not implemented).

## B. Customer-authorised integrations (OSU systems)

These are enabled per tenant by a Govern admin. Credentials are stored per tenant in AWS Secrets Manager (`lambdas/shared/govern/secrets.py`). Status is from `GOVERN_ARCHITECTURE.md` and `lambdas/shared/govern/connectors.py`. Each must be confirmed as live and tested before it is listed in an OSU order form.

| Integration | Direction | Data exchanged | Who contracts with the vendor | Status |
|---|---|---|---|---|
| Huron Research Suite (Agreements) | Pull agreements; push findings and status back | Agreement records and documents; Govern findings | OSU | Designed, connector code present. Live connection **not yet** made |
| Workday (Financials / HCM) | Pull award, cost-centre and spend data | Spend and award fields | OSU | Designed, connector code present. **Not yet** live |
| DocuSign (Connect webhooks) | Inbound "signed" events | Envelope id, status | OSU | Webhook route designed with HMAC verification. **Not yet** live |
| Microsoft Teams (incoming webhook) / Microsoft 365 | Outbound alert cards | Contract title, stage, assignee, link | OSU | Webhook URL validated to Microsoft domains (`govern_api/handler.py`). **Not yet** live |

Govern sends no OSU data to these systems unless OSU configures them. Disconnecting deletes the per-tenant secret. Synced records in `govern-sync` expire after 400 days (TTL).

## Change notification

Blue-IQ will give OSU at least 30 days' written notice before adding or replacing a subprocessor in section A, with a right to object. This is consistent with the published DPA summary (`/legal/dpa`). It needs to be written into the OSU agreement.
