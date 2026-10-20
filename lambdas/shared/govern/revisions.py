"""A revised version of a contract, uploaded straight to the raw bucket.

One path for every way a revision arrives — a reviewer's upload
(``GET /contracts/{id}/revision-upload-url``) and the counterparty's portal
link (``POST /portal/{token}/upload-url``): a PENDING document linked to the
contract (``revisionOf``), filed in the given projects, and the contract's
``pendingDocId`` / ``versionDocIds`` pointing at it. The pipeline reads the
object when it lands; govern-intake then makes it the current version and
rescores the contract (``revision_received``), exactly as for any revision.
"""
from __future__ import annotations

import uuid
from typing import Any

from .. import dynamodb as ddb
from ..uploads import pending_document_meta, upload_key
from . import store

UPLOAD_URL_TTL_S = 300


def presign_revision(c: dict[str, Any], *, raw_bucket: str, tenant_id: str, owner_sub: str,
                     owner_email: str | None, filename: str, project_ids: list[str],
                     extra: dict[str, Any] | None = None, content_type: str | None = None,
                     content_length: int | None = None) -> tuple[str, str]:
    """(presigned PUT url, new docId). ``content_type`` / ``content_length``
    are signed into the URL when given, so S3 refuses any other type or size."""
    from ..aws import s3_client

    contract_id = c["contractId"]
    base = ddb.get_doc_meta(c.get("currentDocId") or contract_id) or ddb.get_doc_meta(contract_id) or {}
    doc_id = str(uuid.uuid4())
    params: dict[str, Any] = {"Bucket": raw_bucket, "Key": upload_key(tenant_id, doc_id, filename)}
    if content_type:
        params["ContentType"] = content_type
    if content_length is not None:
        params["ContentLength"] = int(content_length)
    upload_url = s3_client().generate_presigned_url("put_object", Params=params, ExpiresIn=UPLOAD_URL_TTL_S)
    ddb.put_doc_meta(pending_document_meta(
        doc_id=doc_id, tenant_id=tenant_id, owner_sub=owner_sub, owner_email=owner_email,
        filename=filename, doc_type=base.get("docType") or "OTHER", project_ids=project_ids,
        extra={"parentDocId": contract_id, "revisionOf": contract_id, **(extra or {})}))
    for project_id in project_ids:
        ddb.mutate_project(project_id, lambda p: p.__setitem__("docIds", list(p.get("docIds") or []) + [doc_id]))

    def link(stored: dict[str, Any]) -> None:
        stored["versionDocIds"] = list(stored.get("versionDocIds") or [contract_id]) + [doc_id]
        stored.setdefault("versionRounds", {})[doc_id] = int(stored.get("rounds") or 0) + 1
        stored["pendingDocId"] = doc_id
        stored["updatedAt"] = store.iso()

    store.contracts.mutate(contract_id, link)
    return upload_url, doc_id


def base_project_ids(c: dict[str, Any]) -> list[str]:
    """The projects the contract's current document is filed in."""
    base = ddb.get_doc_meta(c.get("currentDocId") or c["contractId"]) or ddb.get_doc_meta(c["contractId"]) or {}
    return [str(p) for p in base.get("projectIds") or []]
