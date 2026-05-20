"""Lazy, region-aware boto3 client factories.

Boto3 clients are heavy to construct (TLS handshakes, credential resolution).
We memoise them per-process so Lambda warm starts reuse them.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from .config import settings


_BOTO_CONFIG = Config(
    region_name=settings.aws_region,
    retries={"max_attempts": 5, "mode": "adaptive"},
    user_agent_extra=f"blue-iq/{settings.stage}",
)


@lru_cache(maxsize=None)
def session() -> boto3.Session:
    return boto3.Session(region_name=settings.aws_region)


@lru_cache(maxsize=None)
def s3_client() -> Any:
    return session().client("s3", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def s3_resource() -> Any:
    return session().resource("s3", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def dynamodb_resource() -> Any:
    return session().resource("dynamodb", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def dynamodb_client() -> Any:
    return session().client("dynamodb", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def secrets_client() -> Any:
    return session().client("secretsmanager", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def textract_client() -> Any:
    return session().client("textract", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def appsync_client() -> Any:
    return session().client("appsync", config=_BOTO_CONFIG)


def get_credentials():
    """Return boto3 frozen credentials (used by SigV4 signers e.g. OpenSearch)."""
    return session().get_credentials()
