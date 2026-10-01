"""Stage 01 — Parse: extract the full text of a DOCX, PDF or plain-text file.

Strategy
--------
  DOCX → direct WordprocessingML walk (shared/docx_text.py): body in document
         order with tables in place, content controls, tracked insertions, text
         boxes, automatic numbering, headers/footers and footnotes.
  TXT  → BOM-aware decode (UTF-8 / UTF-16), then Windows-1252, never failing.
  PDF  → pdfplumber page by page (two-column pages are read column by column;
         running headers/footers and page numbers are removed after the first
         occurrence), with Textract OCR for
           · a PDF with no text layer at all, and
           · the individual pages of a mixed PDF that are scanned images or
             whose text is rotated — those pages used to be silently lost.

Nothing is truncated here: there is no page cap and no character cap. What the
stage could NOT read is reported in ``parsed.stats`` (empty pages, pages that
needed OCR, warnings) instead of being dropped without trace.
"""
from __future__ import annotations

import io
import os
import re
import time
from typing import Any

from shared.aws import textract_client
from shared.docx_text import extract_docx
from shared.dynamodb import get_doc_meta, update_status
from shared.errors import UserFacingError
from shared.logger import get_logger
from shared.s3 import get_object, head_object, processed_key, put_json
from shared.schema import ExtractionMethod, now_iso
from shared.text import clean_text, sha256_hex

log = get_logger("blue-iq.parse")

_PDF_MAGIC  = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"

TEXTRACT_POLL_INTERVAL_S = 5
TEXTRACT_MAX_WAIT_S      = 240

# A presigned PUT cannot carry a size limit, so the cap is enforced here, before
# the object is pulled into memory. Matches the "up to 50 MB" the UI advertises.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

# A PDF whose text layer holds fewer characters than this in total is treated as
# scanned and sent to OCR.
MIN_TEXT_CHARS = 200
# A page with almost no text that is mostly one image is a scanned page.
SPARSE_PAGE_CHARS = 40
SCANNED_IMAGE_COVERAGE = 0.5

_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_HEADING_LINE_RE = re.compile(
    r"^(schedule|exhibit|annexure|annex|appendix|attachment|addendum|part|article|section|clause|amendment)\b",
    re.IGNORECASE,
)
_PAGE_NUMBER_RE = re.compile(r"^\W*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\W*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    raw_bucket       = event["rawBucket"]
    raw_key          = event["rawKey"]
    processed_bucket = event["processedBucket"]

    # Parse tenantId / docId from key: tenants/<tenantId>/uploads/<docId>/<file>
    doc_id, tenant_id = _ids_from_key(raw_key, event)
    event["docId"]    = doc_id
    event["tenantId"] = tenant_id

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    log.info("parse.start")

    # Only process an upload the API issued: there must be a META row for this
    # docId, owned by the tenant named in the key, pointing at this exact object.
    # This also stops a run for a document that was deleted while it was queued.
    meta = get_doc_meta(doc_id)
    if not meta or meta.get("tenantId") != tenant_id or meta.get("rawKey") != raw_key:
        raise PermissionError("parse: upload does not match a known document")

    size = int(head_object(raw_bucket, raw_key).get("ContentLength", 0))
    if size > MAX_UPLOAD_BYTES:
        raise UserFacingError(
            f"File is too large ({size // (1024 * 1024)} MB). "
            f"The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    update_status(doc_id, "PARSING")

    blob     = get_object(raw_bucket, raw_key)
    checksum = sha256_hex(blob)
    ftype    = _detect_type(raw_key, blob)
    warnings: list[str] = []
    stats: dict[str, Any] = {}
    headings: list[str] = []

    if ftype == "docx":
        extracted = _parse_docx(blob)
        method    = ExtractionMethod.DOCX
        headings  = extracted.get("headings") or []
        stats.update(extracted.get("stats") or {})
    elif ftype == "txt":
        extracted = _parse_txt(blob)
        method    = ExtractionMethod.TEXT
        stats["encoding"] = extracted.get("encoding")
    else:
        extracted, method = _parse_pdf(blob, raw_bucket, raw_key, warnings, stats)

    pages = [
        {**p, "text": clean_text(p.get("text") or "")} for p in extracted["pages"]
    ]
    for p in pages:
        p["char_count"] = len(p["text"])
    # The document text is exactly the pages joined by a blank line, so a character
    # offset in it can always be mapped back to its page.
    text = "\n\n".join(p["text"] for p in pages)
    if not text.strip():
        raise UserFacingError(
            "No readable text was found in this file. If it is a scan, make sure the pages are "
            "legible; if it is password-protected, remove the password and upload it again."
        )

    empty_pages = [p["page"] for p in pages if not p["text"].strip()]
    if empty_pages and len(pages) > 1:
        stats["emptyPages"] = empty_pages
    stats.update(pages=len(pages), chars=len(text))
    if warnings:
        stats["warnings"] = warnings

    parsed = {
        "text":              text,
        "pages":             pages,
        "extracted_at":      now_iso(),
        "extraction_method": method.value,
        "checksum":          checksum,
        "headings":          headings,
        "stats":             stats,
    }
    out_key = processed_key(tenant_id, doc_id, "parsed.json")
    put_json(processed_bucket, out_key, parsed)
    log.info("parse.done", method=method.value, pages=len(pages), chars=len(text),
             emptyPages=len(empty_pages), ocrPages=len(stats.get("ocrPages") or []),
             columnPages=len(stats.get("columnPages") or []),
             headerFooterLinesRemoved=stats.get("headerFooterLinesRemoved", 0),
             warnings=len(warnings), fileBytes=size)

    event["parsed"] = parsed
    return event


# ---------------------------------------------------------------------------
# DOCX extraction
# ---------------------------------------------------------------------------


def _parse_docx(data: bytes) -> dict[str, Any]:
    try:
        out = extract_docx(data)
    except UserFacingError:
        raise
    except Exception as exc:
        log.warning("parse.docx.unreadable", error_type=type(exc).__name__)
        raise UserFacingError(
            "This Word file could not be opened. It may be corrupted or password-protected. "
            "Re-save it as .docx (or PDF) and upload it again."
        ) from exc
    full = out["text"]
    return {"text": full, "pages": [{"page": 1, "text": full, "char_count": len(full)}],
            "headings": out["headings"], "stats": out["stats"]}


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def _parse_txt(data: bytes) -> dict[str, Any]:
    """Decode a text file without ever raising and without mangling it.

    Byte-order marks are honoured (Windows Notepad "Unicode" is UTF-16, which a
    Latin-1 fallback turns into NUL-riddled garbage), UTF-16 without a BOM is
    recognised by its NUL pattern, and the last resort is Windows-1252 — the
    encoding in which €, curly quotes and dashes are actual characters.
    """
    encoding = "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        full, encoding = data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        full, encoding = data.decode("utf-16", errors="replace"), "utf-16"
    else:
        sample = data[:4000]
        nul_even = sample[0::2].count(0)
        nul_odd = sample[1::2].count(0)
        half = max(1, len(sample) // 2)
        if nul_odd > half * 0.3 and nul_even < half * 0.05:
            full, encoding = data.decode("utf-16-le", errors="replace"), "utf-16-le"
        elif nul_even > half * 0.3 and nul_odd < half * 0.05:
            full, encoding = data.decode("utf-16-be", errors="replace"), "utf-16-be"
        else:
            try:
                full = data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    full, encoding = data.decode("cp1252"), "cp1252"
                except UnicodeDecodeError:
                    full, encoding = data.decode("latin-1", errors="replace"), "latin-1"
    full = clean_text(full).strip()
    return {"text": full, "pages": [{"page": 1, "text": full, "char_count": len(full)}],
            "encoding": encoding}


# ---------------------------------------------------------------------------
# PDF — pdfplumber (native text layer)
# ---------------------------------------------------------------------------


def _parse_pdf(
    blob: bytes, bucket: str, key: str, warnings: list[str], stats: dict[str, Any]
) -> tuple[dict[str, Any], ExtractionMethod]:
    extracted = _try_pdfplumber(blob)
    if not extracted or not _has_content(extracted["pages"]):
        log.info("parse.pdf.fallback_to_textract", reason="insufficient pdfplumber text")
        ocr = _textract_async(bucket, key)
        if ocr.get("partial"):
            warnings.append("Text recognition could not read every page of this scan.")
        stats["ocrPages"] = [p["page"] for p in ocr["pages"]]
        return ocr, ExtractionMethod.TEXTRACT

    pages = extracted["pages"]
    stats["columnPages"] = [p["page"] for p in pages if p.get("columns")]
    need_ocr = [p["page"] for p in pages if p.get("needs_ocr")]
    if need_ocr:
        # Mixed PDF: some pages are scans (or rotated). Read those with OCR and
        # keep the native text layer for the rest.
        try:
            ocr = _textract_async(bucket, key)
            by_page = {p["page"]: p["text"] for p in ocr["pages"]}
            recovered = []
            for p in pages:
                if p["page"] in need_ocr and len(by_page.get(p["page"], "").strip()) > len(p["text"].strip()):
                    p["text"] = by_page[p["page"]]
                    recovered.append(p["page"])
            stats["ocrPages"] = recovered
            missed = [n for n in need_ocr if n not in recovered]
            if missed:
                warnings.append(
                    f"{len(missed)} page(s) appear to be scanned images and could not be read: "
                    f"{', '.join(map(str, missed[:20]))}."
                )
        except Exception as exc:
            log.warning("parse.pdf.page_ocr_failed", error_type=type(exc).__name__, pages=len(need_ocr))
            warnings.append(
                f"{len(need_ocr)} page(s) appear to be scanned images and could not be read: "
                f"{', '.join(map(str, need_ocr[:20]))}."
            )
    for p in pages:
        p.pop("needs_ocr", None)
        p.pop("columns", None)
    stats["headerFooterLinesRemoved"] = strip_repeating_lines(pages)
    return {"text": "\n\n".join(p["text"] for p in pages), "pages": pages}, ExtractionMethod.PDFPLUMBER


def _try_pdfplumber(data: bytes) -> dict[str, Any] | None:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return None

    pages: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    pages.append({"page": i, **_extract_pdf_page(page)})
                except Exception as exc:
                    # One unreadable page must not discard the others — mark it
                    # for OCR instead.
                    log.warning("parse.pdf.page_error", page=i, error_type=type(exc).__name__)
                    pages.append({"page": i, "text": "", "char_count": 0, "needs_ocr": True})
    except Exception as exc:
        log.warning("parse.pdf.pdfplumber_error", error_type=type(exc).__name__)
        return None

    full = "\n\n".join(p["text"] for p in pages)
    return {"text": full, "pages": pages}


def _extract_pdf_page(page: Any) -> dict[str, Any]:
    """Text of one pdfplumber page, in reading order, plus what it needs."""
    width, height = float(page.width), float(page.height)
    words = page.extract_words() or []
    layout = detect_columns(words, width, height) if not _has_tables(page) else None
    if layout:
        gutter, top_cut, bottom_cut = layout
        parts = []
        if top_cut > 0:
            parts.append(page.crop((0, 0, width, top_cut)).extract_text() or "")
        parts.append(page.crop((0, top_cut, gutter, bottom_cut)).extract_text() or "")
        parts.append(page.crop((gutter, top_cut, width, bottom_cut)).extract_text() or "")
        if bottom_cut < height:
            parts.append(page.crop((0, bottom_cut, width, height)).extract_text() or "")
        text = "\n".join(p.strip() for p in parts if p and p.strip())
    else:
        text = (page.extract_text() or "").strip()

    needs_ocr = False
    if len(text) < SPARSE_PAGE_CHARS and _image_coverage(page, width, height) >= SCANNED_IMAGE_COVERAGE:
        needs_ocr = True
    else:
        chars = getattr(page, "chars", None) or []
        if len(chars) >= 40 and sum(1 for c in chars if c.get("upright") is False) / len(chars) > 0.5:
            needs_ocr = True          # rotated text: the text layer reads as garbage
    return {"text": text, "char_count": len(text), "needs_ocr": needs_ocr, "columns": bool(layout)}


def _has_tables(page: Any) -> bool:
    try:
        return bool(page.find_tables())
    except Exception:
        return False


def _image_coverage(page: Any, width: float, height: float) -> float:
    area = max(1.0, width * height)
    best = 0.0
    for img in getattr(page, "images", None) or []:
        try:
            w = float(img.get("x1", 0)) - float(img.get("x0", 0))
            h = float(img.get("bottom", 0)) - float(img.get("top", 0))
        except (TypeError, ValueError):
            continue
        best = max(best, max(0.0, w) * max(0.0, h) / area)
    return best


def detect_columns(
    words: list[dict[str, Any]], width: float, height: float
) -> tuple[float, float, float] | None:
    """Decide whether a page is set in two prose columns.

    Returns ``(gutter_x, top_cut, bottom_cut)`` — the x of the gutter and the
    band between a full-width heading at the top and a full-width footer at the
    bottom — or None for an ordinary page. The test is deliberately strict,
    because reading a page column-by-column when it is NOT in columns would
    separate the two halves of every table row:

    * a vertical strip near the middle that no word crosses, apart from words in
      the top/bottom margin (a centred title, a footer);
    * at least 8 text lines on each side, each side holding 30 %+ of the words;
    * prose on both sides — a median of 4+ words per line, with the left-hand
      lines running up to the gutter. (A label/value table has short left cells.)
    """
    usable = [w for w in words if w.get("text", "").strip()]
    if len(usable) < 80 or width <= 0 or height <= 0:
        return None
    lo, hi = width * 0.35, width * 0.65
    top_zone, bottom_zone = height * 0.14, height * 0.92

    def crossing(x: float) -> list[dict[str, Any]]:
        return [w for w in usable if float(w["x0"]) < x < float(w["x1"])]

    best: tuple[float, list[dict[str, Any]]] | None = None
    steps = 24
    for i in range(steps + 1):
        x = lo + (hi - lo) * i / steps
        cross = crossing(x)
        body_cross = [w for w in cross if top_zone <= float(w["top"]) <= bottom_zone]
        if body_cross:
            continue
        # the strip must be genuinely empty, not a gap between two words
        near = [w for w in usable if top_zone <= float(w["top"]) <= bottom_zone
                and (abs(float(w["x1"]) - x) < width * 0.008 or abs(float(w["x0"]) - x) < width * 0.008)]
        if near:
            continue
        if best is None or abs(x - width / 2) < abs(best[0] - width / 2):
            best = (x, cross)
    if best is None:
        return None
    gutter, cross = best

    top_cut = max((float(w["bottom"]) for w in cross if float(w["top"]) < top_zone), default=0.0)
    bottom_cut = min((float(w["top"]) for w in cross if float(w["top"]) > bottom_zone), default=height)
    body = [w for w in usable if float(w["top"]) >= top_cut and float(w["bottom"]) <= bottom_cut]
    left = [w for w in body if float(w["x1"]) <= gutter]
    right = [w for w in body if float(w["x0"]) >= gutter]
    if not body or min(len(left), len(right)) < 0.3 * len(body):
        return None

    def lines(side: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        rows: dict[int, list[dict[str, Any]]] = {}
        for w in side:
            rows.setdefault(int(round(float(w["top"]) / 3.0)), []).append(w)
        return list(rows.values())

    def median(values: list[float]) -> float:
        s = sorted(values)
        return s[len(s) // 2] if s else 0.0

    left_lines, right_lines = lines(left), lines(right)
    if len(left_lines) < 8 or len(right_lines) < 8:
        return None
    if median([len(l) for l in left_lines]) < 4 or median([len(l) for l in right_lines]) < 4:
        return None
    left_edge = median([max(float(w["x1"]) for w in l) for l in left_lines])
    if gutter - left_edge > width * 0.12:
        return None
    return gutter, top_cut, bottom_cut


def strip_repeating_lines(pages: list[dict[str, Any]]) -> int:
    """Remove running headers / footers and page numbers that repeat across pages.

    A line counts as a header/footer when the same text (digits ignored, so
    "Page 3 of 12" matches "Page 4 of 12") is one of the first two or last two
    lines on at least 60 % of the pages (minimum 3). Its FIRST occurrence is
    kept unless it is only a page number — a header often carries the contract
    number, which must survive once. Returns how many lines were removed.
    """
    with_text = [p for p in pages if (p.get("text") or "").strip()]
    if len(with_text) < 3:
        return 0

    def norm(line: str) -> str:
        """Key under which two lines count as "the same header/footer": the exact
        text, except that a page number is folded (so "Page 3 of 12" matches
        "Page 4 of 12", and "Acme Confidential 3" matches "… 4"). Digits anywhere
        else are NOT folded — "Milestone 3 … $10,000" and "Milestone 7 … $20,000"
        at the top of two pages are different lines of a fee table."""
        text = re.sub(r"\s+", " ", line.strip().lower())
        if _PAGE_NUMBER_RE.match(text) or re.search(r"\bpage\s+\d", text):
            return re.sub(r"\d+", "#", text)
        if _HEADING_LINE_RE.match(text):
            return text                 # "Appendix 2" / "Schedule 3" are headings, not page furniture
        return re.sub(r"^\d{1,3}\s+(?=\D)|(?<=\D)\s+\d{1,3}$", " #", text).strip()

    def edges(lines: list[str]) -> tuple[list[int], list[int]]:
        idx = [i for i, line in enumerate(lines) if line.strip()]
        return idx[:2], idx[-2:] if len(idx) > 2 else []

    counts: dict[tuple[str, str], int] = {}
    for p in with_text:
        lines = p["text"].split("\n")
        top, bottom = edges(lines)
        for zone, indexes in (("top", top), ("bottom", bottom)):
            for key in {norm(lines[i]) for i in indexes}:
                if key:
                    counts[(zone, key)] = counts.get((zone, key), 0) + 1
    threshold = max(3, int(len(with_text) * 0.6 + 0.999))
    repeating = {key for key, n in counts.items() if n >= threshold}
    if not repeating:
        return 0

    removed = 0
    kept_once: set[tuple[str, str]] = set()
    for p in with_text:
        lines = p["text"].split("\n")
        top, bottom = edges(lines)
        drop: set[int] = set()
        for zone, indexes in (("top", top), ("bottom", bottom)):
            for i in indexes:
                key = (zone, norm(lines[i]))
                if key not in repeating:
                    continue
                if not _PAGE_NUMBER_RE.match(lines[i].strip()) and key not in kept_once:
                    kept_once.add(key)
                    continue
                drop.add(i)
        if drop:
            removed += len(drop)
            p["text"] = "\n".join(line for i, line in enumerate(lines) if i not in drop).strip()
            p["char_count"] = len(p["text"])
    return removed


def _has_content(pages: list[dict[str, Any]]) -> bool:
    """Return True when the document has enough text to be usable.

    Only the TOTAL character count matters here — a single sparse page (cover,
    blank, table-of-contents) should not force OCR of the whole file. Individual
    scanned pages inside a text PDF are handled per page (see _parse_pdf).
    """
    return sum(p["char_count"] for p in pages) >= MIN_TEXT_CHARS


# ---------------------------------------------------------------------------
# PDF — Textract async (scanned / image-only)
# ---------------------------------------------------------------------------


def _textract_async(bucket: str, key: str) -> dict[str, Any]:
    tx     = textract_client()
    job_id = tx.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )["JobId"]
    log.info("textract.started", jobId=job_id)

    waited = 0
    while True:
        if waited >= TEXTRACT_MAX_WAIT_S:
            raise TimeoutError(f"Textract job {job_id} timed out after {TEXTRACT_MAX_WAIT_S}s")
        time.sleep(TEXTRACT_POLL_INTERVAL_S)
        waited += TEXTRACT_POLL_INTERVAL_S
        status  = tx.get_document_text_detection(JobId=job_id, MaxResults=1)["JobStatus"]
        if status != "IN_PROGRESS":
            break

    # PARTIAL_SUCCESS still carries the pages that were read; discarding them
    # would turn "one bad page" into "no document".
    if status not in ("SUCCEEDED", "PARTIAL_SUCCESS"):
        raise RuntimeError(f"Textract job {job_id} failed with status: {status}")

    blocks: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"JobId": job_id, "MaxResults": 1000}
        if next_token:
            kwargs["NextToken"] = next_token
        resp       = tx.get_document_text_detection(**kwargs)
        blocks    += resp.get("Blocks", [])
        next_token = resp.get("NextToken")
        if not next_token:
            break

    page_lines: dict[int, list[str]] = {}
    for b in blocks:
        if b.get("BlockType") == "LINE":
            page_lines.setdefault(int(b.get("Page", 1)), []).append(b.get("Text", ""))

    page_list = [
        {"page": p, "text": "\n".join(lines), "char_count": sum(len(l) for l in lines)}
        for p, lines in sorted(page_lines.items())
    ]
    full = "\n\n".join(p["text"] for p in page_list)
    log.info("textract.done", pages=len(page_list), chars=len(full), status=status)
    return {"text": full, "pages": page_list, "partial": status == "PARTIAL_SUCCESS"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ids_from_key(raw_key: str, event: dict[str, Any]) -> tuple[str, str]:
    """Extract (docId, tenantId) from key: tenants/<tenantId>/uploads/<docId>/<file>."""
    parts = raw_key.split("/")
    if (
        len(parts) == 5
        and parts[0] == "tenants"
        and parts[2] == "uploads"
        and _KEY_ID_RE.fullmatch(parts[1])
        and _KEY_ID_RE.fullmatch(parts[3])
    ):
        return parts[3], parts[1]
    # No fallback: an object outside the layout the API issues must never be
    # attributed to a guessed tenant.
    raise ValueError("parse: object key is not tenants/<tenantId>/uploads/<docId>/<file>")


def _detect_type(filename: str, blob: bytes) -> str:
    name = filename.lower()
    head = blob[:8]
    # Magic bytes win over extension — a mislabelled file is classified by content.
    if head.startswith(_PDF_MAGIC) or _PDF_MAGIC in blob[:1024]:
        return "pdf"
    if head.startswith(_DOCX_MAGIC):
        return "docx"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx"):
        return "docx"
    if name.endswith(".txt"):
        return "txt"
    raise UserFacingError("Unsupported file type. Upload a PDF, a Word .docx or a .txt file.")
