"""Stage 01 parse — type detection and the format-specific extractors."""
from __future__ import annotations

import pytest

from stages import parse
from shared.schema import ExtractionMethod


# ── _detect_type ──────────────────────────────────────────────────────────────


def test_detect_pdf_by_magic_even_with_wrong_extension():
    assert parse._detect_type("contract.bin", b"%PDF-1.7\n...") == "pdf"


def test_detect_docx_by_magic():
    assert parse._detect_type("x", b"PK\x03\x04rest") == "docx"


def test_detect_txt_by_extension():
    assert parse._detect_type("notes.txt", b"plain text body here") == "txt"


def test_detect_pdf_by_extension():
    assert parse._detect_type("a.PDF", b"\x00\x00not-magic") == "pdf"


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        parse._detect_type("image.png", b"\x89PNG\r\n")


# ── _parse_txt ──────────────────────────────────────────────────────────────


def test_parse_txt_utf8():
    out = parse._parse_txt("Hello — world".encode("utf-8"))
    assert out["text"] == "Hello — world"
    assert out["pages"][0]["char_count"] == len("Hello — world")


def test_parse_txt_invalid_bytes_fall_back_to_latin1():
    # An invalid UTF-8 byte must not crash the parse stage.
    out = parse._parse_txt(b"caf\xe9 terms")  # 0xe9 is invalid standalone UTF-8
    assert "caf" in out["text"]
    assert out["pages"][0]["page"] == 1


# ── _has_content (Textract gating) ──────────────────────────────────────────


def test_has_content_threshold():
    assert parse._has_content([{"char_count": 200}]) is True
    assert parse._has_content([{"char_count": 199}]) is False
    assert parse._has_content([{"char_count": 100}, {"char_count": 150}]) is True


def test_text_method_enum_exists():
    assert ExtractionMethod.TEXT.value == "text"
