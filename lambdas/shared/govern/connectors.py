"""Connector framework — one way to plug Govern into a system of record.

A connector is an ``Adapter`` (code, below) plus a per-tenant configuration
(data: ``T#<tenant> / CONN#<id>`` — enabled flag, endpoint config and FIELD
MAPPING) plus credentials (Secrets Manager, never stored in DynamoDB, never
returned). A new university or Workday tenant is configuration, not code.

    huron     both  pull agreement records (Huron owns PI / department / sponsor / record id);
                    push Govern status, findings, blockers and next step back to the record
    workday   in    pull awards / cost centre / supplier / spend; match by workdayRef, else
                    mark the contract unmatched for the manual match screen
    m365      both  sign-on, alerts and optional SharePoint / inbox intake (alerts go via the notifier)
    docusign  in    signature status (live updates arrive on the signed webhook)

Runs are triggered by the daily schedule, by an admin (``POST
/integrations/{id}/sync``) or by a Govern event (Huron push-back). EVERY run
writes a SyncRun to the sync log. With no credentials a run is a DRY RUN: it
reads nothing external and changes nothing, and reports what it would do.

System of record wins (``workflow.sync_fields``): a value from the record
replaces Govern's for a field the system owns, and a disagreement is logged
as a ``conflict`` entry and in ``syncConflicts``.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..logger import get_logger
from . import secrets, store, workflow
from .store import iso

log = get_logger("blue-iq.govern.connectors")

_HTTP_TIMEOUT_S = 15
_HTTP_ATTEMPTS = 3
_MAX_RECORDS = 5000
_MAX_CONFIG_KEYS = 50


@dataclass(frozen=True)
class Adapter:
    id: str
    name: str
    direction: str                         # "in" | "out" | "both"
    system: str                            # name used for external ids and conflicts
    secret_setting: str                    # Settings attribute holding the secret ARN
    owns_fields: tuple[str, ...] = ()
    default_mapping: dict[str, str] = field(default_factory=dict)
    id_field: str | None = None            # Govern field that links a record to a contract
    default_config: dict[str, str] = field(default_factory=dict)
    credential_keys: tuple[str, ...] = ()   # exactly what PUT credentials must carry


ADAPTERS: dict[str, Adapter] = {
    "huron": Adapter(
        id="huron", name="Huron Research Suite", direction="both", system="huron",
        secret_setting="huron_secret_arn",
        owns_fields=("huronRecordId", "piName", "department", "college", "sponsor"),
        default_mapping={"huronRecordId": "ID", "piName": "PrincipalInvestigator.Name",
                         "department": "Department.Name", "college": "College.Name",
                         "sponsor": "Sponsor.Name", "requestedDate": "DateReceived"},
        id_field="huronRecordId",
        default_config={"baseUrl": "", "tokenUrl": "", "agreementsPath": "/agreements",
                        "statusPath": "/agreements/{id}/govern-status"},
        credential_keys=("clientId", "clientSecret"),
    ),
    "workday": Adapter(
        id="workday", name="Workday", direction="in", system="workday",
        secret_setting="workday_secret_arn",
        owns_fields=("workdayRef", "expectedValue", "costCenter", "supplier", "spendToDate"),
        default_mapping={"workdayRef": "Award_Reference_ID", "expectedValue": "Award_Amount",
                         "costCenter": "Cost_Center", "supplier": "Supplier_Name", "spendToDate": "Spend_To_Date"},
        id_field="workdayRef",
        default_config={"baseUrl": "", "tokenUrl": "", "awardsPath": "/awards"},
        credential_keys=("clientId", "clientSecret", "refreshToken"),
    ),
    "m365": Adapter(
        id="m365", name="Microsoft 365", direction="both", system="m365", secret_setting="m365_secret_arn",
        default_config={"tenantDomain": "", "intakeFolder": ""},
        credential_keys=("clientId", "clientSecret"),
    ),
    "docusign": Adapter(
        id="docusign", name="DocuSign", direction="in", system="docusign", secret_setting="docusign_secret_arn",
        owns_fields=("signature",),
        default_config={"accountId": "", "baseUrl": ""},
        credential_keys=("integrationKey", "userId", "privateKey", "connectHmacKey"),
    ),
}
# Govern fields a mapping may target: contract fields plus record-only extras.
MAPPABLE_FIELDS = frozenset(workflow.PATCH_FIELDS) | {"title", "costCenter", "supplier", "spendToDate"}


class ConnectorError(Exception):
    """A request to the external system failed."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _secret_arn(adapter: Adapter) -> str:
    return getattr(settings, adapter.secret_setting, "") or ""


def connector_view(adapter: Adapter, stored: dict[str, Any] | None) -> dict[str, Any]:
    """Connector JSON (GOVERN_API.md) — never includes credentials."""
    s = stored or {}
    enabled = bool(s.get("enabled", False))
    has_creds = bool(s.get("credentialsConfigured", False))
    status = "error" if s.get("lastError") else ("connected" if enabled and has_creds else "not_connected")
    return {
        "id": adapter.id, "name": adapter.name, "enabled": enabled, "status": status,
        "direction": adapter.direction, "lastSyncAt": s.get("lastSyncAt"), "lastError": s.get("lastError"),
        "credentialsConfigured": has_creds,
        "config": {**adapter.default_config, **(s.get("config") or {})},
        "fieldMapping": {**adapter.default_mapping, **(s.get("fieldMapping") or {})},
        "ownsFields": list(adapter.owns_fields),
    }


def list_connectors(tenant_id: str) -> list[dict[str, Any]]:
    stored = store.config.connectors(tenant_id)
    return [connector_view(a, stored.get(a.id)) for a in ADAPTERS.values()]


def _clean_str_map(value: Any, name: str, *, keys: frozenset[str] | None = None) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > _MAX_CONFIG_KEYS:
        raise workflow.BadRequest(f"{name} must be an object (max {_MAX_CONFIG_KEYS} keys)")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k or len(k) > 64:
            raise workflow.BadRequest(f"{name} keys must be short strings")
        if keys is not None and k not in keys:
            raise workflow.BadRequest(f"{name}.{k} is not a Govern field that can be mapped")
        if v is not None and (not isinstance(v, str) or len(v) > 500):
            raise workflow.BadRequest(f"{name}.{k} must be a string (max 500 characters)")
        if v:
            out[k] = v
    return out


def update_connector(tenant_id: str, connector_id: str, body: Any) -> dict[str, Any]:
    """PUT /integrations/{id}. Credentials go to Secrets Manager only."""
    adapter = ADAPTERS.get(connector_id)
    if adapter is None:
        raise workflow.NotFound("Connector not found")
    if not isinstance(body, dict):
        raise workflow.BadRequest("Body must be a JSON object")
    unknown = set(body) - {"enabled", "config", "fieldMapping", "credentials"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    stored = dict(store.config.connector(tenant_id, connector_id) or {"id": connector_id})
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise workflow.BadRequest("enabled must be true or false")
        stored["enabled"] = body["enabled"]
    if "config" in body:
        cfg = _clean_str_map(body["config"], "config")
        if cfg.get("baseUrl") and not cfg["baseUrl"].startswith("https://"):
            raise workflow.BadRequest("config.baseUrl must be an https:// URL")
        stored["config"] = cfg
    if "fieldMapping" in body:
        stored["fieldMapping"] = _clean_str_map(body["fieldMapping"], "fieldMapping", keys=MAPPABLE_FIELDS)
    if "credentials" in body:
        creds = _clean_credentials(adapter, body["credentials"])
        try:
            secrets.put_tenant_value(_secret_arn(adapter), tenant_id, creds or None)
        except secrets.SecretUnavailable:
            raise workflow.BadRequest("Credentials storage is not configured for this connector on this deployment.")
        stored["credentialsConfigured"] = bool(creds)
        stored["lastError"] = None
    stored["id"] = connector_id
    stored["updatedAt"] = iso()
    store.config.put_connector(tenant_id, stored)
    return connector_view(adapter, stored)


def _clean_credentials(adapter: Adapter, creds: Any) -> dict[str, str] | None:
    """The connector's credential keys, all present, nothing else (or None to
    remove them)."""
    if creds is None:
        return None
    keys = ", ".join(adapter.credential_keys)
    if not isinstance(creds, dict) or len(json.dumps(creds)) > 8000:
        raise workflow.BadRequest(f"credentials must be an object with {keys} (max 8 KB), or null")
    unknown = set(creds) - set(adapter.credential_keys)
    missing = [k for k in adapter.credential_keys if not isinstance(creds.get(k), str) or not creds[k].strip()]
    if unknown or missing:
        raise workflow.BadRequest(f"{adapter.name} credentials need exactly: {keys}")
    return {k: creds[k].strip() for k in adapter.credential_keys}


# ---------------------------------------------------------------------------
# HTTP (only used once credentials are configured)
# ---------------------------------------------------------------------------


def _request(method: str, url: str, *, token: str | None = None, body: Any = None,
             form: dict[str, str] | None = None) -> Any:
    """JSON over HTTPS with a timeout and bounded retries with backoff on
    network errors, 429 and 5xx. Credentials never appear in errors or logs."""
    if not url.startswith("https://"):
        raise ConnectorError("endpoint is not configured")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310 — https only
                raw = resp.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise ConnectorError(f"{method} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
        time.sleep(min(4.0, 0.5 * 2 ** attempt))
    raise ConnectorError(f"{method} failed after {_HTTP_ATTEMPTS} attempts ({type(last).__name__})")


def access_token(conn: dict[str, Any], creds: dict[str, Any]) -> str:
    """OAuth 2 token from the connector's ``tokenUrl``: the refresh-token grant
    when a refresh token is configured (Workday), else client credentials."""
    form = {"client_id": str(creds.get("clientId") or ""), "client_secret": str(creds.get("clientSecret") or "")}
    if creds.get("refreshToken"):
        form.update(grant_type="refresh_token", refresh_token=str(creds["refreshToken"]))
    else:
        form["grant_type"] = "client_credentials"
    reply = _request("POST", conn["config"].get("tokenUrl") or "", form=form)
    token = (reply or {}).get("access_token") if isinstance(reply, dict) else None
    if not token:
        raise ConnectorError("the token endpoint returned no access token")
    return str(token)


def _get_path(record: Any, path: str) -> Any:
    """``"PrincipalInvestigator.Name"`` → record["PrincipalInvestigator"]["Name"]."""
    cur = record
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def map_record(record: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for govern_field, path in mapping.items():
        value = _get_path(record, path)
        if isinstance(value, str):
            value = value.strip() or None
        if govern_field == "expectedValue" and value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        out[govern_field] = value
    return out


def _fetch_list(url: str, token: str) -> list[dict[str, Any]]:
    data = _request("GET", url, token=token)
    rows = (data.get("items") or data.get("data") or []) if isinstance(data, dict) else data
    return [r for r in (rows or [])[:_MAX_RECORDS] if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _new_run(connector_id: str, trigger: str, dry: bool) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:16], "connectorId": connector_id, "startedAt": iso(), "finishedAt": None,
            "trigger": trigger, "dryRun": dry, "recordsIn": 0, "recordsOut": 0, "errors": [], "status": "ok",
            "summary": ""}


def apply_record(tenant_id: str, adapter: Adapter, mapped: dict[str, Any], now: datetime | None = None) -> str:
    """Apply one mapped record. "applied" | "conflict" | "unmatched"."""
    key = mapped.get(adapter.id_field or "")
    contract_id = store.sync.find_contract(tenant_id, adapter.system, str(key)) if key else None
    if not contract_id:
        return "unmatched"
    _, conflicts = workflow.sync_fields(contract_id, adapter.system, adapter.name, mapped,
                                        adapter.owns_fields, now=now)
    return "conflict" if conflicts else "applied"


def _pull(tenant_id: str, adapter: Adapter, conn: dict[str, Any], creds: dict[str, Any] | None,
          run: dict[str, Any], now: datetime) -> None:
    contracts = store.contracts.for_tenant(tenant_id)
    linked = [c for c in contracts if adapter.id_field and c.get(adapter.id_field)]
    if creds is None:
        if adapter.id == "workday":
            run["summary"] = (f"Dry run: would pull Workday awards and spend, matching {len(linked)} contract(s) by "
                              f"Workday reference; {len(contracts) - len(linked)} have no reference and stay unmatched.")
        else:
            run["summary"] = (f"Dry run: would pull agreement records changed since {conn.get('lastSyncAt') or 'the start'} "
                              f"and refresh {len(linked)} linked contract(s).")
        run["recordsIn"] = len(linked)
        return
    path = conn["config"].get("awardsPath" if adapter.id == "workday" else "agreementsPath") or ""
    query = f"?modifiedSince={urllib.parse.quote(conn['lastSyncAt'])}" if conn.get("lastSyncAt") else ""
    records = _fetch_list(conn["config"].get("baseUrl", "").rstrip("/") + path + query, access_token(conn, creds))
    seen_refs: set[str] = set()
    for record in records:
        mapped = map_record(record, conn["fieldMapping"])
        ref = mapped.get(adapter.id_field or "")
        try:
            outcome = apply_record(tenant_id, adapter, mapped, now)
        except Exception as exc:  # noqa: BLE001 — one bad record must not stop the run
            run["errors"].append({"record": str(ref or "?")[:100], "message": type(exc).__name__})
            continue
        if outcome == "unmatched":
            run["errors"].append({"record": str(ref or "?")[:100],
                                  "message": "No Govern contract carries this id yet."})
            continue
        seen_refs.add(str(ref))
        run["recordsIn"] += 1
    if adapter.id == "workday" and not conn.get("lastSyncAt"):
        # A full pull: a reference Workday does not know needs a manual match.
        for c in linked:
            if str(c.get("workdayRef")) not in seen_refs:
                workflow.set_workday_unmatched(c["contractId"], now=now)
    run["summary"] = f"Pulled {len(records)} record(s); {run['recordsIn']} applied."


def push_payload(c: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What Govern writes back to a Huron agreement record."""
    view = workflow.to_api(c, now)
    return {"huronRecordId": c.get("huronRecordId"), "governContractId": c["contractId"],
            "status": view["state"], "stage": view["stage"], "waitingOn": view["waitingOn"]["label"],
            "nextStep": view["nextStep"]["headline"], "matrixCounts": (view["matrix"] or {}).get("counts"),
            "openBlockers": [b.get("text") for b in store.contracts.blockers(c["contractId"])
                             if b.get("status", "open") == "open"][:50],
            "updatedAt": iso(now)}


def _push(tenant_id: str, adapter: Adapter, conn: dict[str, Any], creds: dict[str, Any] | None,
          run: dict[str, Any], contract_ids: list[str], now: datetime) -> None:
    contracts = store.contracts.get_many(contract_ids).values() if contract_ids else \
        [c for c in store.contracts.for_tenant(tenant_id) if c.get("huronRecordId")]
    targets = [c for c in contracts if c.get("huronRecordId")]
    if creds is None:
        run["recordsOut"] = len(targets)
        run["summary"] = (run["summary"] + " " if run["summary"] else "") + \
            f"Dry run: would push status, findings, blockers and next step to {len(targets)} Huron record(s)."
        return
    token = access_token(conn, creds)
    template = conn["config"].get("statusPath") or "/agreements/{id}/govern-status"
    base = conn["config"].get("baseUrl", "").rstrip("/")
    for c in targets:
        try:
            _request("PUT", base + template.replace("{id}", urllib.parse.quote(str(c["huronRecordId"]))),
                     token=token, body=push_payload(c, now))
            run["recordsOut"] += 1
        except ConnectorError as exc:
            run["errors"].append({"record": str(c["huronRecordId"])[:100], "message": str(exc)[:300]})


def run_connector(tenant_id: str, connector_id: str, trigger: str = "manual", *,
                  contract_ids: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Run one connector and write its SyncRun. Never raises for an external
    failure: the run is stored with status failed / partial instead."""
    adapter = ADAPTERS.get(connector_id)
    if adapter is None:
        raise workflow.NotFound("Connector not found")
    now = now or datetime.now(timezone.utc)
    stored = dict(store.config.connector(tenant_id, connector_id) or {"id": connector_id})
    conn = connector_view(adapter, stored)
    creds = secrets.tenant_value(_secret_arn(adapter), tenant_id) if conn["credentialsConfigured"] else None
    if creds is not None and not isinstance(creds, dict):
        creds = None
    live = creds is not None and conn["enabled"]
    run = _new_run(connector_id, trigger, dry=not live)
    try:
        if adapter.id in ("huron", "workday") and trigger != "event":
            _pull(tenant_id, adapter, conn, creds if live else None, run, now)
        if adapter.id == "huron" and (trigger == "event" or contract_ids is None):
            _push(tenant_id, adapter, conn, creds if live else None, run, contract_ids or [], now)
        if adapter.id in ("m365", "docusign"):
            run["summary"] = ("Dry run: " if not live else "") + (
                "alerts are sent by the notifier; folder / inbox intake runs when a folder is configured."
                if adapter.id == "m365" else "signature status arrives on the signed DocuSign webhook.")
        status = "dry_run" if not live else ("partial" if run["errors"] else "ok")
        stored["lastError"] = None
    except ConnectorError as exc:
        run["errors"].append({"record": "*", "message": str(exc)[:300]})
        status = "failed"
        stored["lastError"] = str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 — recorded on the run, never lost
        log.exception("govern.connector_run_failed", connector=connector_id, error_type=type(exc).__name__)
        run["errors"].append({"record": "*", "message": f"Internal error ({type(exc).__name__})"})
        status = "failed"
        stored["lastError"] = "Internal error"
    run["status"] = status
    run["finishedAt"] = iso()
    run["errors"] = run["errors"][:200]
    if live and status != "failed":
        stored["lastSyncAt"] = run["startedAt"]
    stored["id"] = connector_id
    store.config.put_connector(tenant_id, stored)
    store.sync.put_run(tenant_id, run)
    log.info("govern.connector_run", connector=connector_id, trigger=trigger, status=status,
             recordsIn=run["recordsIn"], recordsOut=run["recordsOut"], errors=len(run["errors"]))
    return run


def run_scheduled(tenant_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Daily run: every enabled connector; the two systems of record always
    run (as a dry run while not connected) so admins see what they would do."""
    stored = store.config.connectors(tenant_id)
    ids = [a.id for a in ADAPTERS.values() if (stored.get(a.id) or {}).get("enabled") or a.id in ("huron", "workday")]
    return [run_connector(tenant_id, cid, "schedule", now=now) for cid in ids]


def sync_run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run.get(k) for k in ("id", "connectorId", "startedAt", "finishedAt", "trigger", "dryRun",
                                    "recordsIn", "recordsOut", "errors", "status")}

