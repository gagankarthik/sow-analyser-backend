"""Govern e-signature: envelopes, signer links and the signing lifecycle
(docs/GOVERN_ESIGN.md).

    draft ──send──► sent ──every signer signed──► sealing ──govern-seal──► completed
                     ├── a signer declines ──► declined
                     ├── the reviewer voids ─► voided
                     └── the expiry passes ──► expired

STORAGE. One item per envelope in govern-contracts (``CON#<id> / ENV#<id>``),
changed read-modify-write under optimistic locking on ``rev`` like contracts.
Files live in the processed bucket under ``esign/<tenant>/<contract>/<envelope>/``.

SIGNER LINKS follow the portal's token model (shared/govern/portal.py): 32
random bytes, returned once inside the url, stored only as a SHA-256 in
govern-config (``SIGNER#<sha256> / LINK``). Unknown, malformed, revoked and
expired tokens all raise the same ``LinkGone``. A recipient may hold several
live links (a reminder sends a fresh one without breaking the first email);
all of them are revoked together when the envelope closes.

ORDER OF EFFECTS. The envelope write comes first; links, emails, the contract
transition and the seal queue follow it, so a failed side effect never leaves
an envelope claiming something that did not happen. Sending moves the contract
to ``out_for_signature`` after the envelope is ``sent``; if that transition is
refused the envelope is put back to ``draft`` and its links revoked.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import io
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError

from .. import s3 as s3_store
from .. import aws
from ..config import settings
from ..dynamodb import _to_ddb, get_doc_meta
from ..logger import get_logger
from ..uploads import clean_upload_filename
from . import esign_mail, esign_pdf, store, workflow
from .store import clean_item, iso, parse_iso

log = get_logger("blue-iq.govern.esign")

TOKEN_BYTES = 32
FIELD_TYPES = ("signature", "initials", "name", "email", "date", "text", "checkbox")
ROLES = ("signer", "cc")
ORDERS = ("sequential", "parallel")
MAX_SIGNERS = 10
MAX_RECIPIENTS = 20
MAX_FIELDS = 300
MIN_EXPIRY_DAYS, MAX_EXPIRY_DAYS, DEFAULT_EXPIRY_DAYS = 1, 60, 14
MAX_REMINDER_DAYS, DEFAULT_REMINDER_DAYS = 14, 3
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 300 * 1024
MAX_TEXT_CHARS = 500
MAX_MESSAGE_CHARS = 2000
MAX_LINKS_PER_RECIPIENT = 10
MAX_OPENS = 500
DOWNLOAD_DAYS_AFTER_COMPLETION = 30
PAGE_URL_TTL_S = 900
DOWNLOAD_URL_TTL_S = 300
UPLOAD_URL_TTL_S = 300
GONE_MESSAGE = "This signing link has expired or isn't valid. Ask the sender for a new one."
CONSENT_TEXT = ("I agree to use electronic records and signatures for this document. My electronic signature is "
                "the legal equivalent of my handwritten signature, and I can ask the sender for a paper copy.")

OPEN = frozenset({"sent", "sealing"})
CLOSED = frozenset({"completed", "declined", "voided", "expired"})

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_ENV_ID_RE = re.compile(r"[a-f0-9]{16}")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
_UPLOAD_KEY_RE = re.compile(r"esign/uploads/[A-Za-z0-9_.:@-]{1,80}/[A-Za-z0-9_-]{1,64}/[a-f0-9]{32}\.pdf")


class LinkGone(workflow.NotFound):
    """Unknown, malformed, expired or revoked — deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__(GONE_MESSAGE)


class NotPdf(workflow.GovernError):
    status = 409
    code = "not_pdf"


class Unavailable(workflow.GovernError):
    status = 503
    code = "signing_unavailable"


class RateLimited(workflow.GovernError):
    status = 429
    code = "rate_limited"


class EnvelopeConflict(store.ContractConflict):
    """Lost the optimistic-lock race on an envelope several times."""


# ---------------------------------------------------------------------------
# Keys, time, storage
# ---------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _env_key(contract_id: str, envelope_id: str) -> dict[str, str]:
    return {"PK": f"CON#{contract_id}", "SK": f"ENV#{envelope_id}"}


def _link_key(digest: str) -> dict[str, str]:
    return {"PK": f"SIGNER#{digest}", "SK": "LINK"}


def _prefix(env: dict[str, Any]) -> str:
    return f"esign/{env['tenantId']}/{env['contractId']}/{env['envelopeId']}"


def _bucket() -> str:
    bucket = settings.processed_bucket
    if not bucket:
        raise RuntimeError("PROCESSED_BUCKET is not set")
    return bucket


def _conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def get_envelope(contract_id: str, envelope_id: str) -> dict[str, Any] | None:
    if not isinstance(envelope_id, str) or not _ENV_ID_RE.fullmatch(envelope_id):
        return None
    item = store.contracts.table().get_item(Key=_env_key(contract_id, envelope_id)).get("Item")
    env = clean_item(item)
    return env if env and env.get("contractId") == contract_id else None


def envelopes_for(contract_id: str) -> list[dict[str, Any]]:
    envs = [e for e in store.contracts.children(f"CON#{contract_id}", "ENV#") if e.get("envelopeId")]
    return sorted(envs, key=lambda e: str(e.get("createdAt") or ""), reverse=True)


def _put_new(env: dict[str, Any]) -> None:
    item = {**_to_ddb(env), **_env_key(env["contractId"], env["envelopeId"]), "entityType": "ENVELOPE"}
    store.contracts.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")


def _mutate(contract_id: str, envelope_id: str, change: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Read-modify-write under ``rev``; ``change`` edits in place or returns False."""
    for _ in range(5):
        current = get_envelope(contract_id, envelope_id)
        if current is None:
            raise workflow.NotFound("Envelope not found")
        working = copy.deepcopy(current)
        rev = int(current.get("rev") or 0)
        if change(working) is False:
            return current
        working["rev"] = rev + 1
        item = {**_to_ddb(working), **_env_key(contract_id, envelope_id), "entityType": "ENVELOPE"}
        try:
            store.contracts.table().put_item(Item=item, ConditionExpression="rev = :rev",
                                             ExpressionAttributeValues={":rev": rev})
        except ClientError as exc:
            if _conditional_failure(exc):
                continue
            raise
        return working
    raise EnvelopeConflict("This envelope is being changed by someone else. Try again.")


def _recipient(env: dict[str, Any], recipient_id: str) -> dict[str, Any] | None:
    return next((r for r in env.get("recipients") or [] if r.get("id") == recipient_id), None)


def _signers(env: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in env.get("recipients") or [] if r.get("role") == "signer"]


def _person(r: dict[str, Any]) -> dict[str, Any]:
    return {"email": r.get("email"), "name": r.get("name")}


def _organization(tenant_id: str) -> str | None:
    org = (workflow.get_settings(tenant_id).get("organization") or {}).get("name")
    return str(org).strip()[:200] or None if org else None


def _contract_url(contract_id: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/contracts/{contract_id}"


def sign_url(token: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/sign/{token}"


def _long_date(value: Any) -> str:
    dt = parse_iso(value)
    return dt.strftime("%B %d, %Y").replace(" 0", " ") if dt else str(value or "")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

_RECIPIENT_PUBLIC = ("id", "name", "email", "role", "order", "status", "sentAt", "viewedAt", "consentAt",
                     "signedAt", "declinedAt", "declineReason", "linkExpiresAt")
_FIELD_PUBLIC = ("id", "recipientId", "type", "page", "x", "y", "w", "h", "required", "label", "value")


def _page_urls(env: dict[str, Any], ttl: int = PAGE_URL_TTL_S) -> list[dict[str, Any]]:
    pages = (env.get("source") or {}).get("pages") or []
    bucket = _bucket()
    return [{"url": s3_store.presign_get(bucket, f"{_prefix(env)}/pages/{i}.png", ttl),
             "width": p.get("width"), "height": p.get("height")} for i, p in enumerate(pages)]


def summary(env: dict[str, Any]) -> dict[str, Any]:
    signers = _signers(env)
    return {"id": env["envelopeId"], "status": env.get("status"), "name": env.get("name"),
            "createdAt": env.get("createdAt"), "sentAt": env.get("sentAt"), "completedAt": env.get("completedAt"),
            "expiresAt": env.get("expiresAt"),
            "signers": [{"name": r.get("name"), "email": r.get("email"), "status": r.get("status")} for r in signers],
            "signedCount": sum(1 for r in signers if r.get("status") == "signed"), "signerCount": len(signers)}


def detail(env: dict[str, Any], *, can_edit: bool) -> dict[str, Any]:
    """What reviewers see: never token hashes, storage keys, IPs of others' devices aside."""
    source = env.get("source") or {}
    return {
        **summary(env),
        "subject": env.get("subject"), "message": env.get("message"),
        "signingOrder": env.get("signingOrder"), "expiresInDays": env.get("expiresInDays"),
        "reminderEveryDays": env.get("reminderEveryDays"), "voidReason": env.get("voidReason"),
        "voidedAt": env.get("voidedAt"), "createdBy": env.get("createdBy"), "sentBy": env.get("sentBy"),
        "source": {"docId": source.get("docId"), "filename": source.get("filename"), "pageCount": source.get("pageCount"),
                   "sha256": source.get("sha256")},
        "sealed": ({"sha256": (env.get("sealed") or {}).get("sha256"), "sealedAt": (env.get("sealed") or {}).get("sealedAt"),
                    "timestamped": (env.get("sealed") or {}).get("timestamped")} if env.get("sealed") else None),
        "recipients": [{k: r.get(k) for k in _RECIPIENT_PUBLIC} | {"ip": r.get("ip"), "userAgent": r.get("userAgent")}
                       for r in env.get("recipients") or []],
        "fields": [{k: f.get(k) for k in _FIELD_PUBLIC} for f in env.get("fields") or []],
        "pages": _page_urls(env),
        "failure": env.get("failure"),
        "canEdit": bool(can_edit) and env.get("status") == "draft",
    }


# ---------------------------------------------------------------------------
# Drafts (govern-api)
# ---------------------------------------------------------------------------


def upload_url(c: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """A presigned PUT for a PDF to sign instead of the contract's document."""
    unknown = set(body) - {"filename", "size"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    filename, problem = clean_upload_filename(body.get("filename"))
    if problem or not filename:
        raise workflow.BadRequest(problem or "Invalid filename")
    if not filename.lower().endswith(".pdf"):
        raise workflow.BadRequest("Upload a PDF to sign.")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_UPLOAD_BYTES:
        raise workflow.BadRequest(f"size must be the file's size in bytes, at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    key = f"esign/uploads/{c['tenantId']}/{c['contractId']}/{uuid.uuid4().hex}.pdf"
    url = aws.s3_client().generate_presigned_url(
        "put_object", ExpiresIn=UPLOAD_URL_TTL_S,
        Params={"Bucket": _bucket(), "Key": key, "ContentType": "application/pdf", "ContentLength": size})
    return {"uploadUrl": url, "headers": {"Content-Type": "application/pdf"}, "uploadKey": key,
            "filename": filename, "expiresIn": UPLOAD_URL_TTL_S}


def _source_bytes(c: dict[str, Any], source: Any) -> tuple[bytes, str | None, str]:
    """(pdf bytes, docId or None, filename) for a draft's source."""
    if source is not None and not isinstance(source, dict):
        raise workflow.BadRequest("source must be an object")
    source = source or {}
    unknown = set(source) - {"docId", "uploadKey", "filename"}
    if unknown:
        raise workflow.BadRequest(f"Unknown source field(s): {', '.join(sorted(unknown))}")
    if source.get("uploadKey"):
        key = str(source["uploadKey"])
        expected = f"esign/uploads/{c['tenantId']}/{c['contractId']}/"
        if not _UPLOAD_KEY_RE.fullmatch(key) or not key.startswith(expected):
            raise workflow.BadRequest("uploadKey is not an upload for this contract")
        try:
            data = s3_store.get_object(_bucket(), key)
        except ClientError as exc:
            raise workflow.BadRequest("The upload was not found. Upload the PDF again.") from exc
        filename = clean_upload_filename(source.get("filename"))[0] or "document.pdf"
        return data, None, filename
    allowed = list(dict.fromkeys([*(c.get("versionDocIds") or []), c.get("currentDocId"), c["contractId"]]))
    doc_id = source.get("docId") or c.get("currentDocId") or c["contractId"]
    if doc_id not in [d for d in allowed if d]:
        raise workflow.BadRequest("docId is not a version of this contract")
    meta = get_doc_meta(doc_id) or {}
    raw_key = str(meta.get("rawKey") or "")
    filename = str(meta.get("filename") or raw_key.rsplit("/", 1)[-1] or "document.pdf")
    # Membership in the contract's versions is the check: a revision is filed
    # under its uploader's workspace, so its tenant may differ from the contract's.
    if not raw_key:
        raise workflow.NotFound("Document not found")
    if not raw_key.lower().endswith(".pdf"):
        raise NotPdf("This version is a Word file. Upload a PDF of it to send for signature.")
    bucket = settings.raw_bucket
    if not bucket:
        raise RuntimeError("RAW_BUCKET is not set")
    return s3_store.get_object(bucket, raw_key), doc_id, filename


def create_draft(c: dict[str, Any], body: dict[str, Any], actor: dict[str, Any] | None,
                 now: datetime | None = None) -> dict[str, Any]:
    """A draft envelope with the source PDF stored and its pages rendered."""
    unknown = set(body) - {"source", "name"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    if c.get("state") in workflow.TERMINAL_STATES or c.get("state") in ("signed", "active"):
        raise workflow.InvalidTransition("This contract is already signed or closed.")
    data, doc_id, filename = _source_bytes(c, body.get("source"))
    try:
        pages = esign_pdf.render_pages(data)
    except esign_pdf.PdfProblem as exc:
        raise NotPdf(str(exc)) from exc
    now_s = iso(_now(now))
    env = {"envelopeId": uuid.uuid4().hex[:16], "contractId": c["contractId"], "tenantId": c["tenantId"],
           "status": "draft", "rev": 0, "createdAt": now_s, "createdBy": actor,
           "name": str(body.get("name") or c.get("title") or filename)[:200],
           "subject": None, "message": None, "signingOrder": "sequential",
           "expiresInDays": DEFAULT_EXPIRY_DAYS, "reminderEveryDays": DEFAULT_REMINDER_DAYS,
           "recipients": [], "fields": [],
           "source": {"docId": doc_id, "filename": filename[:200], "sha256": esign_pdf.sha256(data),
                      "pageCount": len(pages), "pages": [{"width": p.width, "height": p.height} for p in pages]}}
    bucket = _bucket()
    s3_store.put_bytes(bucket, f"{_prefix(env)}/source.pdf", data, content_type="application/pdf")
    for i, page in enumerate(pages):
        s3_store.put_bytes(bucket, f"{_prefix(env)}/pages/{i}.png", page.png, content_type="image/png")
    _put_new(env)
    return env


def _clean_recipients(raw: Any, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise workflow.BadRequest("recipients must be a list")
    if len(raw) > MAX_RECIPIENTS:
        raise workflow.BadRequest(f"At most {MAX_RECIPIENTS} recipients")
    # Ids the client sends are kept (fields point at them); new ones only need
    # to be unique within this list.
    del existing
    out: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    next_n = 1
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise workflow.BadRequest(f"recipients[{i}] must be an object")
        unknown = set(r) - {"id", "name", "email", "role", "order"}
        if unknown:
            raise workflow.BadRequest(f"recipients[{i}]: unknown field(s) {', '.join(sorted(unknown))}")
        name = " ".join(str(r.get("name") or "").split())[:200]
        email = str(r.get("email") or "").strip().lower()[:254]
        role = r.get("role") or "signer"
        order = r.get("order", i + 1)
        if not name:
            raise workflow.BadRequest(f"recipients[{i}]: name is required")
        if not _EMAIL_RE.match(email):
            raise workflow.BadRequest(f"recipients[{i}]: a valid email is required")
        if role not in ROLES:
            raise workflow.BadRequest(f"recipients[{i}]: role must be signer or cc")
        if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= MAX_RECIPIENTS:
            raise workflow.BadRequest(f"recipients[{i}]: order must be a whole number from 1 to {MAX_RECIPIENTS}")
        if (email, role) in {(e, "signer") for e in seen_emails} and role == "signer":
            raise workflow.BadRequest(f"{email} is listed as a signer twice")
        if role == "signer":
            seen_emails.add(email)
        rid = r.get("id") if isinstance(r.get("id"), str) and re.fullmatch(r"r\d{1,3}", r["id"]) else None
        if rid is None or any(o["id"] == rid for o in out):
            taken = {o["id"] for o in out} | {x.get("id") for x in raw if isinstance(x, dict)}
            while f"r{next_n}" in taken:
                next_n += 1
            rid = f"r{next_n}"
        out.append({"id": rid, "name": name, "email": email, "role": role, "order": order, "status": "pending"})
    if sum(1 for r in out if r["role"] == "signer") > MAX_SIGNERS:
        raise workflow.BadRequest(f"At most {MAX_SIGNERS} signers")
    return out


def _fraction(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise workflow.BadRequest(f"{label} must be a number between 0 and 1")
    value = float(value)
    if not 0 <= value <= 1:
        raise workflow.BadRequest(f"{label} must be between 0 and 1")
    return round(value, 5)


def _clean_fields(raw: Any, recipients: list[dict[str, Any]], page_count: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise workflow.BadRequest("fields must be a list")
    if len(raw) > MAX_FIELDS:
        raise workflow.BadRequest(f"At most {MAX_FIELDS} fields")
    signer_ids = {r["id"] for r in recipients if r["role"] == "signer"}
    out: list[dict[str, Any]] = []
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            raise workflow.BadRequest(f"fields[{i}] must be an object")
        unknown = set(f) - {"id", "recipientId", "type", "page", "x", "y", "w", "h", "required", "label"}
        if unknown:
            raise workflow.BadRequest(f"fields[{i}]: unknown field(s) {', '.join(sorted(unknown))}")
        if f.get("recipientId") not in signer_ids:
            raise workflow.BadRequest(f"fields[{i}]: recipientId must be one of the signers")
        if f.get("type") not in FIELD_TYPES:
            raise workflow.BadRequest(f"fields[{i}]: type must be one of {', '.join(FIELD_TYPES)}")
        page = f.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < page_count:
            raise workflow.BadRequest(f"fields[{i}]: page must be from 0 to {page_count - 1}")
        x, y = _fraction(f.get("x"), f"fields[{i}].x"), _fraction(f.get("y"), f"fields[{i}].y")
        w, h = _fraction(f.get("w"), f"fields[{i}].w"), _fraction(f.get("h"), f"fields[{i}].h")
        if w < 0.01 or h < 0.005 or x + w > 1.0001 or y + h > 1.0001:
            raise workflow.BadRequest(f"fields[{i}] must sit inside the page")
        required = f.get("required", f["type"] != "checkbox")
        if not isinstance(required, bool):
            raise workflow.BadRequest(f"fields[{i}].required must be true or false")
        label = " ".join(str(f.get("label") or "").split())[:80] or None
        out.append({"id": f"f{i + 1}", "recipientId": f["recipientId"], "type": f["type"], "page": page,
                    "x": x, "y": y, "w": w, "h": h, "required": required, "label": label, "value": None})
    return out


def update_draft(c: dict[str, Any], envelope_id: str, body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "subject", "message", "signingOrder", "expiresInDays", "reminderEveryDays", "recipients", "fields"}
    unknown = set(body) - allowed
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")

    def change(env: dict[str, Any]) -> Any:
        if env.get("status") != "draft":
            raise workflow.InvalidTransition("Only a draft can be edited. Void it and prepare a new one.")
        for key in ("name", "subject"):
            if key in body:
                value = " ".join(str(body[key] or "").split())[:200]
                if key == "name" and not value:
                    raise workflow.BadRequest("name is required")
                env[key] = value or None
        if "message" in body:
            env["message"] = (str(body["message"] or "").strip()[:MAX_MESSAGE_CHARS]) or None
        if "signingOrder" in body:
            if body["signingOrder"] not in ORDERS:
                raise workflow.BadRequest("signingOrder must be sequential or parallel")
            env["signingOrder"] = body["signingOrder"]
        if "expiresInDays" in body:
            days = body["expiresInDays"]
            if isinstance(days, bool) or not isinstance(days, int) or not MIN_EXPIRY_DAYS <= days <= MAX_EXPIRY_DAYS:
                raise workflow.BadRequest(f"expiresInDays must be from {MIN_EXPIRY_DAYS} to {MAX_EXPIRY_DAYS}")
            env["expiresInDays"] = days
        if "reminderEveryDays" in body:
            days = body["reminderEveryDays"]
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_REMINDER_DAYS:
                raise workflow.BadRequest(f"reminderEveryDays must be from 0 (off) to {MAX_REMINDER_DAYS}")
            env["reminderEveryDays"] = days
        if "recipients" in body:
            env["recipients"] = _clean_recipients(body["recipients"], env.get("recipients") or [])
            # Fields of a recipient who is gone (or now only copied) go with them.
            signer_ids = {r["id"] for r in env["recipients"] if r["role"] == "signer"}
            env["fields"] = [f for f in env.get("fields") or [] if f.get("recipientId") in signer_ids]
        if "fields" in body:
            env["fields"] = _clean_fields(body["fields"], env.get("recipients") or [],
                                          int((env.get("source") or {}).get("pageCount") or 0))
        env["updatedAt"] = iso()

    return _mutate(c["contractId"], envelope_id, change)


def delete_draft(c: dict[str, Any], envelope_id: str) -> None:
    env = get_envelope(c["contractId"], envelope_id)
    if env is None:
        raise workflow.NotFound("Envelope not found")
    if env.get("status") != "draft":
        raise workflow.InvalidTransition("Only a draft can be deleted. Void a sent envelope instead.")
    try:
        store.contracts.table().delete_item(Key=_env_key(c["contractId"], envelope_id),
                                            ConditionExpression="#s = :draft",
                                            ExpressionAttributeNames={"#s": "status"},
                                            ExpressionAttributeValues={":draft": "draft"})
    except ClientError as exc:
        if _conditional_failure(exc):
            raise workflow.InvalidTransition("This envelope was just sent.") from exc
        raise
    s3_store.delete_prefix(_bucket(), f"{_prefix(env)}/")


# ---------------------------------------------------------------------------
# Signer links
# ---------------------------------------------------------------------------


def _issue_link(env: dict[str, Any], recipient: dict[str, Any], expires_s: str) -> str:
    """Store a new link for a recipient and return its token (shown once)."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    digest = token_hash(token)
    item = {**_link_key(digest), "entityType": "SIGNER_LINK", "tokenHash": digest, "tenantId": env["tenantId"],
            "contractId": env["contractId"], "envelopeId": env["envelopeId"], "recipientId": recipient["id"],
            "createdAt": iso(), "expiresAt": expires_s, "opens": 0}
    if not store.config.put_new(item):
        raise store.ContractConflict("signer token collision; try again")
    return token


def _revoke(digests: list[str], now_s: str) -> None:
    for digest in digests:
        try:
            store.config.table().update_item(
                Key=_link_key(digest), UpdateExpression="SET #r = :now",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#r)",
                ExpressionAttributeNames={"#r": "revokedAt"}, ExpressionAttributeValues={":now": now_s})
        except ClientError as exc:
            if not _conditional_failure(exc):
                raise


def _all_digests(env: dict[str, Any]) -> list[str]:
    return [d for r in env.get("recipients") or [] for d in r.get("tokenHashes") or []]


def _extend_links(env: dict[str, Any], until_s: str) -> None:
    """After completion, links stay usable to download the signed copy."""
    for digest in _all_digests(env):
        try:
            store.config.table().update_item(
                Key=_link_key(digest), UpdateExpression="SET #e = :until",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#r)",
                ExpressionAttributeNames={"#e": "expiresAt", "#r": "revokedAt"},
                ExpressionAttributeValues={":until": until_s})
        except ClientError as exc:
            if not _conditional_failure(exc):
                raise


def _due_signers(env: dict[str, Any]) -> list[dict[str, Any]]:
    """Signers whose turn it is and who have not signed."""
    waiting = [r for r in _signers(env) if r.get("status") not in ("signed", "declined")]
    if not waiting:
        return []
    if env.get("signingOrder") == "parallel":
        return waiting
    first = min(int(r.get("order") or 1) for r in waiting)
    return [r for r in waiting if int(r.get("order") or 1) == first]


def _invite(env: dict[str, Any], due: list[dict[str, Any]], *, reminder: bool, now_s: str) -> list[dict[str, Any]]:
    """Issue a link to each due signer, store its hash on the envelope and
    email it. Returns ``[{recipientId, url, emailed}]``."""
    expires_s = env.get("expiresAt") or now_s
    issued: dict[str, str] = {}
    for r in due:
        issued[r["id"]] = _issue_link(env, r, expires_s)

    def change(e: dict[str, Any]) -> Any:
        if e.get("status") != "sent":
            return False
        for r in e.get("recipients") or []:
            if r["id"] in issued:
                r["tokenHashes"] = ([*(r.get("tokenHashes") or []), token_hash(issued[r["id"]])])[-MAX_LINKS_PER_RECIPIENT:]
                r["linkExpiresAt"] = expires_s
                if r.get("status") == "pending":
                    r["status"] = "sent"
                r["sentAt"] = r.get("sentAt") or now_s
                if reminder:
                    r["remindedAt"] = now_s
        if reminder:
            e["lastReminderAt"] = now_s

    stored = _mutate(env["contractId"], env["envelopeId"], change)
    if stored.get("status") != "sent":
        _revoke([token_hash(t) for t in issued.values()], now_s)
        return []
    org = _organization(env["tenantId"])
    sender = workflow.display(env.get("sentBy") or env.get("createdBy"))
    out = []
    for r in due:
        url = sign_url(issued[r["id"]])
        emailed = esign_mail.invitation(to=r["email"], signer_name=r["name"], sender_name=sender, organization=org,
                                        document=stored.get("name") or "the agreement", message=stored.get("message"),
                                        url=url, expires_on=_long_date(stored.get("expiresAt")), reminder=reminder)
        out.append({"recipientId": r["id"], "name": r["name"], "email": r["email"], "url": url, "emailed": emailed})
    return out


# ---------------------------------------------------------------------------
# Sending, voiding, resending (govern-api)
# ---------------------------------------------------------------------------


def _check_ready(env: dict[str, Any]) -> None:
    signers = _signers(env)
    if not signers:
        raise workflow.BadRequest("Add at least one signer.")
    for r in signers:
        own = [f for f in env.get("fields") or [] if f.get("recipientId") == r["id"]]
        if not any(f.get("type") == "signature" for f in own):
            raise workflow.BadRequest(f"Place a signature field for {r['name']}.")


def send(c: dict[str, Any], envelope_id: str, actor: dict[str, Any] | None,
         now: datetime | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """draft → sent; contract → out_for_signature. (envelope, links issued)."""
    if not settings.signing_kms_key_id:
        raise Unavailable("Signing is not set up for this deployment yet.")
    now_dt = _now(now)
    now_s = iso(now_dt)
    if c.get("state") != "ready_to_sign":
        raise workflow.InvalidTransition("Approve the contract before sending it for signature.")
    others = [e for e in envelopes_for(c["contractId"]) if e.get("status") in OPEN and e["envelopeId"] != envelope_id]
    if others:
        raise workflow.InvalidTransition("Another envelope is already out for signature. Void it first.")

    def change(env: dict[str, Any]) -> Any:
        if env.get("status") != "draft":
            raise workflow.InvalidTransition("This envelope was already sent.")
        _check_ready(env)
        env["status"] = "sent"
        env["sentAt"] = now_s
        env["sentBy"] = actor
        env["expiresAt"] = iso(now_dt + timedelta(days=int(env.get("expiresInDays") or DEFAULT_EXPIRY_DAYS)))
        env["subject"] = env.get("subject") or f"Please sign: {env.get('name') or 'agreement'}"

    env = _mutate(c["contractId"], envelope_id, change)
    try:
        workflow.send_for_esign(c["contractId"], envelope_id, [_person(r) for r in _signers(env)], actor, now=now_dt)
    except Exception:
        _mutate(c["contractId"], envelope_id,
                lambda e: e.update({"status": "draft", "sentAt": None, "sentBy": None, "expiresAt": None})
                if e.get("status") == "sent" else False)
        raise
    links = _invite(env, _due_signers(env), reminder=False, now_s=now_s)
    return get_envelope(c["contractId"], envelope_id) or env, links


def void(c: dict[str, Any], envelope_id: str, body: dict[str, Any], actor: dict[str, Any] | None,
         now: datetime | None = None) -> dict[str, Any]:
    reason = " ".join(str(body.get("reason") or "").split())[:1000]
    if not reason:
        raise workflow.BadRequest("Say why the signing request is cancelled.")
    now_s = iso(_now(now))

    def change(env: dict[str, Any]) -> Any:
        if env.get("status") != "sent":
            raise workflow.InvalidTransition("Only an envelope that is out for signature can be voided.")
        env.update({"status": "voided", "voidedAt": now_s, "voidedBy": actor, "voidReason": reason})

    env = _mutate(c["contractId"], envelope_id, change)
    _revoke(_all_digests(env), now_s)
    workflow.cancel_esign(c["contractId"], envelope_id, "voided", actor=actor, reason=reason, now=_now(now))
    reached = [r["email"] for r in env.get("recipients") or [] if r.get("sentAt") and r.get("status") != "signed"]
    esign_mail.voided(to=reached, organization=_organization(env["tenantId"]), document=env.get("name") or "",
                      reason=reason)
    return env


def abandon(contract_id: str, envelope_id: str, actor: dict[str, Any] | None, *, reason: str,
            now: datetime | None = None) -> bool:
    """The contract left ``out_for_signature`` another way: void the envelope
    and revoke its links (the contract itself is not touched). False when the
    envelope is not open (already completed, sealing, voided)."""
    now_s = iso(_now(now))

    def change(env: dict[str, Any]) -> Any:
        if env.get("status") != "sent":
            return False
        env.update({"status": "voided", "voidedAt": now_s, "voidedBy": actor, "voidReason": reason})

    try:
        env = _mutate(contract_id, envelope_id, change)
    except workflow.NotFound:
        return False
    if env.get("status") != "voided" or env.get("voidedAt") != now_s:
        return False
    _revoke(_all_digests(env), now_s)
    reached = [r["email"] for r in env.get("recipients") or [] if r.get("sentAt") and r.get("status") != "signed"]
    esign_mail.voided(to=reached, organization=_organization(env["tenantId"]), document=env.get("name") or "",
                      reason=reason)
    return True


def resend(c: dict[str, Any], envelope_id: str, recipient_id: str, actor: dict[str, Any] | None,
           now: datetime | None = None) -> dict[str, Any]:
    """A fresh link for a signer whose turn it is (their earlier links keep working)."""
    env = get_envelope(c["contractId"], envelope_id)
    if env is None:
        raise workflow.NotFound("Envelope not found")
    if env.get("status") != "sent":
        raise workflow.InvalidTransition("This envelope is not out for signature.")
    r = _recipient(env, recipient_id)
    if r is None or r.get("role") != "signer":
        raise workflow.NotFound("Signer not found")
    if r not in _due_signers(env):
        raise workflow.InvalidTransition("It is not this signer's turn yet, or they already signed.")
    (link,) = _invite(env, [r], reminder=False, now_s=iso(_now(now))) or [None]
    if link is None:
        raise workflow.InvalidTransition("This envelope is not out for signature.")
    workflow.record_system_entry(c, "signature_link_resent", f"{workflow.display(actor)} sent {r['name']} a new signing link.",
                                 detail={"envelopeId": envelope_id, "recipientId": recipient_id}, actor=actor)
    return link


def download(env: dict[str, Any], which: str) -> dict[str, Any]:
    if which not in ("sealed", "source"):
        raise workflow.BadRequest("file must be sealed or source")
    base = (env.get("name") or "agreement").strip()[:80].replace("/", "-") or "agreement"
    if which == "sealed":
        if env.get("status") != "completed" or not env.get("sealed"):
            raise workflow.InvalidTransition("The signed document is not ready yet.")
        key, filename = f"{_prefix(env)}/sealed.pdf", f"{base} (signed).pdf"
    else:
        key, filename = f"{_prefix(env)}/source.pdf", f"{base}.pdf"
    url = aws.s3_client().generate_presigned_url(
        "get_object", ExpiresIn=DOWNLOAD_URL_TTL_S,
        Params={"Bucket": _bucket(), "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename.encode("ascii", "ignore").decode()}"',
                "ResponseContentType": "application/pdf"})
    return {"url": url, "filename": filename}


# ---------------------------------------------------------------------------
# The signer (govern-signing, no JWT: the token is the credential)
# ---------------------------------------------------------------------------


def resolve(token: Any, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(link, envelope, recipient) for a live token, else ``LinkGone``."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise LinkGone()
    digest = token_hash(token)
    link = clean_item(store.config.table().get_item(Key=_link_key(digest)).get("Item")) or {}
    stored = str(link.get("tokenHash") or "")
    if not hmac.compare_digest(stored.encode("ascii", "ignore"), digest.encode("ascii")):
        raise LinkGone()
    expires = parse_iso(link.get("expiresAt"))
    if link.get("revokedAt") or expires is None or expires <= _now(now):
        raise LinkGone()
    env = get_envelope(str(link.get("contractId") or ""), str(link.get("envelopeId") or ""))
    if env is None or env.get("tenantId") != link.get("tenantId"):
        raise LinkGone()
    r = _recipient(env, str(link.get("recipientId") or ""))
    if r is None or digest not in (r.get("tokenHashes") or []):
        raise LinkGone()
    return link, env, r


def _count_open(link: dict[str, Any]) -> None:
    try:
        store.config.table().update_item(
            Key=_link_key(str(link["tokenHash"])), UpdateExpression="ADD #n :one",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#r) AND #n < :cap",
            ExpressionAttributeNames={"#n": "opens", "#r": "revokedAt"},
            ExpressionAttributeValues={":one": 1, ":cap": MAX_OPENS})
    except ClientError as exc:
        if not _conditional_failure(exc):
            raise
        raise RateLimited("This link has been opened too many times. Ask the sender for a new one.") from exc


def _signer_status(env: dict[str, Any], r: dict[str, Any]) -> str:
    if r.get("status") == "signed":
        return "signed"
    if r.get("status") == "declined":
        return "declined"
    if env.get("status") != "sent":
        return "closed"
    return "ready" if r in _due_signers(env) else "waiting"


def signer_view(token: Any, *, ip: str | None, user_agent: str | None, now: datetime | None = None) -> dict[str, Any]:
    """GET /sign/{token}: the signer's page. The first view is recorded."""
    now_dt = _now(now)
    link, env, r = resolve(token, now_dt)
    _count_open(link)
    now_s = iso(now_dt)
    if env.get("status") == "sent" and not r.get("viewedAt"):
        def change(e: dict[str, Any]) -> Any:
            rec = _recipient(e, r["id"])
            if rec is None or rec.get("viewedAt"):
                return False
            rec["viewedAt"] = now_s
            rec["status"] = "viewed" if rec.get("status") == "sent" else rec.get("status")
            rec["ip"] = rec.get("ip") or ip
            rec["userAgent"] = rec.get("userAgent") or (user_agent or "")[:300] or None

        env = _mutate(env["contractId"], env["envelopeId"], change)
        r = _recipient(env, r["id"]) or r
        c = store.contracts.get(env["contractId"])
        if c is not None:
            workflow.record_system_entry(c, "signature_viewed", f"{r['name']} opened the document to sign.",
                                         detail={"envelopeId": env["envelopeId"], "recipientId": r["id"]},
                                         actor=_person(r))
    status = _signer_status(env, r)
    fields = [{k: f.get(k) for k in ("id", "type", "page", "x", "y", "w", "h", "required", "label")}
              for f in env.get("fields") or [] if f.get("recipientId") == r["id"]]
    return {"documentName": env.get("name"), "organizationName": _organization(env["tenantId"]),
            "senderName": workflow.display(env.get("sentBy") or env.get("createdBy")),
            "message": env.get("message"), "recipient": {"name": r.get("name"), "email": r.get("email")},
            "envelopeStatus": env.get("status"), "status": status,
            "pages": _page_urls(env) if status in ("ready", "signed") or env.get("status") == "completed" else [],
            "fields": fields if status == "ready" else [], "consentText": CONSENT_TEXT,
            "canDownload": env.get("status") == "completed", "expiresAt": env.get("expiresAt")}


def _png(data_url: Any, label: str) -> bytes:
    """A PNG from a ``data:image/png;base64,`` URL, checked for size and type."""
    from PIL import Image

    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        raise workflow.BadRequest(f"{label} must be a PNG image")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise workflow.BadRequest(f"{label} is not a valid image") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise workflow.BadRequest(f"{label} is too large")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format != "PNG" or img.width > 2400 or img.height > 1200 or img.width < 20 or img.height < 10:
                raise workflow.BadRequest(f"{label} must be a PNG up to 2400 × 1200 pixels")
            img.verify()
    except workflow.BadRequest:
        raise
    except Exception as exc:  # noqa: BLE001 — anything Pillow cannot read is not an image
        raise workflow.BadRequest(f"{label} is not a valid image") from exc
    return raw


def _values_for(env: dict[str, Any], r: dict[str, Any], body: dict[str, Any], now_s: str) -> dict[str, str | None]:
    raw = body.get("values") or {}
    if not isinstance(raw, dict):
        raise workflow.BadRequest("values must be an object")
    out: dict[str, str | None] = {}
    for f in env.get("fields") or []:
        if f.get("recipientId") != r["id"]:
            continue
        kind, value = f["type"], raw.get(f["id"])
        if kind in ("signature", "initials"):
            out[f["id"]] = "adopted"
            continue
        if kind == "date":
            out[f["id"]] = _long_date(now_s)               # the date of signing, never typed
            continue
        if kind == "email":
            out[f["id"]] = r["email"]
            continue
        if kind == "checkbox":
            checked = value is True or str(value).lower() == "true"
            if f.get("required") and not checked:
                raise workflow.BadRequest(f"Tick {f.get('label') or 'the required box'}.")
            out[f["id"]] = "true" if checked else "false"
            continue
        text = " ".join(str(value or "").split())[:MAX_TEXT_CHARS]
        if kind == "name" and not text:
            text = r["name"]
        if f.get("required") and not text:
            raise workflow.BadRequest(f"Fill in {f.get('label') or kind}.")
        out[f["id"]] = text or None
    return out


def _enqueue_seal(env: dict[str, Any]) -> None:
    if not settings.seal_queue_url:
        raise RuntimeError("SEAL_QUEUE_URL is not set")
    aws.sqs_client().send_message(QueueUrl=settings.seal_queue_url,
                              MessageBody=json.dumps({"contractId": env["contractId"], "envelopeId": env["envelopeId"]}))


def complete(token: Any, body: dict[str, Any], *, ip: str | None, user_agent: str | None,
             now: datetime | None = None) -> dict[str, Any]:
    """POST /sign/{token}/complete: the signer adopts their signature and fills their fields."""
    unknown = set(body) - {"consent", "values", "signature", "initials"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    now_dt = _now(now)
    now_s = iso(now_dt)
    _, env, r = resolve(token, now_dt)
    if _signer_status(env, r) != "ready":
        raise workflow.InvalidTransition("This document can't be signed from this link right now.")
    if body.get("consent") is not True:
        raise workflow.BadRequest("Agree to use electronic records and signatures first.")
    own = [f for f in env.get("fields") or [] if f.get("recipientId") == r["id"]]
    values = _values_for(env, r, body, now_s)
    images: dict[str, bytes] = {}
    if any(f["type"] == "signature" for f in own):
        images["signature"] = _png((body.get("signature") or {}).get("image"), "The signature")
    if any(f["type"] == "initials" for f in own):
        images["initials"] = _png((body.get("initials") or {}).get("image"), "The initials")
    kind = (body.get("signature") or {}).get("kind") if isinstance(body.get("signature"), dict) else None
    bucket = _bucket()
    for name, png in images.items():
        s3_store.put_bytes(bucket, f"{_prefix(env)}/sig/{r['id']}-{name}.png", png, content_type="image/png")

    def change(e: dict[str, Any]) -> Any:
        rec = _recipient(e, r["id"])
        if rec is None or _signer_status(e, rec) != "ready":
            raise workflow.InvalidTransition("This document can't be signed from this link right now.")
        for f in e.get("fields") or []:
            if f["id"] in values:
                f["value"] = values[f["id"]]
        rec.update({"status": "signed", "signedAt": now_s, "consentAt": now_s, "ip": ip or rec.get("ip"),
                    "userAgent": (user_agent or "")[:300] or rec.get("userAgent"), "signatureKind":
                    kind if kind in ("drawn", "typed") else None, "hasInitials": "initials" in images})
        rec["viewedAt"] = rec.get("viewedAt") or now_s
        if all(s.get("status") == "signed" for s in _signers(e)):
            e["status"] = "sealing"
            e["signedAt"] = now_s

    env = _mutate(env["contractId"], env["envelopeId"], change)
    r = _recipient(env, r["id"]) or r
    c = store.contracts.get(env["contractId"])
    if c is not None:
        workflow.record_system_entry(c, "signature_signed", f"{r['name']} signed.",
                                     detail={"envelopeId": env["envelopeId"], "recipientId": r["id"]}, actor=_person(r))
    if env.get("status") == "sealing":
        _enqueue_seal(env)
    else:
        _invite(env, [s for s in _due_signers(env) if not s.get("sentAt")], reminder=False, now_s=now_s)
    return {"status": "signed", "envelopeStatus": env.get("status")}


def decline(token: Any, body: dict[str, Any], *, ip: str | None, user_agent: str | None,
            now: datetime | None = None) -> dict[str, Any]:
    reason = " ".join(str(body.get("reason") or "").split())[:1000]
    if not reason:
        raise workflow.BadRequest("Say why you are declining, so the sender can follow up.")
    now_dt = _now(now)
    now_s = iso(now_dt)
    _, env, r = resolve(token, now_dt)
    if _signer_status(env, r) not in ("ready", "waiting"):
        raise workflow.InvalidTransition("This document can't be declined from this link now.")

    def change(e: dict[str, Any]) -> Any:
        rec = _recipient(e, r["id"])
        if e.get("status") != "sent" or rec is None or rec.get("status") in ("signed", "declined"):
            raise workflow.InvalidTransition("This document can't be declined from this link now.")
        rec.update({"status": "declined", "declinedAt": now_s, "declineReason": reason, "ip": ip or rec.get("ip"),
                    "userAgent": (user_agent or "")[:300] or rec.get("userAgent")})
        e["status"] = "declined"

    env = _mutate(env["contractId"], env["envelopeId"], change)
    _revoke(_all_digests(env), now_s)
    workflow.cancel_esign(env["contractId"], env["envelopeId"], "declined", actor=_person(r), reason=reason, now=now_dt)
    sender = (env.get("sentBy") or env.get("createdBy") or {}).get("email")
    esign_mail.declined(to=[sender] if sender else [], organization=_organization(env["tenantId"]),
                        document=env.get("name") or "", signer=r["name"], reason=reason,
                        contract_url=_contract_url(env["contractId"]))
    return {"status": "declined"}


def signer_download(token: Any, now: datetime | None = None) -> dict[str, Any]:
    _, env, _ = resolve(token, _now(now))
    return download(env, "sealed")


# ---------------------------------------------------------------------------
# Sealing (govern-seal)
# ---------------------------------------------------------------------------

MAX_SEAL_ATTEMPTS = 3


def seal(contract_id: str, envelope_id: str, *, seal_fn: Callable[[bytes], tuple[bytes, dict[str, Any]]] | None = None,
         now: datetime | None = None) -> str:
    """Build the final PDF, seal it, mark the contract signed, send the copies.
    Returns what happened ("completed", "skipped"). Raises to let the queue retry."""
    from . import esign_seal

    env = get_envelope(contract_id, envelope_id)
    if env is None or env.get("status") != "sealing":
        return "skipped"
    now_s = iso(_now(now))
    bucket = _bucket()
    try:
        source = s3_store.get_object(bucket, f"{_prefix(env)}/source.pdf")
        if esign_pdf.sha256(source) != (env.get("source") or {}).get("sha256"):
            raise RuntimeError("source document changed since it was prepared")
        adopted: dict[str, dict[str, bytes]] = {}
        for r in _signers(env):
            adopted[r["id"]] = {}
            for name in ("signature", "initials"):
                key = f"{_prefix(env)}/sig/{r['id']}-{name}.png"
                if s3_store.object_exists(bucket, key):
                    adopted[r["id"]][name] = s3_store.get_object(bucket, key)
        images = {f["id"]: adopted.get(f["recipientId"], {}).get(f["type"], b"")
                  for f in env.get("fields") or [] if f.get("type") in ("signature", "initials")}
        flat = esign_pdf.flatten(source, env.get("fields") or [], {k: v for k, v in images.items() if v})
        cert_page = esign_pdf.certificate(
            envelope=env, organization=_organization(env["tenantId"]), source_sha256=env["source"]["sha256"],
            signed_sha256=esign_pdf.sha256(flat), completed_at=now_s,
            signatures={rid: imgs["signature"] for rid, imgs in adopted.items() if imgs.get("signature")})
        final = esign_pdf.append(flat, cert_page)
        sealed, used = (seal_fn or esign_seal.seal_pdf)(final)
    except Exception as exc:
        attempts = int(env.get("sealAttempts") or 0) + 1

        def note(e: dict[str, Any]) -> Any:
            if e.get("status") != "sealing":
                return False
            e["sealAttempts"] = attempts
            if attempts >= MAX_SEAL_ATTEMPTS:
                e["status"] = "failed"
                e["failure"] = {"at": now_s, "reason": "The signed document could not be sealed."}

        _mutate(contract_id, envelope_id, note)
        log.error("esign.seal_failed", envelopeId=envelope_id, attempts=attempts, error_type=type(exc).__name__)
        raise

    s3_store.put_bytes(bucket, f"{_prefix(env)}/sealed.pdf", sealed, content_type="application/pdf")
    sealed_meta = {"sha256": esign_pdf.sha256(sealed), "sealedAt": now_s, "keyId": used.get("keyId"),
                   "timestamped": bool(used.get("timestamped"))}

    def done(e: dict[str, Any]) -> Any:
        if e.get("status") != "sealing":
            return False
        e.update({"status": "completed", "completedAt": now_s, "sealed": sealed_meta, "failure": None})

    env = _mutate(contract_id, envelope_id, done)
    if env.get("status") != "completed":
        return "skipped"
    until = iso(_now(now) + timedelta(days=DOWNLOAD_DAYS_AFTER_COMPLETION))
    _extend_links(env, until)
    signed_at = max((str(r.get("signedAt") or "") for r in _signers(env)), default=now_s) or now_s
    try:
        workflow.perform_action(contract_id, "mark_signed", {"signedAt": signed_at, "envelopeId": envelope_id}, None,
                                system_name=workflow.ESIGN_SYSTEM)
    except workflow.GovernError as exc:
        # The contract moved on (reopened, signed by hand): the sealed copy still stands.
        log.warning("esign.mark_signed_skipped", envelopeId=envelope_id, code=exc.code)
    c = store.contracts.get(contract_id)
    if c is not None:
        workflow.record_system_entry(c, "signature_completed",
                                     "Everyone signed. The sealed document and its certificate are stored.",
                                     detail={"envelopeId": envelope_id, "sha256": sealed_meta["sha256"]},
                                     actor=None)
    sender = (env.get("sentBy") or env.get("createdBy") or {}).get("email")
    to = [r["email"] for r in env.get("recipients") or []] + ([sender] if sender else [])
    name = (env.get("name") or "agreement").strip()[:80]
    esign_mail.completed(to=to, organization=_organization(env["tenantId"]), document=env.get("name") or "",
                         pdf=sealed, filename=f"{name} (signed).pdf", download_url=_contract_url(contract_id))
    return "completed"


# ---------------------------------------------------------------------------
# Reminders and expiry (govern-sweeper)
# ---------------------------------------------------------------------------


def sweep_contract(c: dict[str, Any], now: datetime | None = None) -> str | None:
    """For a contract out with a Govern envelope: expire it, or remind the
    signers whose turn it is. Returns what was done."""
    sig = c.get("signature") or {}
    if c.get("state") != "out_for_signature" or sig.get("provider") != workflow.ESIGN_PROVIDER:
        return None
    env = get_envelope(c["contractId"], str(sig.get("envelopeId") or ""))
    if env is None or env.get("status") != "sent":
        return None
    now_dt = _now(now)
    now_s = iso(now_dt)
    expires = parse_iso(env.get("expiresAt"))
    if expires is not None and expires <= now_dt:
        def change(e: dict[str, Any]) -> Any:
            if e.get("status") != "sent":
                return False
            e["status"] = "expired"
            e["expiredAt"] = now_s

        env = _mutate(c["contractId"], env["envelopeId"], change)
        if env.get("status") != "expired":
            return None
        _revoke(_all_digests(env), now_s)
        workflow.cancel_esign(c["contractId"], env["envelopeId"], "expired", actor=None, now=now_dt)
        sender = (env.get("sentBy") or env.get("createdBy") or {}).get("email")
        esign_mail.expired(to=[sender] if sender else [], organization=_organization(env["tenantId"]),
                           document=env.get("name") or "", contract_url=_contract_url(c["contractId"]))
        return "expired"
    every = int(env.get("reminderEveryDays") or 0)
    if every <= 0:
        return None
    due = [r for r in _due_signers(env) if r.get("sentAt")]
    last = parse_iso(env.get("lastReminderAt")) or min(
        (parse_iso(r.get("remindedAt") or r.get("sentAt")) for r in due if parse_iso(r.get("sentAt"))), default=None)
    if not due or last is None or now_dt - last < timedelta(days=every):
        return None
    _invite(env, due, reminder=True, now_s=now_s)
    workflow.record_system_entry(c, "signature_reminder_sent",
                                 f"Reminded {', '.join(r['name'] for r in due[:3])} to sign.",
                                 detail={"envelopeId": env["envelopeId"], "recipients": [r["id"] for r in due]})
    return "reminded"
