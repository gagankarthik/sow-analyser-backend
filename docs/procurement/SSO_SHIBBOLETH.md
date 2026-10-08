# Blue-IQ Govern: federating with OSU Shibboleth / InCommon (SAML 2.0)

Prepared 2026-10-08. **Status: not yet implemented.** This is the implementation plan and the request to OSU IT. The architecture already assumes Cognito SAML federation (`GOVERN_ARCHITECTURE.md` §2, delivery plan item 14).

## 1. Target design

```
OSU user ──► Govern web app ──► Cognito managed login (/oauth2/authorize, PKCE)
                                     │  SAML AuthnRequest (signed)
                                     ▼
                           OSU Shibboleth IdP (InCommon; bridges to Entra ID via Cooperative Authentication)
                                     │  SAML Response (signed, encrypted)
                                     ▼
              Cognito: attribute mapping ─► Pre sign-up trigger (auto-verify email)
                                         ─► Pre token generation trigger (eduPerson ─► govern-* groups, tenant)
                                     │  ID token (JWT)
                                     ▼
              API Gateway JWT authorizer (unchanged) ─► access.py / govern_api roles (unchanged)
```

The backend authorisation code does not change. It already reads `sub`, `email`/`email_verified`, `custom:tenantId` and `cognito:groups` from the verified JWT (`shared/access.py`, `shared/auth.py`, `govern_api/handler.py`).

## 2. Attribute mapping

| OSU attribute (SAML name, URI format) | Cognito attribute | Used for |
|---|---|---|
| `eduPersonPrincipalName`, `urn:oid:1.3.6.1.4.1.5923.1.1.1.6` | `custom:eppn` (new) | Stable person identifier; Cognito username source (NameID) |
| `mail`, `urn:oid:0.9.2342.19200300.100.1.3` | `email` | Project membership and notifications (`access.py` requires a verified email) |
| `displayName`, `urn:oid:2.16.840.1.113730.3.1.241` | `name` | Name shown on actions and in the activity log |
| `givenName` `urn:oid:2.5.4.42` / `sn` `urn:oid:2.5.4.4` | `given_name` / `family_name` | Fallback name |
| `eduPersonScopedAffiliation`, `urn:oid:1.3.6.1.4.1.5923.1.1.1.9` | `custom:affiliation` (new) | Eligibility (e.g. `staff@osu.edu`, `faculty@osu.edu`) |
| `isMemberOf`, `urn:oid:1.3.6.1.4.1.5923.1.5.1.1` (Grouper) | `custom:groups` (new) | Govern role mapping |
| NameID | — | Persistent or transient. Request `persistent` or use eppn |

**Role mapping** (done in the pre-token trigger; the group names are placeholders for OSU to supply):

| OSU group (`isMemberOf`), to be named by OSU | Govern group | Govern rights |
|---|---|---|
| `<osu-grouper-path>:govern-admins` | `govern-admin` | Matrix, routing, settings, integrations |
| `<osu-grouper-path>:govern-reviewers` | `govern-reviewer` | Review actions |
| `<osu-grouper-path>:govern-leaders` | `govern-leader` | View, comment, approve when routed |
| none of the above | no Govern group | **Deny at sign-in** (recommended): the trigger raises an error so the user cannot obtain a token |

Notes:
- Cognito custom attributes hold at most 2,048 characters. Ask OSU to release only the Govern-relevant `isMemberOf` values, or an `eduPersonEntitlement` (`urn:oid:1.3.6.1.4.1.5923.1.1.1.7`) value per role.
- Set `GOVERN_OPEN_ADMIN=false` for the OSU deployment. Otherwise a user with no Govern group is treated as admin (`shared/config.py`). With `false`, an ungrouped user defaults to `reviewer` (`govern_api/handler.py`) and still sees only documents in projects they belong to. Denying ungrouped users in the trigger closes this completely.
- All OSU users must share one workspace. The pre-token trigger sets `custom:tenantId = "osu"` for identities whose provider is `OSU`.
- Project membership is keyed on verified email. Federated users are not automatically `email_verified`. The pre sign-up trigger sets `autoVerifyEmail = true` for the `PreSignUp_ExternalProvider` source. The email comes from the university IdP, so this is acceptable.

## 3. Terraform

The user pool is currently created outside Terraform (`var.cognito_user_pool_id`). Recommended first step: `terraform import` the pool into a managed `aws_cognito_user_pool` resource, so that the schema (custom attributes) and Lambda triggers are under change control. Until then, add the custom attributes with `aws cognito-idp add-custom-attributes` and attach the triggers with `update-user-pool`.

```hcl
# terraform/sso_osu.tf  (proposed)

variable "osu_idp_metadata_url" {
  description = "OSU Shibboleth IdP metadata URL (from OSU IAM), or use MetadataFile."
  type        = string
  default     = ""
}

variable "govern_app_url" {
  type    = string
  default = "https://govern.blue-iq.ai"
}

resource "aws_cognito_user_pool_domain" "govern" {
  domain       = "${local.prefix}-govern"          # or a custom domain + ACM cert
  user_pool_id = var.cognito_user_pool_id
}

resource "aws_cognito_identity_provider" "osu" {
  user_pool_id  = var.cognito_user_pool_id
  provider_name = "OSU"
  provider_type = "SAML"

  provider_details = {
    MetadataURL             = var.osu_idp_metadata_url
    IDPSignout              = "false"
    IDPInit                 = "false"       # SP-initiated only
    RequestSigningAlgorithm = "rsa-sha256"  # sign AuthnRequests
    EncryptedResponses      = "true"        # OSU encrypts assertions to Cognito's cert
  }

  attribute_mapping = {
    email                = "urn:oid:0.9.2342.19200300.100.1.3"
    name                 = "urn:oid:2.16.840.1.113730.3.1.241"
    given_name           = "urn:oid:2.5.4.42"
    family_name          = "urn:oid:2.5.4.4"
    "custom:eppn"        = "urn:oid:1.3.6.1.4.1.5923.1.1.1.6"
    "custom:affiliation" = "urn:oid:1.3.6.1.4.1.5923.1.1.1.9"
    "custom:groups"      = "urn:oid:1.3.6.1.4.1.5923.1.5.1.1"
  }

  idp_identifiers = ["osu.edu"]   # lets the login page route by email domain

  lifecycle {
    # AWS adds computed keys (e.g. ActiveEncryptionCertificate, SLO/SSO URLs).
    ignore_changes = [provider_details["ActiveEncryptionCertificate"],
                      provider_details["SSORedirectBindingURI"],
                      provider_details["SLORedirectBindingURI"]]
  }
}

resource "aws_cognito_user_pool_client" "govern_osu" {
  name         = "${local.prefix}-govern-osu"
  user_pool_id = var.cognito_user_pool_id

  generate_secret                      = false            # public SPA client, PKCE
  supported_identity_providers         = [aws_cognito_identity_provider.osu.provider_name]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = ["${var.govern_app_url}/auth/callback"]
  logout_urls                          = ["${var.govern_app_url}/signed-out"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]   # no passwords
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 12
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "hours"
  }

  read_attributes = ["email", "email_verified", "name", "given_name", "family_name",
                     "custom:eppn", "custom:affiliation", "custom:tenantId"]
}

# The API authorizer must accept the new client as an audience:
#   jwt_configuration { audience = [var.cognito_client_id, aws_cognito_user_pool_client.govern_osu.id] ... }
```

Pre token generation trigger (V2 event; sketch):

```python
ROLE_MAP = {  # OSU-supplied group values -> Govern groups
    "<osu-grouper-path>:govern-admins": "govern-admin",
    "<osu-grouper-path>:govern-reviewers": "govern-reviewer",
    "<osu-grouper-path>:govern-leaders": "govern-leader",
}

def handler(event, _ctx):
    attrs = event["request"]["userAttributes"]
    if '"providerName":"OSU"' not in attrs.get("identities", "").replace(" ", ""):
        return event                                   # non-OSU users unchanged
    released = {g.strip() for g in attrs.get("custom:groups", "").replace(";", ",").split(",") if g.strip()}
    groups = sorted({ROLE_MAP[g] for g in released if g in ROLE_MAP})
    if not groups:
        raise Exception("Not authorised for Govern")   # sign-in fails, no token issued
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "idTokenGeneration": {"claimsToAddOrOverride": {"custom:tenantId": "osu"}},
        "groupOverrideDetails": {"groupsToOverride": groups},
    }
    return event
```

**Front-end change:** sign-in today uses `amazon-cognito-identity-js` with username and password (SRP). SAML federation needs the OAuth 2.0 authorization-code flow with PKCE against the Cognito managed login (`/oauth2/authorize?identity_provider=OSU`). Add a "Sign in with Ohio State" button, a `/auth/callback` route, and token storage and refresh. Local password sign-in should be disabled for the OSU tenant.

## 4. Metadata exchange

1. **Blue-IQ sends OSU** its SP metadata:
   - Entity ID: `urn:amazon:cognito:sp:<user-pool-id>`
   - ACS URL: `https://<domain>.auth.<region>.amazoncognito.com/saml2/idpresponse`
   - Signing and encryption certificates (download from Cognito after the IdP is created)
   - Metadata: `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>/saml2/metadata` (or from the console)
   - Requested attributes (table in §2), NameID format, and contacts (technical, security).
2. **Path:** either bilateral (OSU loads our SP metadata directly) or InCommon registration. Cognito does not consume the full InCommon aggregate, so Cognito trusts only the OSU IdP's metadata in either case. OSU IAM decides which path.
3. **OSU sends Blue-IQ** its IdP metadata URL or file, and confirms signing certificate rollover practice.
4. Blue-IQ creates `aws_cognito_identity_provider.osu`, then sends the final Cognito certificates for OSU to attach to the relying party.

## 5. What OSU IT must provide

| Item | Notes |
|---|---|
| IdP entity ID and metadata (URL preferred) | Including signing cert and rollover schedule |
| Attribute release for the Govern SP | eppn, mail, displayName, givenName, sn, eduPersonScopedAffiliation, and **filtered** isMemberOf or eduPersonEntitlement |
| Grouper (or Entra) groups for admin, reviewer, leader | Names and owners. Membership is managed by OSU |
| Who may sign in | e.g. restrict release to members of the Govern groups |
| MFA expectation | Whether Duo/Entra MFA at the IdP is required (enforced on OSU's side), and whether `AuthnContextClassRef` is asserted |
| Test accounts | One per role, plus one account outside all groups (negative test) |
| Contacts | IAM technical contact and security contact |

## 6. Test plan

| # | Test | Expected |
|---|---|---|
| 1 | SP-initiated login, admin account | Token carries `cognito:groups=[govern-admin]`, `custom:tenantId=osu`; matrix settings reachable |
| 2 | Reviewer and leader accounts | Correct actions only (`Contract.allowedActions`); leader cannot edit the matrix (403) |
| 3 | User in no Govern group | Sign-in refused; no token issued |
| 4 | Remove user from group at OSU | Next sign-in or token refresh drops the role (max 60 min with the token lifetimes above) |
| 5 | Project membership by email | Invited email equals the released `mail`; access works with no separate verification |
| 6 | Tampered or unsigned assertion; expired assertion | Rejected by Cognito |
| 7 | IdP-initiated login | Rejected (`IDPInit=false`) |
| 8 | Sign-out | App session and Cognito session cleared |
| 9 | OSU certificate rollover in test | Login continues after metadata refresh |
| 10 | Accessibility of the sign-in flow | Keyboard and screen reader pass (OSU login pages are OSU's responsibility) |

## 7. Effort and dependencies

Estimated 1–2 sprints of engineering after OSU provides metadata and attributes: Terraform and triggers 3–4 days; front-end OAuth flow 3–5 days; testing with OSU 2–3 days. Not started.
