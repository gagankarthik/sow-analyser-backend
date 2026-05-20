"""OpenAI SDK wrapper with Secrets Manager key fetch, retries, and cost logging.

Usage
-----

    from shared.openai_client import chat_json, embed_texts

    obj = chat_json(
        system="You classify legal contracts.",
        user=document_text,
        json_schema={...},
    )

    vecs = embed_texts(["clause 1 body", "clause 2 body"])

Retries
-------
tenacity, 4 attempts, exponential backoff, retry on RateLimitError and
APIConnectionError.  All other errors bubble up immediately so Step Functions
can mark the execution failed.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

import orjson
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
from .logger import get_logger

log = get_logger("blue-iq.openai")


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _fetch_api_key() -> str:
    key = settings.openai_api_key
    if not key:
        raise RuntimeError("OPENAI_API_KEY env var is not set")
    return key


@lru_cache(maxsize=1)
def client():
    from openai import OpenAI

    return OpenAI(api_key=_fetch_api_key())


# ---------------------------------------------------------------------------
# Retryable wrappers
# ---------------------------------------------------------------------------


def _retryable_excs():
    """Import lazily so module import doesn't fail in environments without the SDK."""
    try:
        from openai import APIConnectionError, RateLimitError, APITimeoutError

        return (RateLimitError, APIConnectionError, APITimeoutError)
    except Exception:  # pragma: no cover
        return (Exception,)


_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(_retryable_excs()),
)


def _log_usage(model: str, op: str, usage: Any) -> None:
    """Emit a structured cost log entry."""
    if usage is None:
        return
    try:
        prompt_t = getattr(usage, "prompt_tokens", None) or usage.get("prompt_tokens", 0)
        compl_t = getattr(usage, "completion_tokens", None) or usage.get(
            "completion_tokens", 0
        )
        total_t = getattr(usage, "total_tokens", None) or usage.get("total_tokens", 0)
    except Exception:
        return
    log.info(
        "openai.usage",
        op=op,
        model=model,
        prompt_tokens=prompt_t,
        completion_tokens=compl_t,
        total_tokens=total_t,
    )


# ---------------------------------------------------------------------------
# High-level helpers
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
    """Strict JSON-schema-constrained chat completion.

    Returns the parsed dict.  Raises if the model failed to produce schema-valid
    output (the SDK normally throws BadRequestError in that case).
    """
    mdl = model or settings.chat_model
    resp = client().chat.completions.create(
        model=mdl,
        temperature=temperature,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            },
        },
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    _log_usage(mdl, "chat.json", getattr(resp, "usage", None))
    content = resp.choices[0].message.content or "{}"
    return orjson.loads(content)


@_RETRY
def chat_text(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
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
    resp = client().chat.completions.create(**kwargs)
    _log_usage(mdl, "chat.text", getattr(resp, "usage", None))
    return resp.choices[0].message.content or ""


@_RETRY
def embed_texts(texts: Iterable[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch of texts.  Caller is responsible for batching ≤ 100 inputs."""
    mdl = model or settings.embedding_model
    inputs = [t if t else " " for t in texts]
    if not inputs:
        return []
    resp = client().embeddings.create(model=mdl, input=inputs)
    _log_usage(mdl, "embeddings", getattr(resp, "usage", None))
    return [d.embedding for d in resp.data]
