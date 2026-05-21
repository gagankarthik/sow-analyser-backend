"""S3 helpers — get/put bytes, put_json, presigned URLs."""
from __future__ import annotations

from typing import Any

import orjson
from botocore.exceptions import ClientError

from .aws import s3_client
from .logger import get_logger

log = get_logger("blue-iq.s3")


def get_object(bucket: str, key: str) -> bytes:
    log.debug("s3.get_object", bucket=bucket, key=key)
    resp = s3_client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def head_object(bucket: str, key: str) -> dict[str, Any]:
    return s3_client().head_object(Bucket=bucket, Key=key)


def put_bytes(
    bucket: str,
    key: str,
    body: bytes,
    content_type: str = "application/octet-stream",
    metadata: dict[str, str] | None = None,
) -> None:
    log.debug("s3.put_bytes", bucket=bucket, key=key, bytes=len(body))
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata=metadata or {},
    )


def put_json(bucket: str, key: str, data: Any) -> None:
    """Serialise `data` with orjson and write to S3."""
    body = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)
    put_bytes(bucket, key, body, content_type="application/json")


def get_json(bucket: str, key: str) -> Any:
    raw = get_object(bucket, key)
    return orjson.loads(raw)


def object_exists(bucket: str, key: str) -> bool:
    try:
        s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def presign_get(bucket: str, key: str, expires_seconds: int = 3600) -> str:
    return s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )


def presign_put(
    bucket: str,
    key: str,
    expires_seconds: int = 3600,
    content_type: str = "application/octet-stream",
) -> str:
    return s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_seconds,
    )


def delete_object(bucket: str, key: str) -> None:
    if not key:
        return
    log.debug("s3.delete_object", bucket=bucket, key=key)
    s3_client().delete_object(Bucket=bucket, Key=key)


def delete_prefix(bucket: str, prefix: str) -> int:
    """Delete every object under `prefix`. Returns the number deleted."""
    if not prefix:
        return 0
    s3 = s3_client()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents") or []
        if not contents:
            continue
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
        )
        deleted += len(contents)
    log.debug("s3.delete_prefix", bucket=bucket, prefix=prefix, deleted=deleted)
    return deleted


def processed_key(tenant_id: str, doc_id: str, filename: str) -> str:
    """Canonical S3 key layout for processed artefacts."""
    return f"{tenant_id}/{doc_id}/{filename}"
