# Govern e-signature

Blue-IQ Govern's own electronic signature: a reviewer prepares a contract's PDF
for signature (signers, signing order, fields placed on the pages), the signers
sign through a private link with no account, and Govern seals the finished
document with a PAdES signature from a key held in AWS KMS. It replaces the
DocuSign round trip for organizations that want signing inside Govern; the
DocuSign connector and manual signing stay available.

Everything here is Govern's own code on Govern's own AWS stack. No third-party
e-signature product, branding or code is used.

## 1. What a signature is worth

Electronic signatures are valid under the US ESIGN Act and UETA when the record
shows **intent** (the signer chose to sign), **consent** to do business
electronically, **attribution** (who signed, how they were reached) and an
**unaltered record**. Govern captures each:

| Requirement | How Govern records it |
|---|---|
| Consent | The signer ticks the electronic-records consent before any field can be filled; `consentAt` is stored. |
| Intent | Signing is an explicit "Finish" after every required field is filled; a drawn or typed signature image is captured. |
| Attribution | A link sent only to the signer's email (a 32-byte token, stored only as a SHA-256 hash); IP address, user agent and timestamps of view, consent and signature. |
| Integrity | The final PDF is sealed with a PAdES (ETSI.CAdES.detached) signature over the whole file from a KMS key (FIPS 140 validated HSMs), optionally with an RFC 3161 timestamp. SHA-256 of the source and the sealed file are stored and printed. |
| Record | A certificate-of-completion page is appended: envelope id, document hashes, and per signer the email, the events with times, IP and user agent, and the signature image. |

EU "advanced" or "qualified" signatures (eIDAS) need identity proofing and a
qualified certificate; that is out of scope.

## 2. Lifecycle

```
envelope   draft ──send──► sent ──all signers signed──► sealing ──► completed
                              │                             └──(seal error)──► failed (retried by the queue)
                              ├── a signer declines ──► declined
                              ├── reviewer voids ─────► voided
                              └── expiry passes ──────► expired

recipient  pending ──their turn──► sent ──► viewed ──► signed
                                           └──────────► declined
contract   ready_to_sign ──send──► out_for_signature ──completed──► signed
                                       └── declined / voided / expired ──► ready_to_sign
```

* **Signing order.** `sequential`: recipients sign in `order` (1, 2, …); a
  recipient's link is sent when everyone before them has signed. `parallel`:
  every signer gets a link at once. Recipients with role `cc` sign nothing and
  receive the completed document.
* **Expiry** (1–60 days, default 14) and **reminders** (every 1–14 days, or off)
  run in govern-sweeper (hourly).
* **Completion** queues the envelope on the seal queue; govern-seal builds the
  final PDF, seals it, stores it, moves the contract to `signed` through the
  same `mark_signed` path as a reviewer's click, and emails every recipient the
  sealed PDF.

## 3. Storage

```
govern-contracts  CON#<contractId> / ENV#<envelopeId>     the envelope (entityType ENVELOPE)
govern-config     SIGNER#<sha256(token)> / LINK           a signer link → tenant, contract, envelope, recipient
processed bucket  esign/<tenant>/<contractId>/<envelopeId>/source.pdf
                                                  …/pages/<n>.png        page images for the editor and signers
                                                  …/sig/<recipientId>-<fieldId>.png
                                                  …/sealed.pdf
govern-config     SIGNING / CERT#<keyId>                  the signing certificate (generated once per KMS key)
```

Envelope item:

```
Envelope {
  envelopeId, contractId, tenantId, rev,
  status: "draft" | "sent" | "sealing" | "completed" | "declined" | "voided" | "expired" | "failed",
  name, subject, message, signingOrder: "sequential" | "parallel",
  expiresInDays, expiresAt, reminderEveryDays (0 = off), lastReminderAt,
  source: { docId | null, filename, key, sha256, pageCount, pages: [{ width, height }] (points) },
  recipients: Recipient[], fields: Field[],
  sealed: { key, sha256, sealedAt, keyId, timestamped } | null,
  createdAt, createdBy, sentAt, sentBy, completedAt, voidedAt, voidedBy, voidReason, failure
}
Recipient { id ("r1"…), name, email, role: "signer" | "cc", order, status, sentAt, viewedAt, consentAt,
            signedAt, declinedAt, declineReason, ip, userAgent, linkExpiresAt, tokenHash (never returned) }
Field { id ("f1"…), recipientId, type: "signature" | "initials" | "name" | "email" | "date" | "text" | "checkbox",
        page (0-based), x, y, w, h (fractions of the page, origin top-left), required, label, value }
```

## 4. Signing key and certificate

* `aws_kms_key` `KEY_SPEC = RSA_3072`, `KEY_USAGE = SIGN_VERIFY`. The private key
  never leaves KMS; govern-seal calls `kms:Sign` on a SHA-256 digest
  (`RSASSA_PKCS1_V1_5_SHA_256`).
* The certificate is generated on first use: an X.509 v3 certificate for the KMS
  public key, subject "Blue-IQ Govern Document Signing", key usage
  digitalSignature + nonRepudiation, valid 10 years, **signed by the KMS key
  itself**, and cached in govern-config. No certificate authority is needed and
  nothing costs more than the KMS key ($1/month + requests).
* A self-issued certificate makes the signature cryptographically valid and
  tamper-evident, but Adobe Reader shows "validity unknown" until the reader
  trusts it. To get Adobe's green check, buy a document-signing certificate from
  an AATL member for the same KMS key (CSR from the public key) and store it as
  the certificate; nothing else changes.
* `SIGNING_TSA_URL` (default `http://timestamp.digicert.com`) adds an RFC 3161
  timestamp; empty turns timestamps off.

## 5. API (govern-api, JWT; `edit` permission on the contract)

| Method | Path | |
|---|---|---|
| GET | `/contracts/{id}/envelopes` | `{envelopes: EnvelopeSummary[]}` newest first |
| POST | `/contracts/{id}/envelopes` | `{source?: {docId} \| {uploadKey}}` → 201 `EnvelopeDetail` (draft; pages rendered). Default source: the contract's current document, which must be a PDF. |
| POST | `/contracts/{id}/envelopes/upload-url` | `{filename: *.pdf, size ≤ 25 MB}` → `{uploadUrl, headers, uploadKey, expiresIn}` (sign a different PDF) |
| GET | `/contracts/{id}/envelopes/{envId}` | `EnvelopeDetail` |
| PUT | `/contracts/{id}/envelopes/{envId}` | draft only: `{name?, subject?, message?, signingOrder?, expiresInDays?, reminderEveryDays?, recipients?, fields?}` → `EnvelopeDetail` |
| POST | `/contracts/{id}/envelopes/{envId}/send` | draft → sent; contract → `out_for_signature` (provider `govern`) |
| POST | `/contracts/{id}/envelopes/{envId}/void` | `{reason}`; contract → `ready_to_sign` |
| POST | `/contracts/{id}/envelopes/{envId}/recipients/{rid}/resend` | a fresh link to a recipient whose turn it is |
| GET | `/contracts/{id}/envelopes/{envId}/download?file=sealed\|source` | `{url, filename}` (presigned, 5 min) |
| DELETE | `/contracts/{id}/envelopes/{envId}` | draft only |

`EnvelopeDetail` = the envelope without token hashes or storage keys, plus
`pages: [{url, width, height}]` (presigned images, 15 min) and `canEdit`.

Errors: 400 `bad_request`, 404 `not_found`, 409 `invalid_transition`
(not a draft; contract not `ready_to_sign`; another envelope already out),
409 `not_pdf`, 503 `signing_unavailable` (no KMS key configured).

## 6. Public signer routes (govern-signing, no JWT; the token is the credential)

| Method | Path | |
|---|---|---|
| GET | `/sign/{token}` | `SignerView`; records the first view (IP, user agent) |
| POST | `/sign/{token}/complete` | `{consent: true, values: {fieldId: string}, signature?: {image: "data:image/png;base64,…", kind: "drawn" \| "typed"}, initials?: {image}}` → `{status}` |
| POST | `/sign/{token}/decline` | `{reason}` → `{status: "declined"}` |
| GET | `/sign/{token}/download` | `{url, filename}` once the envelope is completed |

```
SignerView { documentName, organizationName, senderName, message,
             recipient: {name, email}, envelopeStatus,
             status: "ready" | "waiting" | "signed" | "declined" | "closed",
             pages: [{url, width, height}], fields: [{id, type, page, x, y, w, h, required, label}] (this signer's),
             consentText, canDownload, expiresAt }
```

Unknown, malformed, revoked and expired tokens all get one identical 404.
Responses carry `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and
`X-Robots-Tag: noindex`. Request bodies are capped (signature images ≤ 300 KB).

## 7. Lambdas and infrastructure (terraform/govern_esign.tf)

* **govern-signing** (public): GetItem/UpdateItem on `SIGNER#*`, contract
  read/update, activity writes, S3 get/put under `esign/`, SQS send to the seal
  queue, SES send (next signer's invitation).
* **govern-seal** (SQS worker, 1024 MB, 120 s): S3 under `esign/`, `kms:Sign` and
  `kms:GetPublicKey` on the signing key, contract update, SES SendRawEmail
  (sealed PDF attached up to 7 MB, else a link).
* **govern-api**: envelope routes; S3 under `esign/`, raw-bucket read (source
  document), SES (invitations).
* **govern-sweeper**: reminders and expiry.
* KMS key `alias/<prefix>-govern-signing`, SQS `govern-seal` + DLQ (3 tries).
* Layer adds `pyhanko`, `pypdf`, `reportlab` (MIT / BSD).
