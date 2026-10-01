"""Deterministic clause segmentation.

Why this exists
---------------
The classify stage used to ask the model to both FIND the clauses and COPY their
text back out. That loses content three ways: the middle of a long document is
cut to fit the prompt, a long contract's verbatim text does not fit in the
model's output budget, and a model may skip, merge or paraphrase clauses. Here the
document is cut into clauses in code instead — every character of the parsed text
lands in exactly one clause (or in a heading that is kept as the ``section`` of
its sub-clauses) — and the model is only asked to LABEL each clause.

What is recognised
------------------
* numbered headings: ``1.`` ``1.1`` ``1.1.1`` ``1)`` and ``Section/Article/Clause/§ 4.2``
* roman articles: ``ARTICLE IV`` / ``IV. TERM``
* schedules: ``Schedule A`` ``Exhibit 1`` ``Annex II`` ``Appendix B`` ``Attachment 2``
  ``Part B`` — each restarts numbering, and its clauses are prefixed with its name
* the signature block (``IN WITNESS WHEREOF`` …)
* un-numbered documents: ALL-CAPS and ``Title Case:`` headings
* anything before the first heading becomes the ``Preamble`` clause (parties,
  dates and recitals live there)
* a document with no detectable structure falls back to paragraph blocks.

A numbered line is only a heading if it fits the running sequence (``7`` follows
``6``; ``7.2`` follows ``7.1``), so a wrapped line such as ``30 days after …`` or a
table row is not mistaken for clause 30. A table of contents is recognised and
left in the preamble. Sub-clauses two levels deep (``7.1``) are clauses of their
own with ``parent`` = ``7``; deeper items (``7.1.1``, ``(a)``, ``(ii)``) stay inside
their clause and are listed in ``subclauses`` with character offsets, so they are
individually addressable without being torn from their sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .money import find_amounts

DEFAULT_MAX_CLAUSE_CHARS = 8000
DEFAULT_MAX_DEPTH = 2
PARAGRAPH_TARGET_CHARS = 1800

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
_KEYWORD = r"(?:article|section|clause|paragraph|§+)"
_DECIMAL_RE = re.compile(
    rf"^\s*(?:(?P<kw>{_KEYWORD})\s*)?(?P<num>\d{{1,3}}(?:\.\d{{1,3}}){{0,4}})\s*(?P<punct>[.):]|\s[\-–—])?(?:\s+(?P<rest>\S.*))?$",
    re.IGNORECASE,
)
_ROMAN_KW_RE = re.compile(
    r"^\s*(?P<kw>article|section|part)\s+(?P<num>[IVXLC]{1,7})\b\s*[.:\-–—]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_ROMAN_BARE_RE = re.compile(r"^\s*(?P<num>[IVXLC]{1,7})\.\s+(?P<rest>[A-Z].*)$")
_SCHEDULE_RE = re.compile(
    r"^\s*(?P<kw>schedule|exhibit|annexure|annex|appendix|attachment|addendum|part)\s+"
    r"(?P<id>[A-Z]{1,2}|\d{1,2}|[IVX]{1,5})\b\s*[.:\-–—]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_SIGNATURE_RE = re.compile(
    r"^\s*(in\s+witness\s+whereof|signed\s+(?:by|for)\b|executed\s+(?:as|by)\b|agreed\s+and\s+accepted"
    r"|accepted\s+and\s+agreed|signature\s+page|signatures?\s*:?\s*$|for\s+and\s+on\s+behalf\s+of)",
    re.IGNORECASE,
)
_TOC_LINE_RE = re.compile(r"(?:\.{3,}|…{1,}|_{3,}|\s{3,}|\t)\s*(?:page\s*)?\d{1,3}\s*$", re.IGNORECASE)
_SUBITEM_RE = re.compile(
    r"^[ \t]*(?:\((?P<a>[a-z]{1,2}|[ivxl]{1,5}|\d{1,2}|[A-Z])\)|(?P<b>[a-z]|[ivxl]{1,4})[.)])[ \t]+\S",
    re.MULTILINE,
)
_DEEP_NUM_RE = re.compile(r"^[ \t]*(?P<n>\d{1,3}(?:\.\d{1,3}){2,4})[.)]?[ \t]+\S", re.MULTILINE)
_SENTENCE_VERB_RE = re.compile(
    r"\b(shall|will|must|may|agrees?|is|are|was|were|has|have|means|includes?|hereby|provided)\b"
)
_CONTINUATION_RE = re.compile(
    r"^(of|above|below|hereof|herein|hereto|thereof|and|or|to|in|shall|is|are|as|for|the\s+agreement"
    r"|attached|sets?|contains?|lists?|describes?|forms?|which|that|will)\b",
    re.IGNORECASE,
)
# A line that ends mid-phrase ("…as set out in", "…subject to the"): what follows
# on the next line continues the sentence.
_DANGLING_RE = re.compile(
    r"\b(in|to|of|under|see|per|with|by|the|and|or|as|at|from|on|for|this|that|said|such|attached|hereto)\s*$",
    re.IGNORECASE,
)
_NOT_A_TITLE_RE = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|business|calendar|working|days?|weeks?|months?|years?"
    r"|hours?|percent)\b|\b(street|avenue|road|lane|drive|boulevard|suite|floor|square)\b",
    re.IGNORECASE,
)
_PARTY_SUFFIX_RE = re.compile(
    r"\b(INC|LLC|LLP|LTD|LIMITED|CORP|CORPORATION|GMBH|PLC|PVT|CO|COMPANY|S\.A|B\.V|AG|PTY)\.?$"
)
_NOISE_CAPS = {"CONFIDENTIAL", "DRAFT", "PRIVILEGED", "BETWEEN", "AND", "PAGE", "COPY", "ORIGINAL"}
_INLINE_TITLE_RE = re.compile(r"^(?P<title>[A-Z][^.:\n]{1,70}?)\s*[.:]\s+(?P<body>\S.*)$", re.DOTALL)
_SMALL_WORDS = {"of", "and", "the", "for", "to", "in", "on", "or", "a", "an", "by", "with", "at"}


@dataclass
class Segment:
    number: str
    title: str
    body: str
    start: int
    end: int
    kind: str = "clause"            # preamble | clause | schedule | signature
    level: int = 1
    parent: str | None = None
    section: str | None = None      # heading(s) this clause sits under
    heading: str = ""               # the heading as written: keyword + number + title
    synthetic_number: bool = False
    part_of: str | None = None
    part_index: int | None = None
    part_count: int | None = None
    subclauses: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "number": self.number, "title": self.title, "body": self.body,
            "kind": self.kind, "level": self.level, "parent": self.parent,
            "section": self.section, "heading": self.heading,
            "charStart": self.start, "charEnd": self.end,
            "numberSynthetic": self.synthetic_number, "subclauses": self.subclauses,
        }
        if self.part_of:
            out.update(partOf=self.part_of, partIndex=self.part_index, partCount=self.part_count)
        return out


@dataclass
class _Heading:
    line: int                # index of the heading line
    kind: str                # decimal | schedule | signature | caps
    nums: tuple[int, ...]    # numeric path (empty for schedule/signature/caps)
    label: str               # number as written ("4.2", "IV", "Schedule A")
    rest: str                # text after the number on the heading line
    extra_lines: int = 0     # heading continues onto this many following lines
    scope: str = ""          # schedule prefix ("Schedule A"), "" for the main body
    has_content: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def segment_document(
    text: str,
    *,
    max_clause_chars: int = DEFAULT_MAX_CLAUSE_CHARS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    heading_hints: list[str] | None = None,
    force_paragraphs: bool = False,
) -> dict[str, Any]:
    """Cut ``text`` into clauses. Returns ``{"segments": [Segment], "method": str}``
    where method is "headings" or "paragraphs".

    ``heading_hints`` are lines the source file marks as headings (Word heading
    styles); they are used for documents whose headings carry no number.
    ``force_paragraphs`` skips structure detection — the safer path the caller
    takes when the heading-based result does not cover the document.
    """
    text = text or ""
    if not text.strip():
        return {"segments": [], "method": "empty"}

    lines = _lines(text)
    hints = {h.strip() for h in (heading_hints or []) if h and h.strip()}
    headings = [] if force_paragraphs else _find_headings(lines, max_depth, hints)
    structural = [h for h in headings if h.kind in ("decimal", "caps", "schedule")]
    if len(structural) >= 2 or (structural and len(text) < 3000):
        segments = _build(text, lines, headings)
        method = "headings"
    else:
        segments = paragraph_segments(text, target_chars=PARAGRAPH_TARGET_CHARS)
        method = "paragraphs"

    segments = _split_oversize(segments, max_clause_chars)
    for seg in segments:
        seg.subclauses = _subclauses(seg)
    _make_numbers_unique(segments)
    return {"segments": segments, "method": method}


def paragraph_segments(text: str, target_chars: int = PARAGRAPH_TARGET_CHARS) -> list[Segment]:
    """Structure-free fallback: pack consecutive paragraphs into blocks. Used when
    no headings are found, and as the safer path when coverage comes out low."""
    blocks = _paragraph_spans(text)
    segments: list[Segment] = []
    cur_start: int | None = None
    cur_end = 0
    for start, end in blocks:
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        if end - cur_start > target_chars and cur_end - cur_start >= target_chars // 3:
            segments.append(_plain_segment(text, cur_start, cur_end, len(segments) + 1))
            cur_start = start
        cur_end = end
    if cur_start is not None:
        segments.append(_plain_segment(text, cur_start, cur_end, len(segments) + 1))
    return segments


def page_for_offset(page_starts: list[int], offset: int) -> int | None:
    """1-based page containing ``offset`` given each page's start offset."""
    if not page_starts:
        return None
    lo, hi = 0, len(page_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if page_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------


def _lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    for raw in text.split("\n"):
        out.append((pos, raw))
        pos += len(raw) + 1
    return out


def _roman(value: str) -> int | None:
    value = value.upper()
    total, prev = 0, 0
    for ch in reversed(value):
        n = _ROMAN.get(ch)
        if n is None:
            return None
        total += -n if n < prev else n
        prev = max(prev, n)
    return total if 0 < total < 200 and _to_roman(total) == value else None


def _to_roman(n: int) -> str:
    out = ""
    for val, sym in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                     (5, "V"), (4, "IV"), (1, "I")):
        while n >= val:
            out += sym
            n -= val
    return out


def _title_like(rest: str) -> bool:
    r = rest.strip()
    if not r or len(r) > 110:
        return False
    words = r.split()
    if len(words) > 14:
        return False
    if r[-1] in ";," or (r[-1] == "." and len(words) > 6):
        return False
    if _SENTENCE_VERB_RE.search(r) and not r.isupper():
        return False
    return True


def _is_caps_heading(line: str) -> bool:
    s = line.strip()
    if not (4 <= len(s) <= 80):
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 4 or any(c.islower() for c in letters):
        return False
    words = s.rstrip(":").split()
    if len(words) > 9 or s[-1] in ".,;":
        return False
    if all(w.strip(":,.") in _NOISE_CAPS for w in words):
        return False
    if _PARTY_SUFFIX_RE.search(s.rstrip(":")) or find_amounts(s):
        return False
    return sum(c.isdigit() for c in s) <= 4


def _is_colon_heading(line: str) -> bool:
    s = line.strip()
    if not s.endswith(":") or not (3 <= len(s) <= 60):
        return False
    words = s[:-1].split()
    if not 1 <= len(words) <= 6 or not words[0][:1].isupper():
        return False
    return all(w[:1].isupper() or w.lower() in _SMALL_WORDS for w in words if w[:1].isalpha())


def _candidate(lines: list[tuple[int, str]], i: int) -> _Heading | None:
    line = lines[i][1]
    stripped = line.strip()
    if not stripped or len(stripped) > 400:
        return None
    if _TOC_LINE_RE.search(line) and re.match(r"^\s*(?:\w+\s+)?[\dIVXLC]", line):
        return None

    m = _SCHEDULE_RE.match(line)
    if m and len(stripped) <= 120:
        rest = m.group("rest").strip()
        kw = m.group("kw").lower()
        # "Part" needs an upper-case/ numeric id written as a heading, not "part of".
        ok_rest = not rest or (_title_like(rest) and not _CONTINUATION_RE.match(rest))
        ident = m.group("id")
        caps_or_title = line.strip()[:1].isupper() and (ident.isdigit() or ident.isupper())
        # "…as set out in\nSchedule A." is a wrapped reference, not a heading.
        prev = lines[i - 1][1].rstrip() if i else ""
        # A reference, not a heading: the previous line ends mid-phrase, the line
        # is a table-of-contents entry, or what follows the name reads like the
        # rest of a sentence ("Schedule A (Statement of Work)." / "Schedule A.").
        wrapped = bool(_DANGLING_RE.search(prev)) or bool(_TOC_LINE_RE.search(line)) \
            or rest.startswith("(") or rest.endswith(".") or (not rest and stripped.endswith("."))
        if ok_rest and caps_or_title and not wrapped:
            label = f"{m.group('kw').capitalize()} {m.group('id').upper()}"
            return _Heading(i, "schedule", (), label, rest)

    if _SIGNATURE_RE.match(line):
        return _Heading(i, "signature", (), "Signatures", stripped)

    m = _ROMAN_KW_RE.match(line)
    if m and _roman(m.group("num")) and m.group("num").isupper():
        rest = m.group("rest").strip()
        if not rest or (_title_like(rest) and not _CONTINUATION_RE.match(rest)):
            return _Heading(i, "decimal", (_roman(m.group("num")),), m.group("num").upper(), rest)

    m = _DECIMAL_RE.match(line)
    if m:
        nums = tuple(int(p) for p in m.group("num").split("."))
        rest = (m.group("rest") or "").strip()
        if not rest:
            # Number alone on its line (common in PDFs) — the title is the next line.
            nxt = _next_nonblank(lines, i)
            if nxt is not None and nxt == i + 1 and _title_like(lines[nxt][1]) \
                    and lines[nxt][1].strip()[:1].isupper() and not _DECIMAL_RE.match(lines[nxt][1]):
                return _Heading(i, "decimal", nums, m.group("num"), lines[nxt][1].strip(), extra_lines=1)
            return None
        first = rest[:1]
        if not (first.isupper() or first in "\"'“‘([" or (m.group("kw") and first.isalpha())):
            return None
        if m.group("kw") and _CONTINUATION_RE.match(rest):
            return None
        if len(nums) == 1 and not m.group("punct") and not m.group("kw"):
            # A bare number with no dot is only a heading when it clearly starts a
            # block. "within\n5 Business Days of the invoice date", "Dated\n1 January
            # 2024" and "2 Park Avenue" are running text.
            prev = lines[i - 1][1].rstrip() if i else ""
            starts_block = not prev or prev[-1] in ".:;!?"
            if not _title_like(rest) or not starts_block or _NOT_A_TITLE_RE.match(rest):
                return None
        if len(nums) == 1 and not m.group("kw") and _title_like(rest) and (
            find_amounts(rest)
            or re.search(r"(?:\s{2,}|\t)\d[\d,.]*\s*%?$|\d{1,3}(?:,\d{3})+(?:\.\d+)?$|\d\s*%$", rest)
        ):
            return None                      # table row: "3 Deployment $5,000"
        return _Heading(i, "decimal", nums, m.group("num"), rest)

    m = _ROMAN_BARE_RE.match(line)
    if m and _roman(m.group("num")) and _title_like(m.group("rest")):
        return _Heading(i, "decimal", (_roman(m.group("num")),), m.group("num"), m.group("rest").strip())
    return None


def _next_nonblank(lines: list[tuple[int, str]], i: int) -> int | None:
    for j in range(i + 1, min(len(lines), i + 3)):
        if lines[j][1].strip():
            return j
    return None


def _find_headings(
    lines: list[tuple[int, str]], max_depth: int, hints: set[str] | None = None
) -> list[_Heading]:
    """Accept the candidates that fit the running numbering sequence."""
    hints = hints or set()
    accepted: list[_Heading] = []
    scope = ""                      # current schedule prefix
    scope_start = 0                 # index in `accepted` where the scope began
    path: list[int] = []            # numeric path of the last accepted decimal heading
    signature_seen = False
    total = len(lines)
    skip_until = -1

    def mark_content() -> None:
        if accepted:
            accepted[-1].has_content = True

    for i in range(total):
        if i <= skip_until:
            continue
        cand = _candidate(lines, i)
        if cand is None:
            if lines[i][1].strip():
                mark_content()
            continue

        if cand.kind == "schedule":
            # A reference inside running text ("as set out in\nSchedule A") is not a
            # heading: require it to stand at the start of a block or look like a title.
            cand.scope = cand.label
            accepted.append(cand)
            scope, scope_start, path = cand.label, len(accepted), []
            continue

        if cand.kind == "signature":
            if signature_seen or i < total * 0.4:
                mark_content()
                continue
            signature_seen = True
            cand.scope = scope
            accepted.append(cand)
            continue

        nums = cand.nums
        ok = False
        if len(nums) == 1:
            n = nums[0]
            last_top = path[0] if path else 0
            if not path:
                ok = n <= 3
            elif last_top < n <= last_top + (3 if _title_like(cand.rest) else 1):
                ok = True
            elif n == 1 and _scope_is_toc(accepted[scope_start:]):
                # Numbering restarts and everything numbered so far was a bare list
                # of headings: that was a table of contents. Leave it in the preamble.
                del accepted[scope_start:]
                ok = True
        else:
            top = path[0] if path else 0
            if nums[0] == top:
                ok = _fits_under(path, nums)
            elif nums[0] == top + 1 and nums[1] <= 2 and all(x <= 2 for x in nums[2:]):
                ok = True               # "5.1" straight after clause 4: heading 5 was unnumbered
            elif not path and nums[0] <= 2 and nums[1] <= 2:
                ok = True
        if not ok:
            mark_content()
            continue

        cand.scope = scope
        path = list(nums)
        skip_until = i + cand.extra_lines
        if len(nums) > max_depth:
            # Deeper than the clause level: it stays inside its parent clause and is
            # reported as a sub-clause instead.
            mark_content()
            continue
        accepted.append(cand)

    if not any(h.kind == "decimal" for h in accepted):
        accepted = _caps_headings(lines, accepted, hints)
    return accepted


def _fits_under(path: list[int], nums: tuple[int, ...]) -> bool:
    """Is ``nums`` a plausible successor of the last accepted number ``path``?"""
    depth = len(nums)
    if depth > len(path) + 1:
        return False
    # shared prefix must match; the last component must advance (or start a level)
    if depth == len(path) + 1:
        return list(nums[:-1]) == path and nums[-1] <= 2
    if list(nums[: depth - 1]) != path[: depth - 1]:
        return False
    return path[depth - 1] < nums[-1] <= path[depth - 1] + 3


def _scope_is_toc(scope_headings: list[_Heading]) -> bool:
    decimals = [h for h in scope_headings if h.kind == "decimal"]
    # The LAST entry may be followed by the preamble ("This Agreement is made…"),
    # so it is not held against the list being a table of contents.
    return len(decimals) >= 3 and not any(h.has_content for h in decimals[:-1])


def _caps_headings(
    lines: list[tuple[int, str]], accepted: list[_Heading], hints: set[str]
) -> list[_Heading]:
    """Un-numbered document: use ALL-CAPS / 'Title:' lines — and the paragraphs the
    source file itself marks as headings (``hints``) — as headings."""
    taken = {h.line for h in accepted}
    out = list(accepted)
    first_content_seen = False
    for i, (_, line) in enumerate(lines):
        stripped = line.strip()
        if i in taken or not stripped:
            continue
        hinted = stripped in hints and len(stripped) <= 160
        if hinted or _is_caps_heading(line) or _is_colon_heading(line):
            nxt = _next_nonblank(lines, i)
            # A heading introduces text: skip a run of caps lines (title page), the
            # document title and a heading with nothing after it.
            if nxt is None or (not hinted and _is_caps_heading(lines[nxt][1])) \
                    or (not first_content_seen and i < 3):
                first_content_seen = True
                continue
            out.append(_Heading(i, "caps", (), "", stripped.rstrip(":")))
        first_content_seen = True
    out.sort(key=lambda h: h.line)
    # Number them 1..n, restarting inside each schedule.
    scope = ""
    n_in_scope = 0
    for h in out:
        if h.kind == "schedule":
            scope, n_in_scope = h.label, 0
        elif h.kind == "caps":
            n_in_scope += 1
            h.scope = scope
            h.label = str(n_in_scope)
    return out


# ---------------------------------------------------------------------------
# Building segments
# ---------------------------------------------------------------------------


def _split_title(rest: str) -> tuple[str, str]:
    """Separate a heading line into (title, body-on-the-same-line)."""
    rest = rest.strip()
    if _title_like(rest):
        return rest.rstrip(".:").strip(), ""
    m = _INLINE_TITLE_RE.match(rest)
    if m:
        title = m.group("title").strip()
        words = title.split()
        cased = [w for w in words if w[:1].isalpha() and w.lower() not in _SMALL_WORDS]
        if len(words) <= 8 and cased and all(w[:1].isupper() for w in cased) \
                and not _SENTENCE_VERB_RE.search(title):
            return title, m.group("body").strip()
    return "", rest


def _build(text: str, lines: list[tuple[int, str]], headings: list[_Heading]) -> list[Segment]:
    headings = sorted(headings, key=lambda h: h.line)
    segments: list[Segment] = []

    first_start = lines[headings[0].line][0]
    if text[:first_start].strip():
        segments.append(Segment(
            number="Preamble", title="Preamble", body=text[:first_start].strip(),
            start=0, end=first_start, kind="preamble", level=0, synthetic_number=True,
        ))

    open_schedule = ""                      # "Schedule A — Fee Schedule"
    open_top: tuple[str, str] = ("", "")    # (number of the open level-1 clause, its heading)

    for idx, h in enumerate(headings):
        start = lines[h.line][0]
        end = lines[headings[idx + 1].line][0] if idx + 1 < len(headings) else len(text)
        body_line = h.line + 1 + h.extra_lines
        body_start = min(lines[body_line][0] if body_line < len(lines) else len(text), end)
        following = text[body_start:end].strip()
        heading_text = " ".join(text[start:body_start].split())
        parent: str | None = None
        section: str | None = None

        if h.kind == "schedule":
            title = h.rest.rstrip(".:").strip()
            number, level, kind, body = h.label, 1, "schedule", following
            open_schedule = heading_text
            open_top = ("", "")
        elif h.kind == "signature":
            title, number, level, kind = "Signatures", "Signatures", 1, "signature"
            body = text[start:end].strip()
            heading_text = ""                   # the whole block, heading line included, is the body
        else:
            title, inline = _split_title(h.rest)
            body = "\n".join(x for x in (inline, following) if x)
            # The heading as written, without the text that became the body:
            # "Section 4.1 Payment Terms." for "Section 4.1 Payment Terms. Client shall pay…".
            inline_flat = " ".join(inline.split())
            if inline_flat and heading_text.endswith(inline_flat):
                heading_text = heading_text[: len(heading_text) - len(inline_flat)].rstrip()
            level = len(h.nums) if h.kind == "decimal" else 1
            number = f"{h.scope} {h.label}" if h.scope else h.label
            kind = "clause"
            ctx = [open_schedule] if h.scope else []
            if level > 1:
                parent_label = ".".join(h.label.split(".")[:-1])
                parent = f"{h.scope} {parent_label}" if h.scope else parent_label
                top_label = h.label.split(".")[0]
                top_number = f"{h.scope} {top_label}" if h.scope else top_label
                if open_top[0] == top_number and open_top[1]:
                    ctx.append(open_top[1])
            else:
                parent = h.scope or None
                open_top = (number, heading_text)
            section = " › ".join(c for c in ctx if c) or None

        segments.append(Segment(
            number=number, title=title, body=body, start=start, end=end, kind=kind, level=level,
            parent=parent, section=section, heading=heading_text,
            synthetic_number=(h.kind == "caps"),
        ))

    return _fold_empty_headings(segments)


def _fold_empty_headings(segments: list[Segment]) -> list[Segment]:
    """A heading with no text of its own ("7. TERMINATION" followed straight by
    7.1) is not a clause: it lives on as the ``section`` of its sub-clauses. A
    heading with no text and no sub-clauses keeps its heading as its body, so it
    is never emitted empty and never dropped."""
    out: list[Segment] = []
    for i, seg in enumerate(segments):
        if seg.body.strip():
            out.append(seg)
            continue
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        if nxt is not None and nxt.parent == seg.number and seg.kind in ("clause", "schedule"):
            label = seg.heading or f"{seg.number} {seg.title}".strip()
            for child in segments[i + 1:]:
                if child.parent != seg.number:
                    break
                if label not in (child.section or ""):
                    child.section = " › ".join(x for x in (child.section, label) if x)
            continue
        seg.body = seg.heading or seg.title or seg.number
        out.append(seg)
    return out


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    sep = re.compile(r"\n[ \t]*\n+") if re.search(r"\n[ \t]*\n", text) else re.compile(r"\n")
    pos = 0
    for m in sep.finditer(text):
        if text[pos:m.start()].strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    return spans


def _plain_segment(text: str, start: int, end: int, n: int) -> Segment:
    return Segment(number=str(n), title="", body=text[start:end].strip(), start=start, end=end,
                   kind="clause", level=1, synthetic_number=True)


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def _split_oversize(segments: list[Segment], max_chars: int) -> list[Segment]:
    """Cut a clause longer than ``max_chars`` at line / sentence boundaries into
    numbered parts, so the model always sees the whole of it."""
    out: list[Segment] = []
    for seg in segments:
        if len(seg.body) <= max_chars:
            out.append(seg)
            continue
        pieces = _pack_lines(seg.body, max_chars)
        for i, piece in enumerate(pieces, start=1):
            out.append(Segment(
                number=f"{seg.number} (part {i} of {len(pieces)})",
                title=seg.title, body=piece, start=seg.start, end=seg.end, kind=seg.kind,
                level=seg.level, parent=seg.parent, section=seg.section, heading=seg.heading,
                synthetic_number=seg.synthetic_number, part_of=seg.number, part_index=i,
                part_count=len(pieces),
            ))
    return out


def _pack_lines(body: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for line in body.split("\n"):
        if len(line) <= max_chars:
            units.append(line)
            continue
        # A single very long line (no line breaks in the source): cut at sentences.
        cur = ""
        for sentence in re.split(r"(?<=[.;:])\s+", line):
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                if cur:
                    units.append(cur)
                    cur = ""
                units.append(sentence[:cut])
                sentence = sentence[cut:].lstrip()
            if cur and len(cur) + 1 + len(sentence) > max_chars:
                units.append(cur)
                cur = sentence
            else:
                cur = f"{cur} {sentence}".strip()
        if cur:
            units.append(cur)
    pieces: list[str] = []
    cur_lines: list[str] = []
    size = 0
    for unit in units:
        if cur_lines and size + len(unit) + 1 > max_chars:
            pieces.append("\n".join(cur_lines).strip())
            cur_lines, size = [], 0
        cur_lines.append(unit)
        size += len(unit) + 1
    if cur_lines:
        pieces.append("\n".join(cur_lines).strip())
    return [p for p in pieces if p]


def _subclauses(seg: Segment) -> list[dict[str, Any]]:
    """List the lettered / deeper-numbered items inside a clause with offsets into
    its body, so "7.1(b)" is addressable without splitting the clause."""
    marks: list[tuple[int, str]] = []
    for m in _DEEP_NUM_RE.finditer(seg.body):
        marks.append((m.start(), m.group("n")))
    for m in _SUBITEM_RE.finditer(seg.body):
        label = f"({m.group('a')})" if m.group("a") else f"({m.group('b')})"
        marks.append((m.start(), label))
    if not marks:
        return []
    marks.sort()
    base = seg.part_of or seg.number
    out: list[dict[str, Any]] = []
    for i, (pos, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(seg.body)
        ref = label if label[:1].isdigit() else f"{base}{label}"
        out.append({"ref": ref, "label": label, "parent": base, "start": pos, "end": end})
    return out


def _make_numbers_unique(segments: list[Segment]) -> None:
    """Clause numbers key the timeline state and the search index, so two clauses
    sharing a number would overwrite one another. Disambiguate repeats."""
    seen: dict[str, int] = {}
    for seg in segments:
        key = seg.number.strip().lower()
        if not key:
            seg.number, seg.synthetic_number = f"U{len(seen) + 1}", True
            key = seg.number.lower()
        if key in seen:
            seen[key] += 1
            seg.number = f"{seg.number} (dup {seen[key]})"
            seen[seg.number.lower()] = 1
        else:
            seen[key] = 1


def unique_numbers(clauses: list[dict[str, Any]]) -> int:
    """Same guarantee for clause dicts that did not come from this module (the
    legacy model-segmented path). Returns how many numbers were changed."""
    seen: dict[str, int] = {}
    changed = 0
    for i, c in enumerate(clauses):
        number = str(c.get("number") or "").strip()
        if not number:
            number = f"U{i + 1}"
            c["numberSynthetic"] = True
            changed += 1
        key = number.lower()
        if key in seen:
            seen[key] += 1
            number = f"{number} (dup {seen[key]})"
            seen[number.lower()] = 1
            changed += 1
        else:
            seen[key] = 1
        c["number"] = number
    return changed
