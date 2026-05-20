"""Stage 2 — Classify.

Sends the parsed text to OpenAI with a strict JSON-schema response_format,
extracts clauses, computes the structural hash used downstream for parent
detection, and writes classification.json.
"""
from __future__ import annotations

from aws_lambda_powertools import Tracer

from shared.config import settings
from shared.logger import get_logger
from shared.openai_client import chat_json
from shared.s3 import processed_key, put_json
from shared.text import detect_clause_headers, structural_hash, truncate_to_tokens

from .prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    classification_schema,
)

log = get_logger("blue-iq.classify")
tracer = Tracer(service="blue-iq.classify")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    try:
        return _run(event)
    except Exception as e:
        log.exception("classify.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    doc_id: str = event["docId"]
    tenant_id: str = event["tenantId"]
    processed_bucket: str = event["processedBucket"]

    parsed = event.get("parsed")
    if not parsed or not parsed.get("text"):
        raise ValueError("classify: pipeline event missing parsed.text")

    log.append_keys(docId=doc_id, tenantId=tenant_id)

    # Truncate to model budget while preserving head + tail.
    text = truncate_to_tokens(
        parsed["text"],
        max_tokens=settings.classify_max_input_tokens,
        model=settings.chat_model,
    )

    # Cheap header detection — surfaced to the model as a hint.
    headers = detect_clause_headers(parsed["text"])
    hints = [f"{n} {t}" for n, t, _ in headers[:50]]
    log.info("classify.detected_header_hints", n=len(headers))

    user = build_user_prompt(text=text, header_hints=hints)
    result = chat_json(
        system=SYSTEM_PROMPT,
        user=user,
        json_schema=classification_schema(),
        schema_name="ContractClassification",
        model=settings.chat_model,
        temperature=0.0,
    )

    # Defensive: tighten missing optional fields.
    result.setdefault("parties", [])
    result.setdefault("clauses", [])
    result.setdefault("effectiveDate", None)
    result.setdefault("lifecycle", "draft")

    s_hash = structural_hash(result["clauses"])
    result["structuralHash"] = s_hash

    out_key = processed_key(tenant_id, doc_id, "classification.json")
    put_json(processed_bucket, out_key, result)
    log.info(
        "classify.done",
        docType=result["docType"],
        clauses=len(result["clauses"]),
        structuralHash=s_hash[:8],
        s3=f"s3://{processed_bucket}/{out_key}",
    )

    event["classification"] = result
    return event
