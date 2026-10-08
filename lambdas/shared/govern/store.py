"""Govern storage — one repository per table (docs/GOVERN_ARCHITECTURE.md §2.1).

    contracts  govern-contracts  CON#<id> / META · BLK#<id> · OBL#<id> · INC#<id> · REVIEW#<docId>
                                 GSI1 board        T#<tenant>      / <stage>#<stageEnteredAt>   (META only)
                                 GSI2 owner queue  OWN#<email>     / <stageEnteredAt>
                                 GSI3 obligations  T#<tenant>#OBL  / <dueDate>                  (open, dated only)
    activity   govern-activity   CON#<id> / <iso>#<appendNs>#<eventId>  append-only (PutItem only)
    config     govern-config     T#<tenant> / MATRIX#<v:06d> · MATRIX#CURRENT · SETTINGS · CONN#<id>
                                 TENANTS / T#<tenant>               tenants that use Govern (sweeper)
    sync       govern-sync       T#<tenant> / RUN#<iso>#<runId> (TTL) · RECONCILE
                                 CON#<id> / EXT#<system>            GSI1 EXT#<tenant>#<system>#<extId> / CON#<id>
    metrics    govern-metrics    T#<tenant> / D#<yyyy-mm-dd>        daily trend counters (atomic ADD)
                                 <scope> / SEEN#<id> (TTL)          idempotency markers

Contracts change read-modify-write under optimistic locking on ``rev``
(``contracts.mutate``) — the same pattern as projects in shared/dynamodb.py:
two reviewers acting at once never overwrite each other, and the loser
re-validates its transition against the winner's state.

Nothing here knows workflow rules; that is shared/govern/workflow.py. Use the
module-level instances (``store.contracts``, ``store.activity`` …).
"""
from __future__ import annotations

import copy
import json
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from botocore.exceptions import ClientError

from ..aws import dynamodb_resource
from ..config import settings
from ..dynamodb import _from_ddb, _to_ddb
from ..logger import get_logger

log = get_logger("blue-iq.govern.store")

_MAX_WRITE_RETRIES = 5
_KEY_ATTRS = frozenset({"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK", "entityType", "ttl"})
# A matrix review above this size goes to S3 (DynamoDB items cap at 400 KB).
_REVIEW_INLINE_LIMIT = 350_000
_SYNC_RUN_TTL_DAYS = 400


class ContractConflict(RuntimeError):
    """A write lost the optimistic-lock race several times in a row."""


# ---------------------------------------------------------------------------
# Time and item helpers
# ---------------------------------------------------------------------------


def iso(dt: datetime | None = None) -> str:
    """``2026-10-08T14:03:00Z`` — the one timestamp format Govern writes."""
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    """ISO date or date-time → aware UTC datetime (None when unparsable)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text if len(text) == 10 else text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clean_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored item without its key attributes, numbers as int / float."""
    if not item:
        return None
    return {k: _from_ddb(v) for k, v in item.items() if k not in _KEY_ATTRS}


def ttl_in(days: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def _is_conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _unzip_json(raw: Any) -> Any:
    raw = getattr(raw, "value", raw)            # boto3 Binary → bytes
    return json.loads(zlib.decompress(bytes(raw)).decode("utf-8"))


def _zip_json(value: Any) -> bytes:
    return zlib.compress(json.dumps(value, default=str).encode("utf-8"))


# ---------------------------------------------------------------------------
# Base repository
# ---------------------------------------------------------------------------


class _Repository:
    """One DynamoDB table, named by a Settings attribute."""

    table_setting = ""

    @property
    def name(self) -> str:
        name = getattr(settings, self.table_setting, "")
        if not name:
            raise RuntimeError(f"{self.table_setting.upper()} env var is not set")
        return name

    def table(self) -> Any:
        return _resource().Table(self.name)

    def query_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Query following LastEvaluatedKey (one page stops at 1 MB)."""
        items: list[dict[str, Any]] = []
        while True:
            resp = self.table().query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def batch_get(self, keys: list[dict[str, str]]) -> list[dict[str, Any]]:
        """BatchGetItem, 100 keys per call, UnprocessedKeys followed."""
        out: list[dict[str, Any]] = []
        for start in range(0, len(keys), 100):
            pending: dict[str, Any] = {self.name: {"Keys": keys[start:start + 100]}}
            for _ in range(6):
                resp = _resource().batch_get_item(RequestItems=pending)
                out.extend((resp.get("Responses") or {}).get(self.name, []))
                pending = resp.get("UnprocessedKeys") or {}
                if not pending:
                    break
        return out

    def put_new(self, item: dict[str, Any]) -> bool:
        """PutItem only if the key is free. False when it already exists."""
        try:
            self.table().put_item(Item=_to_ddb(item), ConditionExpression="attribute_not_exists(PK)")
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def children(self, pk: str, sk_prefix: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        items = self.query_all(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix))
        return [c for c in (clean_item(i) for i in items) if c]


def _resource() -> Any:
    return dynamodb_resource()


# ---------------------------------------------------------------------------
# govern-contracts
# ---------------------------------------------------------------------------


class ContractRepository(_Repository):
    """The contract aggregate: META + blockers, obligations, income, reviews."""

    table_setting = "contracts_table"

    # -- contract (META) ------------------------------------------------------
    @staticmethod
    def _item(c: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in c.items() if k not in _KEY_ATTRS}
        item = {**body, "PK": f"CON#{body['contractId']}", "SK": "META", "entityType": "CONTRACT",
                "GSI1PK": f"T#{body['tenantId']}",
                "GSI1SK": f"{body.get('stage') or 'draft'}#{body.get('stageEnteredAt') or ''}"}
        owner_email = ((body.get("owner") or {}).get("email") or "").lower()
        if owner_email:
            item["GSI2PK"] = f"OWN#{owner_email}"
            item["GSI2SK"] = body.get("stageEnteredAt") or ""
        return _to_ddb(item)

    def get(self, contract_id: str) -> dict[str, Any] | None:
        resp = self.table().get_item(Key={"PK": f"CON#{contract_id}", "SK": "META"})
        return clean_item(resp.get("Item"))

    def get_many(self, contract_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """{contractId: contract} for the ids that exist."""
        ids = [i for i in dict.fromkeys(contract_ids) if i]
        out: dict[str, dict[str, Any]] = {}
        for item in self.batch_get([{"PK": f"CON#{i}", "SK": "META"} for i in ids]):
            c = clean_item(item)
            if c and c.get("contractId"):
                out[c["contractId"]] = c
        return out

    def create(self, contract: dict[str, Any]) -> bool:
        """Write a new contract. False (writing nothing) if it exists — which
        makes intake idempotent under SQS re-delivery."""
        try:
            self.table().put_item(Item=self._item(dict(contract, rev=1)),
                                  ConditionExpression="attribute_not_exists(PK)")
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def mutate(self, contract_id: str, change: Callable[[dict[str, Any]], Any]) -> dict[str, Any] | None:
        """Read-modify-write under optimistic locking on ``rev``.

        ``change(contract)`` edits the dict in place, or returns False to
        write nothing. After a lost race it runs again on a fresh copy, so a
        transition is validated against the state it is applied to; its
        exceptions propagate unchanged. Returns the stored contract, or None
        if it does not exist.
        """
        for _ in range(_MAX_WRITE_RETRIES):
            current = self.get(contract_id)
            if current is None:
                return None
            working = copy.deepcopy(current)
            rev = int(current.get("rev") or 0)
            if change(working) is False:
                return current
            working["rev"] = rev + 1
            try:
                self.table().put_item(Item=self._item(working), ConditionExpression="rev = :rev",
                                      ExpressionAttributeValues={":rev": rev})
            except ClientError as exc:
                if _is_conditional_failure(exc):
                    continue
                raise
            return working
        raise ContractConflict(f"contract {contract_id} is being changed by someone else; try again")

    def for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        """Every contract of a tenant — one GSI1 partition, paginated."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"T#{tenant_id}"))
        return [c for c in (clean_item(i) for i in items) if c and c.get("contractId")]

    def for_owner(self, email: str) -> list[dict[str, Any]]:
        """A reviewer's queue, oldest stage entry first (GSI2)."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(f"OWN#{email.lower()}"))
        return [c for c in (clean_item(i) for i in items) if c and c.get("contractId")]

    # -- blockers -------------------------------------------------------------
    def blockers(self, contract_id: str) -> list[dict[str, Any]]:
        return sorted(self.children(f"CON#{contract_id}", "BLK#"),
                      key=lambda b: (b.get("createdAt") or "", b.get("id") or ""))

    def put_blocker(self, contract_id: str, blocker: dict[str, Any]) -> None:
        self.table().put_item(Item=_to_ddb({
            **{k: v for k, v in blocker.items() if k not in _KEY_ATTRS},
            "PK": f"CON#{contract_id}", "SK": f"BLK#{blocker['id']}", "entityType": "BLOCKER",
            "contractId": contract_id,
        }))

    def delete_blocker(self, contract_id: str, blocker_id: str) -> None:
        self.table().delete_item(Key={"PK": f"CON#{contract_id}", "SK": f"BLK#{blocker_id}"})

    # -- obligations ----------------------------------------------------------
    def obligations(self, contract_id: str) -> list[dict[str, Any]]:
        return sorted(self.children(f"CON#{contract_id}", "OBL#"),
                      key=lambda o: (o.get("dueDate") or "9999", o.get("id") or ""))

    def put_obligation(self, contract_id: str, tenant_id: str, obligation: dict[str, Any]) -> None:
        """Open, dated obligations are indexed by due date (GSI3) for the
        sweeper; done or undated ones drop out of the sparse index."""
        item = {**{k: v for k, v in obligation.items() if k not in _KEY_ATTRS},
                "PK": f"CON#{contract_id}", "SK": f"OBL#{obligation['id']}", "entityType": "OBLIGATION",
                "contractId": contract_id, "tenantId": tenant_id}
        if obligation.get("status", "open") == "open" and obligation.get("dueDate"):
            item["GSI3PK"] = f"T#{tenant_id}#OBL"
            item["GSI3SK"] = str(obligation["dueDate"])[:10]
        self.table().put_item(Item=_to_ddb(item))

    def delete_obligation(self, contract_id: str, obligation_id: str) -> None:
        self.table().delete_item(Key={"PK": f"CON#{contract_id}", "SK": f"OBL#{obligation_id}"})

    def obligations_due(self, tenant_id: str, through_date: str) -> list[dict[str, Any]]:
        """Open obligations of a tenant due on or before ``through_date``."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(IndexName="GSI3", KeyConditionExpression=Key("GSI3PK").eq(f"T#{tenant_id}#OBL")
                               & Key("GSI3SK").lte(through_date))
        return [o for o in (clean_item(i) for i in items) if o]

    def open_obligations(self, tenant_id: str) -> list[dict[str, Any]]:
        """Every open, dated obligation of a tenant, soonest due first (GSI3)."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(IndexName="GSI3", KeyConditionExpression=Key("GSI3PK").eq(f"T#{tenant_id}#OBL"))
        out = [o for o in (clean_item(i) for i in items) if o]
        return sorted(out, key=lambda o: (str(o.get("dueDate") or ""), str(o.get("id") or "")))

    # -- licensing income -----------------------------------------------------
    def income(self, contract_id: str) -> list[dict[str, Any]]:
        return sorted(self.children(f"CON#{contract_id}", "INC#"),
                      key=lambda i: (i.get("order", 0), i.get("id") or ""))

    def replace_income(self, contract_id: str, items: list[dict[str, Any]]) -> None:
        """Make the income items exactly ``items`` (order kept)."""
        keep = {it["id"] for it in items}
        for old in self.income(contract_id):
            if old.get("id") not in keep:
                self.table().delete_item(Key={"PK": f"CON#{contract_id}", "SK": f"INC#{old['id']}"})
        for order, it in enumerate(items):
            self.table().put_item(Item=_to_ddb({**it, "order": order, "PK": f"CON#{contract_id}",
                                                "SK": f"INC#{it['id']}", "entityType": "INCOME",
                                                "contractId": contract_id}))

    # -- matrix reviews -------------------------------------------------------
    def put_review(self, contract_id: str, tenant_id: str, review: dict[str, Any]) -> None:
        """Store a full review; a large one goes to the processed bucket with
        a pointer here, so the 400 KB item limit can never lose a review."""
        doc_id = review.get("docId") or "unknown"
        body = _zip_json(review)
        item: dict[str, Any] = {"PK": f"CON#{contract_id}", "SK": f"REVIEW#{doc_id}", "entityType": "REVIEW",
                                "contractId": contract_id, "docId": doc_id, "reviewedAt": review.get("reviewedAt")}
        if len(body) > _REVIEW_INLINE_LIMIT:
            from ..s3 import put_json

            key = f"govern/{tenant_id}/{contract_id}/review-{doc_id}.json"
            put_json(settings.processed_bucket, key, review)
            item["s3Key"] = key
        else:
            item["reviewZ"] = body
        self.table().put_item(Item=item)

    def get_review(self, contract_id: str, doc_id: str) -> dict[str, Any] | None:
        resp = self.table().get_item(Key={"PK": f"CON#{contract_id}", "SK": f"REVIEW#{doc_id}"})
        item = resp.get("Item")
        if not item:
            return None
        if item.get("s3Key"):
            from ..s3 import get_json

            try:
                return get_json(settings.processed_bucket, item["s3Key"])
            except Exception as exc:  # noqa: BLE001
                log.warning("govern.review_read_failed", error_type=type(exc).__name__)
                return None
        return _unzip_json(item["reviewZ"]) if item.get("reviewZ") is not None else None


# ---------------------------------------------------------------------------
# govern-activity
# ---------------------------------------------------------------------------


class ActivityLog(_Repository):
    """Append-only audit log. Writers hold PutItem only: an entry is written
    once and can never be replaced, edited or removed."""

    table_setting = "activity_table"

    def append(self, contract_id: str, tenant_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        entry = dict(entry)
        entry.setdefault("id", uuid.uuid4().hex)
        entry.setdefault("at", iso())
        # ``at`` has one-second precision and one change can write several
        # entries; the append clock keeps them in write order within a second.
        sort_key = f"{entry['at']}#{time.time_ns():020d}#{entry['id']}"
        self.table().put_item(Item=_to_ddb({**entry, "PK": f"CON#{contract_id}", "SK": sort_key,
                                            "contractId": contract_id, "tenantId": tenant_id}),
                              ConditionExpression="attribute_not_exists(PK)")
        return entry

    def recent(self, contract_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first, at most ``limit``."""
        from boto3.dynamodb.conditions import Key

        resp = self.table().query(KeyConditionExpression=Key("PK").eq(f"CON#{contract_id}"),
                                  ScanIndexForward=False, Limit=limit)
        return [{k: _from_ddb(v) for k, v in i.items() if k not in ("PK", "SK")} for i in resp.get("Items", [])]

    def all_for_tenant_contracts(self, contract_ids: Iterable[str]) -> Iterable[dict[str, Any]]:
        """Every entry of the given contracts, oldest first (backfill)."""
        from boto3.dynamodb.conditions import Key

        for cid in contract_ids:
            for item in self.query_all(KeyConditionExpression=Key("PK").eq(f"CON#{cid}")):
                yield {k: _from_ddb(v) for k, v in item.items() if k not in ("PK", "SK")}


# ---------------------------------------------------------------------------
# govern-config
# ---------------------------------------------------------------------------


class ConfigRepository(_Repository):
    """Per-tenant configuration: matrix versions, settings, connectors; and
    the registry of tenants that use Govern."""

    table_setting = "config_table"

    # -- tenants --------------------------------------------------------------
    def register_tenant(self, tenant_id: str) -> None:
        self.table().put_item(Item={"PK": "TENANTS", "SK": f"T#{tenant_id}", "entityType": "TENANT",
                                    "tenantId": tenant_id})

    def tenants(self) -> list[str]:
        from boto3.dynamodb.conditions import Key

        return [str(i["tenantId"]) for i in self.query_all(KeyConditionExpression=Key("PK").eq("TENANTS"))
                if i.get("tenantId")]

    # -- matrix ---------------------------------------------------------------
    @staticmethod
    def _matrix(item: dict[str, Any]) -> dict[str, Any]:
        meta = clean_item({k: v for k, v in item.items() if k != "playbooksZ"}) or {}
        return {"version": int(meta.get("version") or 0), "effectiveDate": meta.get("effectiveDate"),
                "createdAt": meta.get("createdAt"), "createdBy": meta.get("createdBy"), "note": meta.get("note"),
                "homeState": meta.get("homeState"),
                "playbooks": _unzip_json(item["playbooksZ"]) if item.get("playbooksZ") is not None else {}}

    def _put_matrix(self, tenant_id: str, matrix: dict[str, Any]) -> bool:
        playbooks = matrix.get("playbooks") or {}
        return self.put_new({
            "PK": f"T#{tenant_id}", "SK": f"MATRIX#{int(matrix['version']):06d}", "entityType": "MATRIX",
            "version": int(matrix["version"]), "effectiveDate": matrix.get("effectiveDate"),
            "createdAt": matrix.get("createdAt"), "createdBy": matrix.get("createdBy"), "note": matrix.get("note"),
            "homeState": matrix.get("homeState"),
            "clauseCount": sum(len((pb or {}).get("clauses") or []) for pb in playbooks.values()),
            "playbooksZ": _zip_json(playbooks),
        })

    def _pointer(self, tenant_id: str) -> int | None:
        item = self.table().get_item(Key={"PK": f"T#{tenant_id}", "SK": "MATRIX#CURRENT"}).get("Item")
        return int(item["version"]) if item and item.get("version") is not None else None

    def matrix_version(self, tenant_id: str, version: int) -> dict[str, Any] | None:
        item = self.table().get_item(Key={"PK": f"T#{tenant_id}", "SK": f"MATRIX#{int(version):06d}"}).get("Item")
        return self._matrix(item) if item else None

    def current_matrix(self, tenant_id: str) -> dict[str, Any]:
        """The tenant's current matrix. The first read seeds version 1 from
        the built-in default (shared/govern/matrix.py)."""
        for _ in range(3):
            version = self._pointer(tenant_id)
            if version:
                matrix = self.matrix_version(tenant_id, version)
                if matrix:
                    return self._upgrade_system_default(tenant_id, matrix)
            from .matrix import default_matrix

            seed = dict(default_matrix())
            now = iso()
            seed.update(version=1, createdAt=seed.get("createdAt") or now, createdBy=None,
                        effectiveDate=seed.get("effectiveDate") or now[:10], note=seed.get("note"))
            self._put_matrix(tenant_id, seed)
            self.put_new({"PK": f"T#{tenant_id}", "SK": "MATRIX#CURRENT", "entityType": "MATRIX_POINTER",
                          "version": 1})
        raise RuntimeError("could not load or seed the matrix")

    def _upgrade_system_default(self, tenant_id: str, matrix: dict[str, Any]) -> dict[str, Any]:
        """Keep an untouched built-in matrix current. A version nobody saved
        (createdBy is None: the system seed) whose positions differ from
        today's built-in default gets today's default as a NEW version, so
        history stays intact. Any version a person saved is never changed."""
        if matrix.get("createdBy") is not None:
            return matrix
        from .matrix import default_matrix

        fresh = default_matrix()
        if matrix.get("playbooks") == fresh.get("playbooks"):
            return matrix
        prev = int(matrix["version"])
        now = iso()
        upgraded = {"version": prev + 1, "effectiveDate": now[:10], "createdAt": now, "createdBy": None,
                    "note": "Built-in standard matrix updated", "homeState": matrix.get("homeState"),
                    "playbooks": fresh["playbooks"]}
        if not self._put_matrix(tenant_id, upgraded):
            # Another request upgraded it first; read what is current now.
            return self.matrix_version(tenant_id, self._pointer(tenant_id) or prev) or matrix
        try:
            self.table().put_item(Item={"PK": f"T#{tenant_id}", "SK": "MATRIX#CURRENT",
                                        "entityType": "MATRIX_POINTER", "version": prev + 1},
                                  ConditionExpression="version = :v", ExpressionAttributeValues={":v": prev})
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            return self.matrix_version(tenant_id, self._pointer(tenant_id) or prev) or matrix
        return upgraded

    def save_matrix(self, tenant_id: str, playbooks: dict[str, Any], *, created_by: dict[str, Any] | None,
                    note: str | None, effective_date: str | None,
                    home_state: str | None | object = ...) -> dict[str, Any]:
        """Save ``playbooks`` as a NEW immutable version and point CURRENT at
        it. Concurrent saves each get their own version number. ``home_state``
        left out keeps the current version's home state."""
        for _ in range(_MAX_WRITE_RETRIES):
            current = self.current_matrix(tenant_id)
            prev = int(current["version"])
            now = iso()
            home = current.get("homeState") if home_state is ... else home_state
            matrix = {"version": prev + 1, "effectiveDate": effective_date or now[:10], "createdAt": now,
                      "createdBy": created_by, "note": note, "homeState": home, "playbooks": playbooks}
            if not self._put_matrix(tenant_id, matrix):
                continue
            try:
                self.table().put_item(Item={"PK": f"T#{tenant_id}", "SK": "MATRIX#CURRENT",
                                            "entityType": "MATRIX_POINTER", "version": prev + 1},
                                      ConditionExpression="version = :v", ExpressionAttributeValues={":v": prev})
            except ClientError as exc:
                if _is_conditional_failure(exc):
                    continue
                raise
            return matrix
        raise ContractConflict("the matrix is being changed by someone else; try again")

    def matrix_versions(self, tenant_id: str) -> list[dict[str, Any]]:
        """MatrixVersionInfo of every version, newest first (playbooks not read)."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(
            KeyConditionExpression=Key("PK").eq(f"T#{tenant_id}") & Key("SK").begins_with("MATRIX#0"),
            ProjectionExpression="version, effectiveDate, createdAt, createdBy, note, clauseCount",
            ScanIndexForward=False)
        out = []
        for i in items:
            c = clean_item(i) or {}
            out.append({"version": int(c.get("version") or 0), "effectiveDate": c.get("effectiveDate"),
                        "createdAt": c.get("createdAt"), "createdBy": c.get("createdBy"), "note": c.get("note"),
                        "clauseCount": int(c.get("clauseCount") or 0)})
        return sorted(out, key=lambda v: v["version"], reverse=True)

    # -- workflow settings ----------------------------------------------------
    def settings(self, tenant_id: str) -> dict[str, Any] | None:
        item = clean_item(self.table().get_item(Key={"PK": f"T#{tenant_id}", "SK": "SETTINGS"}).get("Item"))
        return (item or {}).get("settings") if item else None

    def put_settings(self, tenant_id: str, value: dict[str, Any]) -> None:
        self.table().put_item(Item=_to_ddb({"PK": f"T#{tenant_id}", "SK": "SETTINGS", "entityType": "SETTINGS",
                                            "settings": value, "updatedAt": iso()}))

    # -- connectors -----------------------------------------------------------
    def connector(self, tenant_id: str, connector_id: str) -> dict[str, Any] | None:
        return clean_item(self.table().get_item(Key={"PK": f"T#{tenant_id}", "SK": f"CONN#{connector_id}"}).get("Item"))

    def connectors(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        return {c["id"]: c for c in self.children(f"T#{tenant_id}", "CONN#") if c.get("id")}

    def put_connector(self, tenant_id: str, connector: dict[str, Any]) -> None:
        self.table().put_item(Item=_to_ddb({**connector, "PK": f"T#{tenant_id}", "SK": f"CONN#{connector['id']}",
                                            "entityType": "CONNECTOR", "tenantId": tenant_id}))


# ---------------------------------------------------------------------------
# govern-sync
# ---------------------------------------------------------------------------


class SyncRepository(_Repository):
    """Connector sync runs, the external-id map and the capture reconciliation."""

    table_setting = "sync_table"

    def put_run(self, tenant_id: str, run: dict[str, Any]) -> None:
        self.table().put_item(Item=_to_ddb({**run, "PK": f"T#{tenant_id}",
                                            "SK": f"RUN#{run['startedAt']}#{time.time_ns():020d}#{run['id']}",
                                            "entityType": "SYNC_RUN", "tenantId": tenant_id,
                                            "ttl": ttl_in(_SYNC_RUN_TTL_DAYS)}))

    def runs(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first."""
        from boto3.dynamodb.conditions import Key

        resp = self.table().query(KeyConditionExpression=Key("PK").eq(f"T#{tenant_id}") & Key("SK").begins_with("RUN#"),
                                  ScanIndexForward=False, Limit=limit)
        return [c for c in (clean_item(i) for i in resp.get("Items", [])) if c]

    def set_external_id(self, tenant_id: str, contract_id: str, system: str, external_id: str | None) -> None:
        """Point ``system``'s id at a contract (one per system per contract);
        ``None`` removes the pointer."""
        key = {"PK": f"CON#{contract_id}", "SK": f"EXT#{system}"}
        if not external_id:
            self.table().delete_item(Key=key)
            return
        self.table().put_item(Item={**key, "entityType": "EXTERNAL_ID", "contractId": contract_id,
                                    "tenantId": tenant_id, "system": system, "externalId": str(external_id),
                                    "GSI1PK": f"EXT#{tenant_id}#{system}#{external_id}", "GSI1SK": f"CON#{contract_id}"})

    def find_contract(self, tenant_id: str, system: str, external_id: str) -> str | None:
        """contractId carrying ``system``'s id, or None."""
        from boto3.dynamodb.conditions import Key

        items = self.query_all(IndexName="GSI1",
                               KeyConditionExpression=Key("GSI1PK").eq(f"EXT#{tenant_id}#{system}#{external_id}"))
        return next((str(i["contractId"]) for i in items if i.get("contractId")), None)

    def reconcile_state(self, tenant_id: str) -> dict[str, Any]:
        return clean_item(self.table().get_item(Key={"PK": f"T#{tenant_id}", "SK": "RECONCILE"}).get("Item")) or {}

    def put_reconcile_state(self, tenant_id: str, state: dict[str, Any]) -> None:
        self.table().put_item(Item=_to_ddb({**state, "PK": f"T#{tenant_id}", "SK": "RECONCILE",
                                            "entityType": "RECONCILE", "tenantId": tenant_id}))


# ---------------------------------------------------------------------------
# govern-metrics
# ---------------------------------------------------------------------------


class MetricsRepository(_Repository):
    """Daily trend counters (atomic ADD) and one-shot idempotency markers."""

    table_setting = "metrics_table"

    def claim(self, scope: str, marker_id: str, days: int = 7) -> bool:
        """True the first time (scope, marker) is claimed, False on a repeat."""
        return self.put_new({"PK": scope, "SK": f"SEEN#{marker_id}", "entityType": "MARKER", "ttl": ttl_in(days)})

    def release(self, scope: str, marker_id: str) -> None:
        self.table().delete_item(Key={"PK": scope, "SK": f"SEEN#{marker_id}"})

    def add(self, tenant_id: str, day: str, increments: dict[str, float]) -> None:
        """Atomic ADD of ``increments`` onto the tenant's day item."""
        if not increments:
            return
        names = {"#t": "tenantId", "#d": "day", "#e": "entityType"}
        values: dict[str, Any] = {":t": tenant_id, ":d": day, ":e": "METRICS_DAY"}
        adds = []
        for i, (k, v) in enumerate(sorted(increments.items())):
            names[f"#a{i}"] = k
            values[f":a{i}"] = v
            adds.append(f"#a{i} :a{i}")
        self.table().update_item(Key={"PK": f"T#{tenant_id}", "SK": f"D#{day}"},
                                 UpdateExpression="SET #t = :t, #d = :d, #e = :e ADD " + ", ".join(adds),
                                 ExpressionAttributeNames=names, ExpressionAttributeValues=_to_ddb(values))

    def days(self, tenant_id: str, days: list[str]) -> dict[str, dict[str, Any]]:
        """{yyyy-mm-dd: counters} for the days that have any."""
        out: dict[str, dict[str, Any]] = {}
        for item in self.batch_get([{"PK": f"T#{tenant_id}", "SK": f"D#{d}"} for d in days]):
            out[str(item["SK"])[2:]] = {k: _from_ddb(v) for k, v in item.items() if k not in _KEY_ATTRS}
        return out

    def replace_days(self, tenant_id: str, per_day: dict[str, dict[str, float]]) -> None:
        """Rebuild: delete the tenant's day items, write ``per_day``."""
        from boto3.dynamodb.conditions import Key

        for item in self.query_all(KeyConditionExpression=Key("PK").eq(f"T#{tenant_id}") & Key("SK").begins_with("D#"),
                                   ProjectionExpression="PK, SK"):
            self.table().delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        for day, counters in per_day.items():
            self.table().put_item(Item=_to_ddb({"PK": f"T#{tenant_id}", "SK": f"D#{day}", "entityType": "METRICS_DAY",
                                                "tenantId": tenant_id, "day": day, **counters}))


contracts = ContractRepository()
activity = ActivityLog()
config = ConfigRepository()
sync = SyncRepository()
metrics = MetricsRepository()
