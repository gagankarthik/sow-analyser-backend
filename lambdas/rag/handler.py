"""RAG Lambda — backs the AppSync `askBluely` mutation.

Flow:
  1. Embed the question via OpenAI.
  2. Hybrid search (vector + BM25) over OpenSearch clause indices.
  3. Stream a grounded GPT response; push each token batch via a SigV4-signed
     AppSync mutation (`onBluelyToken`) so subscribed clients see it live.
  4. Return the full assembled answer in the mutation response.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from shared.aws import get_credentials
from shared.config import settings
from shared.guardrails import (
    Redaction,
    Redactor,
    StreamRestorer,
    assert_provider_allowed,
    audit_send,
    validate_output,
)
from shared.logger import get_logger
from shared.openai_client import openai_client, embed_texts, chat_text
from shared.opensearch import hybrid_search, clause_search

log: Logger = get_logger("blue-iq.rag")
tracer = Tracer(service="blue-iq.rag")

APPSYNC_URL       = os.environ.get("APPSYNC_GRAPHQL_ENDPOINT") or os.environ.get("APPSYNC_GRAPHQL_URL", "")
MAX_CONTEXT_CLAUSES = int(os.environ.get("RAG_MAX_CONTEXT_CLAUSES", "8"))
MAX_CLAUSE_CHARS    = int(os.environ.get("RAG_MAX_CLAUSE_CHARS", "1200"))

_SYSTEM_PROMPT = """You are Bluely, the contract-intelligence assistant for Blue-IQ.

Rules:
- Answer ONLY using the clause excerpts in <context>. If the answer is not
  there, say so plainly.
- Cite the clause number in square brackets, e.g. [§7.2], every time you
  reference contract language.
- Be concise. Use bullet lists when comparing multiple clauses.
- Never invent dollar amounts, dates, or counterparty names not in the context.
- Surface contradictions if multiple clauses conflict.
"""

_TOKEN_MUTATION = """
  mutation OnBluelyToken($sessionId: ID!, $token: String!, $final: Boolean!) {
    onBluelyToken(sessionId: $sessionId, token: $token, final: $final) {
      sessionId token final
    }
  }
"""


def _redact(text: str) -> Redaction:
    """Pseudonymise PII before the text leaves AWS (no-op if guardrails off)."""
    if not settings.guardrails_enabled:
        return Redaction(text=text)
    return Redactor(classes=settings.redact_class_list()).redact(text)


def _audit_rag(red: Redaction, **keys: Any) -> None:
    """Record the redacted RAG send with per-class counts (best-effort)."""
    try:
        audit_send(
            provider=assert_provider_allowed(settings.ai_provider),
            op="rag.chat",
            byte_count=len(red.text.encode("utf-8")),
            redaction=red,
            **keys,
        )
    except Exception:  # pragma: no cover - auditing must never break a call
        pass


@tracer.capture_lambda_handler
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    log.append_keys(invocation_id=str(uuid.uuid4()))
    is_http = isinstance(event, dict) and bool(event.get("requestContext", {}).get("http"))
    try:
        return _http_handle(event) if is_http else _handle(event)
    except Exception:
        log.exception("rag.handler.error")
        if is_http:
            return _http_resp(500, {"error": "Internal server error"})
        raise


# ---------------------------------------------------------------------------
# HTTP API path — POST /documents/{docId}/chat (non-streaming JSON)
# ---------------------------------------------------------------------------


def _http_handle(event: dict[str, Any]) -> dict[str, Any]:
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "POST").upper()
    if method == "OPTIONS":
        return _http_resp(200, {})

    claims = (event.get("requestContext", {}).get("authorizer", {}) or {}).get("jwt", {}).get("claims", {}) or {}
    tenant_id = claims.get("custom:tenantId") or (event.get("headers") or {}).get("x-tenant-id") or "default"
    doc_id = (event.get("pathParameters") or {}).get("docId")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _http_resp(400, {"error": "Invalid JSON body"})

    question = (body.get("question") or "").strip()
    if not question:
        return _http_resp(400, {"error": "A 'question' is required"})
    top_k = max(1, min(int(body.get("topK") or MAX_CONTEXT_CLAUSES), 20))

    log.append_keys(tenant_id=tenant_id, docId=doc_id or "all")

    [q_vec] = embed_texts([question], model=settings.embedding_model)
    hits = clause_search(text=question, vector=q_vec, tenant_id=tenant_id, k=top_k, doc_id=doc_id)

    if not hits:
        return _http_resp(200, {
            "answer": "I couldn't find any clauses relevant to that question in this document. Try rephrasing, or ask about a specific clause or topic.",
            "citations": [],
        })

    context_blocks: list[str] = []
    citations: list[dict[str, str]] = []
    for h in hits:
        src = h.get("_source", {})
        cn = src.get("clauseNumber", "")
        context_blocks.append(f"[§{cn}] {(src.get('text') or '')[:MAX_CLAUSE_CHARS]}")
        citations.append({
            "clauseNumber": cn,
            "docId": src.get("docId", ""),
            "category": src.get("category", ""),
        })

    user_msg = f"<context>\n{chr(10).join(context_blocks)}\n</context>\n\nQuestion: {question}"
    red = _redact(user_msg)
    _audit_rag(red, tenantId=tenant_id, docId=doc_id or "all", clauses=len(hits))

    answer = chat_text(
        system=_SYSTEM_PROMPT,
        user=red.text,
        model=settings.chat_model,
        temperature=0.2,
        max_tokens=600,
    )
    # Restore the real values the provider never saw, then guardrail-check the reply.
    answer = Redactor.restore(answer, red.mapping)
    validate_output(answer, red.mapping)
    return _http_resp(200, {"answer": answer, "citations": citations})


def _http_resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    args       = event.get("arguments") or event.get("input") or event
    ai_input   = args.get("input") or args
    question   = (ai_input.get("question") or "").strip()
    doc_id     = ai_input.get("documentId")
    top_k      = int(ai_input.get("topK") or MAX_CONTEXT_CLAUSES)
    tenant_id  = (
        event.get("identity", {}).get("resolverContext", {}).get("tenantId")
        or ai_input.get("tenantId")
        or "default"
    )
    session_id = str(uuid.uuid4())
    log.append_keys(session_id=session_id, tenant_id=tenant_id)

    if not question:
        return {"sessionId": session_id, "answer": "(empty question)"}

    # Embed + retrieve.
    [q_vec] = embed_texts([question], model=settings.embedding_model)
    hits    = hybrid_search(text=question, vector=q_vec, tenant_id=tenant_id, k=top_k, alpha=0.6)

    if not hits:
        msg = "I couldn't find any matching clauses. Try rephrasing or specifying a document."
        _push_token(session_id, msg, final=True)
        return {"sessionId": session_id, "answer": msg}

    # Build grounded context.
    context_blocks = [
        f"[doc={h['docId']}] §{h['clauseNumber']}\n{(h.get('text') or '')[:MAX_CLAUSE_CHARS]}"
        for h in hits[:top_k]
    ]
    context_str = "\n\n---\n\n".join(context_blocks)
    user_msg    = f"<context>\n{context_str}\n</context>\n\nQuestion: {question}"

    # Pseudonymise PII before the prompt leaves AWS; restore on the way out.
    red = _redact(user_msg)
    _audit_rag(red, tenantId=tenant_id, sessionId=session_id, clauses=len(hits))

    # Stream response.
    stream = openai_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": red.text},
        ],
        stream=True,
        temperature=0.2,
        max_tokens=600,
    )

    restorer        = StreamRestorer(red.mapping)
    buf: list[str]  = []
    full: list[str] = []
    last_flush      = time.monotonic()

    for chunk in stream:
        delta = (chunk.choices[0].delta.content if chunk.choices and chunk.choices[0].delta else None)
        if not delta:
            continue
        # Restore placeholders before they reach the client; holds back any token
        # straddling a chunk boundary so it is never emitted half-restored.
        out = restorer.push(delta)
        if not out:
            continue
        buf.append(out)
        full.append(out)
        # Flush every ~80 ms or every ~16 restored chars.
        if time.monotonic() - last_flush > 0.08 or sum(len(x) for x in buf) >= 16:
            _push_token(session_id, "".join(buf), final=False)
            buf, last_flush = [], time.monotonic()

    tail = restorer.flush()
    if tail:
        buf.append(tail)
        full.append(tail)
    if buf:
        _push_token(session_id, "".join(buf), final=False)
    _push_token(session_id, "", final=True)

    answer = "".join(full)
    validate_output(answer, red.mapping)
    return {"sessionId": session_id, "answer": answer}


def _push_token(session_id: str, token: str, *, final: bool) -> None:
    if not APPSYNC_URL:
        log.warning("appsync.url.not_configured")
        return

    body = json.dumps({
        "query":     _TOKEN_MUTATION,
        "variables": {"sessionId": session_id, "token": token, "final": final},
    }).encode()

    req = AWSRequest(method="POST", url=APPSYNC_URL, data=body,
                     headers={"Content-Type": "application/json"})
    creds = get_credentials()
    if creds is None:
        log.warning("appsync.no_credentials")
        return

    SigV4Auth(creds, "appsync", settings.aws_region).add_auth(req)

    http_req = urllib.request.Request(
        req.url, data=body, headers=dict(req.headers.items()), method="POST"
    )
    try:
        with urllib.request.urlopen(http_req, timeout=2.0) as r:
            r.read()
    except Exception as exc:
        log.warning("appsync.push.failed", error=str(exc), final=final)
