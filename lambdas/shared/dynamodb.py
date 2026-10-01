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

import json
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


def update_doc_fields(doc_id: str, fields: dict[str, Any], remove: Iterable[str] = ()) -> None:
    """Set (and optionally remove) fields on a document META record, leaving every
    other attribute — ownership, project membership, keys — untouched."""
    expr_parts = ["updatedAt = :ts"]
    names: dict[str, str] = {}
    values: dict[str, Any] = {":ts": now_iso()}
    for i, (k, v) in enumerate(fields.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = _to_ddb(v)
        expr_parts.append(f"{nk} = {vk}")
    expression = "SET " + ", ".join(expr_parts)
    drop = [k for k in remove if k not in fields]
    if drop:
        for j, k in enumerate(drop):
            names[f"#r{j}"] = k
        expression += " REMOVE " + ", ".join(f"#r{j}" for j in range(len(drop)))
    kwargs: dict[str, Any] = {
        "Key": {"PK": f"DOC#{doc_id}", "SK": "META"},
        "UpdateExpression": expression,
        "ExpressionAttributeValues": values,
        # update_item upserts: without this, updating a document that was just
        # deleted would recreate a tenant-less ghost META row.
        "ConditionExpression": "attribute_exists(PK)",
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
        # Fails (ConditionalCheckFailedException) when the document was deleted
        # mid-pipeline, which stops the run instead of resurrecting the row.
        ConditionExpression="attribute_exists(PK)",
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


def _query_all(**kwargs: Any) -> list[dict[str, Any]]:
    """Run a Query and follow LastEvaluatedKey — a single Query page stops at
    1 MB, so an un-paginated read silently drops the rest."""
    items: list[dict[str, Any]] = []
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _query_doc_prefix(doc_id: str, sk_prefix: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    return _query_all(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}")
        & Key("SK").begins_with(sk_prefix)
    )


def query_doc_versions(doc_id: str) -> list[dict[str, Any]]:
    return _query_doc_prefix(doc_id, "V#")


def query_doc_changes(doc_id: str) -> list[dict[str, Any]]:
    return _query_doc_prefix(doc_id, "CHG#")


def query_doc_children(doc_id: str) -> list[dict[str, Any]]:
    return _query_doc_prefix(doc_id, "CHILD#")


def query_doc_parents(doc_id: str) -> list[dict[str, Any]]:
    return _query_doc_prefix(doc_id, "LINK#")


# ---------------------------------------------------------------------------
# Tenant document listing
# ---------------------------------------------------------------------------


def list_tenant_docs(tenant_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Return the tenant's document META records via GSI1 (at most `limit`).

    Pages through the index: one Query page stops at 1 MB, which a tenant with
    large summaries reaches well before `limit` rows, silently hiding documents.
    """
    from boto3.dynamodb.conditions import Key

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(f"TENANT#{tenant_id}"),
    }
    while len(items) < limit:
        resp = _table().query(Limit=limit - len(items), **kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    # Strip DDB internals and return clean META dicts.
    return [
        {k: v for k, v in item.items() if not k.startswith("GSI") and k not in ("PK", "SK", "entityType")}
        for item in items
    ]


# ---------------------------------------------------------------------------
# Projects state — per-tenant project groupings (cloud-stored, replaces the
# old browser-localStorage projects). One item per tenant. No GSI keys, so it
# never shows up in `list_tenant_docs`. Stored as a JSON string to sidestep
# DynamoDB's number/empty-value quirks for the nested project list.
# ---------------------------------------------------------------------------


def get_projects_state(tenant_id: str) -> list[dict[str, Any]]:
    """Return the tenant's saved projects (list of {id,name,client?,createdAt,docIds})."""
    resp = _table().get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROJECTS"})
    item = resp.get("Item")
    if not item:
        return []
    raw = item.get("projects")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return raw if isinstance(raw, list) else []


def put_projects_state(tenant_id: str, projects: list[dict[str, Any]]) -> None:
    """Persist the tenant's projects (overwrites the single per-tenant record)."""
    _table().put_item(
        Item={
            "PK": f"TENANT#{tenant_id}",
            "SK": "PROJECTS",
            "entityType": "ProjectsState",
            "projects": json.dumps(projects),
            "updatedAt": now_iso(),
        }
    )


# ---------------------------------------------------------------------------
# Compliance packs — per-tenant set of enabled regulatory frameworks. One item
# per tenant (SK=COMPLIANCE); read by the classify stage to grade documents and
# by the API so the settings UI is authoritative across browsers.
# ---------------------------------------------------------------------------


def get_compliance_packs(tenant_id: str) -> list[str] | None:
    """Return the tenant's enabled compliance-pack ids, or None if never set.

    None (no row) means "use system defaults"; an empty list means the tenant has
    explicitly disabled every pack.
    """
    resp = _table().get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "COMPLIANCE"})
    item = resp.get("Item")
    if not item:
        return None
    packs = item.get("packs")
    return [str(p) for p in packs] if isinstance(packs, list) else None


def put_compliance_packs(tenant_id: str, packs: list[str]) -> None:
    """Persist the tenant's enabled compliance packs (single per-tenant record)."""
    _table().put_item(
        Item={
            "PK": f"TENANT#{tenant_id}",
            "SK": "COMPLIANCE",
            "entityType": "ComplianceConfig",
            "packs": [str(p) for p in packs],
            "updatedAt": now_iso(),
        }
    )


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

    # Paginated + keys-only: change rows carry clause text, so a document can
    # exceed one 1 MB Query page — an un-paginated delete left rows behind.
    items = _query_all(
        KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}"),
        ProjectionExpression="PK, SK",
    )
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


def get_cached_embeddings(
    content_hashes: list[str], *, dimensions: int | None = None, max_workers: int = 8
) -> dict[str, list[float]]:
    """Look up many cached embeddings at once (bounded parallel GetItems on the
    thread-safe low-level client — one round-trip per clause in sequence used to
    dominate the embed stage). A cached vector of the wrong size is ignored, so a
    changed embedding model can never be served from the cache. Lookup failures
    are treated as misses."""
    from boto3.dynamodb.types import TypeDeserializer

    from .aws import dynamodb_client
    from .concurrency import bounded_map

    wanted = [h for h in dict.fromkeys(content_hashes) if h]
    if not wanted:
        return {}
    table_name = settings.table_name
    if not table_name:
        raise RuntimeError("TABLE_NAME env var is not set")
    ddb = dynamodb_client()
    deserialise = TypeDeserializer().deserialize

    def fetch(content_hash: str) -> list[float] | None:
        resp = ddb.get_item(
            TableName=table_name,
            Key={"PK": {"S": f"CACHE#{content_hash}"}, "SK": {"S": "EMBEDDING"}},
            ProjectionExpression="#v",
            ExpressionAttributeNames={"#v": "vector"},
        )
        raw = (resp.get("Item") or {}).get("vector")
        if not raw:
            return None
        vec = [float(x) for x in deserialise(raw)]
        return vec if vec and (dimensions is None or len(vec) == dimensions) else None

    out: dict[str, list[float]] = {}
    for content_hash, (vec, error) in zip(wanted, bounded_map(fetch, wanted, max_workers)):
        if error is None and vec:
            out[content_hash] = vec
    return out


def put_cached_embeddings(
    vectors: dict[str, list[float]], model: str, *, max_workers: int = 8
) -> int:
    """Store many embeddings (bounded parallel PutItems). Best-effort: returns how
    many were written; a failed cache write never fails the stage."""
    from boto3.dynamodb.types import TypeSerializer

    from .aws import dynamodb_client
    from .concurrency import bounded_map

    if not vectors:
        return 0
    table_name = settings.table_name
    if not table_name:
        return 0
    ddb = dynamodb_client()
    serialise = TypeSerializer().serialize
    now = now_iso()

    def store(pair: tuple[str, list[float]]) -> bool:
        content_hash, vec = pair
        ddb.put_item(TableName=table_name, Item={
            "PK": {"S": f"CACHE#{content_hash}"}, "SK": {"S": "EMBEDDING"},
            "entityType": {"S": "EMB_CACHE"}, "model": {"S": model}, "createdAt": {"S": now},
            "vector": serialise([Decimal(str(x)) for x in vec]),
        })
        return True

    return sum(1 for ok, error in bounded_map(store, list(vectors.items()), max_workers) if error is None and ok)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def batch_put(items: Iterable[dict[str, Any]]) -> None:
    """Best-effort batch write with retry on UnprocessedItems."""
    with _table().batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for it in items:
            batch.put_item(Item=_to_ddb(it))


# ---------------------------------------------------------------------------
# Projects and membership (access control — see shared/access.py)
#
#   PK = PROJ#<id>   SK = META              project (name, owner, docIds, rev)
#   PK = PROJ#<id>   SK = OWNER             GSI1PK = USER#<sub>      GSI1SK = PROJ#<id>
#   PK = PROJ#<id>   SK = MEMBER#<email>    GSI1PK = MEMBER#<email>  GSI1SK = PROJ#<id>
#
# Every lookup is by key or by GSI1 partition — nothing scans the table.
# Project META is changed with optimistic locking on `rev`, so two people
# editing the same project cannot silently overwrite each other.
# ---------------------------------------------------------------------------

_PROJECT_INTERNAL = ("PK", "SK", "GSI1PK", "GSI1SK", "entityType")
_MAX_WRITE_RETRIES = 5


class ProjectConflict(RuntimeError):
    """A project could not be written after several optimistic-lock retries."""


def _strip(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {k: _from_ddb(v) for k, v in item.items() if k not in _PROJECT_INTERNAL}


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_ddb(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_from_ddb(v) for v in value)
    return value


def _is_conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _batch_get(keys: list[dict[str, str]]) -> list[dict[str, Any]]:
    """BatchGetItem (100 keys per call), following UnprocessedKeys."""
    if not keys:
        return []
    name = settings.table_name
    if not name:
        raise RuntimeError("TABLE_NAME env var is not set")
    out: list[dict[str, Any]] = []
    resource = dynamodb_resource()
    for start in range(0, len(keys), 100):
        pending: dict[str, Any] = {name: {"Keys": keys[start:start + 100]}}
        for _ in range(6):
            resp = resource.batch_get_item(RequestItems=pending)
            out.extend((resp.get("Responses") or {}).get(name, []))
            pending = resp.get("UnprocessedKeys") or {}
            if not pending:
                break
    return out


def get_docs(doc_ids: list[str]) -> list[dict[str, Any]]:
    """META records for many documents (missing ids are simply absent)."""
    ids = [d for d in dict.fromkeys(doc_ids) if d]
    return _batch_get([{"PK": f"DOC#{d}", "SK": "META"} for d in ids])


def get_project(project_id: str) -> dict[str, Any] | None:
    resp = _table().get_item(Key={"PK": f"PROJ#{project_id}", "SK": "META"})
    return _strip(resp.get("Item"))


def get_projects(project_ids: list[str]) -> list[dict[str, Any]]:
    ids = [p for p in dict.fromkeys(project_ids) if p]
    items = _batch_get([{"PK": f"PROJ#{p}", "SK": "META"} for p in ids])
    by_id = {i.get("projectId"): _strip(i) for i in items}
    return [by_id[p] for p in ids if p in by_id]


def project_roles_for(sub: str | None, email: str | None) -> dict[str, str]:
    """{projectId: role} for a user: projects they own (by sub) and projects they
    were added to (by verified email). Two GSI1 partition reads."""
    from boto3.dynamodb.conditions import Key

    rank = {"viewer": 1, "editor": 2, "owner": 3}
    roles: dict[str, str] = {}
    partitions = []
    if sub:
        partitions.append(f"USER#{sub}")
    if email:
        partitions.append(f"MEMBER#{email}")
    for partition in partitions:
        for item in _query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(partition)):
            pid = item.get("projectId")
            # Once a membership has been accepted it belongs to THAT account: a
            # different user who later holds the same address does not inherit it.
            if partition.startswith("MEMBER#") and item.get("sub") and sub and item["sub"] != sub:
                continue
            role = str(item.get("role") or "viewer")
            role = role if role in rank else "viewer"
            if pid and rank[role] > rank.get(roles.get(pid, ""), 0):
                roles[pid] = role
    return roles


def create_project(project: dict[str, Any]) -> bool:
    """Create a project and its owner pointer. Returns False — writing nothing —
    if a project with this id already exists. Ids are global, so re-using
    another project's id can never take that project over."""
    project_id = project["projectId"]
    now = now_iso()
    item = {
        "PK": f"PROJ#{project_id}", "SK": "META", "entityType": "PROJECT",
        "docIds": [], **project, "rev": 1,
        "createdAt": project.get("createdAt") or now, "updatedAt": now,
    }
    try:
        _table().put_item(Item=_to_ddb(item), ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise
    if project.get("ownerSub"):
        put_project_owner(project_id, project["ownerSub"])
    return True


def put_project_owner(project_id: str, owner_sub: str) -> None:
    """The pointer that lets a user find the projects they own by their sub."""
    _table().put_item(Item={
        "PK": f"PROJ#{project_id}", "SK": "OWNER", "entityType": "PROJECT_OWNER",
        "GSI1PK": f"USER#{owner_sub}", "GSI1SK": f"PROJ#{project_id}",
        "projectId": project_id, "role": "owner", "createdAt": now_iso(),
    })


def mutate_project(project_id: str, change: Any) -> dict[str, Any] | None:
    """Read-modify-write a project under optimistic locking.

    ``change(project)`` edits the dict in place (or returns False to make no
    write). Returns the stored project, or None if it does not exist.
    """
    for _ in range(_MAX_WRITE_RETRIES):
        resp = _table().get_item(Key={"PK": f"PROJ#{project_id}", "SK": "META"})
        item = resp.get("Item")
        if not item:
            return None
        project = _from_ddb(dict(item))
        rev = project.get("rev") or 0
        if change(project) is False:
            return _strip(project)
        project["rev"] = rev + 1
        project["updatedAt"] = now_iso()
        try:
            if rev:
                _table().put_item(Item=_to_ddb(project), ConditionExpression="rev = :rev",
                                  ExpressionAttributeValues={":rev": rev})
            else:
                _table().put_item(Item=_to_ddb(project), ConditionExpression="attribute_not_exists(rev)")
        except ClientError as exc:
            if _is_conditional_failure(exc):
                continue                # someone else wrote first — re-read and retry
            raise
        return _strip(project)
    raise ProjectConflict(f"project {project_id} is being changed by someone else; try again")


def delete_project(project_id: str) -> list[str]:
    """Delete a project, its owner pointer and its memberships. Documents are NOT
    deleted — they stay with whoever uploaded them. Returns the docIds it held."""
    from boto3.dynamodb.conditions import Key

    project = get_project(project_id) or {}
    items = _query_all(KeyConditionExpression=Key("PK").eq(f"PROJ#{project_id}"),
                       ProjectionExpression="PK, SK")
    for item in items:
        _table().delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return list(project.get("docIds") or [])


def list_project_members(project_id: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    items = _query_all(
        KeyConditionExpression=Key("PK").eq(f"PROJ#{project_id}") & Key("SK").begins_with("MEMBER#")
    )
    return [m for m in (_strip(i) for i in items) if m]


def get_project_member(project_id: str, email: str) -> dict[str, Any] | None:
    resp = _table().get_item(Key={"PK": f"PROJ#{project_id}", "SK": f"MEMBER#{email}"})
    return _strip(resp.get("Item"))


def put_project_member(project_id: str, member: dict[str, Any]) -> None:
    """Add or replace a membership. ``member["email"]`` must be lower-cased."""
    email = member["email"]
    _table().put_item(Item=_to_ddb({
        "PK": f"PROJ#{project_id}", "SK": f"MEMBER#{email}", "entityType": "PROJECT_MEMBER",
        "GSI1PK": f"MEMBER#{email}", "GSI1SK": f"PROJ#{project_id}",
        "projectId": project_id, **member,
    }))


def update_project_member(project_id: str, email: str, fields: dict[str, Any]) -> bool:
    """Change fields of an EXISTING membership. Returns False (writing nothing) if
    the row is gone — so a change racing with a removal can never re-create it."""
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": _to_ddb(v) for i, v in enumerate(fields.values())}
    try:
        _table().update_item(
            Key={"PK": f"PROJ#{project_id}", "SK": f"MEMBER#{email}"},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def delete_project_member(project_id: str, email: str) -> None:
    _table().delete_item(Key={"PK": f"PROJ#{project_id}", "SK": f"MEMBER#{email}"})


def set_doc_projects(doc_id: str, project_ids: list[str]) -> None:
    """Rewrite a document's ``projectIds`` hint. Best-effort bookkeeping: access
    is always re-checked against the project record, never trusted from here."""
    try:
        update_doc_fields(doc_id, {"projectIds": sorted(set(project_ids))})
    except ClientError as exc:
        if not _is_conditional_failure(exc):   # the document was deleted
            raise


def related_doc_ids(doc_id: str) -> list[str]:
    """Documents that share a project with ``doc_id`` (excluding itself) — the
    set an amendment's parent may be found in besides the uploader's own."""
    meta = get_doc_meta(doc_id) or {}
    out: list[str] = []
    for project in get_projects(list(meta.get("projectIds") or [])):
        ids = project.get("docIds") or []
        if doc_id in ids:
            out.extend(d for d in ids if d != doc_id)
    return list(dict.fromkeys(out))
