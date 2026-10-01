"""Parsing — nothing in the file is dropped on the way to text.

DOCX files are built in memory with the standard library; PDF pages are small
fakes of the pdfplumber page API; Textract is a fake client. No file on disk,
no network.
"""
from __future__ import annotations

import io
import sys
import types
import zipfile

import pytest

from shared.docx_text import extract_docx
from shared.errors import UserFacingError
from stages import parse

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
NS = (W + ' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
      ' xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
      ' xmlns:v="urn:schemas-microsoft-com:vml"')


def p(text: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    ppr = ""
    if style or num:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if num:
            ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr += "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def docx(body: str, **parts: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}</w:body></w:document>")
        for name, xml in parts.items():
            z.writestr(f"word/{name}.xml", xml)
    return buf.getvalue()


NUMBERING = f"""<w:numbering {W}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>
    <w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%3)"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""


# ── DOCX ─────────────────────────────────────────────────────────────────────


def test_docx_tables_stay_where_they_are_in_the_document():
    """python-docx returned every table AFTER all paragraphs, so a fee table
    ended up below the signature block, detached from its clause."""
    table = ("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Milestone</w:t></w:r></w:p></w:tc>"
             "<w:tc><w:p><w:r><w:t>Amount</w:t></w:r></w:p></w:tc></w:tr>"
             "<w:tr><w:tc><w:p><w:r><w:t>Design</w:t></w:r></w:p></w:tc>"
             "<w:tc><w:p><w:r><w:t>$10,000</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    out = extract_docx(docx(p("Fees are as follows:") + table + p("Signed by both parties.")))
    assert out["text"].split("\n") == [
        "Fees are as follows:", "Milestone | Amount", "Design | $10,000", "Signed by both parties."]
    assert out["stats"]["tables"] == 1


def test_docx_merged_and_nested_table_cells():
    merged = ("<w:tbl><w:tr><w:tc><w:tcPr><w:vMerge w:val=\"restart\"/></w:tcPr><w:p><w:r><w:t>Phase 1</w:t></w:r></w:p></w:tc>"
              "<w:tc><w:p><w:r><w:t>Design</w:t></w:r></w:p></w:tc></w:tr>"
              "<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>"
              "<w:tc><w:p><w:r><w:t>Build</w:t></w:r></w:p>"
              "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>inner</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>")
    lines = extract_docx(docx(merged))["text"].split("\n")
    assert lines == ["Phase 1 | Design", " | Build inner"]            # merged cell once; nested text kept


def test_docx_automatic_numbering_is_restored():
    """Word stores "1.", "1.1", "(a)" as list formatting, not as text — without it
    the clause numbers vanish and the document cannot be segmented."""
    body = (p("Definitions", num=(1, 0)) + p("Terms are defined here.", num=(1, 1)) +
            p("first item", num=(1, 2)) + p("second item", num=(1, 2)) +
            p("More terms.", num=(1, 1)) + p("Fees", num=(1, 0)) + p("Fees are due.", num=(1, 1)))
    out = extract_docx(docx(body, numbering=NUMBERING))
    assert out["text"].split("\n") == [
        "1. Definitions", "1.1 Terms are defined here.", "(a) first item", "(b) second item",
        "1.2 More terms.", "2. Fees", "2.1 Fees are due."]
    assert out["stats"]["numberedParagraphs"] == 7


def test_docx_numbering_inherited_from_a_heading_style():
    styles = (f'<w:styles {W}><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
              '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style></w:styles>')
    out = extract_docx(docx(p("Scope", style="Heading1") + p("Body.") + p("Fees", style="Heading1"),
                            numbering=NUMBERING, styles=styles))
    assert out["text"].split("\n") == ["1. Scope", "Body.", "2. Fees"]
    assert out["headings"] == ["1. Scope", "2. Fees"]


def test_docx_content_controls_tracked_changes_and_text_boxes():
    body = (
        # a fill-in field: the party name lives in a content control
        "<w:p><w:r><w:t xml:space=\"preserve\">Client: </w:t></w:r>"
        "<w:sdt><w:sdtPr/><w:sdtContent><w:r><w:t>Acme Holdings Ltd</w:t></w:r></w:sdtContent></w:sdt></w:p>"
        # a tracked insertion is part of the agreement; a tracked deletion is not
        "<w:p><w:r><w:t xml:space=\"preserve\">The fee is </w:t></w:r>"
        "<w:del><w:r><w:delText>$5,000</w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>$7,500</w:t></w:r></w:ins></w:p>"
        # a block-level content control holding a whole paragraph
        "<w:sdt><w:sdtContent>" + p("Effective Date: 1 March 2026") + "</w:sdtContent></w:sdt>"
        # a text box: Choice and Fallback hold the same text — it must appear once
        "<w:p><w:r><mc:AlternateContent><mc:Choice><wps:wsp><wps:txbx><w:txbxContent>"
        + p("CONFIDENTIAL DRAFT") +
        "</w:txbxContent></wps:txbx></wps:wsp></mc:Choice><mc:Fallback><v:textbox><w:txbxContent>"
        + p("CONFIDENTIAL DRAFT") + "</w:txbxContent></v:textbox></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )
    out = extract_docx(docx(body))
    text = out["text"]
    assert "Client: Acme Holdings Ltd" in text
    assert "The fee is $7,500" in text and "$5,000" not in text
    assert "Effective Date: 1 March 2026" in text
    assert text.count("CONFIDENTIAL DRAFT") == 1
    assert out["stats"]["trackedInsertions"] == 1 and out["stats"]["trackedDeletionsSkipped"] == 1
    assert out["stats"]["contentControls"] == 2 and out["stats"]["textBoxes"] == 1


def test_docx_headers_footers_and_footnotes():
    body = ("<w:p><w:r><w:t>Liability is capped</w:t></w:r>"
            "<w:r><w:footnoteReference w:id=\"2\"/></w:r><w:r><w:t>.</w:t></w:r></w:p>")
    footnotes = (f'<w:footnotes {W}><w:footnote w:type="separator" w:id="0"><w:p/></w:footnote>'
                 f'<w:footnote w:id="2">{p("The cap is USD 1,000,000.")}</w:footnote></w:footnotes>')
    header = f'<w:hdr {W}>{p("SOW-2024-0042")}</w:hdr>'
    footer = f'<w:ftr {W}>{p("Page 1 of 3")}{p("Acme Confidential")}</w:ftr>'
    out = extract_docx(docx(body, footnotes=footnotes, header1=header, footer1=footer))
    lines = out["text"].split("\n")
    assert lines[0] == "SOW-2024-0042"                              # the contract number in the header
    assert "Liability is capped [footnote 1]." in lines
    assert "[footnote 1] The cap is USD 1,000,000." in lines
    assert "Acme Confidential" in lines and "Page 1 of 3" not in lines     # page numbers are noise


def test_docx_that_is_not_a_word_file_gives_a_clear_message():
    with pytest.raises(UserFacingError, match="could not be opened"):
        parse._parse_docx(b"PK\x03\x04 this is not a real zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    with pytest.raises(UserFacingError):
        parse._parse_docx(buf.getvalue())


# ── TXT ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("data,encoding", [
    ("Fee: €1,200 — “net 30”".encode("utf-8"), "utf-8"),
    (b"\xef\xbb\xbf" + "Fee: €1,200 — “net 30”".encode("utf-8"), "utf-8-sig"),
    ("Fee: €1,200 — “net 30”".encode("utf-16"), "utf-16"),                  # Notepad "Unicode"
    ("Fee: 1,200 net 30 days from invoice".encode("utf-16-le"), "utf-16-le"),   # no BOM
    ("Fee: €1,200 — “net 30”".encode("cp1252"), "cp1252"),
])
def test_txt_encodings_keep_every_character(data, encoding):
    out = parse._parse_txt(data)
    assert out["encoding"] == encoding
    assert "\x00" not in out["text"]
    assert "1,200" in out["text"] and "net 30" in out["text"]
    if encoding != "utf-16-le":
        assert "€" in out["text"]                    # latin-1 fallback used to lose this


def test_txt_line_endings_and_control_characters_are_cleaned():
    out = parse._parse_txt(b"1. Scope\r\nBuild\x00 it.\r\n\x0c2. Fees\r\n")
    assert out["text"] == "1. Scope\nBuild it.\n2. Fees"


# ── PDF ──────────────────────────────────────────────────────────────────────


class _Page:
    def __init__(self, text="", words=None, images=None, chars=None, width=600.0, height=800.0, tables=None,
                 crops=None, error=False):
        self._text, self._words, self.images, self.chars = text, words or [], images or [], chars or []
        self.width, self.height, self._tables, self._crops, self._error = width, height, tables or [], crops or {}, error

    def extract_text(self):
        if self._error:
            raise ValueError("broken content stream")
        return self._text

    def extract_words(self):
        return self._words

    def find_tables(self):
        return self._tables

    def crop(self, box):
        return _Page(text=self._crops.get(tuple(round(b) for b in box), ""))


def _fake_pdfplumber(monkeypatch, pages):
    class _Pdf:
        def __init__(self):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _f: _Pdf()))


class _Textract:
    def __init__(self, pages: dict[int, list[str]], status="SUCCEEDED"):
        self.pages, self.status, self.started = pages, status, 0

    def start_document_text_detection(self, **_k):
        self.started += 1
        return {"JobId": "job"}

    def get_document_text_detection(self, **kw):
        if kw.get("MaxResults") == 1:
            return {"JobStatus": self.status}
        blocks = [{"BlockType": "LINE", "Page": n, "Text": line} for n, lines in self.pages.items() for line in lines]
        return {"JobStatus": self.status, "Blocks": blocks}


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(parse.time, "sleep", lambda _s: None)


def _column_words():
    words = []
    for row in range(12):
        top = 150 + row * 20
        for col, x0 in ((0, 40), (1, 320)):
            for i in range(5):
                words.append({"text": f"w{col}{row}{i}", "x0": x0 + i * 48, "x1": x0 + i * 48 + 40,
                              "top": top, "bottom": top + 10})
    # a centred title across the gutter, in the top margin
    words.append({"text": "TERMS", "x0": 250, "x1": 350, "top": 40, "bottom": 55})
    return words


def test_two_column_page_is_read_column_by_column():
    words = _column_words()
    gutter, top_cut, bottom_cut = parse.detect_columns(words, 600.0, 800.0)
    assert 275 <= gutter <= 320 and top_cut == 55 and bottom_cut == 800.0
    page = _Page(text="interleaved garbage", words=words, crops={
        (0, 0, 600, 55): "TERMS", (0, 55, round(gutter), 800): "left column text",
        (round(gutter), 55, 600, 800): "right column text"})
    out = parse._extract_pdf_page(page)
    assert out["text"] == "TERMS\nleft column text\nright column text" and out["columns"] is True


def test_label_value_tables_and_ordinary_pages_are_not_treated_as_columns():
    # a fee table: short labels on the left, amounts on the right
    table = []
    for row in range(14):
        top = 150 + row * 20
        table.append({"text": f"Item{row}", "x0": 40, "x1": 90, "top": top, "bottom": top + 10})
        for i in range(5):
            table.append({"text": "x", "x0": 330 + i * 40, "x1": 360 + i * 40, "top": top, "bottom": top + 10})
    assert parse.detect_columns(table * 2, 600.0, 800.0) is None
    full_width = [{"text": "w", "x0": 40 + i * 50, "x1": 85 + i * 50, "top": 100 + r * 20, "bottom": 110 + r * 20}
                  for r in range(12) for i in range(10)]
    assert parse.detect_columns(full_width, 600.0, 800.0) is None
    # a page pdfplumber finds a ruled table on is never re-ordered
    page = _Page(text="row one\nrow two", words=_column_words(), tables=[object()])
    assert parse._extract_pdf_page(page)["columns"] is False


def test_repeating_headers_footers_and_page_numbers_are_removed_once_kept():
    pages = [{"page": i, "text": f"SOW-2024-0042 | Acme Confidential\nMilestone {i} Design ${i},000\n"
                                 f"More text {i}.\nPage {i} of 4", "char_count": 0} for i in range(1, 5)]
    removed = parse.strip_repeating_lines(pages)
    assert removed == 7                                   # 3 headers (first kept) + 4 page numbers
    assert pages[0]["text"].startswith("SOW-2024-0042")   # the contract number survives once
    assert all("Page " not in p["text"] for p in pages)
    assert all("SOW-2024-0042" not in p["text"] for p in pages[1:])
    # A fee-table row at the top of each page differs only in its numbers. It is
    # content, not a header: every one of them is kept.
    assert all(f"Milestone {p['page']} Design ${p['page']},000" in p["text"] for p in pages)


def test_a_header_carrying_the_page_number_is_recognised():
    pages = [{"page": i, "text": f"Acme Confidential {i}\nBody of page {i} with different words {'abcd'[i]}.\n"
                                 f"closing {'wxyz'[i]}", "char_count": 0} for i in range(4)]
    assert parse.strip_repeating_lines(pages) == 3
    assert pages[0]["text"].startswith("Acme Confidential 0") and "Acme" not in pages[1]["text"]


def test_lines_that_do_not_repeat_are_never_removed():
    pages = [{"page": i, "text": f"Unique opening line {'abc'[i]}\nBody {'def'[i]}.\nUnique closing line {'xyz'[i]}",
              "char_count": 0} for i in range(3)]
    before = [p["text"] for p in pages]
    assert parse.strip_repeating_lines(pages) == 0 and [p["text"] for p in pages] == before
    assert parse.strip_repeating_lines(pages[:2]) == 0                # too few pages to judge


def test_mixed_pdf_scanned_pages_are_recovered_with_ocr(monkeypatch, no_wait):
    """A text PDF with a scanned page appended (a signed signature page, an
    exhibit) used to lose that page silently."""
    full_image = [{"x0": 0, "x1": 600, "top": 0, "bottom": 800}]
    pages = [_Page(text="1. Scope\n" + "The supplier shall deliver the services. " * 10),
             _Page(text="", images=full_image),
             _Page(text="2. Fees\n" + "The fee is $5,000 payable net 30. " * 10)]
    _fake_pdfplumber(monkeypatch, pages)
    textract = _Textract({1: ["ignored"], 2: ["SIGNED by Jane Doe", "Date: 1 March 2026"], 3: ["ignored"]})
    monkeypatch.setattr(parse, "textract_client", lambda: textract)
    warnings, stats = [], {}
    extracted, method = parse._parse_pdf(b"%PDF-", "raw", "key", warnings, stats)
    assert method.value == "pdfplumber" and textract.started == 1
    assert "SIGNED by Jane Doe" in extracted["pages"][1]["text"]
    assert extracted["pages"][0]["text"].startswith("1. Scope")       # native text kept for the rest
    assert stats["ocrPages"] == [2] and warnings == []


def test_unreadable_scanned_page_is_reported_not_silently_dropped(monkeypatch, no_wait):
    pages = [_Page(text="Body text. " * 40), _Page(text="", images=[{"x0": 0, "x1": 600, "top": 0, "bottom": 800}])]
    _fake_pdfplumber(monkeypatch, pages)

    def failing():
        raise RuntimeError("textract unavailable")
    monkeypatch.setattr(parse, "textract_client", failing)
    warnings, stats = [], {}
    extracted, _ = parse._parse_pdf(b"%PDF-", "raw", "key", warnings, stats)
    assert len(extracted["pages"]) == 2                               # the document still parses
    assert warnings and "page(s) appear to be scanned" in warnings[0] and "2" in warnings[0]


def test_a_small_logo_does_not_trigger_ocr_and_rotated_text_does(monkeypatch):
    logo = _Page(text="", images=[{"x0": 10, "x1": 110, "top": 10, "bottom": 60}])
    assert parse._extract_pdf_page(logo)["needs_ocr"] is False
    rotated = _Page(text="e h T\nr e i l p p u S " * 5, chars=[{"upright": False}] * 60)
    assert parse._extract_pdf_page(rotated)["needs_ocr"] is True


def test_one_broken_page_does_not_discard_the_others(monkeypatch):
    _fake_pdfplumber(monkeypatch, [_Page(text="Good page one. " * 20), _Page(error=True),
                                   _Page(text="Good page three. " * 20)])
    out = parse._try_pdfplumber(b"%PDF-")
    assert [bool(p["text"]) for p in out["pages"]] == [True, False, True]
    assert out["pages"][1]["needs_ocr"] is True


def test_fully_scanned_pdf_goes_to_ocr_and_partial_success_is_kept(monkeypatch, no_wait):
    _fake_pdfplumber(monkeypatch, [_Page(text=""), _Page(text="")])
    textract = _Textract({1: ["Page one text"], 2: ["Page two text"]}, status="PARTIAL_SUCCESS")
    monkeypatch.setattr(parse, "textract_client", lambda: textract)
    warnings, stats = [], {}
    extracted, method = parse._parse_pdf(b"%PDF-", "raw", "key", warnings, stats)
    assert method.value == "textract" and len(extracted["pages"]) == 2
    assert warnings == ["Text recognition could not read every page of this scan."]


# ── The stage as a whole ─────────────────────────────────────────────────────

DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def stage(monkeypatch):
    state = {"blob": b"", "saved": None}
    key = f"tenants/acme/uploads/{DOC}/c.txt"
    monkeypatch.setattr(parse, "get_doc_meta", lambda _id: {"docId": DOC, "tenantId": "acme", "rawKey": key})
    monkeypatch.setattr(parse, "head_object", lambda b, k: {"ContentLength": len(state["blob"])})
    monkeypatch.setattr(parse, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(parse, "get_object", lambda b, k: state["blob"])
    monkeypatch.setattr(parse, "put_json", lambda b, k, data: state.update(saved=data))
    state["event"] = lambda: {"rawBucket": "raw", "rawKey": key, "processedBucket": "processed"}
    return state


def test_no_page_or_character_cap(stage):
    stage["blob"] = ("Clause text that goes on. " * 40_000).encode()        # ~1 MB of text
    out = parse.run(stage["event"]())
    assert len(out["parsed"]["text"]) == len(stage["blob"].decode().strip())
    assert out["parsed"]["stats"]["chars"] == len(out["parsed"]["text"])


def test_file_with_no_text_fails_with_a_message_for_the_user(stage):
    stage["blob"] = b"   \n\n  "
    with pytest.raises(UserFacingError, match="No readable text"):
        parse.run(stage["event"]())
    assert stage["saved"] is None


def test_text_is_exactly_the_pages_joined_so_offsets_map_to_pages(stage, monkeypatch, no_wait):
    stage["blob"] = b"%PDF-1.7 fake"
    _fake_pdfplumber(monkeypatch, [_Page(text="Page one text " * 20), _Page(text="Page two text " * 20)])
    out = parse.run(stage["event"]())["parsed"]
    assert out["text"] == "\n\n".join(p["text"] for p in out["pages"])
    assert out["stats"]["pages"] == 2 and out["extraction_method"] == "pdfplumber"
