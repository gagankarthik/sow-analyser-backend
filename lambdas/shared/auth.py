"""Caller identity — the ONLY place a tenant id is derived for a request.

The tenant comes exclusively from claims that API Gateway's Cognito JWT
authorizer has already verified. It is never read from a header, the query
string, the path or the body: anything the client can type is not identity.

Resolution order
----------------
1. ``custom:tenantId`` claim (set by an admin / the invite flow). A malformed
   claim is an error (403) — it is never "repaired" or replaced.
2. No claim → the caller gets a private workspace keyed on their Cognito
   ``sub`` (``u-<sub>``), so self-signed-up users are isolated from each other.

There is no shared or default tenant, and no setting that creates one. Anything
else raises ``AuthError`` and the handler answers 403.
"""
from __future__ import annotations

import re
from typing import Any

# Tenant ids are embedded in S3 keys (tenants/<tenantId>/uploads/...) and in
# DynamoDB / OpenSearch keys, so they are restricted to a safe character set:
# no "/" (would shift the key segments the pipeline parses), no whitespace.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUB_RE = re.compile(r"^[A-Za-z0-9-]{1,60}$")


class AuthError(Exception):
    """The request carries no usable verified identity."""


def jwt_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Verified JWT claims from the API Gateway authorizer context ({} if none)."""
    ctx = (event or {}).get("requestContext") or {}
    authorizer = ctx.get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or authorizer.get("claims") or {}
    return claims if isinstance(claims, dict) else {}


def tenant_for_identity(tenant_claim: Any, sub: Any) -> str:
    """Tenant for a (tenant claim, sub) pair — shared by request auth and by the
    invite flow, which must work out which tenant an EXISTING pool user is in."""
    if isinstance(tenant_claim, str) and tenant_claim.strip():
        tenant = tenant_claim.strip()
        if not _TENANT_RE.fullmatch(tenant):
            raise AuthError("tenant claim is malformed")
        return tenant
    if isinstance(sub, str) and _SUB_RE.fullmatch(sub):
        return f"u-{sub}"
    raise AuthError("no verified identity on the request")


def tenant_from_event(event: dict[str, Any]) -> str:
    """Tenant id for an API Gateway (HTTP API, JWT authorizer) request."""
    claims = jwt_claims(event)
    return tenant_for_identity(claims.get("custom:tenantId"), claims.get("sub"))
