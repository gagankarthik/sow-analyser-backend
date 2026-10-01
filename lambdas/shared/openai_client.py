"""OpenAI SDK wrapper — timeouts, retries, model differences, usage accounting.

Root problem (do not remove this comment):
  aws-lambda-powertools Tracer activates aws-xray-sdk, which monkey-patches
  httpx.Client.__init__.  The patched __init__ does not forward **kwargs, so
  when the OpenAI SDK creates its internal SyncHttpxClientWrapper (which passes
  proxies={} to httpx.Client), Lambda raises:
      TypeError: Client.__init__() got an unexpected keyword argument 'proxies'

Fix: pass a pre-built httpx.Client to OpenAI().  OpenAI uses it directly and
never calls httpx.Client(proxies=...) itself.  The pre-built client is created
with the patched __init__ but without any unsupported kwargs, which is fine.

Reliability contract of this module
-----------------------------------
* Every request has its own timeout, and never runs past the stage deadline set
  with ``set_deadline`` — one hung call cannot consume the Lambda's time limit.
* Retries are owned here (the SDK's own hidden retries are switched off so they
  are not multiplied): rate limits, timeouts, connection errors, 5xx and
  unusable output are retried with exponential backoff + jitter, honouring a
  ``Retry-After`` header.
* A reply cut off by the output limit raises ``OutputTruncatedError``; a reply
  that is not valid JSON, is empty, or is a refusal raises ``ModelOutputError``.
  Neither is ever parsed into a partial result.
* Models differ. A model that rejects ``max_tokens``, ``temperature`` or strict
  ``json_schema`` output is detected from its 400 response and the request is
  re-sent in the form it accepts; the reply is then conformed to the schema in
  code, so callers always receive every key the schema defines.
"""
from __future__ import annotations

import random
import threading
import time
from functools import lru_cache
from typing import Any, Callable, Iterable

import httpx
import orjson

from .config import settings
from .logger import get_logger

log = get_logger("blue-iq.openai")

_sleep = time.sleep          # indirection so tests do not actually wait


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutputTruncatedError(RuntimeError):
    """Raised when the model stopped because it hit the output token limit.

    Silently parsing a length-truncated structured response would drop trailing
    content. We raise so the caller can retry with a larger budget (or a smaller
    batch) instead of persisting a partial extraction.
    """


class ModelOutputError(RuntimeError):
    """The model answered, but not with something usable: invalid JSON, an empty
    body, a refusal or a content-filter stop."""


class DeadlineExceededError(RuntimeError):
    """The stage ran out of time before this call could be made or retried."""


# ---------------------------------------------------------------------------
# Stage deadline + usage accounting (process-wide, thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_deadline: float | None = None
_usage: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "retries": 0}
_quirks: dict[str, set[str]] = {}


def set_deadline(remaining_ms: int | None, reserve_s: float = 25.0) -> None:
    """Tell the client how long the current invocation may still run. ``reserve_s``
    is held back for the stage's own bookkeeping (writing results, status)."""
    global _deadline
    with _lock:
        _deadline = None if remaining_ms is None else time.monotonic() + max(0.0, remaining_ms / 1000.0 - reserve_s)


def time_left() -> float | None:
    with _lock:
        return None if _deadline is None else _deadline - time.monotonic()


def usage_snapshot(reset: bool = False) -> dict[str, int]:
    """Calls / tokens / retries since the last reset — for the per-stage log line."""
    with _lock:
        snap = dict(_usage)
        if reset:
            for k in _usage:
                _usage[k] = 0
    return snap


def _count(**inc: int) -> None:
    with _lock:
        for k, v in inc.items():
            _usage[k] = _usage.get(k, 0) + int(v or 0)


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _api_key() -> str:
    """Return the OpenAI API key from env or Secrets Manager."""
    key = settings.openai_api_key
    if key:
        return key

    import os
    arn = os.environ.get("OPENAI_SECRET_ARN", "")
    if not arn:
        raise RuntimeError(
            "OpenAI API key not configured. Set OPENAI_API_KEY env var "
            "or OPENAI_SECRET_ARN pointing to a Secrets Manager secret."
        )

    from .aws import secrets_client
    try:
        key = secrets_client().get_secret_value(SecretId=arn).get("SecretString", "")
    except Exception as exc:
        raise RuntimeError(f"Failed to read OpenAI key from Secrets Manager ({arn}): {exc}") from exc

    if not key:
        raise RuntimeError(f"Secrets Manager secret {arn} is empty.")
    return key


# ---------------------------------------------------------------------------
# Client — singleton per Lambda process
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def openai_client():
    """Return a cached OpenAI client with an explicit httpx transport.

    Passing http_client bypasses OpenAI's own httpx.Client construction,
    which is the call that triggers the aws-xray-sdk compatibility error.

    Before any client is built we assert the configured provider is on the
    no-train allowlist (guardrails). This fails closed: a misconfigured provider
    can never receive a single byte of client data.
    """
    from openai import OpenAI

    from .guardrails import assert_provider_allowed

    assert_provider_allowed(settings.ai_provider)

    transport = httpx.Client(
        timeout=httpx.Timeout(timeout=settings.openai_timeout_s, connect=10.0),
        follow_redirects=True,
    )
    # max_retries=0: retries are done once, visibly, by _with_retries below.
    return OpenAI(api_key=_api_key(), http_client=transport, max_retries=0)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

_RETRYABLE_NAMES = {
    "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
    "ConnectError", "ReadTimeout", "ConnectTimeout", "TimeoutException", "RemoteProtocolError",
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ModelOutputError):
        return True
    if isinstance(exc, (OutputTruncatedError, DeadlineExceededError)):
        return False
    if type(exc).__name__ in _RETRYABLE_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status in (408, 409, 429))


def _retry_after(exc: BaseException) -> float | None:
    try:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        raw = headers.get("retry-after")
        return min(60.0, float(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def _request_timeout(configured: float) -> float:
    """Timeout for the next request: the configured value, capped by what is left
    of the stage deadline."""
    left = time_left()
    if left is None:
        return configured
    if left <= 1.0:
        raise DeadlineExceededError(
            "The analysis ran out of time before it could finish. Re-analyze the document."
        )
    return max(1.0, min(configured, left))


_slots: threading.BoundedSemaphore | None = None


def _request_slots() -> threading.BoundedSemaphore:
    """Process-wide cap on simultaneous OpenAI requests (LLM_MAX_CONCURRENCY), no
    matter how many thread pools the stages open."""
    global _slots
    with _lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(max(1, settings.llm_max_concurrency))
        return _slots


def _with_retries(op: str, fn: Callable[[], Any]) -> Any:
    attempts = max(1, settings.openai_max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            with _request_slots():
                return fn()
        except Exception as exc:
            if attempt >= attempts or not _is_retryable(exc):
                raise
            base = min(30.0, 2.0 ** attempt)
            wait = _retry_after(exc) or random.uniform(base / 2.0, base)
            left = time_left()
            if left is not None and left < wait + 5.0:
                raise DeadlineExceededError(
                    "The analysis ran out of time while retrying the AI service. "
                    "Re-analyze the document."
                ) from exc
            _count(retries=1)
            log.warning("openai.retry", op=op, attempt=attempt, wait_s=round(wait, 1),
                        error_type=type(exc).__name__)
            _sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


def _audit(op: str, *texts: str) -> None:
    """Record an outbound AI call for compliance evidence (best-effort)."""
    try:
        from .guardrails import assert_provider_allowed, audit_send

        provider = assert_provider_allowed(settings.ai_provider)
        audit_send(
            provider=provider,
            op=op,
            byte_count=sum(len(t.encode("utf-8")) for t in texts if t),
        )
    except Exception:  # pragma: no cover - auditing must never break a call
        pass


def _log_usage(model: str, op: str, usage: Any) -> None:
    _count(calls=1)
    if not usage:
        return
    try:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        _count(prompt_tokens=prompt, completion_tokens=completion)
        log.info(
            "openai.usage",
            op=op,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(getattr(usage, "total_tokens", 0) or prompt + completion),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Model differences
# ---------------------------------------------------------------------------


def _model_quirks(model: str) -> set[str]:
    with _lock:
        return set(_quirks.get(model, ()))


def _learn_quirk(model: str, exc: BaseException) -> str | None:
    """Read a 400 response and work out which request feature the model rejects.
    Returns the quirk learnt, or None if the error is something else."""
    status = getattr(exc, "status_code", None)
    if status != 400 and type(exc).__name__ != "BadRequestError":
        return None
    msg = str(exc).lower()
    known = _model_quirks(model)
    quirk: str | None = None
    if "max_completion_tokens" in msg and "max_completion_tokens" not in known:
        quirk = "max_completion_tokens"
    elif "temperature" in msg and ("unsupported" in msg or "not support" in msg or "only the default" in msg) \
            and "no_temperature" not in known:
        quirk = "no_temperature"
    elif ("json_schema" in msg or "response_format" in msg) and ("not supported" in msg or "unsupported" in msg) \
            and "json_object" not in known:
        quirk = "json_object"
    if quirk:
        with _lock:
            _quirks.setdefault(model, set()).add(quirk)
        log.warning("openai.model_quirk", model=model, quirk=quirk)
    return quirk


def _chat_kwargs(model: str, messages: list[dict[str, str]], temperature: float,
                 max_tokens: int | None, timeout: float) -> dict[str, Any]:
    quirks = _model_quirks(model)
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "timeout": timeout}
    if "no_temperature" not in quirks:
        kwargs["temperature"] = temperature
    if max_tokens:
        kwargs["max_completion_tokens" if "max_completion_tokens" in quirks else "max_tokens"] = max_tokens
    return kwargs


def _json_mode(model: str) -> str:
    if settings.structured_output_mode == "json_object" or "json_object" in _model_quirks(model):
        return "json_object"
    return "json_schema"


def conform(value: Any, schema: dict[str, Any]) -> Any:
    """Shape a model reply to ``schema`` without inventing data.

    Every property the schema defines is present afterwards; one the model left
    out becomes null (or [] for a list) — never a made-up value. A value outside
    an enum becomes null so a caller can see it was not answered.
    """
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "object" in types:
        if not isinstance(value, dict):
            if value is None and "null" in types:
                return None
            value = {}
        out = dict(value)
        for key, sub in (schema.get("properties") or {}).items():
            out[key] = conform(value.get(key), sub)
        return out
    if "array" in types:
        if not isinstance(value, list):
            return []
        items = schema.get("items") or {}
        return [conform(v, items) for v in value]
    if value is None:
        return None
    if "enum" in schema:
        return value if value in schema["enum"] else None
    if "boolean" in types:
        return value if isinstance(value, bool) else None
    if "number" in types or "integer" in types:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            return None
    if "string" in types:
        return value if isinstance(value, str) else str(value)
    return value


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def chat_json(
    *,
    system: str,
    user: str,
    json_schema: dict[str, Any],
    schema_name: str = "Output",
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Structured-output chat completion. Returns a dict conformed to the schema.

    Raises ``OutputTruncatedError`` if the model stopped on the output limit and
    ``ModelOutputError`` if the reply is unusable after retries — a truncated or
    malformed body is never returned as if it were a complete answer.
    """
    mdl = model or settings.model_for("extraction")
    budget = max_tokens or settings.chat_max_output_tokens
    _audit("chat.json", system, user)

    def once() -> dict[str, Any]:
        for _ in range(4):  # at most one re-send per learnable quirk
            mode = _json_mode(mdl)
            if mode == "json_schema":
                sys_msg = system
                response_format: dict[str, Any] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
                }
            else:
                sys_msg = (
                    f"{system}\n\nReturn ONE JSON object that conforms to this JSON Schema. "
                    f"Include every property; use null when a value is absent.\n"
                    f"{orjson.dumps(json_schema).decode()}"
                )
                response_format = {"type": "json_object"}
            kwargs = _chat_kwargs(
                mdl,
                [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
                temperature, budget, _request_timeout(timeout or settings.openai_timeout_s),
            )
            try:
                resp = openai_client().chat.completions.create(response_format=response_format, **kwargs)
            except Exception as exc:
                if _learn_quirk(mdl, exc):
                    continue
                raise
            break
        else:  # pragma: no cover - defensive
            raise ModelOutputError(f"{schema_name}: model rejected every supported request form")

        _log_usage(mdl, "chat.json", getattr(resp, "usage", None))
        choice = resp.choices[0]
        finish = getattr(choice, "finish_reason", None)
        if finish == "length":
            raise OutputTruncatedError(
                f"{schema_name}: model output hit the {budget}-token limit and was truncated — "
                "the result would be missing trailing content."
            )
        message = choice.message
        if getattr(message, "refusal", None):
            raise ModelOutputError(f"{schema_name}: the model declined to answer")
        if finish == "content_filter":
            raise ModelOutputError(f"{schema_name}: the reply was blocked by the content filter")
        content = getattr(message, "content", None)
        if not content or not content.strip():
            raise ModelOutputError(f"{schema_name}: the model returned an empty reply")
        try:
            parsed = orjson.loads(content)
        except orjson.JSONDecodeError as exc:
            raise ModelOutputError(f"{schema_name}: the model returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelOutputError(f"{schema_name}: the model returned JSON that is not an object")
        return conform(parsed, json_schema)

    return _with_retries("chat.json", once)


def chat_text(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    """Free-text chat completion."""
    mdl = model or settings.chat_model
    _audit("chat.text", system, user)

    def once() -> str:
        for _ in range(3):
            kwargs = _chat_kwargs(
                mdl,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature, max_tokens, _request_timeout(timeout or settings.openai_timeout_s),
            )
            try:
                resp = openai_client().chat.completions.create(**kwargs)
            except Exception as exc:
                if _learn_quirk(mdl, exc):
                    continue
                raise
            _log_usage(mdl, "chat.text", getattr(resp, "usage", None))
            return resp.choices[0].message.content or ""
        raise ModelOutputError("chat.text: model rejected every supported request form")  # pragma: no cover

    return _with_retries("chat.text", once)


def stream_kwargs(model: str, messages: list[dict[str, str]], temperature: float,
                  max_tokens: int) -> dict[str, Any]:
    """Request arguments for a streaming chat call, in the form ``model`` accepts."""
    return {**_chat_kwargs(model, messages, temperature, max_tokens,
                           _request_timeout(settings.openai_timeout_s)), "stream": True}


def embed_texts(texts: Iterable[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch of texts, one vector per input, in input order.

    Raises if the API returns a different number of vectors than inputs — a
    short reply must never be zipped against the wrong clauses.
    """
    mdl = model or settings.embedding_model
    inputs = [t.strip() or " " for t in texts]
    if not inputs:
        return []
    _audit("embeddings", *inputs)

    def once() -> list[list[float]]:
        kwargs: dict[str, Any] = {
            "model": mdl, "input": inputs,
            "timeout": _request_timeout(settings.openai_embed_timeout_s),
        }
        if settings.embedding_send_dimensions:
            kwargs["dimensions"] = settings.embedding_dimensions
        resp = openai_client().embeddings.create(**kwargs)
        _log_usage(mdl, "embeddings", getattr(resp, "usage", None))
        data = sorted(resp.data, key=lambda item: getattr(item, "index", 0) or 0)
        if len(data) != len(inputs):
            raise ModelOutputError(
                f"embeddings: asked for {len(inputs)} vectors, received {len(data)}"
            )
        return [item.embedding for item in data]

    return _with_retries("embeddings", once)
