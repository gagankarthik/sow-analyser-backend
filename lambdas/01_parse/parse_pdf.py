"""PDF → text via pdfplumber, with Textract async fallback for scanned PDFs."""
from __future__ import annotations

import io
import time
from typing import Any

from shared.aws import textract_client
from shared.logger import get_logger

log = get_logger("blue-iq.parse.pdf")


PDFPLUMBER_MIN_TOTAL_CHARS = 200
PDFPLUMBER_MIN_CHARS_PER_PAGE = 50
TEXTRACT_POLL_INTERVAL_S = 5
TEXTRACT_MAX_WAIT_S = 240


def parse_pdf_bytes(data: bytes) -> dict[str, Any]:
    """Try pdfplumber, fall back to Textract if extraction looks anaemic.

    Returns
    -------
    {
        "text": str,
        "pages": [{page, text, char_count}, ...],
        "method": "pdfplumber" | "textract",
    }
    """
    plumber_pages = _try_pdfplumber(data)
    if plumber_pages is not None and _looks_extractable(plumber_pages):
        full = "\n\n".join(p["text"] for p in plumber_pages)
        return {"text": full, "pages": plumber_pages, "method": "pdfplumber"}

    log.info(
        "parse.pdf.fallback_to_textract",
        plumber_total_chars=sum(p["char_count"] for p in (plumber_pages or [])),
    )
    return _textract(data)


def _looks_extractable(pages: list[dict[str, Any]]) -> bool:
    total = sum(p["char_count"] for p in pages)
    if total < PDFPLUMBER_MIN_TOTAL_CHARS:
        return False
    if any(p["char_count"] < PDFPLUMBER_MIN_CHARS_PER_PAGE for p in pages):
        return False
    return True


def _try_pdfplumber(data: bytes) -> list[dict[str, Any]] | None:
    try:
        import pdfplumber  # type: ignore
    except ImportError:  # pragma: no cover
        log.error("parse.pdf.pdfplumber_unavailable")
        return None

    pages: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                pages.append(
                    {"page": i, "text": text, "char_count": len(text)}
                )
    except Exception as e:
        log.warning("parse.pdf.pdfplumber_failed", error=str(e))
        return None
    return pages


# ---------------------------------------------------------------------------
# Textract async path
# ---------------------------------------------------------------------------


def _textract(data: bytes) -> dict[str, Any]:
    """Start an async Textract job, poll for completion, paginate results.

    Textract's async API requires the document to be already in S3.  At call
    time we typically only have the bytes (we just downloaded from raw bucket).
    The brief assumes the doc is in S3 already; in the handler we pass the
    bucket+key directly via `_textract_from_s3`.  For unit testing the bytes
    flow is unused.
    """
    raise NotImplementedError("Use _textract_from_s3 with bucket+key inputs")


def textract_from_s3(bucket: str, key: str) -> dict[str, Any]:
    """Async Textract on an existing S3 object.  Returns the same shape as
    `parse_pdf_bytes`."""
    tx = textract_client()
    job = tx.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    job_id = job["JobId"]
    log.info("textract.started", jobId=job_id, bucket=bucket, key=key)

    status = "IN_PROGRESS"
    waited = 0
    while status == "IN_PROGRESS":
        if waited >= TEXTRACT_MAX_WAIT_S:
            raise TimeoutError(
                f"Textract job {job_id} did not finish in {TEXTRACT_MAX_WAIT_S}s"
            )
        time.sleep(TEXTRACT_POLL_INTERVAL_S)
        waited += TEXTRACT_POLL_INTERVAL_S
        resp = tx.get_document_text_detection(JobId=job_id, MaxResults=1)
        status = resp["JobStatus"]

    if status != "SUCCEEDED":
        raise RuntimeError(f"Textract job {job_id} failed with status {status}")

    # Paginate the full result.
    blocks: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"JobId": job_id, "MaxResults": 1000}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = tx.get_document_text_detection(**kwargs)
        blocks.extend(resp.get("Blocks", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    pages: dict[int, list[str]] = {}
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        p = int(b.get("Page", 1))
        pages.setdefault(p, []).append(b.get("Text", ""))

    page_list = []
    for p in sorted(pages):
        joined = "\n".join(pages[p])
        page_list.append({"page": p, "text": joined, "char_count": len(joined)})

    full = "\n\n".join(p["text"] for p in page_list)
    return {"text": full, "pages": page_list, "method": "textract"}
