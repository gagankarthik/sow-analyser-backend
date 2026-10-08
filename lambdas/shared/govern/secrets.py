"""Secrets Manager access for Govern (Teams webhook URL, DocuSign HMAC key,
Huron / Workday / Microsoft 365 credentials).

Each secret is created EMPTY by Terraform; values are written out-of-band or
through the admin API. A secret's value is a JSON object:

    {"tenants": {"<tenantId>": <value>}, "hmacKey": "...", "hmacKeys": ["..."]}

Per-tenant values live under ``tenants`` so one secret serves every workspace
of a stage. Values are cached for a few minutes per warm Lambda (a rotated
key is picked up within the TTL) and NEVER logged or returned by the API.
"""
from __future__ import annotations

import json
import time
from typing import Any

from botocore.exceptions import ClientError

from .. import aws
from ..logger import get_logger

log = get_logger("blue-iq.govern.secrets")

_TTL_S = 300
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class SecretUnavailable(RuntimeError):
    """The secret is not configured or could not be read."""


def read(secret_arn: str, *, fresh: bool = False) -> dict[str, Any]:
    """The secret as a dict ({} when it exists but is empty / not JSON).
    Raises SecretUnavailable when there is no ARN or it cannot be read."""
    if not secret_arn:
        raise SecretUnavailable("secret not configured")
    hit = _cache.get(secret_arn)
    if hit and not fresh and time.monotonic() - hit[0] < _TTL_S:
        return hit[1]
    try:
        raw = aws.secrets_client().get_secret_value(SecretId=secret_arn).get("SecretString") or ""
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            value: dict[str, Any] = {}
        else:
            log.warning("govern.secret_read_failed", code=code)
            raise SecretUnavailable("secret could not be read") from exc
    else:
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"value": raw.strip()}      # a bare string secret (e.g. an HMAC key)
        value = parsed if isinstance(parsed, dict) else {}
    _cache[secret_arn] = (time.monotonic(), value)
    return value


def tenant_value(secret_arn: str, tenant_id: str) -> Any:
    """The tenant's value in a secret, or None (also when unavailable)."""
    try:
        return (read(secret_arn).get("tenants") or {}).get(tenant_id)
    except SecretUnavailable:
        return None


def put_tenant_value(secret_arn: str, tenant_id: str, value: Any) -> None:
    """Set (or with ``None`` remove) the tenant's value, keeping the others.
    Raises SecretUnavailable when the secret is not configured."""
    current = dict(read(secret_arn, fresh=True))
    tenants = dict(current.get("tenants") or {})
    if value is None:
        tenants.pop(tenant_id, None)
    else:
        tenants[tenant_id] = value
    current["tenants"] = tenants
    try:
        aws.secrets_client().put_secret_value(SecretId=secret_arn, SecretString=json.dumps(current))
    except ClientError as exc:
        raise SecretUnavailable("secret could not be written") from exc
    _cache.pop(secret_arn, None)


def hmac_keys(secret_arn: str) -> list[str]:
    """HMAC keys (current first; more than one during rotation)."""
    value = read(secret_arn)
    keys = [value.get("hmacKey")] + list(value.get("hmacKeys") or []) + [value.get("value")]
    return [k for k in dict.fromkeys(keys) if isinstance(k, str) and k]


def clear_cache() -> None:
    _cache.clear()
