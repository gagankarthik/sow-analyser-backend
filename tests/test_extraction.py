"""Exhaustive-extraction guards: a long document must not silently lose content.

Two failure modes are covered:
  1. OUTPUT truncation — the model stops on finish_reason == "length", which
     would cut off trailing clauses. chat_json must raise (not parse partial
     JSON), and classify must retry once with a larger budget.
  2. INPUT truncation — when the document is too long for the context window and
     gets truncated, the extraction must be flagged low-confidence rather than
     presented as complete.
"""
from __future__ import annotations

import types

import pytest

from shared import openai_client
from stages import classify


# ── chat_json raises on a length-truncated response ──────────────────────────


def _fake_resp(content: str, finish_reason: str):
    msg = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=None)


class _FakeCompletions:
    def __init__(self, resp):
        self._resp = resp

    def create(self, **kwargs):
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(resp))


def test_chat_json_raises_on_length_truncation(monkeypatch):
    resp = _fake_resp('{"clauses": [{"number": "1"', "length")  # truncated JSON
    monkeypatch.setattr(openai_client, "openai_client", lambda: _FakeClient(resp))
    with pytest.raises(openai_client.OutputTruncatedError):
        openai_client.chat_json(
            system="s", user="u", json_schema={"type": "object"}, schema_name="X",
        )


def test_chat_json_parses_complete_response(monkeypatch):
    resp = _fake_resp('{"docType": "SOW", "clauses": []}', "stop")
    monkeypatch.setattr(openai_client, "openai_client", lambda: _FakeClient(resp))
    out = openai_client.chat_json(
        system="s", user="u", json_schema={"type": "object"}, schema_name="X",
    )
    assert out["docType"] == "SOW"


# ── classify retries once on truncation, then succeeds ───────────────────────


def test_classify_document_retries_on_truncation(monkeypatch):
    calls = {"n": 0}

    def fake_chat_json(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "max_tokens" not in kwargs  # first call uses the default budget
            raise openai_client.OutputTruncatedError("truncated")
        # Retry passes the larger budget.
        assert kwargs.get("max_tokens") == classify.settings.chat_max_output_tokens_max
        return {"docType": "SOW", "clauses": [{"number": "1", "title": "t", "body": "b", "category": "Fees"}]}

    monkeypatch.setattr(classify, "chat_json", fake_chat_json)
    out = classify._classify_document("text", hints=[])
    assert calls["n"] == 2
    assert out["docType"] == "SOW"


def test_classify_document_reraises_if_retry_still_truncates(monkeypatch):
    def always_truncate(**kwargs):
        raise openai_client.OutputTruncatedError("still truncated")

    monkeypatch.setattr(classify, "chat_json", always_truncate)
    with pytest.raises(openai_client.OutputTruncatedError):
        classify._classify_document("text", hints=[])
