"""RAG resolver Lambda — backs AppSync ``askBluely`` mutation.

Flow per invocation:
  1. AppSync invokes us with ``{ input: { documentId?, question, topK }, identity }``.
  2. We do a hybrid search (vector + BM25) over OpenSearch clause indices,
     optionally filtered to one document.
  3. We compose a streaming OpenAI Chat Completions call grounded on the
     retrieved clauses.
  4. As each token arrives we POST a SigV4-signed mutation to AppSync
     (``onBluelyToken``) so any client subscribed via ``bluelyTokens`` sees
     the answer stream live.
  5. We return the full assembled answer in the mutation response.

This is a single-shot blocking Lambda — the streaming is via the AppSync
subscription channel, not via the HTTP response. Frontend pattern:
  - call ``askBluely`` mutation, receive ``{ sessionId }``
  - subscribe to ``bluelyTokens(sessionId)`` for live tokens
  - when the mutation resolves with ``answer``, finalize the bubble.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from shared.aws import get_credentials
from shared.config import settings
from shared.logger import get_logger
from shared.openai_client import client as get_openai_client, embed_texts
from shared.opensearch import hybrid_search

log: Logger = get_logger("rag")
tracer = Tracer(service="rag")

# AppSync HTTP endpoint — passed by the api-stack via env var.
# CDK sets APPSYNC_GRAPHQL_ENDPOINT; accept both names for flexibility.
APPSYNC_URL = os.environ.get("APPSYNC_GRAPHQL_ENDPOINT") or os.environ.get("APPSYNC_GRAPHQL_URL", "")
APPSYNC_REGION = settings.aws_region

# How many clauses to feed the LLM (capped to keep context cost bounded)
MAX_CONTEXT_CLAUSES = int(os.environ.get("RAG_MAX_CONTEXT_CLAUSES", "8"))
# How many input chars per clause before truncating (preserves citations)
MAX_CLAUSE_CHARS = int(os.environ.get("RAG_MAX_CLAUSE_CHARS", "1200"))

SYSTEM_PROMPT = """You are Bluely, the contract-intelligence assistant for Blue-IQ.

Rules:
- Answer ONLY using the clause excerpts provided in <context>. If the answer
  is not in the context, say so plainly.
- Cite the clause number in square brackets, e.g. [§7.2], every time you
  reference contract language.
- Be concise. Bullet lists when comparing across clauses.
- Never invent dollar amounts, dates, or counterparty names that aren't in
  the context.
- If multiple clauses contradict, surface the contradiction.
"""


@tracer.capture_lambda_handler
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    log.append_keys(invocation_id=str(uuid.uuid4()))
    try:
        # AppSync passes the mutation args under `arguments` in the default
        # Lambda direct-resolver mapping template.
        args = event.get("arguments") or event.get("input") or event
        ai_input = args.get("input") or args
        question: str = ai_input.get("question", "").strip()
        document_id: str | None = ai_input.get("documentId")
        top_k: int = int(ai_input.get("topK") or MAX_CONTEXT_CLAUSES)
        tenant_id: str = (
            event.get("identity", {}).get("resolverContext", {}).get("tenantId")
            or ai_input.get("tenantId")
            or "default"
        )
        session_id = str(uuid.uuid4())

        log.append_keys(
            session_id=session_id, document_id=document_id, tenant_id=tenant_id
        )

        if not question:
            return {"sessionId": session_id, "answer": "(empty question)"}

        # 1. Retrieve relevant clauses
        [question_vec] = embed_texts([question], model=settings.embedding_model)
        hits = hybrid_search(
            text=question,
            vector=question_vec,
            tenant_id=tenant_id,
            k=top_k,
            alpha=0.6,
        )

        if not hits:
            answer = "I couldn't find any clauses matching that question. Try rephrasing or pointing me at a specific document."
            _push_token(session_id, answer, final=True)
            return {"sessionId": session_id, "answer": answer}

        # 2. Build grounded context
        context_blocks = []
        for h in hits[:top_k]:
            text = (h.get("text") or "")[:MAX_CLAUSE_CHARS]
            context_blocks.append(
                f"[doc={h['docId']}] §{h['clauseNumber']}\n{text}"
            )
        context_str = "\n\n---\n\n".join(context_blocks)

        # 3. Streaming chat — emit tokens via AppSync subscription
        client = get_openai_client()
        user_msg = (
            f"<context>\n{context_str}\n</context>\n\nQuestion: {question}"
        )
        stream = client.chat.completions.create(
            model=settings.chat_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            stream=True,
            temperature=0.2,
            max_tokens=600,
        )

        buf: list[str] = []
        full_answer: list[str] = []
        last_flush = time.time()
        for chunk in stream:
            delta = (
                chunk.choices[0].delta.content
                if chunk.choices and chunk.choices[0].delta
                else None
            )
            if not delta:
                continue
            buf.append(delta)
            full_answer.append(delta)
            # Flush every ~80 ms or every 4 tokens — whichever is sooner.
            if time.time() - last_flush > 0.08 or len(buf) >= 4:
                _push_token(session_id, "".join(buf), final=False)
                buf = []
                last_flush = time.time()

        if buf:
            _push_token(session_id, "".join(buf), final=False)
        _push_token(session_id, "", final=True)

        answer = "".join(full_answer)
        return {"sessionId": session_id, "answer": answer}

    except Exception:
        log.exception("rag.handler.error")
        raise


def _push_token(session_id: str, token: str, *, final: bool) -> None:
    """SigV4-signed mutation to ``onBluelyToken`` so subscribers see the stream."""
    if not APPSYNC_URL:
        log.warning("appsync.url.not_configured", session_id=session_id)
        return

    payload = {
        "query": """
          mutation OnBluelyToken($sessionId: ID!, $token: String!, $final: Boolean!) {
            onBluelyToken(sessionId: $sessionId, token: $token, final: $final) {
              sessionId
              token
              final
            }
          }
        """,
        "variables": {
            "sessionId": session_id,
            "token": token,
            "final": final,
        },
    }
    body = json.dumps(payload).encode("utf-8")

    request = AWSRequest(
        method="POST",
        url=APPSYNC_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    creds = get_credentials()
    if creds is None:
        log.warning("appsync.no_credentials")
        return
    SigV4Auth(creds, "appsync", APPSYNC_REGION).add_auth(request)

    # Lazy import to avoid a hard dep — urllib is stdlib, fine for low volume
    import urllib.request

    req = urllib.request.Request(
        request.url,
        data=body,
        headers=dict(request.headers.items()),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as r:  # noqa: S310
            _ = r.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("appsync.push.failed", error=str(exc), final=final)
