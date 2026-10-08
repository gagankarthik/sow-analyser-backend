# Blue-IQ Govern: OSU procurement and security review pack

Prepared 2026-10-08 for The Ohio State University's procurement and IT security review (OTDI, Digital Accessibility Services, Legal Affairs, IAM). Background is in `../COMPETITIVE_ANALYSIS.md` §8.2 (procurement objections).

**Honesty rule for this pack.** Blue-IQ holds **no** SOC 2 report, ISO certification or penetration test, has **no** published VPAT/ACR or accessibility audit, and has **no** signed DPA with OpenAI. Where an item is planned, the pack says "planned" or "not yet". Facts come from the code and Terraform in this repo and in `sow-analyzer`. What could not be verified is marked "to confirm".

## Contents and status

| # | Item | File | Status | Owner | Next step |
|---|---|---|---|---|---|
| 1 | Pack index and action list | `README.md` | Draft | Security lead | Assign named owners; review weekly |
| 2 | Security overview (architecture, data flow, controls) | `SECURITY_OVERVIEW.md` | Draft | Security lead + backend lead | Resolve "to confirm" items (prod region, account-level CloudTrail/GuardDuty, MFA) |
| 3 | HECVAT preparation | `HECVAT_PREP.md` | Draft | Security lead | Get OSU's HECVAT version and data classification; transfer into the official file |
| 3a | HECVAT (official EDUCAUSE file, completed) | — | Not started | Security lead | Start once the version is confirmed |
| 4 | Accessibility conformance plan | `ACCESSIBILITY_CONFORMANCE_PLAN.md` | Draft | Front-end lead | Add axe CI gate; internal AT pass |
| 4a | ACR on VPAT 2.5 INT | — | Not started | Front-end lead | After third-party audit (target Mar 2027) |
| 4b | Third-party WCAG 2.1 AA audit and letter | — | Requires vendor action | Product lead | Select auditor; book Jan 2027 |
| 5 | Subprocessor list | `SUBPROCESSORS.md` | Ready (content); public page out of date | Legal / security lead | Update `/legal/subprocessors` to name OpenAI and AWS services |
| 5a | OpenAI DPA (signed) | — | Requires vendor action | CEO / legal | Execute OpenAI DPA |
| 5b | OpenAI Zero Data Retention | — | Requires vendor action | CEO / backend lead | Apply; then set `AI_PROVIDER=openai-zdr` |
| 5c | AWS AI services opt-out (Textract) | — | Requires vendor action | Backend lead | Apply AWS Organizations opt-out policy; record evidence |
| 6 | Shibboleth / InCommon SSO guide | `SSO_SHIBBOLETH.md` | Draft (feature not yet built) | Backend lead | Send attribute and metadata request to OSU IAM; build in 1–2 sprints |
| 7 | AI use statement | `AI_USE_STATEMENT.md` | Ready | Product lead | Re-issue after ZDR + DPA, to remove the retention caveat |
| 8 | Penetration test summary | — | Not started | Security lead | Commission an external test (include prompt injection) before the pilot |
| 9 | SOC 2 | — | Not started | CEO / security lead | SOC 2 Type I readiness (policies, controls, tooling); auditor selection. Say "planned" to OSU, nothing more |
| 10 | Incident response plan | — (outline in `SECURITY_OVERVIEW.md` §14) | Not started | Security lead | Write, approve, tabletop |
| 11 | BC/DR plan with RPO/RTO | — | Not started | Backend lead | Define targets; run a restore test |
| 12 | Signable DPA (Blue-IQ to OSU) | — (summary at `/legal/dpa`) | Draft | Legal | Draft against OSU's data-protection terms |
| 13 | Exit and data-export procedure | — | Not started | Backend lead | Document and test full-tenant export and certified deletion |

## Corrections needed to current public claims

The public pages (`sow-analyzer/app/security/page.tsx`, `app/legal/documents.ts`) state things that the code does not support yet. OSU reviewers will read these pages. **Fix them before sending the pack.**

| Page | Current wording | Problem | Replace with |
|---|---|---|---|
| `/security`, `/legal/privacy`, `/legal/subprocessors` | AI text "is not retained by the provider" under "an enterprise data-handling agreement" | ZDR not enabled; no OpenAI DPA signed | "Not used to train models. Retained by the provider for up to 30 days for abuse monitoring; zero-retention is being enabled." |
| `/legal/security` | "Aligned to SOC 2, GDPR, and HIPAA controls, plus WCAG 2.1 AA and ADA... Current reports are available under NDA." | No reports exist; no audit; HIPAA not assessed | "We are preparing for SOC 2 and a third-party WCAG 2.1 AA audit. Our security overview is available on request." |
| `/legal/security` | "TLS 1.3" | Minimum enforced is TLS 1.2 | "TLS 1.2 or higher" |
| `/security`, `/legal/security` | "MFA can be enforced per tenant" | Not verifiable; pool outside Terraform | Confirm, or say "MFA through your institution's SSO" once SAML is live |
| `/security`, `/legal/privacy` | Deleting a document removes everything | Backups and residual copies persist up to 35 days | Add "Backup copies expire within 35 days." |

## Engineering gaps found while preparing this pack

From the code and Terraform (details and file references in `SECURITY_OVERVIEW.md` and `HECVAT_PREP.md`):

1. The OpenAI API key is a Terraform variable passed into Lambda environment variables, so it sits in Terraform state. Secrets Manager is supported in code (`OPENAI_SECRET_ARN`) but not wired.
2. `AI_PROVIDER` is not set, so it defaults to `openai` (not zero-retention).
3. PII pseudonymisation runs only on the chat and drafting paths, not on extraction.
4. ~~Govern infrastructure not in Terraform~~ — resolved 2026-10-08: `terraform/govern.tf` defines the 5 tables (customer-managed KMS key with rotation, PITR on contracts/activity/config), queues + DLQs, event bus, schedules, secrets and per-function least-privilege roles (activity writers are PutItem-only).
5. ~~`GOVERN_OPEN_ADMIN` defaults to `true`~~ — resolved: defaults to `false`; only the `dev` stage can enable it (`var.govern_open_admin`).
6. No WAF, CloudTrail, GuardDuty or alarms are in Terraform. OpenSearch is single-node and outside a VPC.
7. CI uses long-lived AWS keys and auto-applies to the target stage with no approval gate. There is no SAST or dependency scanning.
8. The API access log has no caller identity. Log retention is 30 days.
9. Embedding-cache vectors are not purged on document delete.
10. The front end has no automated accessibility tests.

## Top vendor actions before OSU review (priority order)

1. **Sign the OpenAI DPA and enable Zero Data Retention.** Then set `AI_PROVIDER=openai-zdr`, move the key to Secrets Manager, apply the AWS AI-services opt-out for Textract, and correct the public "not retained" claims.
2. **Correct the public security and legal pages** (table above), so that nothing claims SOC 2, reports, audits or WCAG conformance that do not exist.
3. **Commission an external penetration test, and start SOC 2 Type I readiness.** Write the IR, BC/DR and core security policies. Enable CloudTrail, GuardDuty, WAF and alarms, and add a production approval gate in CI.
4. **Build Shibboleth/InCommon SAML SSO with role mapping.** Send OSU IAM the metadata and attribute request now. (`GOVERN_OPEN_ADMIN` now defaults to false and Govern is in Terraform; the Cognito user pool is still outside Terraform.)
5. **Start the accessibility programme.** Add an axe CI gate, do an internal NVDA/JAWS/VoiceOver-iPad pass, book a third-party WCAG 2.1 AA audit, and commit to an ACR (VPAT 2.5 INT) by March 2027 with a contractual remediation SLA.

## Questions to ask OSU

- Which HECVAT version, and what data-classification level applies to research and licensing agreements?
- IdP metadata, attribute release process, and Grouper groups for Govern roles (see `SSO_SHIBBOLETH.md` §5).
- Preferred accessibility auditors, and whether OSU's MDAS adds requirements beyond WCAG 2.1 AA.
- Records-retention schedule for agreements and activity logs.
- Whether any agreement classes (export-controlled, CUI, clinical/HIPAA) must be excluded from external AI processing.
