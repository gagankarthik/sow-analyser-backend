"""Deterministic party extraction — who a contract is between, read by rule.

The model names the parties; this module reads them again from the two places a
contract states them on purpose, so the two can be compared:

* the PREAMBLE — "by and between A, a Delaware corporation ("Client"), and B",
  "BETWEEN: (1) A ...; and (2) B", "A ("Sponsor") and B ("Institution")",
  "made by the Regents of ... and ...";
* the SIGNATURE BLOCK — "IN WITNESS WHEREOF ... ACME CORP. / By: ___ / Name:",
  "for and on behalf of X", "Executed by X acting by a director".

``extract_parties(text)`` returns
``[{name, role_label, entity_type, dba, source, span}]`` (``source`` is
"preamble" or "signature"; ``span`` is the (start, end) offset of the name in
the text as given). It never guesses: a preamble that does not yield two
plausible legal names, a template placeholder ("[CLIENT NAME]") or a bare
defined term ("the Company") gives nothing, and the result is then ``[]``.

``check_parties(model_parties, text)`` compares the model's list with the rule
result (names compared without legal suffixes or punctuation) and says which
model names do not occur in the document at all.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ── vocabulary ───────────────────────────────────────────────────────────────

# Legal-form suffixes, compared without dots and case.
_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "llp", "lp",
    "plc", "lllp", "pllc", "pc", "gmbh", "ag", "kg", "se", "sa", "sas", "sarl", "srl", "spa", "bv", "nv",
    "na", "pty", "ab", "as", "asa", "oy", "kk", "pte", "bhd", "sdn", "ulc", "cic", "pbc", "lc",
}
# Display form for suffixes when an ALL-CAPS name is title-cased.
_SUFFIX_DISPLAY = {
    "inc": "Inc", "incorporated": "Incorporated", "corp": "Corp", "corporation": "Corporation", "co": "Co",
    "company": "Company", "ltd": "Ltd", "limited": "Limited", "gmbh": "GmbH", "pty": "Pty", "pte": "Pte",
    "sarl": "SARL", "bhd": "Bhd", "sdn": "Sdn",
}
_CONNECTORS = {"of", "the", "and", "for", "at", "in", "on", "de", "du", "des", "la", "le", "von", "van",
               "der", "den", "y", "&", "-", "da", "di", "del", "et"}
# Words that, on their own (after "the"), are a defined term, not a party name.
_GENERIC = {
    "company", "client", "customer", "vendor", "supplier", "contractor", "subcontractor", "consultant",
    "provider", "service provider", "recipient", "sponsor", "institution", "university", "licensor",
    "licensee", "party", "parties", "buyer", "seller", "purchaser", "agency", "state", "city", "county",
    "owner", "employer", "employee", "discloser", "disclosing party", "receiving party", "investigator",
    "collaborator", "distributor", "reseller", "partner", "lessor", "lessee", "landlord", "tenant",
    "grantor", "grantee", "undersigned", "parties hereto", "parties below", "affiliates", "agreement",
    "member", "members", "college", "hospital", "school", "government", "department", "board",
    "principal investigator", "pi", "offeror", "contracting officer", "prime contractor", "prime",
    "subrecipient", "awardee", "grantee institution", "user", "author", "authority", "council",
    "operator", "manufacturer", "processor", "controller", "data controller", "data processor",
}
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"}
_LEAD_JUNK = {"this", "these", "that", "agreement", "contract", "whereas", "now", "witnesseth", "dated",
              "between", "among", "by", "and", "made", "entered", "effective", "date", "section", "article"}
_PLACEHOLDER_RE = re.compile(
    r"[\[\]{}<>_●•…]|\.{3}|\bX{3,}\b|\b(?:insert|tbd|tbc|n/a|name of|legal name|company name|client name|"
    r"party name|full name|vendor name|supplier name|customer name|sponsor name|entity name)\b", re.I)
_INSTITUTION_RE = re.compile(
    r"\b(?:universit(?:y|ies)|regents|trustees|college|institute|polytechnic|school|academy|"
    r"hospital|health\s+system|medical\s+center|clinic|foundation|"
    r"city\s+of|county\s+of|state\s+of|commonwealth\s+of|town\s+of|village\s+of|district|"
    r"department\s+of|ministry\s+of|office\s+of|agency|administration|authority|commission|council|"
    r"government|national|federal|bureau|service|services|bank|trust|association|society|"
    r"group|holdings|partners|laborator(?:y|ies)|center|centre|systems|technologies|solutions)\b", re.I)
_UNIVERSITY_RE = re.compile(r"\b(?:universit(?:y|ies)|regents|trustees|college|institute\s+of\s+technology|"
                            r"polytechnic|school\s+of)\b", re.I)
_GOVERNMENT_RE = re.compile(
    r"\b(?:city|county|state|commonwealth|town|village|republic|government|province)\s+of\b|"
    r"\b(?:department|ministry|office|bureau)\s+of\b|\b(?:agency|administration|commission|authority)\b|"
    r"^(?:the\s+)?national\s+\w+(?:\s+\w+)?\s+(?:foundation|institutes?|service|agency)\b|"
    r"\b(?:federal|u\.?s\.?|united\s+states)\b", re.I)
_FOUNDATION_RE = re.compile(r"\b(?:foundation|trust|society|association|charity|non-?profit)\b", re.I)
_HONORIFIC_RE = re.compile(r"^(?:dr|mr|mrs|ms|miss|prof|professor)\.?\s", re.I)
_DESCRIPTOR_RE = re.compile(
    r"\ban?\s+(?:[\w.'-]+\s+){0,5}?"
    r"(limited liability (?:company|partnership)|limited partnership|general partnership|partnership|"
    r"public body corporate|body corporate|non-?profit (?:public benefit )?corporation|"
    r"municipal corporation|public corporation|benefit corporation|professional corporation|corporation|"
    r"public limited company|private limited company|limited company|company|"
    r"state agency|federal agency|agency|instrumentality|political subdivision|"
    r"constitutional corporation|educational institution|institution of higher education|"
    r"public university|private university|university|individual|sole proprietorship|trust)\b", re.I)

# Where a name stops even when the next words are capitalised (ALL-CAPS documents).
_STOP_RE = re.compile(
    r"\s*,?\s*\b(?:with\s+(?:its\s+|a\s+)?(?:principal\s+|registered\s+|main\s+|head\s+)?"
    r"(?:offices?|place\s+of\s+business|address|headquarters)|having\s+(?:its|a|an)\b|located\s+at|situated\s+at|"
    r"whose\s+(?:registered|principal|head|main)|of\s+\d|on\s+behalf\s+of|acting\s+(?:by|through|on|for)|"
    r"incorporated\s+and\s+registered|a\s+company\s+(?:incorporated|registered|organi[sz]ed)|incorporated\s+(?:in|under)|"
    r"organi[sz]ed\s+(?:and\s+existing\s+)?under|duly\s+organi[sz]ed|existing\s+under|registered\s+(?:in|office|under)|"
    r"(?:company|registration|registered)\s+(?:number|no)|hereinafter|herein\s+(?:after|referred)|"
    r"referred\s+to\s+as|pursuant\s+to|effective\s+(?:as\s+of|on|from)|as\s+of\s+the|"
    r"(?:each|together|collectively|individually|jointly|both)\b)", re.I)
_DBA_RE = re.compile(r"\s*,?\s*\b(?:d/b/a|d\.b\.a\.?|dba|doing\s+business\s+as|trading\s+as|t/a|"
                     r"a/k/a|aka|also\s+known\s+as|f/k/a|formerly\s+known\s+as)\b\s*", re.I)

_ABBREV = {"inc", "corp", "co", "ltd", "llc", "lp", "llp", "plc", "na", "sa", "bv", "nv", "ag", "pty", "st",
           "no", "nos", "mr", "mrs", "ms", "dr", "prof", "jr", "sr", "us", "uk", "eg", "ie", "ave", "blvd",
           "rd", "ste", "dept", "univ", "assn", "bros", "intl", "mfg", "natl", "mt", "ft", "vs", "etc", "lc",
           "gmbh", "se", "spa", "srl", "sarl", "ab", "kk", "pte", "bhd", "sdn", "inst", "tech", "cos"}

_QUOTES = "\"'“”‘’«»"
_LABEL_RE = re.compile(r"[\"“‘'«]\s*([^\"”’'»]{1,60}?)\s*[\"”’'»]")

# Anchors that open the list of parties, longest form first.
_ANCHOR_RE = re.compile(
    r"(?-i:\bPARTIES\b)(?=\s*:?\s*(?:\n|\(\s*1\s*\)|1\)))|"
    r"\b(?:by\s+and\s+between|by\s+and\s+among|between|among(?:st)?|"
    r"(?:made|entered\s+into|executed|concluded|issued)(?:\s+and\s+entered\s+into)?"
    r"(?:\s+(?:on|as\s+of)\s+[^.;()]{1,40}?)?\s+by(?!\s+and\b))\b\s*:?\s*", re.I)
_TAIL_END_RE = re.compile(
    r"\b(?:WHEREAS|W\s*I\s*T\s*N\s*E\s*S\s*S\s*E\s*T\s*H|RECITALS?|BACKGROUND|NOW,?\s+THEREFORE|"
    r"IT\s+IS\s+(?:HEREBY\s+)?AGREED|THE\s+PARTIES\s+(?:HEREBY\s+)?AGREE|"
    r"(?:the\s+)?parties\s+(?:hereby\s+)?agree)\b")
_PARA = "\n"   # a paragraph break in the normalised text


# ── text normalisation with an offset map ────────────────────────────────────

def _normalise(text: str) -> tuple[str, list[int]]:
    """Whitespace collapsed: a run with two or more line breaks becomes "\\n"
    (a paragraph break), any other run a single space. ``idx[i]`` is the
    offset in ``text`` of normalised character ``i``."""
    out: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            breaks = 0
            while j < n and text[j].isspace():
                breaks += text[j] == "\n"
                j += 1
            out.append(_PARA if breaks >= 2 else " ")
            idx.append(i)
            i = j
            continue
        out.append(ch)
        idx.append(i)
        i += 1
    idx.append(n)
    return "".join(out), idx


def _mask(s: str) -> str:
    """The same string with the inside of (...) and "..." blanked, so that a
    separator inside a role label or descriptor is not mistaken for one
    between parties. Parentheses themselves are kept."""
    out = list(s)
    depth = 0
    quote = False
    for i, ch in enumerate(s):
        if ch in "“”" or ch == '"':
            if depth == 0:
                quote = not quote if ch == '"' else (ch == "“")
                out[i] = "x"
                continue
        if quote:
            out[i] = "x"
            continue
        if ch == "(":
            depth += 1
            if depth > 1:
                out[i] = "x"
            continue
        if ch == ")" and depth:
            depth -= 1
            if depth:
                out[i] = "x"
            continue
        if depth:
            out[i] = "x"
    return "".join(out)


def _bare(word: str) -> str:
    return re.sub(r"[^\w]", "", word).lower()


def _is_suffix(word: str) -> bool:
    return _bare(word) in _SUFFIXES and (len(_bare(word)) > 2 or "." in word or word.isupper()
                                         or _bare(word) in {"co", "lp", "lc", "na", "sa", "bv", "nv", "ag", "ab"})


# ── display form ─────────────────────────────────────────────────────────────

_VOWELS = set("AEIOUY")


def _title_word(word: str, first: bool) -> str:
    bare = _bare(word)
    if not bare:
        return word
    if bare in _SUFFIXES:
        if bare in _SUFFIX_DISPLAY:
            return _SUFFIX_DISPLAY[bare] + ("." if word.endswith(".") else "")
        return word                                      # LLC, PLC, N.A., B.V. stay as written
    if not first and bare in _CONNECTORS:
        return word.lower()
    letters = re.sub(r"[^A-Za-z]", "", word)
    if "." in word.strip(".") or "&" in word or any(c.isdigit() for c in word):
        return word                                      # U.S., AT&T, 3M
    if len(letters) <= 4 and not (_VOWELS & set(letters)):
        return word                                      # NTT, BBC, SRL-type acronyms
    if letters in {"MIT", "NASA", "IBM", "NYU", "UCLA", "USC", "UCSF", "NIH", "NSF", "CERN", "SUNY", "CUNY",
                   "LSU", "UNC", "UVA", "ETH", "EPFL", "KU", "UK", "USA", "EU", "UAE", "HHS", "DOE", "DOD",
                   "DARPA", "NOAA", "FDA", "CDC", "IT", "AI", "SAAS", "IP"}:
        return word if letters != "SAAS" else word[0] + word[1:].lower()
    out = []
    for part in re.split(r"([-/'’])", word):
        if part in "-/'’" or not part:
            out.append(part)
        elif out and out[-1] in "'’" and len(part) <= 2:
            out.append(part.lower())                     # MARGIE'S -> Margie's
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return "".join(out)


def display_name(name: str) -> str:
    """Trim quotes and brackets, collapse whitespace, and title-case a name
    written in capitals (keeping its legal suffix and acronyms)."""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    s = s.strip(_QUOTES + "()[]{}<>,;: ").strip()
    s = re.sub(r"\s+,", ",", s)
    letters = [c for c in s if c.isalpha()]
    if letters and not any(c.islower() for c in letters):
        words = s.split(" ")
        s = " ".join(_title_word(w, i == 0) for i, w in enumerate(words))
    return s


def party_key(name: str) -> str:
    """Comparison form: lower case, no punctuation, no legal suffix, no
    leading article. "ACME CORP." and "Acme Corporation" give the same key."""
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"(?<=\b\w)\.(?=\w\.)", "", s)            # l.l.c. -> llc., n.a. -> na.
    s = re.sub(r"[^\w\s]", " ", s)
    words = [w for w in s.split() if w]
    while words and words[0] in {"the"}:
        words = words[1:]
    while words and (words[-1] in _SUFFIXES or words[-1] in {"the", "and", "of"}):
        words = words[:-1]
    return " ".join(words)


def _doc_key(text: str) -> str:
    s = str(text or "").lower().replace("&", " and ")
    s = re.sub(r"(?<=\b\w)\.(?=\w\.)", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " " + " ".join(w for w in s.split() if w not in _SUFFIXES) + " "


def same_party(a: str, b: str) -> bool:
    ka, kb = party_key(a), party_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return len(short) >= 4 and f" {short} " in f" {long_} "


# ── one party from a chunk of preamble text ──────────────────────────────────

def _scan_name(chunk: str) -> tuple[int, int]:
    """(start, end) of the legal name at the start of ``chunk``: a run of
    capitalised words and connectors that ends at a legal suffix, a comma not
    followed by a suffix, a parenthesis, a quote or a lower-case word."""
    pos = 0
    m = re.match(r"\s*(?:the\s+)?", chunk)               # a lower-case article is not part of the name
    if m and m.group(0).strip() == "the":
        pos = m.end()
    else:
        pos = len(chunk) - len(chunk.lstrip())
    start = pos
    end = pos
    count = 0
    seen_suffix = False
    for tok in re.finditer(r"(\s*,\s*|\s+)?([^\s,]+)", chunk[pos:]):
        sep, word = tok.group(1) or "", tok.group(2)
        w_start = pos + tok.start(2)
        if count and _PARA in sep:
            break
        if word[0] in "(" + _QUOTES:
            break
        core = word.rstrip(";:)")
        stop_after = core != word
        if not core:
            break
        bare = _bare(core)
        if "," in sep and not _is_suffix(core):
            break
        if seen_suffix and not _is_suffix(core):
            break
        alpha = core.lstrip("(\"'“‘")[:1]
        ok = (alpha.isupper() or (alpha.isdigit() and count == 0 and re.search(r"[A-Za-z]", core))
              or (count and bare in _CONNECTORS) or (count and core == "&") or _is_suffix(core))
        if not ok:
            break
        sentence_end = core.endswith(".") and not _is_suffix(core) and bare not in _ABBREV \
            and not re.fullmatch(r"[A-Z]\.", core) and not re.fullmatch(r"(?:[A-Za-z]\.){2,}", core)
        if sentence_end or (core.endswith(".") and _is_suffix(core) and bare not in _ABBREV
                            and not re.fullmatch(r"(?:[A-Za-z]\.){2,}", core)):
            core = core[:-1]                             # "LIMITED." ends the sentence, not the name
        end = w_start + len(core)
        count += 1
        if _is_suffix(core):
            seen_suffix = True
        if stop_after or sentence_end or count >= 14:
            break
    # A trailing connector ("... of", "... and") is not part of the name.
    name = chunk[start:end]
    while True:
        m2 = re.search(r"\s+(\S+)$", name)
        if m2 and _bare(m2.group(1)) in _CONNECTORS:
            name = name[:m2.start()]
            continue
        break
    return start, start + len(name)


def _valid_name(name: str) -> bool:
    s = name.strip()
    if not (2 <= len(s) <= 150) or _PLACEHOLDER_RE.search(s):
        return False
    words = [w for w in re.split(r"\s+", s) if w]
    lowered = re.sub(r"^the\s+", "", s.lower()).strip(" .,")
    if lowered in _GENERIC or re.sub(r"[^\w\s]", "", lowered) in _GENERIC:
        return False
    if _bare(words[0]) in _LEAD_JUNK or _bare(words[0]) in _MONTHS:
        return False
    if any(_bare(w) in {"agreement", "contract", "whereas", "hereto", "hereof", "herein"} for w in words):
        return False
    content = [w for w in words if _bare(w) not in _CONNECTORS and not _is_suffix(w)]
    if not content or not any(re.search(r"[A-Za-z]{2,}", w) for w in content):
        return False
    if all(_bare(w) in _MONTHS or _bare(w).isdigit() for w in content):
        return False
    # A name made only of defined-term words ("Client Name", "The Company") is a placeholder.
    if not any(_is_suffix(w) for w in words[1:]) and all(_bare(w) in _GENERIC or _bare(w) in {"name", "entity", "legal"} for w in content):
        return False
    return True


def _entity_type(name: str, descriptor: str | None) -> str | None:
    if descriptor:
        d = descriptor.lower()
        if "individual" in d or "sole proprietor" in d:
            return "individual"
        if "limited liability company" in d:
            return "llc"
        if "partnership" in d:
            return "partnership"
        if "agency" in d or "instrumentality" in d or "political subdivision" in d or "municipal" in d:
            return "government"
        if "universit" in d or "higher education" in d or "educational" in d:
            return "university"
        if "non" in d and "profit" in d:
            return "nonprofit"
        if "trust" == d:
            return "trust"
        if "corporat" in d:
            return "corporation"
        if "company" in d:
            return "company"
    words = [_bare(w) for w in name.split()]
    if words and words[-1] not in _SUFFIXES or len(words) == 1:
        if _UNIVERSITY_RE.search(name):
            return "university"
        if _GOVERNMENT_RE.search(name):
            return "government"
        if _FOUNDATION_RE.search(name):
            return "nonprofit"
    if words:
        last = words[-1]
        tail2 = "".join(words[-2:])
        if last in {"llc", "lc", "pllc"} or tail2 == "llc":
            return "llc"
        if last in {"llp", "lp", "lllp"}:
            return "partnership"
        if last in {"inc", "incorporated", "corp", "corporation", "pc", "pbc"}:
            return "corporation"
        if last in _SUFFIXES:
            return "company"
    if _HONORIFIC_RE.match(name):
        return "individual"
    return None


def _role_from(paren_text: str) -> str | None:
    for m in _LABEL_RE.finditer(paren_text):
        label = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.I).strip(" .,")
        if not label or re.fullmatch(r"(?:each\s+)?(?:a\s+)?part(?:y|ies)|parties", label, re.I):
            continue
        if _PLACEHOLDER_RE.search(label) or len(label) > 40:
            continue
        return label
    return None


def _parse_chunk(norm: str, a: int, b: int) -> dict[str, Any] | None:
    """One party from ``norm[a:b]``: role label, descriptor, DBA and the name."""
    chunk = norm[a:b]
    role: str | None = None
    # "(1)", "(a)", "1)" numbering, and an "and" left over from the split.
    m = re.match(r"\s*(?:\(\s*(?:\d{1,2}|[a-d]|[ivx]{1,4})\s*\)|\d{1,2}\))?\s*(?:(?:and|AND|&)\s+)?", chunk)
    off = m.end() if m else 0
    # "Customer: Lucerne Publishing, Inc." — a role written as a label.
    m = re.match(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s*:\s*(?=\S)", chunk[off:])
    if m and m.group(1).lower() in _GENERIC | {"licensor", "licensee", "provider", "customer"}:
        role = m.group(1).strip()
        off += m.end()
    body = chunk[off:]
    masked = _mask(body)
    # Role label: a quoted term in a parenthetical, or "hereinafter "X"".
    for pm in re.finditer(r"\(([^()]*)\)", masked):
        cand = _role_from(body[pm.start():pm.end()])
        if cand:
            role = role or cand
            break
    if role is None:
        hm = re.search(r"(?:hereinafter|referred\s+to\s+as)[^\"“'‘]{0,30}([\"“'‘][^\"”'’]{1,40}[\"”'’])", body, re.I)
        if hm:
            role = _role_from(hm.group(1))
    descriptor = None
    dm = _DESCRIPTOR_RE.search(body)
    if dm and dm.start() < 200:
        descriptor = dm.group(1)
    # The name stops at a stop phrase / DBA marker even in capitals.
    cut = len(body)
    for rx in (_STOP_RE, _DBA_RE):
        sm = rx.search(masked)
        if sm and sm.start() > 0:
            cut = min(cut, sm.start())
    dm2 = re.search(r",\s*(?:an?|the)\s+", masked, re.I)
    if dm2 and dm2.start() > 0:
        cut = min(cut, dm2.start())
    s, e = _scan_name(body[:cut])
    name_raw = body[s:e]
    if not _valid_name(name_raw):
        return None
    dba = None
    xm = _DBA_RE.search(masked)
    if xm and xm.start() >= e - 1 and xm.start() <= e + 3:
        rest = body[xm.end():]
        rs, re_ = _scan_name(rest)
        if _valid_name(rest[rs:re_]):
            dba = display_name(rest[rs:re_])
    name = display_name(name_raw)
    return {"name": name, "role_label": role, "entity_type": _entity_type(name_raw, descriptor), "dba": dba,
            "_span": (a + off + s, a + off + e), "_consumed": _consumed(body, e, masked)}


def _consumed(body: str, end: int, masked: str) -> bool:
    """True when nothing but a descriptor, address or label follows the name —
    i.e. the chunk held exactly one party."""
    rest = re.sub(r"\([^()]*\)", "", masked[end:])
    return not re.sub(r"[\s.,;:" + _QUOTES + "]", "", rest)


# ── the preamble ─────────────────────────────────────────────────────────────

def _sentence_end(norm: str, masked: str, start: int, limit: int) -> int:
    """Index of the end of the sentence that starts at ``start``."""
    for m in re.finditer(r"\.(?=\s+[\"“(]?[A-Z])|\.(?=\s*\n)|\.$", masked[start:limit]):
        i = start + m.start()
        word = re.search(r"(\S+)$", norm[start:i + 1])
        w = word.group(1) if word else ""
        bare = _bare(w)
        if bare in _ABBREV or _is_suffix(w) or re.fullmatch(r"[A-Z]\.", w) or \
                re.fullmatch(r"(?:[A-Za-z]\.){2,}", w):
            continue
        if re.match(r"\s*(?:(?:and|AND)\s+)?\(\s*(?:\d{1,2}|[b-d]|i{2,3}|iv)\s*\)", norm[i + 1:i + 16]):
            continue                                     # "(1) A LIMITED ... .  (2) B LTD" goes on
        return i
    return limit


def _tail(norm: str, masked: str, start: int) -> int:
    """End of the party list that begins at ``start``."""
    limit = min(len(norm), start + 1500)
    end = _sentence_end(norm, masked, start, limit)
    tm = _TAIL_END_RE.search(masked, start, end)
    if tm:
        end = tm.start()
    # A paragraph break ends the list unless the next paragraph continues it.
    pos = start
    while True:
        p = norm.find(_PARA, pos, end)
        if p < 0:
            break
        before = norm[start:p]
        after = norm[p + 1:p + 12]
        if re.search(r"[A-Za-z]{2,}", before) and not re.match(
                r"\s*(?:\(\s*(?:\d{1,2}|[a-d]|[ivx]{1,4})\s*\)|\d{1,2}\)|and\b|AND\b|&)", after) \
                and not re.search(r"(?:\band|AND|&|;|,|:)\s*$", before):
            end = p
            break
        pos = p + 1
    return end


def _split(norm: str, masked: str, a: int, b: int) -> list[tuple[int, int]]:
    """Cut the party list norm[a:b] into one span per party."""
    seg = masked[a:b]

    def top(rx: str) -> list[re.Match[str]]:
        # Numbering markers sit at the top level: their "(" is not masked.
        return [m for m in re.finditer(rx, norm[a:b]) if seg[m.start()] in "(0123456789"]

    nums = top(r"(?<![\w(])\(\s*(\d{1,2})\s*\)|(?<![\w(])(\d{1,2})\)")
    if len(nums) < 2:
        nums = top(r"(?<![\w(])\(\s*([a-d])\s*\)")
        nums = nums if len(nums) >= 2 and nums[0].group(1) == "a" else []
    if len(nums) >= 2:
        marks = [a + m.start() for m in nums] + [b]
        return [(marks[i], marks[i + 1]) for i in range(len(marks) - 1)]

    strong = re.compile(
        r"\s*;\s*(?:(?:and|AND|&)\s+)?|(?<=\))\s*,?\s*(?:and|AND|&)\s+|(?<=\))\s*,\s*(?=(?:the\s+|THE\s+)?[A-Z])|"
        r"\s*,\s*(?:and|AND|&)\s+")
    cuts: list[tuple[int, int]] = [(a + m.start(), a + m.end()) for m in strong.finditer(seg)]
    if not cuts:
        return [(s, e) for s, e in _split_plain(norm, masked, a, b) if norm[s:e].strip()]
    # With explicit separators a bare "and" inside a piece belongs to a name
    # ('the Department of Health and Human Services ("HHS"), and ...').
    pieces: list[tuple[int, int]] = []
    prev = a
    for cs, ce in cuts + [(b, b)]:
        pieces.append((prev, cs))
        prev = ce
    return [(s, e) for s, e in pieces if norm[s:e].strip()]


def _split_plain(norm: str, masked: str, a: int, b: int) -> list[tuple[int, int]]:
    """Split on a bare " and " only where both sides are whole names: after a
    legal suffix, or else at the last place where that holds."""
    cands = [(a + m.start(), a + m.end()) for m in re.finditer(r"\s+(?:and|AND|&)\s+", masked[a:b])]
    if not cands:
        return [(a, b)]
    after_suffix = []
    for cs, ce in cands:
        w = re.search(r"(\S+)$", norm[a:cs])
        if w and _is_suffix(w.group(1).rstrip(",")):
            after_suffix.append((cs, ce))
    if after_suffix:
        out, prev = [], a
        for cs, ce in after_suffix:
            out.append((prev, cs))
            prev = ce
        out.append((prev, b))
        return out
    best = None
    for cs, ce in cands:
        left = _parse_chunk(norm, a, cs)
        right = _parse_chunk(norm, ce, b)
        if left and right and left["_consumed"]:
            best = (cs, ce)
    if best is None:
        return [(a, b)]
    return [(a, best[0]), (best[1], b)]


def _from_list(norm: str, masked: str, a: int, b: int) -> list[dict[str, Any]]:
    out = []
    for s, e in _split(norm, masked, a, b):
        p = _parse_chunk(norm, s, e)
        if p:
            out.append(p)
    return out


_LABEL_PAREN_RE = re.compile(r"\(\s*(?:(?:hereinafter|herein)\s+)?(?:(?:referred\s+to\s+as|called)\s+)?"
                             r"(?:the\s+)?[\"“'‘][^\"”'’]{1,40}[\"”'’][^()]{0,60}\)", re.I)
_NAME_BEFORE_RE = re.compile(
    r"((?:[A-Z0-9][\w&'’.-]*|of|the|and|for|at|&)(?:(?:\s+|\s*,\s*(?=(?:Inc|LLC|Ltd|L\.L\.C|N\.A|Corp|LLP|L\.P)\b))"
    r"(?:[A-Z][\w&'’.-]*|of|the|and|for|at|&)){0,12})\s*(?:,\s*an?\s+[^()]{0,80}?)?\s*$")


def _labelled_pair(norm: str, masked: str, region_end: int) -> list[dict[str, Any]]:
    """'A ("Sponsor") and B ("Institution")' with no "between" — two
    role-labelled names joined by "and" in one sentence."""
    labels = [m for m in _LABEL_PAREN_RE.finditer(norm, 0, region_end) if _role_from(m.group(0))]
    for l1, l2 in zip(labels, labels[1:]):
        gap = norm[l1.end():l2.start()]
        jm = re.match(r"\s*,?\s*(?:and|AND|&)\s+", gap)
        if not jm or "." in masked[l1.end():l1.end() + jm.end()]:
            continue
        base = max(norm.rfind(_PARA, 0, l1.start()) + 1, 0)
        line_start = base
        for dot in re.finditer(r"\.\s", norm[base:l1.start()]):
            w = re.search(r"(\S+)$", norm[base:base + dot.start() + 1])
            if w and not (_bare(w.group(1)) in _ABBREV or _is_suffix(w.group(1))
                          or re.fullmatch(r"(?:[A-Za-z]\.)+", w.group(1))):
                line_start = base + dot.end()            # a real sentence end, not "Inc."
        nm = _NAME_BEFORE_RE.search(norm[line_start:l1.start()])
        if not nm:
            continue
        # Drop sentence-opening words ("This Agreement is ...") that ran into the name.
        first_start = line_start + nm.start(1)
        first = _parse_chunk(norm, first_start, l1.end())
        while first is None:
            nxt = re.search(r"\s+(?=\S)", norm[first_start:l1.start()])
            if not nxt:
                break
            first_start += nxt.end()
            first = _parse_chunk(norm, first_start, l1.end())
        second = _parse_chunk(norm, l1.end() + jm.end(), l2.end())
        if first and second:
            return [first, second]
    return []


def _preamble(norm: str, masked: str) -> list[dict[str, Any]]:
    region_end = min(len(norm), 6000)
    for n, am in enumerate(_ANCHOR_RE.finditer(norm, 0, region_end)):
        if n >= 8:
            break
        start = am.end()
        end = _tail(norm, masked, start)
        parties = _from_list(norm, masked, start, end)
        if len(parties) >= 2:
            return parties
    return _labelled_pair(norm, masked, region_end)


# ── the signature block ──────────────────────────────────────────────────────

_WITNESS_RE = re.compile(r"IN\s+WITNESS\s+(?:WHEREOF|THEREOF)|EXECUTED\s+(?:AS\s+A\s+DEED|BY\s+THE\s+PARTIES)|"
                         r"SIGNATURE\s+PAGE|AGREED\s+AND\s+ACCEPTED|ACCEPTED\s+AND\s+AGREED", re.I)
_BY_LINE_RE = re.compile(r"^\s*(?:By|Signature|Signed|Authori[sz]ed\s+Signature)\s*[:_]", re.I)
_ON_BEHALF_RE = re.compile(
    r"(?:for\s+and\s+on\s+behalf\s+of|on\s+behalf\s+of|executed\s+(?:as\s+a\s+deed\s+)?by|for)\s*:?\s+"
    r"(?:the\s+)?(.+?)\s*(?:,?\s+(?:acting\s+by|by\s+its|by\s+a\s+director|through\s+its).*)?[,:]?\s*$", re.I)
_SKIP_LINE_RE = re.compile(r"^\s*(?:name|title|date|by|its|signature|print(?:ed)?\s+name|witness|address|"
                           r"email|phone|tel)\b\s*[:_]", re.I)


def _entity_like(name: str) -> bool:
    words = name.split()
    return bool(words) and (any(_is_suffix(w) for w in words[1:]) or bool(_INSTITUTION_RE.search(name)))


def _signature(text: str) -> list[dict[str, Any]]:
    wm = None
    for wm in _WITNESS_RE.finditer(text):
        pass
    base = wm.start() if wm else 0
    region = text[base:]
    lines = []
    pos = 0
    for raw in region.splitlines(keepends=True):
        lines.append((base + pos, raw.rstrip("\r\n")))
        pos += len(raw)
    out: list[dict[str, Any]] = []
    nonempty = [(o, ln) for o, ln in lines if ln.strip()]

    def add(offset: int, line: str, role: str | None = None) -> None:
        # Two signature columns side by side ("ACME CORP.        LAKESHORE LLC").
        for cm in re.finditer(r"\S+(?:[ \t]{1,2}\S+)*", line):
            cell = cm.group(0)
            lm = re.match(r"([A-Z][A-Za-z ]{1,24}):\s*(?=\S)", cell)
            c_role = role
            c_off = cm.start()
            if lm and not _SKIP_LINE_RE.match(cell):
                c_role = c_role or display_name(lm.group(1))
                c_off += lm.end()
                cell = cell[lm.end():]
            norm, idx = _normalise(cell)
            p = _parse_chunk(norm, 0, len(norm))
            if not p:
                continue
            s, e = p["_span"]
            raw_name = cell[idx[s]:idx[e - 1] + 1] if e > s else ""
            if not _entity_like(raw_name):
                continue
            p["role_label"] = p["role_label"] or c_role
            p["_span"] = (offset + c_off + idx[s], offset + c_off + idx[e - 1] + 1)
            out.append(p)

    for i, (offset, line) in enumerate(nonempty):
        if _SKIP_LINE_RE.match(line) or _WITNESS_RE.search(line) and len(line) > 60:
            continue
        om = _ON_BEHALF_RE.match(line, len(re.match(r"\s*(?:signed\s+)?", line, re.I).group(0)))
        if om:
            add(offset, " " * om.start(1) + om.group(1))
            continue
        following = [ln for _, ln in nonempty[i + 1:i + 3]]
        if any(_BY_LINE_RE.match(ln) for ln in following[:1]) or \
                (len(following) > 1 and not following[0].strip(" _-") and _BY_LINE_RE.match(following[1])):
            if line.strip().endswith(":") and not re.search(r":\s*\S", line):
                continue                                  # "ACCEPTED AND AGREED:" / "CLIENT:"
            add(offset, line)
    return out


# ── entry points ─────────────────────────────────────────────────────────────

def _finish(p: dict[str, Any], source: str) -> dict[str, Any]:
    return {"name": p["name"], "role_label": p.get("role_label"), "entity_type": p.get("entity_type"),
            "dba": p.get("dba"), "source": source, "span": list(p["_span"])}


def extract_parties(text: str) -> list[dict[str, Any]]:
    """The contracting parties as stated in the preamble and signature block,
    preamble first. ``[]`` when neither states them clearly."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        norm, idx = _normalise(text)
        masked = _mask(norm)
        pre = []
        for p in _preamble(norm, masked):
            s, e = p["_span"]
            p["_span"] = (idx[s], idx[e - 1] + 1) if e > s else (idx[s], idx[s])
            if not any(same_party(p["name"], q["name"]) for q in pre):
                pre.append(p)
        sig = []
        for p in _signature(text):
            if not any(same_party(p["name"], q["name"]) for q in sig):
                sig.append(p)
    except Exception:  # noqa: BLE001 — a rule that cannot read the text says nothing
        return []
    if len(pre) < 2 and not (pre and any(same_party(pre[0]["name"], q["name"]) for q in sig)):
        pre = []
    out = [_finish(p, "preamble") for p in pre]
    for q in sig:
        match = next((o for o in out if same_party(o["name"], q["name"])), None)
        if match is None:
            out.append(_finish(q, "signature"))
        elif not match["role_label"] and q.get("role_label"):
            match["role_label"] = q["role_label"]
    return out


def party_names(text: str) -> list[str]:
    """Just the names, in order — for callers that store parties as strings."""
    return [p["name"] for p in extract_parties(text)]


def check_parties(model_parties: Iterable[Any], text: str,
                  extracted: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compare the model's parties with the rule result.

    ``matched``   model names the rule also found (model spelling);
    ``modelOnly`` model names the rule did not find;
    ``textOnly``  rule names the model did not return;
    ``notInText`` model names that occur nowhere in the document;
    ``extracted`` the full rule result."""
    rule = extract_parties(text) if extracted is None else extracted
    names = [str(p).strip() for p in (model_parties or []) if isinstance(p, str) and str(p).strip()]
    doc = _doc_key(text)
    matched, model_only, not_in_text = [], [], []
    for name in names:
        if any(same_party(name, r["name"]) for r in rule):
            matched.append(name)
        else:
            model_only.append(name)
        key = party_key(name)
        if key and f" {key} " not in doc:
            not_in_text.append(name)
    text_only = [r["name"] for r in rule if not any(same_party(n, r["name"]) for n in names)]
    return {"matched": matched, "modelOnly": model_only, "textOnly": text_only, "notInText": not_in_text,
            "extracted": rule}


def counterparty_fallback(text: str, exclude: "re.Pattern[str] | None" = None) -> str | None:
    """The first party read from ``text`` that ``exclude`` (the organization's
    own name pattern) does not match — for when the analysis named none."""
    for p in extract_parties(text):
        if exclude is None or not exclude.search(p["name"]):
            return p["name"]
    return None
