"""OpenAI SDK wrapper — retries, cost logging, X-Ray compatibility.

Root problem (do not remove this comment):
  aws-lambda-powertools Tracer activates aws-xray-sdk, which monkey-patches
  httpx.Client.__init__.  The patched __init__ does not forward **kwargs, so
  when the OpenAI SDK creates its internal SyncHttpxClientWrapper (which passes
  proxies={} to httpx.Client), Lambda raises:
      TypeError: Client.__init__() got an unexpected keyword argument 'proxies'

Fix: pass a pre-built httpx.Client to OpenAI().  OpenAI uses it directly and
never calls httpx.Client(proxies=...) itself.  The pre-built client is created
with the patched __init__ but without any unsupported kwargs, which is fine.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

import httpx
import orjson
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import settings
from .logger import get_logger

log = get_logger("blue-iq.openai")


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
    """
    from openai import OpenAI

    transport = httpx.Client(
        timeout=httpx.Timeout(timeout=120.0, connect=10.0),
        follow_redirects=True,
    )
    return OpenAI(api_key=_api_key(), http_client=transport)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def _retryable() -> tuple[type[Exception], ...]:
    try:
        from openai import APIConnectionError, APITimeoutError, RateLimitError
        return (RateLimitError, APIConnectionError, APITimeoutError)
    except Exception:  # pragma: no cover
        return (Exception,)


_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(_retryable()),
)


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


def _log_usage(model: str, op: str, usage: Any) -> None:
    if not usage:
        return
    try:
        log.info(
            "openai.usage",
            op=op,
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


@_RETRY
def chat_json(
    *,
    system: str,
    user: str,
    json_schema: dict[str, Any],
    schema_name: str = "Output",
    model: str | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Structured-output chat completion. Returns a parsed dict.

    Uses OpenAI's strict JSON schema mode — the model is constrained to
    produce schema-valid output or raise BadRequestError.
    """
    mdl = model or settings.chat_model
    resp = openai_client().chat.completions.create(
        model=mdl,
        temperature=temperature,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
        },
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    _log_usage(mdl, "chat.json", getattr(resp, "usage", None))
    return orjson.loads(resp.choices[0].message.content or "{}")


@_RETRY
def chat_text(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """Free-text chat completion."""
    mdl = model or settings.chat_model
    kwargs: dict[str, Any] = {
        "model": mdl,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    resp = openai_client().chat.completions.create(**kwargs)
    _log_usage(mdl, "chat.text", getattr(resp, "usage", None))
    return resp.choices[0].message.content or ""


@_RETRY
def embed_texts(texts: Iterable[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch of texts. Caller must keep batches ≤ 100 items."""
    mdl = model or settings.embedding_model
    inputs = [t.strip() or " " for t in texts]
    if not inputs:
        return []
    resp = openai_client().embeddings.create(model=mdl, input=inputs)
    _log_usage(mdl, "embeddings", getattr(resp, "usage", None))
    return [item.embedding for item in resp.data]
