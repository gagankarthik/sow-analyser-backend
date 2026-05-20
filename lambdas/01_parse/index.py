"""Stage 1 — Parse.

Pulls the raw upload from S3, detects DOCX vs PDF (by extension + magic bytes),
extracts text, writes parsed.json to the processed bucket, and returns the
updated pipeline event.
"""
from __future__ import annotations

import os
import uuid as _uuid
from typing import Any

from aws_lambda_powertools import Tracer

from shared.logger import get_logger
from shared.s3 import get_object, processed_key, put_json
from shared.schema import ExtractionMethod, now_iso
from shared.text import sha256_hex

from .parse_docx import parse_docx_bytes
from .parse_pdf import parse_pdf_bytes, textract_from_s3

log = get_logger("blue-iq.parse")
tracer = Tracer(service="blue-iq.parse")


# Magic bytes
_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"  # DOCX is a zip


def _detect_type(filename: str, blob: bytes) -> str:
    name = (filename or "").lower()
    head = blob[:8]
    if name.endswith(".pdf") or head.startswith(_PDF_MAGIC):
        return "pdf"
    if name.endswith(".docx") or head.startswith(_DOCX_MAGIC):
        return "docx"
    # Last-ditch: look deeper into PDF (some have a header offset)
    if _PDF_MAGIC in blob[:1024]:
        return "pdf"
    raise ValueError(f"Unsupported file type for key {filename!r}")


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict, context) -> dict:  # noqa: ARG001
    """Entry point.  See module docstring for behavior."""
    try:
        return _run(event)
    except Exception as e:
        log.exception("parse.failed", error=str(e), docId=event.get("docId"))
        raise


def _run(event: dict) -> dict:
    # Accept both naming conventions:
    #   - EventBridge S3 ObjectCreated sends "bucket" / "key"
    #   - Direct / test invocations may use "rawBucket" / "rawKey"
    raw_bucket: str = event.get("rawBucket") or event.get("bucket") or ""
    raw_key: str = event.get("rawKey") or event.get("key") or ""
    # processedBucket is never in the EventBridge payload — read from env.
    processed_bucket: str = (
        event.get("processedBucket") or os.environ.get("PROCESSED_BUCKET", "")
    )

    if not raw_bucket:
        raise ValueError("parse: rawBucket/bucket missing from event and environment")
    if not raw_key:
        raise ValueError("parse: rawKey/key missing from event and environment")
    if not processed_bucket:
        raise ValueError("parse: PROCESSED_BUCKET env var not set")

    # Extract tenantId and docId from the S3 key.
    # Upload keys have the form:  tenants/<tenantId>/uploads/<docId>/<filename>
    parts = raw_key.split("/")
    if len(parts) >= 4 and parts[0] == "tenants" and parts[2] == "uploads":
        tenant_id = parts[1]
        doc_id = parts[3]
    else:
        # Fallback for direct invocations or unexpected key shapes.
        tenant_id = event.get("tenantId") or "default"
        doc_id = event.get("docId") or str(_uuid.uuid4())
        log.warning(
            "parse.key_parse_fallback",
            rawKey=raw_key,
            tenantId=tenant_id,
            docId=doc_id,
        )

    # Normalise event so every downstream stage has consistent field names.
    event["docId"] = doc_id
    event["tenantId"] = tenant_id
    event["rawBucket"] = raw_bucket
    event["rawKey"] = raw_key
    event["processedBucket"] = processed_bucket

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    log.info("parse.start", rawKey=raw_key)

    blob = get_object(raw_bucket, raw_key)
    checksum = sha256_hex(blob)
    ftype = _detect_type(raw_key, blob)

    if ftype == "docx":
        extracted = parse_docx_bytes(blob)
        method = ExtractionMethod.DOCX
    else:
        # PDF: pdfplumber first, fall back to Textract on the in-place S3 object
        extracted = parse_pdf_bytes(blob)
        if extracted["method"] == "pdfplumber":
            method = ExtractionMethod.PDFPLUMBER
        else:
            # parse_pdf_bytes only returns pdfplumber today; fall back to S3 path
            extracted = textract_from_s3(raw_bucket, raw_key)
            method = ExtractionMethod.TEXTRACT

    # Sanity: parse_pdf_bytes returns method="pdfplumber" or signals fallback by
    # returning a method other than "pdfplumber" — but our implementation only
    # decides extractability in parse_pdf_bytes itself.  We re-check here so the
    # Textract S3 path is exercised when pdfplumber gave anaemic output.
    if ftype == "pdf" and method == ExtractionMethod.PDFPLUMBER:
        total = sum(p.get("char_count", 0) for p in extracted["pages"])
        if total < 200 or any(p.get("char_count", 0) < 50 for p in extracted["pages"]):
            log.info("parse.pdf.upgrading_to_textract")
            extracted = textract_from_s3(raw_bucket, raw_key)
            method = ExtractionMethod.TEXTRACT

    parsed = {
        "text": extracted["text"],
        "pages": extracted["pages"],
        "extracted_at": now_iso(),
        "extraction_method": method.value,
        "checksum": checksum,
    }

    out_key = processed_key(tenant_id, doc_id, "parsed.json")
    put_json(processed_bucket, out_key, parsed)
    log.info(
        "parse.done",
        method=method.value,
        pages=len(parsed["pages"]),
        chars=len(parsed["text"]),
        s3=f"s3://{processed_bucket}/{out_key}",
    )

    event["parsed"] = parsed
    return event
