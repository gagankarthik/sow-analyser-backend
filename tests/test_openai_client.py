"""The OpenAI wrapper: timeouts, retries, model differences, unusable replies.

The client is a scripted fake (tests/fakes.py) — no request leaves the process.
"""
from __future__ import annotations

import types

import pytest

from fakes import APITimeoutError, BadRequestError, FakeOpenAI, RateLimitError, chat_response
from shared import openai_client as oc

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "value", "tags", "kind", "flag", "nested"],
    "properties": {
        "title": {"type": "string"}, "value": {"type": ["number", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["a", "b"]}, "flag": {"type": "boolean"},
        "nested": {"type": "object", "properties": {"when": {"type": ["string", "null"]}}},
    },
}


@pytest.fixture
def use(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(oc, "_sleep", waits.append)
    monkeypatch.setattr(oc.settings, "openai_max_attempts", 4)
    monkeypatch.setattr(oc.settings, "structured_output_mode", "json_schema")

    def install(**script):
        client = FakeOpenAI(**script)
        monkeypatch.setattr(oc, "openai_client", lambda: client)
        return client
    install.waits = waits
    return install


def ask(**kw):
    return oc.chat_json(system="s", user="u", json_schema=SCHEMA, schema_name="X", model="m", **kw)


# ── Retries ──────────────────────────────────────────────────────────────────


def test_rate_limit_and_timeout_are_retried_with_backoff_and_jitter(use):
    client = use(chat=[RateLimitError("slow down", 429), APITimeoutError("timed out"),
                       chat_response('{"title": "ok", "kind": "a", "flag": true}')])
    assert ask()["title"] == "ok"
    assert len(client.chat_calls) == 3 and len(use.waits) == 2
    assert 1.0 <= use.waits[0] <= 2.0 and 2.0 <= use.waits[1] <= 4.0        # exponential, jittered
    assert oc.usage_snapshot()["retries"] == 2


def test_retry_after_header_is_honoured(use):
    use(chat=[RateLimitError("slow down", 429, headers={"retry-after": "7"}),
              chat_response('{"title": "ok"}')])
    ask()
    assert use.waits == [7.0]


def test_server_errors_are_retried_and_client_errors_are_not(use):
    class InternalServerError(Exception):
        status_code = 500
    client = use(chat=[InternalServerError("boom"), chat_response('{"title": "ok"}')])
    assert ask()["title"] == "ok" and len(client.chat_calls) == 2

    class AuthenticationError(Exception):
        status_code = 401
    client = use(chat=[AuthenticationError("bad key")])
    with pytest.raises(AuthenticationError):
        ask()
    assert len(client.chat_calls) == 1                                      # no pointless retries


def test_gives_up_after_the_configured_attempts(use):
    client = use(chat=[RateLimitError("slow down", 429)])
    with pytest.raises(RateLimitError):
        ask()
    assert len(client.chat_calls) == 4


def test_the_sdk_is_told_not_to_retry_on_its_own(monkeypatch, real_openai_factory):
    """Otherwise each of our attempts is silently multiplied by the SDK's."""
    import sys
    built = {}

    class _OpenAI:                      # stands in for the SDK class: nothing is sent
        def __init__(self, **kw):
            built.update(kw)
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_OpenAI))
    monkeypatch.setattr(oc.settings, "openai_api_key", "sk-test")
    oc._api_key.cache_clear()
    real_openai_factory.cache_clear()
    try:
        real_openai_factory()
        assert built["max_retries"] == 0 and built["http_client"] is not None
    finally:
        real_openai_factory.cache_clear()
        oc._api_key.cache_clear()


# ── Timeouts and the stage deadline ──────────────────────────────────────────


def test_every_request_carries_a_timeout(use, monkeypatch):
    monkeypatch.setattr(oc.settings, "openai_timeout_s", 180.0)
    monkeypatch.setattr(oc.settings, "openai_embed_timeout_s", 60.0)
    client = use(chat=[chat_response('{"title": "ok"}')])
    ask()
    oc.chat_text(system="s", user="u", model="m")
    oc.embed_texts(["a"], model="e")
    assert [c["timeout"] for c in client.chat_calls] == [180.0, 180.0]
    assert client.embed_calls[0]["timeout"] == 60.0


def test_a_request_never_outlives_the_stage_deadline(use):
    client = use(chat=[chat_response('{"title": "ok"}')])
    oc.set_deadline(remaining_ms=60_000, reserve_s=25.0)          # 35 s of work left
    ask()
    assert 30.0 < client.chat_calls[0]["timeout"] <= 35.0
    oc.set_deadline(remaining_ms=20_000, reserve_s=25.0)          # already past the reserve
    with pytest.raises(oc.DeadlineExceededError):
        ask()
    assert len(client.chat_calls) == 1                            # no request was sent


def test_retries_stop_when_the_deadline_would_be_missed(use):
    client = use(chat=[RateLimitError("slow down", 429, headers={"retry-after": "30"})])
    oc.set_deadline(remaining_ms=45_000, reserve_s=25.0)          # 20 s left < 30 s wait
    with pytest.raises(oc.DeadlineExceededError):
        ask()
    assert len(client.chat_calls) == 1 and use.waits == []


# ── Unusable replies ─────────────────────────────────────────────────────────


def test_truncated_reply_raises_and_is_not_retried_blindly(use):
    client = use(chat=[chat_response('{"title": "cut of', finish_reason="length")])
    with pytest.raises(oc.OutputTruncatedError):
        ask()
    assert len(client.chat_calls) == 1          # the caller decides: bigger budget or smaller batch


@pytest.mark.parametrize("bad", [
    chat_response('{"title": "unterminated'),            # malformed JSON with finish_reason "stop"
    chat_response(""),                                    # empty body
    chat_response(None),
    chat_response("[1, 2, 3]"),                           # JSON, but not an object
    chat_response(None, refusal="I can't help with that."),
    chat_response('{"title": "x"}', finish_reason="content_filter"),
])
def test_unusable_replies_are_retried_then_raised_never_parsed(use, bad):
    client = use(chat=[bad, bad, chat_response('{"title": "recovered"}')])
    assert ask()["title"] == "recovered" and len(client.chat_calls) == 3
    client = use(chat=[bad])
    with pytest.raises(oc.ModelOutputError):
        ask()


def test_partial_reply_is_conformed_without_inventing_values(use):
    use(chat=[chat_response('{"title": "T", "kind": "zzz", "value": "1,250.5", "extra": 1}')])
    out = ask()
    assert out["title"] == "T"
    assert out["kind"] is None                  # not one of the allowed values → unanswered
    assert out["value"] == 1250.5               # a number written as text
    assert out["tags"] == [] and out["flag"] is None and out["nested"] == {"when": None}
    assert oc.conform(None, {"type": ["object", "null"], "properties": {}}) is None
    assert oc.conform({"a": True}, {"type": "object", "properties": {"a": {"type": "number"}}}) == {"a": None}


def test_structured_output_settings_are_deterministic(use):
    client = use(chat=[chat_response('{"title": "ok"}')])
    ask()
    call = client.chat_calls[0]
    assert call["temperature"] == 0.0 and call["max_tokens"] == oc.settings.chat_max_output_tokens
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True


# ── Models that accept different request forms ──────────────────────────────


def test_model_that_rejects_max_tokens_and_temperature_is_adapted_to(use):
    client = use(chat=[
        BadRequestError("Unsupported parameter: 'max_tokens' is not supported with this model. "
                        "Use 'max_completion_tokens' instead.", 400),
        BadRequestError("Unsupported value: 'temperature' does not support 0 with this model.", 400),
        chat_response('{"title": "ok"}'),
        chat_response('{"title": "again"}'),
    ])
    assert ask()["title"] == "ok"
    final = client.chat_calls[2]
    assert "max_tokens" not in final and final["max_completion_tokens"] == oc.settings.chat_max_output_tokens
    assert "temperature" not in final
    assert use.waits == []                      # adapting is not a retry and costs no wait
    ask()                                       # remembered for the rest of the process
    assert len(client.chat_calls) == 4 and "max_completion_tokens" in client.chat_calls[3]


def test_model_without_strict_schema_support_falls_back_to_json_mode(use):
    client = use(chat=[
        BadRequestError("Invalid parameter: 'response_format' of type 'json_schema' is not supported "
                        "with this model.", 400),
        chat_response('{"title": "ok", "kind": "b"}'),
    ])
    out = ask()
    assert out["title"] == "ok" and out["kind"] == "b" and out["tags"] == []      # still schema-shaped
    second = client.chat_calls[1]
    assert second["response_format"] == {"type": "json_object"}
    assert '"enum"' in second["messages"][0]["content"]                           # schema travels in the prompt


def test_json_mode_can_be_chosen_explicitly(use, monkeypatch):
    monkeypatch.setattr(oc.settings, "structured_output_mode", "json_object")
    client = use(chat=[chat_response('{"title": "ok"}')])
    ask()
    assert client.chat_calls[0]["response_format"] == {"type": "json_object"}


def test_a_genuine_bad_request_is_not_swallowed(use):
    client = use(chat=[BadRequestError("Invalid schema for response_format 'X': missing required", 400)])
    with pytest.raises(BadRequestError):
        ask()
    assert len(client.chat_calls) == 1


# ── Embeddings ───────────────────────────────────────────────────────────────


def test_embeddings_keep_input_order_and_refuse_a_short_reply(use):
    out_of_order = types.SimpleNamespace(usage=None, data=[
        types.SimpleNamespace(embedding=[2.0], index=1), types.SimpleNamespace(embedding=[1.0], index=0)])
    use(embed=[out_of_order])
    assert oc.embed_texts(["first", "second"], model="e") == [[1.0], [2.0]]
    short = types.SimpleNamespace(usage=None, data=[types.SimpleNamespace(embedding=[1.0], index=0)])
    use(embed=[short])
    with pytest.raises(oc.ModelOutputError, match="asked for 2 vectors, received 1"):
        oc.embed_texts(["first", "second"], model="e")
    assert oc.embed_texts([], model="e") == []


def test_dimensions_are_only_sent_when_configured(use, monkeypatch):
    client = use()
    oc.embed_texts(["a"], model="e")
    assert "dimensions" not in client.embed_calls[0]
    monkeypatch.setattr(oc.settings, "embedding_send_dimensions", True)
    monkeypatch.setattr(oc.settings, "embedding_dimensions", 1536)
    oc.embed_texts(["a"], model="e")
    assert client.embed_calls[1]["dimensions"] == 1536


# ── Usage accounting and concurrency ─────────────────────────────────────────


def test_token_usage_is_accumulated_for_the_stage_log(use):
    use(chat=[chat_response('{"title": "ok"}', usage=(1000, 200))])
    ask()
    ask()
    snap = oc.usage_snapshot(reset=True)
    assert snap == {"calls": 2, "prompt_tokens": 2000, "completion_tokens": 400, "retries": 0}
    assert oc.usage_snapshot()["calls"] == 0


def test_simultaneous_requests_are_capped(use, monkeypatch):
    import threading
    import time

    from shared.concurrency import bounded_map

    monkeypatch.setattr(oc.settings, "llm_max_concurrency", 2)
    monkeypatch.setattr(oc, "_slots", None)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    class _Slow(FakeOpenAI):
        def _chat(self, **kwargs):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return chat_response('{"title": "ok"}')
    client = _Slow()
    monkeypatch.setattr(oc, "openai_client", lambda: client)
    results = bounded_map(lambda _i: ask(), range(12), 8)            # 8 threads, 2 slots
    assert all(error is None for _, error in results)
    assert state["peak"] <= 2


def test_bounded_map_isolates_failures_and_keeps_order():
    from shared.concurrency import bounded_map

    def work(n):
        if n == 3:
            raise ValueError("three")
        return n * 2
    out = bounded_map(work, range(6), 3)
    assert [r for r, _ in out] == [0, 2, 4, None, 8, 10]
    assert isinstance(out[3][1], ValueError) and all(e is None for i, (_, e) in enumerate(out) if i != 3)
    assert bounded_map(work, [], 3) == []


# ── Per-task model selection ─────────────────────────────────────────────────


def test_each_task_can_use_its_own_model(monkeypatch):
    s = oc.settings
    for name, value in (("chat_model", "base"), ("extraction_model", ""), ("clause_model", ""),
                        ("validation_model", ""), ("rag_model", "")):
        monkeypatch.setattr(s, name, value)
    assert {t: s.model_for(t) for t in ("extraction", "clause", "validation", "rag")} == {
        "extraction": "base", "clause": "base", "validation": "base", "rag": "base"}
    monkeypatch.setattr(s, "extraction_model", "big")
    monkeypatch.setattr(s, "rag_model", "chatty")
    assert s.model_for("extraction") == "big" and s.model_for("clause") == "big"      # inherits extraction
    assert s.model_for("validation") == "big" and s.model_for("rag") == "chatty"
    monkeypatch.setattr(s, "clause_model", "small")
    assert s.model_for("clause") == "small"
