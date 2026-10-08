# Govern runbook

Operating Blue-IQ Govern: deploy, secrets, email, roles, OSU single sign-on, the
demo data, and what to do when an alarm fires. Architecture: `GOVERN_ARCHITECTURE.md`.
REST contract: `GOVERN_API.md`. Infrastructure: `terraform/govern.tf`.

## 1. Deploy

Govern ships with the rest of the backend: push to `main` (or run the workflow
with a stage). `.github/workflows/deploy.yml` runs the offline tests, builds the
shared layer (which must contain `shared/govern/`; the build fails otherwise),
then `terraform plan` / `apply`. Terraform zips each `lambdas/govern_*` folder
itself.

What `terraform/govern.tf` creates, per stage (`<p>` = `blue-iq-sow-<stage>`):

| Kind | Names |
|---|---|
| DynamoDB (KMS CMK; PITR on contracts / activity / config) | `<p>-govern-contracts`, `-activity` (stream), `-config`, `-sync` (TTL), `-metrics` (TTL) |
| Lambdas (logs kept 365 days) | `<p>-govern-api`, `-intake`, `-stream`, `-notifier`, `-sweeper`, `-connectors`, `-webhooks` |
| EventBridge | bus `<p>-platform`; rules `-document-analysed`, `-notify`, `-connectors` |
| SQS | `<p>-govern-intake`, `-notify`, `-connectors` + a DLQ each, plus `-stream-dlq` |
| Scheduler | sweeper hourly; connectors daily 06:00 America/New_York |
| Secrets Manager (empty) | `<p>-govern/teams`, `/docusign`, `/huron`, `/workday`, `/m365`; `<p>/openai-api-key` |
| KMS | `alias/<p>-govern` (rotation on) |
| Alarms | every DLQ not empty; every Govern Lambda `Errors > 0` (optional SNS: `alarm_sns_topic_arn`) |

Terraform variables that matter for Govern:

| Variable | Default | Notes |
|---|---|---|
| `govern_open_admin` | `false` | Honoured **only** when `stage = dev`. The workflow sets it `true` for dev. Staging / prod always need the Cognito groups (§4). |
| `notify_from_email` | `""` | Sender for alert email. Set to create the SES identity (§3). Empty = email off. |
| `app_base_url` | `https://govern.blue-iq.ai` | Links in alerts. |
| `ai_provider` | `openai` | `openai-zdr` once Zero Data Retention is approved (guardrails fail closed on anything else). |
| `govern_log_retention_days` | `365` | Lambda and API access logs. Cannot be set below 365. |
| `openai_api_key` | `""` | **Deprecated.** Leave empty: a key here would sit in Terraform state. Use the secret (§2). |

First deploy into an existing stage: no migration is needed. The first
`GET /contracts` turns each READY library document into a contract, and the
hourly sweeper re-enqueues any READY document that has none (§7).

## 2. Secrets (values are never in Terraform)

Every Govern secret is a JSON object. Per-workspace values live under `tenants`.
`<tenantId>` is the workspace id: the `custom:tenantId` claim, else `u-<sub>`.

```bash
P=blue-iq-sow-dev
# OpenAI key: the deploy workflow writes it from the OPENAI_API_KEY GitHub secret.
# By hand:
aws secretsmanager put-secret-value --secret-id "$P/openai-api-key" --secret-string 'sk-...'

# DocuSign: the connector keys AND the Connect HMAC key the webhook verifies with.
aws secretsmanager put-secret-value --secret-id "$P-govern/docusign" --secret-string '{
  "tenants": {"<tenantId>": {"integrationKey": "...", "userId": "...", "privateKey": "...", "connectHmacKey": "..."}}
}'
# A top-level "hmacKey" (or "hmacKeys": [...] while rotating) is also accepted
# for an account-wide Connect configuration.
```

Huron `{clientId, clientSecret}`, Workday `{clientId, clientSecret, refreshToken}`
and Microsoft 365 `{clientId, clientSecret}` are normally set through the admin
screen (`PUT /integrations/{id}` with `credentials`), which writes the secret and
never returns it. The Teams incoming-webhook URL is set the same way
(`PUT /workflow/settings` with `teamsWebhookUrl`). Without credentials every
connector run is a logged **dry run** that reports what it would do.

DocuSign Connect: URL = output `docusign_webhook_url`; enable "Include HMAC
signature", JSON format, event `envelope-completed`, and add an envelope custom
text field `contractId` = the Govern contract id when sending.

## 3. Email (SES)

1. Set `notify_from_email` (for example `govern-noreply@osu.edu`) and deploy:
   Terraform creates the SES email identity.
2. Confirm the verification email (or, for a domain, publish the DKIM records SES shows).
3. A new SES account is in the **sandbox**: it only sends to verified addresses.
   Request production access in the SES console before inviting real users.
4. Turn alerts on per workspace: `PUT /workflow/settings`
   `{"notifications": {"email": true, "events": {...}}}`.

Each Govern event is notified at most once (a marker per activity entry). If
every channel fails, the message is retried and then lands in the notify DLQ.

## 4. Roles (Cognito groups)

| Group | Can |
|---|---|
| `govern-admin` | everything, plus the matrix, routing / settings and integrations |
| `govern-reviewer` | act on contracts they may edit (document owner / project editor) |
| `govern-leader` | view, comment, and approve for an office they are listed under in settings |

```bash
POOL=us-east-2_xxxxxxxxx
for g in govern-admin govern-reviewer govern-leader; do
  aws cognito-idp create-group --user-pool-id "$POOL" --group-name "$g"
done
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username dana@osu.edu --group-name govern-reviewer
```

Groups arrive in the ID token's `cognito:groups` claim at the next sign-in.
Contract visibility never comes from a group: it is the document's visibility
(owner, or a member of a project that holds it).

## 5. OSU single sign-on (SAML)

OSU signs in through its identity provider (Shibboleth / `login.osu.edu`, or
Microsoft Entra ID). Cognito is the service provider; the app keeps using
Cognito tokens, so nothing in the API changes.

1. In the user pool, add a **SAML identity provider** `OSU`, using OSU's IdP
   metadata URL.
2. Give OSU the SP details: entity ID `urn:amazon:cognito:sp:<pool-id>` and ACS URL
   `https://<cognito-domain>/saml2/idpresponse`.
3. Attribute mapping: `email` ← `mail` (or `urn:oid:0.9.2342.19200300.100.1.3`),
   `name` ← `displayName`, `given_name` / `family_name`. Ask OSU to release `mail`
   and mark it verified (the API only trusts verified email for project membership).
4. Enable the `OSU` provider on the app client and set the callback / sign-out URLs
   of the web app.
5. Roles: Cognito does not turn SAML group attributes into `cognito:groups`
   by itself. Either assign groups as in §4 (simplest for a pilot), or add a
   pre-token-generation Lambda that maps an OSU entitlement attribute (for example
   `eduPersonEntitlement`) to the three Govern groups. **To confirm with OSU**
   which attribute carries the role.
6. Set the `custom:tenantId` attribute for OSU users (an admin attribute) so they
   share the OSU workspace; otherwise each user gets a private `u-<sub>` workspace.

## 6. Demo data (`samples/osu`)

The files, their expected matrix results and the demo story are described in
`samples/osu/README.md`. Sign in as a Govern admin of the demo workspace
(`API` = output `documents_api_url`, `TOKEN` = your ID token), then:

```bash
# 1. Load the OSU-style matrix: one import per agreement type in the sheet
#    (rows for the other type are skipped with a reason).
for t in license sponsored_research; do
  jq -n --arg t "$t" --rawfile csv samples/osu/osu-review-matrix.csv '{agreementType: $t, csv: $csv, mode: "replace"}' |
    curl -s -X POST "$API/matrix/import" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @-
done

# 2. Upload each agreement and open its contract straight away (intake at upload).
curl -s "$API/documents/upload-url?filename=01-exclusive-license-v1.txt&docType=LICENSE" -H "Authorization: Bearer $TOKEN"
curl -s -X PUT --upload-file samples/osu/01-exclusive-license-v1.txt "<uploadUrl>"
curl -s -X POST "$API/contracts" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"docId": "<docId>"}'
```

Huron agreement numbers, Workday references, PI and department are read from the
header lines of each sample. For the demo story, send the licence back, then upload
`01-exclusive-license-v2-revised.txt` through `GET /contracts/{id}/revision-upload-url`:
intake links it, rescores it (clean) and returns the contract to review.

## 7. Capture never misses a contract

* Intake is idempotent and every SQS queue has a DLQ with an alarm.
* If the pipeline cannot publish "Document Analysed" it still succeeds (the
  document is READY); the **hourly reconciliation** in the sweeper re-enqueues
  every READY document with no contract and records FAILED or stalled analyses.
* `GET /reports/capture` lists fields still unknown per contract and every missed
  document with the reason, plus `lastReconciledAt`.

## 8. When an alarm fires

| Alarm | Meaning | Action |
|---|---|---|
| `…-govern-intake-dlq-not-empty` | a document failed intake 5 times | Read the Lambda log (`docId` is logged). Fix, then redrive: `aws sqs start-message-move-task --source-arn <dlq-arn>` |
| `…-govern-notify-dlq-not-empty` | email and Teams both failed | Check SES sending status / the Teams URL; redrive |
| `…-govern-connectors-dlq-not-empty` | Huron push-back failed | See `GET /integrations/sync-log`; redrive after fixing credentials |
| `…-govern-stream-dlq-not-empty` | activity entries not published / counted | Redrive is not possible for stream records: run `python scripts/backfill_govern_aggregates.py --stage <s> --apply` |
| `…-govern-<fn>-errors` | a Lambda raised | Read its log group (`/aws/lambda/<p>-govern-<fn>`) |

## 9. Audit

* The activity table is append-only: every writer's role allows `PutItem` only
  on it, and each write is conditional on the key not existing.
* API access logs (`/aws/apigateway/<p>-docs-api`, 365 days) record the
  verified JWT `sub`, route, status and request id of every call.
* **CloudTrail data events.** Terraform does not create a trail (the account's
  organisation trail is out of scope). For an OSU audit trail of who read or
  wrote Govern data at the AWS level, enable DynamoDB data events for the five
  `…-govern-*` tables and Secrets Manager / KMS management events on the account's
  existing trail.
* Trend aggregates can be rebuilt at any time from the activity log:
  `python scripts/backfill_govern_aggregates.py --stage <s>` (dry run) then `--apply`.
