"""Lazy, region-aware boto3 client factories.

Boto3 clients are heavy to construct (TLS handshakes, credential resolution).
We memoise them per-process so Lambda warm starts reuse them.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

# Outbound calls from the Govern side-effect Lambdas must not hang an invocation.
_SHORT_TIMEOUTS = Config(connect_timeout=5, read_timeout=15)

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


@lru_cache(maxsize=None)
def cognito_idp_client() -> Any:
    """Cognito user-pool admin client — used to invite users to a tenant."""
    return session().client("cognito-idp", config=_BOTO_CONFIG)


@lru_cache(maxsize=None)
def events_client() -> Any:
    """EventBridge — Govern domain events and the pipeline's Document Analysed."""
    return session().client("events", config=_BOTO_CONFIG.merge(_SHORT_TIMEOUTS))


@lru_cache(maxsize=None)
def sqs_client() -> Any:
    """SQS — the sweeper re-enqueues missed documents to the intake queue."""
    return session().client("sqs", config=_BOTO_CONFIG.merge(_SHORT_TIMEOUTS))


@lru_cache(maxsize=None)
def ses_client() -> Any:
    """SES v2 for alert email (its region may differ from the stack's)."""
    return session().client("sesv2", region_name=settings.ses_region or settings.aws_region,
                            config=_BOTO_CONFIG.merge(_SHORT_TIMEOUTS))


def get_credentials():
    """Return boto3 frozen credentials (used by SigV4 signers e.g. OpenSearch)."""
    return session().get_credentials()
