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
from shared.logger import get_logger
from shared.openai_client import openai_client, embed_texts
from shared.opensearch import hybrid_search

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


@tracer.capture_lambda_handler
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    log.append_keys(invocation_id=str(uuid.uuid4()))
    try:
        return _handle(event)
    except Exception:
        log.exception("rag.handler.error")
        raise


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

    # Stream response.
    stream = openai_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        stream=True,
        temperature=0.2,
        max_tokens=600,
    )

    buf: list[str]  = []
    full: list[str] = []
    last_flush      = time.monotonic()

    for chunk in stream:
        delta = (chunk.choices[0].delta.content if chunk.choices and chunk.choices[0].delta else None)
        if not delta:
            continue
        buf.append(delta)
        full.append(delta)
        # Flush every ~80 ms or every 4 tokens.
        if time.monotonic() - last_flush > 0.08 or len(buf) >= 4:
            _push_token(session_id, "".join(buf), final=False)
            buf, last_flush = [], time.monotonic()

    if buf:
        _push_token(session_id, "".join(buf), final=False)
    _push_token(session_id, "", final=True)

    return {"sessionId": session_id, "answer": "".join(full)}


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
