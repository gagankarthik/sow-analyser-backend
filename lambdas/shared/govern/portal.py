"""Counterparty portal links — a reviewer shares one contract's open asks with
the other side, who can read them and upload a revised version without an
account (docs/GOVERN_API.md "Counterparty portal", GOVERN_ARCHITECTURE.md §2.6).

TOKEN MODEL. A link's token is 32 random bytes (``secrets.token_urlsafe``,
43 URL-safe characters). It is returned ONCE, inside the link's url, and never
stored: govern-config keeps only its SHA-256 and the link's metadata.

    PORTAL#<sha256(token)> / LINK          the link (looked up by GetItem on the hash)
    CON#<contractId>#PORTAL / L#<linkId>   per-contract index → the hash (list / revoke)

A request with a token hashes it and reads that one key: no scan, nothing to
enumerate. Unknown, malformed, expired and revoked tokens — and a link whose
contract is gone — all raise the same ``LinkGone`` (one 404 body), so a token
cannot be probed. The stored hash is compared in constant time. Tokens and
hashes are never logged; logs name the ``linkId``.

THE PUBLIC VIEW (``public_view``) is built from an allow-list: the contract's
title, the organization's name, what is needed (open items as clause label,
a plain tier word, a short ask and the matrix's suggested wording — never the
matrix positions, a reviewer's free text or anything else internal), the
requested date, the link's note and whether uploads are allowed.

LIMITS. At most ``MAX_ACTIVE_LINKS`` active links per contract; each link
answers at most ``MAX_OPENS`` views and ``MAX_UPLOADS`` upload URLs (atomic
counters with a condition), on top of the API Gateway route throttling.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

from ..config import settings
from ..dynamodb import _to_ddb
from ..uploads import clean_upload_filename
from . import revisions, store, workflow
from .store import clean_item, iso, parse_iso

TOKEN_BYTES = 32
MAX_ACTIVE_LINKS = 5
MIN_EXPIRY_DAYS, MAX_EXPIRY_DAYS, DEFAULT_EXPIRY_DAYS = 1, 60, 14
MAX_NOTE_CHARS = 1000
MAX_OPENS = 500            # views per link, ever
MAX_UPLOADS = 20           # upload URLs per link, ever
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_LISTED = 50
MAX_NEEDS = 50
GONE_MESSAGE = "This link has expired or isn't valid. Ask your contact for a new one."

# The counterparty, as the activity log names it (an RFC 2606 address, never mailed).
PORTAL_ACTOR = {"email": "portal@counterparty.invalid", "name": "Counterparty via portal link"}

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Exactly what GET /portal/{token} returns (tests assert this key set).
PUBLIC_VIEW_KEYS = ("contractTitle", "organizationName", "needs", "requestedDate", "note", "allowUpload", "expiresAt")
NEED_KEYS = ("clauseLabel", "tier", "title", "suggestedLanguage")

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_LINK_ID_RE = re.compile(r"[a-f0-9]{16}")
_TIER_WORD = {"unacceptable": "Required change", "deviates": "Change requested", "fallback": "Change requested",
              "missing": "Missing clause", "review": "Please confirm"}
_UPLOAD_CLOSED_STATES = workflow.TERMINAL_STATES | {"signed", "active"}


class LinkGone(workflow.NotFound):
    """Unknown, malformed, expired or revoked — deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__(GONE_MESSAGE)


class RateLimited(workflow.GovernError):
    status = 429
    code = "rate_limited"


class UploadsOff(workflow.GovernError):
    status = 403
    code = "upload_not_allowed"


class TooManyLinks(workflow.GovernError):
    status = 409
    code = "limit_reached"


# ---------------------------------------------------------------------------
# Keys and small helpers
# ---------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _link_key(digest: str) -> dict[str, str]:
    return {"PK": f"PORTAL#{digest}", "SK": "LINK"}


def _index_pk(contract_id: str) -> str:
    return f"CON#{contract_id}#PORTAL"


def _table() -> Any:
    return store.config.table()


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def link_status(link: dict[str, Any], now: datetime | None = None) -> str:
    if link.get("revokedAt"):
        return "revoked"
    expires = parse_iso(link.get("expiresAt"))
    if expires is None or expires <= _now(now):
        return "expired"
    return "active"


def link_view(link: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What reviewers see of a link: never the token or its hash."""
    return {"id": link.get("linkId"), "status": link_status(link, now), "createdAt": link.get("createdAt"),
            "createdBy": link.get("createdBy"), "expiresAt": link.get("expiresAt"),
            "allowUpload": bool(link.get("allowUpload")), "note": link.get("note"),
            "opens": int(link.get("opens") or 0), "lastOpenedAt": link.get("lastOpenedAt"),
            "uploads": int(link.get("uploads") or 0), "revokedAt": link.get("revokedAt")}


def portal_url(token: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/portal/{token}"


# ---------------------------------------------------------------------------
# Reviewer side (govern-api, JWT)
# ---------------------------------------------------------------------------


def _clean_create(body: dict[str, Any]) -> tuple[int, bool, str | None]:
    unknown = set(body) - {"expiresInDays", "allowUpload", "note"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    days = body.get("expiresInDays", DEFAULT_EXPIRY_DAYS)
    if days is None:
        days = DEFAULT_EXPIRY_DAYS
    if isinstance(days, bool) or not isinstance(days, int) or not MIN_EXPIRY_DAYS <= days <= MAX_EXPIRY_DAYS:
        raise workflow.BadRequest(f"expiresInDays must be a whole number from {MIN_EXPIRY_DAYS} to {MAX_EXPIRY_DAYS}")
    allow = body.get("allowUpload", True)
    if allow is None:
        allow = True
    if not isinstance(allow, bool):
        raise workflow.BadRequest("allowUpload must be true or false")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise workflow.BadRequest("note must be a string")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise workflow.BadRequest(f"note must be at most {MAX_NOTE_CHARS} characters")
    return days, allow, note or None


def _stored_links(contract_id: str) -> list[dict[str, Any]]:
    """Every link of the contract (index partition → the hash items)."""
    index = store.config.children(_index_pk(contract_id), "L#")
    keys = [_link_key(str(i["tokenHash"])) for i in index if i.get("tokenHash")]
    if not keys:
        return []
    links = [clean_item(i) or {} for i in store.config.batch_get(keys)]
    return [link for link in links if link.get("contractId") == contract_id and link.get("linkId")]


def list_links(contract_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """The contract's links, newest first, as reviewers see them."""
    now_dt = _now(now)
    links = sorted(_stored_links(contract_id), key=lambda link: str(link.get("createdAt") or ""), reverse=True)
    return [link_view(link, now_dt) for link in links[:MAX_LISTED]]


def create_link(c: dict[str, Any], body: dict[str, Any], *, creator: dict[str, Any] | None, creator_sub: str,
                creator_email: str | None, upload_tenant_id: str, project_ids: list[str],
                now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """A new link: (its view, its url). The url carries the raw token and is
    never shown again. Uploads through it are filed exactly as the creator's
    own revision upload would be (their workspace, the projects they may
    upload to), so everyone who sees the contract sees the revision."""
    days, allow_upload, note = _clean_create(body)
    if c.get("state") in workflow.TERMINAL_STATES:
        raise workflow.InvalidTransition("Reopen this contract before sharing it.")
    now_dt = _now(now)
    contract_id = c["contractId"]
    active = [link for link in _stored_links(contract_id) if link_status(link, now_dt) == "active"]
    if len(active) >= MAX_ACTIVE_LINKS:
        raise TooManyLinks(f"This contract already has {MAX_ACTIVE_LINKS} active links. Revoke one first.")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    digest = token_hash(token)
    link_id = uuid.uuid4().hex[:16]
    now_s = iso(now_dt)
    expires_s = iso(now_dt + timedelta(days=days))
    # Index first: a link that exists is always listed (and so revocable).
    if not store.config.put_new({"PK": _index_pk(contract_id), "SK": f"L#{link_id}", "entityType": "PORTAL_INDEX",
                                 "linkId": link_id, "tokenHash": digest, "contractId": contract_id,
                                 "createdAt": now_s}):
        raise store.ContractConflict("portal link id collision; try again")
    link = {"linkId": link_id, "tokenHash": digest, "contractId": contract_id, "tenantId": c["tenantId"],
            "createdAt": now_s, "createdBy": creator, "expiresAt": expires_s, "allowUpload": allow_upload,
            "note": note, "opens": 0, "uploads": 0,
            # Who a counterparty's upload is filed as (never shown to the counterparty).
            "uploadTenantId": upload_tenant_id, "ownerSub": creator_sub, "ownerEmail": creator_email,
            "projectIds": list(project_ids)}
    if not store.config.put_new({**link, **_link_key(digest), "entityType": "PORTAL_LINK"}):
        raise store.ContractConflict("portal token collision; try again")
    noun = workflow.counterparty_noun(c)
    workflow.record_system_entry(
        c, "portal_link_created",
        f"{workflow.display(creator)} shared a link with the {noun}; it expires on {expires_s[:10]}.",
        detail={"linkId": link_id, "expiresAt": expires_s, "allowUpload": allow_upload}, actor=creator)
    return link_view(link, now_dt), portal_url(token)


def revoke_link(c: dict[str, Any], link_id: str, actor: dict[str, Any] | None,
                now: datetime | None = None) -> dict[str, Any]:
    """Revoke a link (idempotent: revoking a revoked link changes nothing)."""
    if not isinstance(link_id, str) or not _LINK_ID_RE.fullmatch(link_id):
        raise workflow.NotFound("Link not found")
    contract_id = c["contractId"]
    index = clean_item(_table().get_item(Key={"PK": _index_pk(contract_id), "SK": f"L#{link_id}"}).get("Item"))
    if not index or not index.get("tokenHash"):
        raise workflow.NotFound("Link not found")
    key = _link_key(str(index["tokenHash"]))
    now_dt = _now(now)
    now_s = iso(now_dt)
    try:
        _table().update_item(Key=key, UpdateExpression="SET #r = :now, #rb = :by",
                             ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#r)",
                             ExpressionAttributeNames={"#r": "revokedAt", "#rb": "revokedBy"},
                             ExpressionAttributeValues=_to_ddb({":now": now_s, ":by": actor}))
        changed = True
    except ClientError as exc:
        if not _conditional_failure(exc):
            raise
        changed = False
    link = clean_item(_table().get_item(Key=key).get("Item"))
    if not link:
        raise workflow.NotFound("Link not found")
    if changed:
        workflow.record_system_entry(c, "portal_link_revoked", f"{workflow.display(actor)} revoked a shared link.",
                                     detail={"linkId": link_id}, actor=actor)
    return link_view(link, now_dt)


# ---------------------------------------------------------------------------
# Counterparty side (govern-portal, no JWT: the token is the credential)
# ---------------------------------------------------------------------------


def resolve(token: Any, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(link, contract) for an active token, else ``LinkGone``."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise LinkGone()
    digest = token_hash(token)
    link = clean_item(_table().get_item(Key=_link_key(digest)).get("Item")) or {}
    stored = str(link.get("tokenHash") or "")
    if not hmac.compare_digest(stored.encode("ascii", "ignore"), digest.encode("ascii")):
        raise LinkGone()
    if link_status(link, now) != "active":
        raise LinkGone()
    c = store.contracts.get(str(link.get("contractId") or ""))
    if c is None or c.get("tenantId") != link.get("tenantId"):
        raise LinkGone()
    return link, c


def _count(link: dict[str, Any], field: str, cap: int, extra_set: dict[str, Any] | None = None) -> None:
    """Atomically add one to ``field`` while it is below ``cap`` and the link
    is not revoked; ``RateLimited`` at the cap."""
    names = {"#n": field, "#r": "revokedAt"}
    values: dict[str, Any] = {":one": 1, ":cap": cap}
    sets = []
    for i, (k, v) in enumerate((extra_set or {}).items()):
        names[f"#s{i}"] = k
        values[f":s{i}"] = v
        sets.append(f"#s{i} = :s{i}")
    expr = (f"SET {', '.join(sets)} ADD #n :one" if sets else "ADD #n :one")
    key = _link_key(str(link["tokenHash"]))
    try:
        _table().update_item(Key=key, UpdateExpression=expr,
                             ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#r) AND #n < :cap",
                             ExpressionAttributeNames=names, ExpressionAttributeValues=_to_ddb(values))
    except ClientError as exc:
        if not _conditional_failure(exc):
            raise
        fresh = clean_item(_table().get_item(Key=key).get("Item")) or {}
        if fresh.get("revokedAt") or not fresh:
            raise LinkGone()
        raise RateLimited("This link has been used too many times. Ask your contact for a new one.")


def _first_today(link: dict[str, Any], day: str) -> bool:
    """True once per link per UTC day (the "opened the link" entry)."""
    try:
        _table().update_item(Key=_link_key(str(link["tokenHash"])), UpdateExpression="SET #d = :day",
                             ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#d) OR #d <> :day)",
                             ExpressionAttributeNames={"#d": "lastOpenedDay"},
                             ExpressionAttributeValues={":day": day})
        return True
    except ClientError as exc:
        if not _conditional_failure(exc):
            raise
        return False


def uploads_allowed(link: dict[str, Any], c: dict[str, Any]) -> bool:
    return bool(link.get("allowUpload")) and c.get("state") not in _UPLOAD_CLOSED_STATES


def _need(b: dict[str, Any], review_index: dict[str, Any]) -> dict[str, Any] | None:
    """One open item as the counterparty reads it. Built from the clause type,
    the tier and the matrix's suggested wording only: an item's own text can
    quote internal positions or a reviewer's notes, so it is never shared."""
    from .matrix import matrix_clause_label

    clause_type = b.get("clauseType") if isinstance(b.get("clauseType"), str) else None
    suggested = (str(b.get("suggestedLanguage") or "").strip() or None)
    if not clause_type and not suggested:
        return None
    idx = (review_index or {}).get(clause_type) or {} if clause_type else {}
    label = (str(idx.get("label") or "").strip() or (matrix_clause_label(clause_type) if clause_type else "Other terms"))[:200]
    tier = idx.get("tier") if b.get("source") == "sonar" else None
    if tier == "missing":
        title = f"Add a {label} clause"
    elif tier == "review":
        title = f"Confirm the {label} terms"
    else:
        title = f"Revise the {label} terms"
    return {"clauseLabel": label, "tier": _TIER_WORD.get(str(tier), "Requested"), "title": title,
            "suggestedLanguage": suggested[:4000] if suggested else None}


def public_view(link: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """The counterparty's page: exactly ``PUBLIC_VIEW_KEYS``."""
    org = (workflow.get_settings(c["tenantId"]).get("organization") or {}).get("name")
    needs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for b in store.contracts.blockers(c["contractId"]):
        if b.get("status", "open") != "open":
            continue
        need = _need(b, c.get("reviewIndex") or {})
        if need is None:
            continue
        key = tuple(need[k] for k in NEED_KEYS)
        if key in seen:
            continue
        seen.add(key)
        needs.append(need)
        if len(needs) >= MAX_NEEDS:
            break
    requested = c.get("requestedDate")
    view = {"contractTitle": str(c.get("title") or "Agreement")[:300],
            "organizationName": (str(org).strip()[:200] or None) if org else None,
            "needs": needs,
            "requestedDate": str(requested)[:10] if requested else None,
            "note": link.get("note") or None,
            "allowUpload": uploads_allowed(link, c),
            "expiresAt": link.get("expiresAt")}
    return {k: view[k] for k in PUBLIC_VIEW_KEYS}


def open_link(token: Any, now: datetime | None = None) -> dict[str, Any]:
    """GET /portal/{token}: count the view (capped), note the first view of
    the day in the activity log, return the public view."""
    now_dt = _now(now)
    link, c = resolve(token, now_dt)
    now_s = iso(now_dt)
    _count(link, "opens", MAX_OPENS, {"lastOpenedAt": now_s})
    if _first_today(link, now_s[:10]):
        workflow.record_system_entry(c, "portal_opened", "Counterparty opened the link.",
                                     detail={"linkId": link.get("linkId")}, actor=PORTAL_ACTOR)
    return public_view(link, c)


def _clean_upload(body: dict[str, Any]) -> tuple[str, str, int]:
    unknown = set(body) - {"filename", "size"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    filename, problem = clean_upload_filename(body.get("filename"))
    if problem or not filename:
        raise workflow.BadRequest(problem or "Invalid filename")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in CONTENT_TYPES:
        raise workflow.BadRequest("Upload a PDF or Word (.docx) file.")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise workflow.BadRequest("size must be the file's size in bytes")
    if size > MAX_UPLOAD_BYTES:
        raise workflow.BadRequest(f"The file is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    return filename, CONTENT_TYPES[ext], size


def start_upload(token: Any, body: dict[str, Any], *, raw_bucket: str, now: datetime | None = None) -> dict[str, Any]:
    """POST /portal/{token}/upload-url: a presigned PUT for a revised version,
    through the same revision path as a reviewer's upload. The type and the
    exact size are signed into the URL."""
    now_dt = _now(now)
    link, c = resolve(token, now_dt)
    if not uploads_allowed(link, c):
        raise UploadsOff("Uploads are turned off for this link.")
    filename, content_type, size = _clean_upload(body)
    if not raw_bucket:
        raise RuntimeError("RAW_BUCKET is not set")
    _count(link, "uploads", MAX_UPLOADS)
    filed_in = set(revisions.base_project_ids(c))
    project_ids = [p for p in link.get("projectIds") or [] if p in filed_in]
    upload_url, doc_id = revisions.presign_revision(
        c, raw_bucket=raw_bucket, tenant_id=str(link.get("uploadTenantId") or c["tenantId"]),
        owner_sub=str(link.get("ownerSub") or ""), owner_email=link.get("ownerEmail"), filename=filename,
        project_ids=project_ids, extra={"uploadedVia": "portal", "portalLinkId": link.get("linkId")},
        content_type=content_type, content_length=size)
    workflow.record_system_entry(c, "portal_upload", f"Counterparty uploaded a revision: {filename}.",
                                 detail={"linkId": link.get("linkId"), "docId": doc_id, "filename": filename},
                                 actor=PORTAL_ACTOR)
    return {"uploadUrl": upload_url, "headers": {"Content-Type": content_type},
            "expiresIn": revisions.UPLOAD_URL_TTL_S, "maxBytes": MAX_UPLOAD_BYTES}
