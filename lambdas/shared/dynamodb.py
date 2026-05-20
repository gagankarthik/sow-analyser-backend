"""Single-table DynamoDB helpers.

Key conventions
---------------
    PK = DOC#<docId>      SK = META                 → Document
    PK = DOC#<docId>      SK = V#<n>                → Version
    PK = DOC#<docId>      SK = CHG#<changeId>       → Change
    PK = DOC#<docId>      SK = LINK#<parentId>      → Lineage (child → parent)
    PK = DOC#<parentId>   SK = CHILD#<childId>      → Lineage (parent → child, reverse)
    PK = CACHE#<sha256>   SK = EMBEDDING            → Embedding cache (stage 3)
    PK = TENANT#<id>      SK = DOC#<docId>          → GSI inverse for tenant listings

All items carry `entityType` so a GSI on it can power admin queries.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from botocore.exceptions import ClientError

from .aws import dynamodb_resource
from .config import settings
from .logger import get_logger
from .schema import now_iso

log = get_logger("blue-iq.ddb")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table():
    name = settings.table_name
    if not name:
        raise RuntimeError("TABLE_NAME env var is not set")
    return dynamodb_resource().Table(name)


def _to_ddb(value: Any) -> Any:
    """Recursively convert floats → Decimal (DDB doesn't accept floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _put(item: dict[str, Any], condition: str | None = None) -> None:
    item = _to_ddb(item)
    kwargs: dict[str, Any] = {"Item": item}
    if condition:
        kwargs["ConditionExpression"] = condition
    try:
        _table().put_item(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info("ddb.put.idempotent_skip", pk=item.get("PK"), sk=item.get("SK"))
            return
        raise


# ---------------------------------------------------------------------------
# Document records
# ---------------------------------------------------------------------------


def put_doc_meta(doc: dict[str, Any]) -> None:
    """Upsert the META record for a document."""
    doc_id = doc["docId"]
    tenant_id = doc["tenantId"]
    item = {
        "PK": f"DOC#{doc_id}",
        "SK": "META",
        "GSI1PK": f"TENANT#{tenant_id}",
        "GSI1SK": f"DOC#{doc_id}",
        "entityType": "DOCUMENT",
        **doc,
        "updatedAt": now_iso(),
    }
    item.setdefault("createdAt", item["updatedAt"])
    _put(item)


def update_doc_fields(doc_id: str, fields: dict[str, Any]) -> None:
    """Update arbitrary fields on a document META record (title, lifecycle, docType, etc.)."""
    expr_parts = ["updatedAt = :ts"]
    names: dict[str, str] = {}
    values: dict[str, Any] = {":ts": now_iso()}
    for i, (k, v) in enumerate(fields.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = _to_ddb(v)
        expr_parts.append(f"{nk} = {vk}")
    kwargs: dict[str, Any] = {
        "Key": {"PK": f"DOC#{doc_id}", "SK": "META"},
        "UpdateExpression": "SET " + ", ".join(expr_parts),
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    _table().update_item(**kwargs)


def update_status(doc_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    """Atomic status update."""
    extra = extra or {}
    expr_parts = ["#s = :s", "#u = :u"]
    names = {"#s": "status", "#u": "updatedAt"}
    values: dict[str, Any] = {":s": status, ":u": now_iso()}
    for i, (k, v) in enumerate(extra.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = v
        expr_parts.append(f"{nk} = {vk}")
    _table().update_item(
        Key={"PK": f"DOC#{doc_id}", "SK": "META"},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=_to_ddb(values),
    )


def put_version(version: dict[str, Any]) -> None:
    doc_id = version["docId"]
    n = version["versionNumber"]
    item = {
        "PK": f"DOC#{doc_id}",
        "SK": f"V#{n:06d}",
        "entityType": "VERSION",
        **version,
        "createdAt": version.get("createdAt", now_iso()),
    }
    # Idempotent: same docId+version is a no-op.
    _put(item, condition="attribute_not_exists(PK) AND attribute_not_exists(SK)")


def put_change(change: dict[str, Any]) -> None:
    doc_id = change["docId"]
    change_id = change["changeId"]
    item = {
        "PK": f"DOC#{doc_id}",
        "SK": f"CHG#{change_id}",
        "entityType": "CHANGE",
        **change,
        "createdAt": change.get("createdAt", now_iso()),
    }
    _put(item)


def put_lineage(parent_id: str, child_id: str) -> None:
    """Write both forward and reverse adjacency edges."""
    now = now_iso()
    _put(
        {
            "PK": f"DOC#{child_id}",
            "SK": f"LINK#{parent_id}",
            "entityType": "LINEAGE_PARENT",
            "parentId": parent_id,
            "childId": child_id,
            "createdAt": now,
        }
    )
    _put(
        {
            "PK": f"DOC#{parent_id}",
            "SK": f"CHILD#{child_id}",
            "entityType": "LINEAGE_CHILD",
            "parentId": parent_id,
            "childId": child_id,
            "createdAt": now,
        }
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_doc_meta(doc_id: str) -> dict[str, Any] | None:
    resp = _table().get_item(Key={"PK": f"DOC#{doc_id}", "SK": "META"})
    return resp.get("Item")


def query_doc_versions(doc_id: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
        & Key("SK").begins_with("V#")
    )
    return resp.get("Items", [])


def query_doc_changes(doc_id: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
        & Key("SK").begins_with("CHG#")
    )
    return resp.get("Items", [])


def query_doc_children(doc_id: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
        & Key("SK").begins_with("CHILD#")
    )
    return resp.get("Items", [])


def query_doc_parents(doc_id: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
        & Key("SK").begins_with("LINK#")
    )
    return resp.get("Items", [])


# ---------------------------------------------------------------------------
# Tenant document listing
# ---------------------------------------------------------------------------


def list_tenant_docs(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return all document META records for a tenant via GSI1."""
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant_id}"),
        Limit=limit,
    )
    items = resp.get("Items", [])
    # Strip DDB internals and return clean META dicts.
    return [
        {k: v for k, v in item.items() if not k.startswith("GSI") and k not in ("PK", "SK", "entityType")}
        for item in items
    ]


# ---------------------------------------------------------------------------
# Version management — delete with rollback
# ---------------------------------------------------------------------------


def delete_doc_version(doc_id: str, version_number: int) -> dict[str, Any] | None:
    """Delete a specific version record and roll META back to the previous version.

    Returns the new META record if a previous version exists, or None if the
    document is now empty (caller should decide whether to hard-delete META too).
    """
    # Delete the version record.
    _table().delete_item(Key={"PK": f"DOC#{doc_id}", "SK": f"V#{version_number:06d}"})

    # Find the highest remaining version.
    remaining = sorted(
        query_doc_versions(doc_id),
        key=lambda v: v.get("SK", ""),
        reverse=True,
    )
    if not remaining:
        return None

    latest = remaining[0]
    latest_n = int((latest.get("SK") or "V#000000")[2:])

    # Update META to reflect the latest surviving version.
    _table().update_item(
        Key={"PK": f"DOC#{doc_id}", "SK": "META"},
        UpdateExpression="SET latestVersion = :v, updatedAt = :ts",
        ExpressionAttributeValues=_to_ddb({":v": latest_n, ":ts": now_iso()}),
    )
    return get_doc_meta(doc_id)


def delete_doc_entirely(doc_id: str) -> None:
    """Remove all DynamoDB records for a document (META, versions, changes, lineage)."""
    from boto3.dynamodb.conditions import Key

    resp = _table().query(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
    )
    items = resp.get("Items", [])
    with _table().batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


def get_cached_embedding(content_hash: str) -> list[float] | None:
    resp = _table().get_item(
        Key={"PK": f"CACHE#{content_hash}", "SK": "EMBEDDING"}
    )
    item = resp.get("Item")
    if not item:
        return None
    vec = item.get("vector")
    if vec is None:
        return None
    # Decimal → float
    return [float(x) for x in vec]


def put_cached_embedding(content_hash: str, vector: list[float], model: str) -> None:
    _put(
        {
            "PK": f"CACHE#{content_hash}",
            "SK": "EMBEDDING",
            "entityType": "EMB_CACHE",
            "vector": [Decimal(str(x)) for x in vector],
            "model": model,
            "createdAt": now_iso(),
        }
    )


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def batch_put(items: Iterable[dict[str, Any]]) -> None:
    """Best-effort batch write with retry on UnprocessedItems."""
    with _table().batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for it in items:
            batch.put_item(Item=_to_ddb(it))
