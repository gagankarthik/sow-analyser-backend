"""PDF work for Govern e-signature (docs/GOVERN_ESIGN.md): page images for the
field editor and the signer, the signed values burned into the pages, and the
certificate-of-completion page.

COORDINATES. A field is placed on the page as people SEE it: ``x, y, w, h`` are
fractions of the displayed page, origin top-left. pypdfium2 renders pages with
their /Rotate applied, so the editor sees the displayed page; before drawing,
each page's rotation is moved into its content (pypdf
``transfer_rotation_to_content``) so the displayed page and the drawing space
agree, and the media box's own origin is added back.

Pure functions over bytes: no AWS here (esign.py and govern-seal own storage).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Iterable

MAX_PAGES = 100
# Page images are rendered about this wide (pixels): sharp on a laptop, small enough to send.
PAGE_IMAGE_WIDTH = 1200
MAX_RENDER_SCALE = 2.5


class PdfProblem(ValueError):
    """The upload is not a PDF Govern can sign (encrypted, broken, too long)."""


@dataclass(frozen=True)
class RenderedPage:
    png: bytes
    width: float   # points, displayed orientation
    height: float


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Checking and rendering
# ---------------------------------------------------------------------------


def check_pdf(data: bytes) -> int:
    """The page count of a signable PDF, else ``PdfProblem``."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise PdfProblem("This file is not a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfProblem("This PDF is password-protected. Upload a copy without a password.")
        count = len(reader.pages)
    except PdfProblem:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise PdfProblem("This PDF could not be read. Upload another copy.") from exc
    if count < 1:
        raise PdfProblem("This PDF has no pages.")
    if count > MAX_PAGES:
        raise PdfProblem(f"This PDF has {count} pages. The limit for signing is {MAX_PAGES}.")
    return count


def render_pages(data: bytes) -> list[RenderedPage]:
    """Every page as a PNG at about ``PAGE_IMAGE_WIDTH`` pixels wide, with its
    displayed size in points."""
    import pypdfium2 as pdfium

    check_pdf(data)
    doc = pdfium.PdfDocument(data)
    try:
        out: list[RenderedPage] = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                width, height = page.get_size()          # displayed (rotation applied)
                scale = min(MAX_RENDER_SCALE, PAGE_IMAGE_WIDTH / max(width, 1.0))
                image = page.render(scale=scale).to_pil()
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="PNG", optimize=True)
                out.append(RenderedPage(png=buf.getvalue(), width=round(float(width), 2), height=round(float(height), 2)))
            finally:
                page.close()
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Burning the signed values into the pages
# ---------------------------------------------------------------------------


def _fit_font(text: str, box_w: float, box_h: float, font: str = "Helvetica") -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    size = max(6.0, min(14.0, box_h * 0.62))
    while size > 6.0 and stringWidth(text, font, size) > box_w - 4:
        size -= 0.5
    return size


def _draw_text(c: Any, text: str, x: float, y: float, w: float, h: float) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    text = " ".join(str(text).split())
    if not text:
        return
    size = _fit_font(text, w, h)
    while text and stringWidth(text, "Helvetica", size) > w - 4:
        text = text[:-1]
    c.setFont("Helvetica", size)
    c.setFillColorRGB(0.05, 0.09, 0.16)
    c.drawString(x + 2, y + (h - size) / 2 + size * 0.22, text)


def _draw_image(c: Any, png: bytes, x: float, y: float, w: float, h: float) -> None:
    from reportlab.lib.utils import ImageReader

    img = ImageReader(io.BytesIO(png))
    iw, ih = img.getSize()
    if iw <= 0 or ih <= 0:
        return
    scale = min(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, width=dw, height=dh, mask="auto")


def _draw_check(c: Any, x: float, y: float, w: float, h: float) -> None:
    side = min(w, h) * 0.8
    ox, oy = x + (w - side) / 2, y + (h - side) / 2
    c.setStrokeColorRGB(0.05, 0.09, 0.16)
    c.setLineWidth(max(1.0, side / 10))
    path = c.beginPath()
    path.moveTo(ox + side * 0.15, oy + side * 0.5)
    path.lineTo(ox + side * 0.42, oy + side * 0.2)
    path.lineTo(ox + side * 0.88, oy + side * 0.82)
    c.drawPath(path, stroke=1, fill=0)


def flatten(source: bytes, fields: Iterable[dict[str, Any]], images: dict[str, bytes]) -> bytes:
    """The source PDF with every filled field drawn onto its page.

    ``images`` maps a signature / initials field id to its PNG. Text-like
    fields draw their ``value``; a checkbox draws a tick when its value is
    "true". Empty optional fields draw nothing.
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    reader = PdfReader(io.BytesIO(source))
    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in fields:
        by_page.setdefault(int(f["page"]), []).append(f)

    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        placed = by_page.get(index) or []
        if placed:
            if page.rotation:
                page.transfer_rotation_to_content()
            box = page.mediabox
            left, bottom = float(box.left), float(box.bottom)
            width, height = float(box.width), float(box.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(width, height))
            for f in placed:
                fx, fy = float(f["x"]) * width, float(f["y"]) * height
                fw, fh = float(f["w"]) * width, float(f["h"]) * height
                # Top-left fractions → the overlay's bottom-left origin.
                x, y = fx, height - fy - fh
                kind = f.get("type")
                if kind in ("signature", "initials"):
                    png = images.get(str(f["id"]))
                    if png:
                        _draw_image(c, png, x, y, fw, fh)
                elif kind == "checkbox":
                    if str(f.get("value") or "").lower() == "true":
                        _draw_check(c, x, y, fw, fh)
                elif f.get("value"):
                    _draw_text(c, str(f["value"]), x, y, fw, fh)
            c.showPage()
            c.save()
            overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
            if left or bottom:
                from pypdf import Transformation

                page.merge_transformed_page(overlay, Transformation().translate(left, bottom))
            else:
                page.merge_page(overlay)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Certificate of completion
# ---------------------------------------------------------------------------


def _when(value: Any) -> str:
    """``2026-10-23T14:25:09Z`` → ``October 23, 2026, 2:25:09 PM UTC`` (unparsable values as given)."""
    from datetime import datetime, timezone

    text = str(value or "")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return text
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M:%S %p')} UTC"


def certificate(*, envelope: dict[str, Any], organization: str | None, source_sha256: str,
                signed_sha256: str, signatures: dict[str, bytes], completed_at: str) -> bytes:
    """The certificate-of-completion page(s) as a PDF.

    ``signed_sha256`` is the hash of the flattened document before this page
    is appended and the whole is sealed; ``signatures`` maps recipient id →
    the signature image they adopted.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    width, height = letter
    margin = 54.0
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("Certificate of completion")
    y = height - margin

    def ink(r: float = 0.05, g: float = 0.09, b: float = 0.16) -> None:
        c.setFillColorRGB(r, g, b)

    def line(text: str, size: float = 9.5, bold: bool = False, gap: float = 13.5, muted: bool = False) -> None:
        nonlocal y
        if y < margin + 40:
            new_page()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        ink(0.28, 0.33, 0.41) if muted else ink()
        c.drawString(margin, y, text[:150])
        y -= gap

    def new_page() -> None:
        nonlocal y
        c.showPage()
        y = height - margin

    def rule(gap: float = 14) -> None:
        nonlocal y
        c.setStrokeColorRGB(0.85, 0.88, 0.92)
        c.setLineWidth(0.6)
        c.line(margin, y + 6, width - margin, y + 6)
        y -= gap

    # Header: Blue-IQ's own mark (a navy square with the wordmark), then the title.
    c.setFillColorRGB(0.12, 0.36, 0.9)
    c.rect(margin, y - 4, 14, 14, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 12)
    ink()
    c.drawString(margin + 20, y - 1, "Blue-IQ Govern")
    y -= 34
    line("Certificate of completion", size=18, bold=True, gap=22)
    line("This document was signed electronically with Blue-IQ Govern e-signature.", muted=True, gap=24)

    line("Document", bold=True, size=10.5, gap=16)
    line(f"Name: {envelope.get('name') or 'Agreement'}")
    if organization:
        line(f"Sent by: {organization}")
    line(f"Envelope ID: {envelope.get('envelopeId')}")
    line(f"Pages: {envelope.get('source', {}).get('pageCount')}  ·  Completed: {_when(completed_at)}")
    line(f"Original document SHA-256: {source_sha256}", size=8.5)
    line(f"Signed document SHA-256 (before this certificate): {signed_sha256}", size=8.5, gap=22)
    rule()

    line("Signers", bold=True, size=10.5, gap=18)
    for r in envelope.get("recipients") or []:
        if r.get("role") != "signer":
            continue
        if y < margin + 150:
            new_page()
        top = y
        line(f"{r.get('name') or ''}  <{r.get('email') or ''}>", bold=True, size=10, gap=15)
        events = [("Sent", r.get("sentAt")), ("Viewed", r.get("viewedAt")),
                  ("Agreed to electronic records", r.get("consentAt")), ("Signed", r.get("signedAt"))]
        for label, at in events:
            if at:
                line(f"{label}: {_when(at)}", size=9, gap=12.5)
        if r.get("ip"):
            line(f"IP address: {r.get('ip')}", size=9, gap=12.5)
        if r.get("userAgent"):
            line(f"Browser: {str(r.get('userAgent'))[:110]}", size=8, gap=12.5, muted=True)
        png = signatures.get(str(r.get("id")))
        if png:
            _draw_image(c, png, width - margin - 170, top - 62, 170, 56)
            c.setStrokeColorRGB(0.85, 0.88, 0.92)
            c.line(width - margin - 170, top - 64, width - margin, top - 64)
        y = min(y, top - 76) - 6
        rule(gap=16)

    copies = [r for r in envelope.get("recipients") or [] if r.get("role") == "cc"]
    if copies:
        line("Copies sent to", bold=True, size=10.5, gap=16)
        for r in copies:
            line(f"{r.get('name') or ''}  <{r.get('email') or ''}>", size=9.5)
        y -= 8

    line("How to check this document", bold=True, size=10.5, gap=16)
    line("The whole file, including this page, is sealed with a digital signature. Any change to it after",
         size=9, muted=True, gap=12)
    line("signing breaks the seal, and PDF readers then report the signature as invalid.", size=9, muted=True)
    c.showPage()
    c.save()
    return buf.getvalue()


def append(document: bytes, extra: bytes) -> bytes:
    """``document`` followed by every page of ``extra``."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in (document, extra):
        for page in PdfReader(io.BytesIO(part)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
