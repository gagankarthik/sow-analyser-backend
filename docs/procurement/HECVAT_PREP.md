# Blue-IQ Govern: HECVAT preparation

Prepared 2026-10-08. This is a working draft to fill in the official EDUCAUSE HECVAT spreadsheet. It is **not** the submitted questionnaire.

**Version.** EDUCAUSE now publishes the HECVAT as a single tool (HECVAT 4.x) that replaces the separate Lite, Full and On-Prem files and includes questions on AI. Many institutions still send HECVAT 3.x Full. **Ask the customer OTDI which version and which data-classification level apply to research and licensing agreements** (open question in `COMPETITIVE_ANALYSIS.md`). The topics below cover both versions; question IDs are mapped once the official file is in hand.

**Answer key.** Yes = implemented and evidenced in the repo. Partial = some of it is. No = not in place. TBC = not verifiable from the repo; to confirm. Every row that is not Yes has a gap and action.

## 1. Company / vendor

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Company profile, years in business, customers in higher ed | TBC | — | Vendor to supply: legal entity, address, size, references |
| Dedicated security officer or team | No | No named role in the repo | Name a security lead and a deputy; publish in pack |
| Cyber insurance | TBC | — | Confirm policy and limits; the customer will ask |
| Breaches in the last N years | TBC | — | Vendor declaration |
| Offshore staff / support location | TBC | — | Declare support locations; offer US-person-only support for export-control content if needed |
| Third-party assessment (SOC 2, ISO 27001) | **No** | None held. AWS infrastructure certifications cover the infrastructure layer only | SOC 2 Type I readiness, then Type I audit; see README |

## 2. Documentation and policies

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Architecture and data-flow diagram | Yes | `SECURITY_OVERVIEW.md`, `GOVERN_ARCHITECTURE.md`, `PROJECT_OVERVIEW_AND_SECURITY.md` | Correct stale items (processed bucket "versioned", AppSync, "Secrets Manager" for OpenAI key) |
| Written information security policy | No | None in repo | Write ISMS policy set (access control, change, IR, vendor, data classification, acceptable use) |
| Incident response plan | No | Outline only (`SECURITY_OVERVIEW.md` §14) | Write, approve, run tabletop |
| Business continuity / DR plan | No | — | Write; define RPO/RTO |
| Privacy policy | Yes | `/legal/privacy` | Correct the "not retained by provider" wording until ZDR is in place |
| DPA available | Partial | Summary at `/legal/dpa`; "signable DPA on request" | Prepare a signable DPA template; align with the customer data-protection terms |
| Employee security training, background checks | TBC | — | Vendor to implement and evidence |

## 3. IT accessibility

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| WCAG 2.1 AA conformance | Partial | Design and tokens built for AA (`ACCESSIBILITY_CONFORMANCE_PLAN.md` §2). Not tested with AT | Execute plan; no conformance claim until audited |
| VPAT / ACR available | No | — | ACR on VPAT 2.5 INT by Mar 2027 |
| Third-party accessibility audit | No | — | Commission Dec 2026–Jan 2027 |
| Accessibility testing in development | No | No a11y lint or axe in CI | Add jsx-a11y and Playwright + axe gate |
| Remediation process and timelines | Partial | Proposed SLA in plan §5 | Put SLA in the customer contract |
| Accessibility roadmap | Yes | Plan §6 timeline | — |

## 4. Application security

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Secure SDLC, code review | Partial | Tests gate deploy (`deploy.yml`); security regression tests; offline fakes block real calls | Require PR review on `main`; written SDLC |
| SAST / dependency / secret scanning | No | Not in workflow | Add CodeQL or Semgrep, Dependabot, pip-audit / npm audit, gitleaks |
| Third-party penetration test | **No** | Never performed | Commission before pilot; share executive summary |
| OWASP Top 10 controls: authN on all routes | Yes | JWT authorizer on every route (`lambda.tf`) | — |
| Authorisation checks server-side | Yes | `access.py` permission matrix; 404 for unknown ids | — |
| Input validation / injection | Partial | DynamoDB and OpenSearch via SDKs (no string-built queries seen); HTML sanitiser in front end (`lib/sanitize-html.ts`) | Confirm in pen test |
| Security headers (CSP, HSTS, X-Frame-Options) | Yes | `sow-analyzer/next.config.ts` | — |
| Rate limiting | Yes | API Gateway stage throttles (`lambda.tf`) | Add AWS WAF with managed rules |
| Secrets management | Partial | Integration secrets in Secrets Manager (`govern/secrets.py`) | Resolved: OpenAI key read from Secrets Manager (`OPENAI_SECRET_ARN`); deploy writes it after apply |
| Webhook security | Yes (design and code) | HMAC with 5-minute replay window | Pen-test it |

## 5. Authentication, authorisation and accounting

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| SSO via SAML 2.0 (Shibboleth / InCommon) | **No** | Planned (`SSO_SHIBBOLETH.md`) | Implement before pilot |
| MFA | TBC | Cognito pool managed outside Terraform | Confirm; with SSO, MFA is enforced at the the customer IdP |
| Role-based access control | Yes | owner/editor/viewer per project; govern-admin/reviewer/leader | `GOVERN_OPEN_ADMIN` defaults to false |
| Least privilege for service accounts | Partial | Per-function roles, resource-scoped (`iam.tf`) | Remove `appsync:GraphQL apis/*` from rag role (Govern roles in `govern.tf`) |
| Session management / token lifetime | TBC | Cognito defaults | Set 60-min ID/access tokens, revocation on (SSO plan §3) |
| Audit logging of user actions | Partial | Govern `govern-activity` append-only log (conditional put, never updated) | API access log lacks caller identity; add it. Confirm IAM put-only and PITR on activity table |
| Log retention | Partial | 30 days (CloudWatch) | Agree retention with the customer (often 1 year); export to S3 with lifecycle |
| Account provisioning / deprovisioning | Partial | Project invites by verified email; removal is effective on next request | With SSO, deprovisioning follows the customer group membership (≤ 60 min) |

## 6. Business continuity and disaster recovery

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Backups | Partial | DynamoDB PITR (35 days); S3 versioning on raw bucket | Enable PITR on Govern tables (TBC); version the processed bucket or document that it is rebuildable |
| Backup restore tested | No | — | Run and record a restore test |
| High availability | Partial | Managed multi-AZ services (S3, DynamoDB, Lambda) | OpenSearch is single-node: go to multi-AZ for prod |
| Multi-region DR | No | Single region | Define RPO/RTO; decide if cross-region backup is needed |
| Uptime SLA | No | Terms say "as is" | Define SLA for the customer order form |

## 7. Change management

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Infrastructure as code | Yes | Terraform (`terraform/`) | Govern in Terraform; bring the Cognito pool into Terraform |
| Automated testing before deploy | Yes | `deploy.yml` runs pytest before plan/apply | — |
| Approval gate for production | No | `apply -auto-approve` on push to `main` | GitHub environment protection with required reviewer for `prod` |
| Customer notification of changes | Partial | Subprocessor change notice promised (`/legal/subprocessors`) | Release notes process; maintenance windows |
| CI credentials | Partial | Long-lived AWS keys in GitHub secrets | Move to GitHub OIDC role |

## 8. Data

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Encryption at rest | Yes | S3 SSE-S3 AES-256; DynamoDB SSE; OpenSearch at-rest and node-to-node | Customer-managed KMS keys: optional, not configured |
| Encryption in transit | Yes | TLS-only bucket policies; OpenSearch TLS 1.2 minimum; HTTPS APIs | Correct site claim "TLS 1.3" |
| Data location | Partial | `us-east-2` default | Confirm production region in writing |
| Data segregation | Yes | Verified tenant claim; per-project ACL; search filtered in the query | Offer dedicated-account deployment if the customer requires |
| Data retention and deletion | Partial | Delete removes file, artefacts, index, rows; residual copies ≤ 35 days | Embedding cache not purged; Govern records on delete TBC; retention schedule settings not built |
| Data return at termination | Partial | Exports exist | Write and test a full-tenant export and certified deletion procedure |
| Sensitive data types (FERPA, HIPAA, CUI, export control) | No | No classification flag | Exclude from pilot by policy; build an "AI-blocked" classification flag |
| PII sent to third parties | Partial | Chat/drafting paths pseudonymised; extraction path not | State plainly; ZDR + DPA |

## 9. Datacenter

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Hosting provider and physical security | Yes (inherited) | AWS. Physical controls per AWS SOC 2 / ISO reports (AWS Artifact) | Download and attach AWS SOC 2 Type II bridge letter |
| Data center location | Partial | AWS `us-east-2` (the home state) default | Confirm prod region |

## 10. Firewalls, IDS/IPS, monitoring

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Network segmentation | Partial | No public storage; IAM-gated OpenSearch; no VPC | Move OpenSearch and Lambdas into a private VPC |
| WAF | No | — | AWS WAF on API (managed rule groups, rate rules) |
| IDS / threat detection | TBC | Not in Terraform | Enable GuardDuty, CloudTrail (all regions, log file validation), Security Hub |
| Alerting | No | No CloudWatch alarms in Terraform | Alarms on 5xx, DLQ depth, auth failures, AI audit anomalies |

## 11. Policies (vendor)

| Topic | Answer | Gap / action |
|---|---|---|
| Acceptable use, access review, password and key rotation, vendor management, data classification | No | Write as part of SOC 2 readiness |
| Vulnerability disclosure | Yes | `security@blue-iq.ai` on `/security`; add a `security.txt` |
| Patch management SLA | No | Adopt: Critical 7 d / High 30 d / Medium 90 d |

## 12. Third parties / subprocessors

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Subprocessor list | Yes | `SUBPROCESSORS.md` | Publish the same list on `/legal/subprocessors` (it now names "our LLM provider" without naming OpenAI) |
| DPAs with subprocessors | Partial | AWS DPA (standard) | **OpenAI DPA not signed** |
| Subprocessor AI-training opt-outs | Partial | OpenAI API: no training by default | **Textract AI services opt-out policy not confirmed** |
| Customer-authorised integrations | Yes | Huron, Workday, DocuSign, Teams: per-tenant, the customer-configured | — |

## 13. Artificial intelligence

| Topic | Answer | Evidence / response | Gap / action |
|---|---|---|---|
| Does the product use AI / ML? | Yes | OpenAI for extraction, clause labelling, embeddings, chat, drafting | — |
| Which model and provider; hosted where | Partial | OpenAI API, `gpt-4.1-mini`, `text-embedding-3-small` | Processing region TBC |
| Is institutional data used to train models? | No | OpenAI API terms; provider allowlist refuses non-no-train providers (`guardrails.py`); no fine-tuning code | Make contractual via DPA |
| Data retention by the AI provider | Partial | Up to 30 days (abuse monitoring) | **Enable ZDR** |
| Can AI be disabled or limited by the institution? | Partial | Matrix review and workflow need no AI; provider is configurable | Per-tenant and per-classification AI switch not built |
| Human oversight of AI outputs | Yes | AI labels; deterministic rules grade; people decide; append-only log | — |
| Explainability | Yes | Each finding names its matrix rule and version; source quote | — |
| Accuracy, bias and testing | Partial | Coverage checks, money re-check pass, regression tests | the customer gold-label set (~50 agreements) and published accuracy figures |
| Prompt-injection / output safety | Partial | Output validation on chat path; answers restricted to permitted docs | Pen test to include prompt injection via uploaded documents |
| AI use statement | Yes | `AI_USE_STATEMENT.md` | — |
