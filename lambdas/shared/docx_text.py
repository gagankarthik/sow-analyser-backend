"""DOCX → text, reading the WordprocessingML directly (standard library only).

``python-docx``'s ``Document.paragraphs`` / ``.tables`` API loses contract
content in ways that are easy to miss:

* tables are returned separately from paragraphs, so a fee table lands at the
  END of the text, detached from the clause it belongs to;
* text inside content controls (``w:sdt`` — the fill-in fields of every
  templated contract: party names, dates, amounts), tracked insertions
  (``w:ins``) and text boxes is skipped entirely;
* automatic numbering ("1.", "1.1", "(a)") is not part of the paragraph text, so
  the clause numbers vanish;
* headers, footers, footnotes and endnotes are not read;
* a merged table cell is returned once per column it spans.

This walker visits the body in document order and handles all of the above.
Tracked DELETIONS (``w:del``, ``w:moveFrom``) are left out — they are no longer
part of the agreement text — and review comments are not contract text.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W = f"{{{W}}}"
_MC = f"{{{MC}}}"

_SKIP = {f"{_W}del", f"{_W}moveFrom", f"{_W}instrText", f"{_W}delText", f"{_W}delInstrText",
         f"{_W}pPr", f"{_W}rPr", f"{_W}tblPr", f"{_W}tcPr", f"{_W}trPr", f"{_W}sectPr",
         f"{_W}sdtPr", f"{_W}sdtEndPr", f"{_W}tblGrid", f"{_W}commentReference",
         f"{_W}annotationRef", f"{_W}footnoteRef", f"{_W}endnoteRef"}
_PAGE_NUMBER_RE = re.compile(r"^\W*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\W*$", re.IGNORECASE)
# zip-bomb guard: the uncompressed XML we are willing to read
_MAX_PART_BYTES = 80 * 1024 * 1024


def _attr(el: ET.Element | None, name: str) -> str | None:
    return el.get(f"{_W}{name}") if el is not None else None


def _read(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_PART_BYTES:
        raise ValueError(f"DOCX part {name} is too large to read safely")
    try:
        return ET.fromstring(z.read(name))
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Automatic numbering
# ---------------------------------------------------------------------------


def _roman(n: int) -> str:
    out = ""
    for val, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                     (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            out += sym
            n -= val
    return out


def _letters(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


def _fmt(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter",):
        return _letters(n)
    if fmt == "upperLetter":
        return _letters(n).upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt in ("none",):
        return ""
    return str(n)


class _Numbering:
    """Replays Word's list counters so each numbered paragraph gets its label."""

    def __init__(self, root: ET.Element | None, styles: ET.Element | None):
        self.abstract: dict[str, dict[int, dict[str, Any]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.style_num: dict[str, tuple[str, int]] = {}
        self.counters: dict[str, list[int | None]] = {}
        self.seen_nums: set[str] = set()
        if root is not None:
            for an in root.findall(f"{_W}abstractNum"):
                levels: dict[int, dict[str, Any]] = {}
                for lvl in an.findall(f"{_W}lvl"):
                    try:
                        ilvl = int(_attr(lvl, "ilvl") or 0)
                    except ValueError:
                        continue
                    levels[ilvl] = {
                        "start": int(_attr(lvl.find(f"{_W}start"), "val") or 1),
                        "fmt": _attr(lvl.find(f"{_W}numFmt"), "val") or "decimal",
                        "text": _attr(lvl.find(f"{_W}lvlText"), "val") or "",
                        "legal": lvl.find(f"{_W}isLgl") is not None,
                        "style": _attr(lvl.find(f"{_W}pStyle"), "val"),
                    }
                self.abstract[_attr(an, "abstractNumId") or ""] = levels
            for num in root.findall(f"{_W}num"):
                overrides: dict[int, int] = {}
                for ov in num.findall(f"{_W}lvlOverride"):
                    start = ov.find(f"{_W}startOverride")
                    if start is not None:
                        try:
                            overrides[int(_attr(ov, "ilvl") or 0)] = int(_attr(start, "val") or 1)
                        except ValueError:
                            pass
                self.nums[_attr(num, "numId") or ""] = (
                    _attr(num.find(f"{_W}abstractNumId"), "val") or "", overrides)
        if styles is not None:
            raw: dict[str, tuple[str | None, int | None, str | None]] = {}
            for st in styles.findall(f"{_W}style"):
                sid = _attr(st, "styleId") or ""
                num_pr = st.find(f"{_W}pPr/{_W}numPr")
                num_id = _attr(num_pr.find(f"{_W}numId"), "val") if num_pr is not None else None
                ilvl_raw = _attr(num_pr.find(f"{_W}ilvl"), "val") if num_pr is not None else None
                raw[sid] = (num_id, int(ilvl_raw) if ilvl_raw and ilvl_raw.isdigit() else None,
                            _attr(st.find(f"{_W}basedOn"), "val"))
            for sid in raw:
                num_id, ilvl, cur, hops = None, None, sid, 0
                while cur in raw and hops < 8:
                    n, l, based = raw[cur]
                    num_id = num_id or n
                    ilvl = ilvl if ilvl is not None else l
                    cur, hops = based or "", hops + 1
                if num_id and num_id != "0":
                    self.style_num[sid] = (num_id, ilvl if ilvl is not None else self._level_for_style(num_id, sid))

    def _level_for_style(self, num_id: str, style_id: str) -> int:
        levels = self.abstract.get(self.nums.get(num_id, ("", {}))[0], {})
        for ilvl, spec in levels.items():
            if spec.get("style") == style_id:
                return ilvl
        return 0

    def label(self, p: ET.Element) -> str:
        ppr = p.find(f"{_W}pPr")
        num_id: str | None = None
        ilvl = 0
        if ppr is not None:
            num_pr = ppr.find(f"{_W}numPr")
            if num_pr is not None:
                num_id = _attr(num_pr.find(f"{_W}numId"), "val")
                try:
                    ilvl = int(_attr(num_pr.find(f"{_W}ilvl"), "val") or 0)
                except ValueError:
                    ilvl = 0
            if num_id is None:
                style = _attr(ppr.find(f"{_W}pStyle"), "val")
                if style in self.style_num:
                    num_id, ilvl = self.style_num[style]
        if not num_id or num_id == "0" or num_id not in self.nums:
            return ""
        abs_id, overrides = self.nums[num_id]
        levels = self.abstract.get(abs_id)
        if not levels or ilvl not in levels:
            return ""
        counters = self.counters.setdefault(abs_id, [None] * 9)
        if num_id not in self.seen_nums:
            self.seen_nums.add(num_id)
            for lvl_i, start in overrides.items():
                if 0 <= lvl_i < 9:
                    counters[lvl_i] = start - 1
                    for deeper in range(lvl_i + 1, 9):
                        counters[deeper] = None
        ilvl = min(ilvl, 8)
        for lvl_i in range(ilvl):
            if counters[lvl_i] is None:
                counters[lvl_i] = levels.get(lvl_i, {}).get("start", 1)
        cur = counters[ilvl]
        counters[ilvl] = levels[ilvl]["start"] if cur is None else cur + 1
        for deeper in range(ilvl + 1, 9):
            counters[deeper] = None

        spec = levels[ilvl]
        if spec["fmt"] == "bullet":
            return "•"
        if spec["fmt"] == "none" and "%" not in spec["text"]:
            return ""

        def sub(m: re.Match[str]) -> str:
            idx = int(m.group(1)) - 1
            value = counters[idx] if 0 <= idx < 9 else None
            if value is None:
                return ""
            fmt = "decimal" if (spec["legal"] and idx != ilvl) else levels.get(idx, {}).get("fmt", "decimal")
            return _fmt(value, fmt)

        return re.sub(r"%(\d)", sub, spec["text"]).strip()


# ---------------------------------------------------------------------------
# Body walk
# ---------------------------------------------------------------------------


class _Walker:
    def __init__(self, numbering: _Numbering, heading_styles: set[str]):
        self.numbering = numbering
        self.heading_styles = heading_styles
        self.blocks: list[str] = []
        self.headings: list[str] = []
        self.note_refs: list[tuple[str, str]] = []   # (kind, id) in order of first reference
        self.stats = {"tables": 0, "textBoxes": 0, "contentControls": 0, "trackedInsertions": 0,
                      "trackedDeletionsSkipped": 0, "numberedParagraphs": 0}

    # -- inline -------------------------------------------------------------
    def _inline(self, el: ET.Element, out: list[str], nested: list[ET.Element]) -> None:
        for child in el:
            tag = child.tag
            if tag in _SKIP:
                if tag in (f"{_W}del", f"{_W}moveFrom"):
                    self.stats["trackedDeletionsSkipped"] += 1
                continue
            if tag == f"{_W}t":
                out.append(child.text or "")
            elif tag == f"{_W}tab":
                out.append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                out.append("\n")
            elif tag == f"{_W}noBreakHyphen":
                out.append("-")
            elif tag == f"{_W}footnoteReference":
                self._note("footnote", _attr(child, "id"), out)
            elif tag == f"{_W}endnoteReference":
                self._note("endnote", _attr(child, "id"), out)
            elif tag == f"{_W}txbxContent":
                self.stats["textBoxes"] += 1
                nested.append(child)
            elif tag == f"{_MC}AlternateContent":
                # Choice and Fallback hold the SAME content in two formats.
                branch = child.find(f"{_MC}Choice")
                if branch is None:
                    branch = child.find(f"{_MC}Fallback")
                if branch is not None:
                    self._inline(branch, out, nested)
            elif tag == f"{_W}p" or tag == f"{_W}tbl":
                continue  # block content is handled by the block walk
            else:
                if tag == f"{_W}ins":
                    self.stats["trackedInsertions"] += 1
                elif tag == f"{_W}sdt":
                    self.stats["contentControls"] += 1
                self._inline(child, out, nested)

    def _note(self, kind: str, note_id: str | None, out: list[str]) -> None:
        if not note_id:
            return
        key = (kind, note_id)
        if key not in self.note_refs:
            self.note_refs.append(key)
        out.append(f" [{kind} {self.note_refs.index(key) + 1}]")

    def paragraph_text(self, p: ET.Element) -> tuple[str, list[ET.Element]]:
        out: list[str] = []
        nested: list[ET.Element] = []
        self._inline(p, out, nested)
        text = "".join(out)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        return re.sub(r"[ \t]{2,}", lambda m: "\t" if "\t" in m.group(0) else " ", text).strip(), nested

    # -- blocks -------------------------------------------------------------
    def walk(self, container: ET.Element, sink: list[str] | None = None) -> None:
        sink = self.blocks if sink is None else sink
        for child in container:
            tag = child.tag
            if tag in _SKIP:
                continue
            if tag == f"{_W}p":
                self._paragraph(child, sink)
            elif tag == f"{_W}tbl":
                self.stats["tables"] += 1
                sink.extend(self._table(child))
            elif tag == f"{_MC}AlternateContent":
                branch = child.find(f"{_MC}Choice")
                if branch is None:
                    branch = child.find(f"{_MC}Fallback")
                if branch is not None:
                    self.walk(branch, sink)
            else:
                # w:sdt / w:sdtContent / w:ins / w:moveTo / w:customXml / w:smartTag …
                if tag == f"{_W}sdt":
                    self.stats["contentControls"] += 1
                self.walk(child, sink)

    def _paragraph(self, p: ET.Element, sink: list[str]) -> None:
        text, nested = self.paragraph_text(p)
        label = self.numbering.label(p)
        if text:
            if label:
                self.stats["numberedParagraphs"] += 1
                text = f"{label} {text}"
            sink.append(text)
            style = _attr(p.find(f"{_W}pPr/{_W}pStyle"), "val")
            outline = p.find(f"{_W}pPr/{_W}outlineLvl")
            if (style in self.heading_styles or outline is not None) and len(text) <= 160 and "\n" not in text:
                self.headings.append(text)
        for box in nested:
            self.walk(box, sink)

    def _table(self, tbl: ET.Element) -> list[str]:
        rows: list[str] = []
        for tr in tbl.iter(f"{_W}tr"):
            if self._owner_table(tbl, tr) is not tbl:
                continue                       # row of a nested table: rendered by its cell
            if tr.find(f"{_W}trPr/{_W}del") is not None:
                self.stats["trackedDeletionsSkipped"] += 1
                continue
            cells: list[str] = []
            for tc in self._cells(tr):
                v_merge = tc.find(f"{_W}tcPr/{_W}vMerge")
                if v_merge is not None and (_attr(v_merge, "val") or "continue") != "restart":
                    cells.append("")           # continuation of a vertically merged cell
                    continue
                parts: list[str] = []
                self.walk(tc, parts)
                cells.append(" ".join(part.replace("\n", " ") for part in parts).strip())
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                rows.append(" | ".join(cells))
        return rows

    @staticmethod
    def _cells(tr: ET.Element) -> list[ET.Element]:
        cells: list[ET.Element] = []
        for child in tr:
            if child.tag == f"{_W}tc":
                cells.append(child)
            elif child.tag in (f"{_W}sdt", f"{_W}ins", f"{_W}customXml"):
                cells.extend(child.iter(f"{_W}tc"))
        return cells

    def _owner_table(self, root_tbl: ET.Element, tr: ET.Element) -> ET.Element | None:
        """The table a row directly belongs to (ElementTree has no parent links)."""
        cache = getattr(self, "_parents", None)
        if cache is None or cache[0] is not root_tbl:
            parents = {child: parent for parent in root_tbl.iter() for child in parent}
            cache = (root_tbl, parents)
            self._parents = cache
        node: ET.Element | None = tr
        while node is not None:
            node = cache[1].get(node)
            if node is not None and node.tag == f"{_W}tbl":
                return node
        return root_tbl


def _heading_styles(styles: ET.Element | None) -> set[str]:
    out: set[str] = set()
    if styles is None:
        return out
    for st in styles.findall(f"{_W}style"):
        sid = _attr(st, "styleId") or ""
        name = (_attr(st.find(f"{_W}name"), "val") or "").lower()
        if name.startswith("heading") or name == "title" or sid.lower().startswith("heading") \
                or st.find(f"{_W}pPr/{_W}outlineLvl") is not None:
            out.add(sid)
    return out


def _part_texts(z: zipfile.ZipFile, prefix: str, numbering: _Numbering) -> list[str]:
    """Distinct text of header / footer parts, without bare page numbers."""
    out: list[str] = []
    for name in sorted(n for n in z.namelist() if re.fullmatch(rf"word/{prefix}\d*\.xml", n)):
        root = _read(z, name)
        if root is None:
            continue
        walker = _Walker(numbering, set())
        walker.walk(root)
        for block in walker.blocks:
            for line in block.split("\n"):
                line = line.strip()
                if line and not _PAGE_NUMBER_RE.match(line) and line not in out:
                    out.append(line)
    return out


def extract_docx(data: bytes) -> dict[str, Any]:
    """Return ``{"text", "headings", "stats"}`` for a .docx file.

    ``headings`` are the paragraphs Word marks as headings — a structure hint the
    clause segmenter uses for documents whose headings carry no number.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        document = _read(z, "word/document.xml")
        if document is None:
            raise ValueError("not a Word document: word/document.xml is missing or unreadable")
        styles = _read(z, "word/styles.xml")
        numbering = _Numbering(_read(z, "word/numbering.xml"), styles)
        walker = _Walker(numbering, _heading_styles(styles))
        body = document.find(f"{_W}body")
        walker.walk(body if body is not None else document)

        blocks = list(walker.blocks)
        headers = _part_texts(z, "header", numbering)
        footers = _part_texts(z, "footer", numbering)

        notes: list[str] = []
        note_roots = {"footnote": _read(z, "word/footnotes.xml"), "endnote": _read(z, "word/endnotes.xml")}
        for i, (kind, note_id) in enumerate(walker.note_refs, start=1):
            root = note_roots.get(kind)
            if root is None:
                continue
            for note in root.findall(f"{_W}{kind}"):
                if _attr(note, "id") == note_id:
                    sub = _Walker(numbering, set())
                    sub.walk(note)
                    body_text = " ".join(sub.blocks).strip()
                    if body_text:
                        notes.append(f"[{kind} {i}] {body_text}")

    parts: list[str] = []
    body_text = "\n".join(blocks)
    # A header/footer line is added once, and only if the body does not already say it.
    parts.extend(h for h in headers if h not in body_text)
    parts.append(body_text)
    if notes:
        parts.append("Notes\n" + "\n".join(notes))
    parts.extend(f for f in footers if f not in body_text)
    stats = dict(walker.stats)
    stats.update(headerLines=len(headers), footerLines=len(footers), notes=len(notes))
    return {"text": "\n".join(p for p in parts if p).strip(), "headings": walker.headings, "stats": stats}
