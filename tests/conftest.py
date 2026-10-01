"""Test bootstrap.

Puts the Lambda source roots on sys.path so tests import the real modules the
way Lambda does:
  - ``lambdas/``          → the ``shared`` package (deployed as a layer)
  - ``lambdas/pipeline/`` → the ``stages`` package + ``handler``

and guarantees the suite is offline: any attempt to build a real AWS session or
a real OpenAI client fails the test instead of reaching the network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for rel in ("lambdas", "lambdas/pipeline", "tests", "scripts"):
    p = str(_ROOT / rel)
    if p not in sys.path:
        sys.path.insert(0, p)


_REAL: dict = {}


@pytest.fixture
def real_openai_factory():
    """The genuine client factory, for the one test that checks how the SDK
    client is configured (with the SDK class itself replaced by a stub)."""
    return _REAL["openai_factory"]


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test may touch AWS or OpenAI. Individual tests install fakes on top."""
    from shared import aws as shared_aws
    from shared import openai_client

    _REAL.setdefault("openai_factory", openai_client.openai_client)

    def no_aws(*_a, **_k):
        raise AssertionError("a test tried to open a real AWS session")

    def no_openai(*_a, **_k):
        raise AssertionError("a test tried to build a real OpenAI client")

    monkeypatch.setattr(shared_aws, "session", no_aws)
    monkeypatch.setattr(openai_client, "openai_client", no_openai)
    # Retries must not actually wait, and state must not leak between tests.
    monkeypatch.setattr(openai_client, "_sleep", lambda _s: None)
    openai_client.set_deadline(None)
    openai_client.usage_snapshot(reset=True)
    openai_client._quirks.clear()
    yield
    openai_client.set_deadline(None)


@pytest.fixture
def ddb(monkeypatch):
    """One in-memory DynamoDB table behind every data-access function."""
    from fakes import install_fake_dynamodb

    return install_fake_dynamodb(monkeypatch)
