"""
staffdir.fields
===============

Atomic field extractors. Each returns a `Field` (or list of them)
with the value already normalized and a confidence set based on how
strong the evidence was.

Why atomic:
  * The old parse_card mashed name/title/email/phone logic into a
    single 100-line function, so a bug in email decoding could only
    be tested end-to-end.
  * Here each extractor takes an HTML fragment (Tag or NavigableString)
    or plain text and does one job. Unit-testable.
  * `Field.source` records which route produced the value, so bad
    output can be traced to the specific rule.

Public functions (all take a bs4 element or plain text):
    extract_emails(elem)      -> List[Field]
    extract_phones(elem)      -> List[Field]
    extract_name(elem)        -> Optional[Field]
    extract_title(elem, name) -> Optional[Field]
    extract_department(elem)  -> Optional[Field]
    extract_photo(elem, base_url) -> Optional[Field]
    extract_profile_url(elem, base_url) -> Optional[Field]

    normalize_name(str) -> (first, last)
    normalize_phone(str) -> (formatted, extension)
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup, Tag           # type: ignore
except Exception:                                 # pragma: no cover
    BeautifulSoup = Tag = None                    # so import doesn't die

from .schema import Field, Confidence, FieldSource


# ---------------------------------------------------------------------------
# Basic patterns
# ---------------------------------------------------------------------------

# RFC-5322-ish but reasonable: don't allow trailing dots, keep the TLD sane.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,24}"
)

# "name (at) domain (dot) com", "name at domain dot com", with brackets or
# unicode spaces. Deliberately permissive on the separators.
_OBF_EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*"
    r"(?:\(|\[|\{)?\s*(?:at|@)\s*(?:\)|\]|\})?\s*"
    r"([A-Za-z0-9\-]+(?:\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*"
    r"[A-Za-z0-9\-]+)+)",
    re.IGNORECASE,
)

# HTML-entity encoded @ / . — some sites emit &#64; and &#46;
_ENTITY_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+(?:&#0*64;|&#x0*40;)"
    r"[A-Za-z0-9.\-]+(?:&#0*46;|&#x0*2e;)[A-Za-z]{2,}"
)

# North-America-first phone pattern; accepts +CC, parens, dots, spaces.
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,2}[\s.\-]?)?"
    r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
    r"(?:\s*(?:ext\.?|extension|x)\s*\d{1,6})?",
    re.IGNORECASE,
)

_EXT_RE = re.compile(r"(?:ext\.?|extension|x)\s*(\d{1,6})", re.IGNORECASE)

# Words that mean "this <a> isn't a person's name, it's a call to action"
_CTA_WORDS = {"click", "here", "more", "email", "e-mail", "mail", "contact",
              "send", "message", "read", "view", "profile", "learn",
              "details", "info", "information", "website"}

# Suffixes that make a token unlikely to be a person's name.
_JUNK_NAME_TOKENS = {
    "school", "district", "office", "department", "administration",
    "grade", "grades", "kindergarten", "elementary", "middle", "high",
    "faculty", "staff", "team", "board", "committee",
}

# Small, deliberately bounded vocabulary for navigation/content labels that
# repeatedly appear in scraped output but are not people.
_NON_PERSON_LABELS = {
    "about", "about us", "students", "community", "resources", "calendar",
    "events", "announcements", "grades", "registration", "enrollment",
    "academics", "athletics", "transportation", "nutrition", "employment",
    "contact", "information", "services", "check grades", "our district",
    "legal notices", "enroll now",
}

# Honorifics stripped from the start of names.
_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor",
               "sir", "madam", "coach", "rev", "fr", "sr", "br", "hon"}

# Suffixes at the end of a name that we drop into a separate credentials
# bag (RN etc.) — we don't want them polluting the last-name field.
_CREDENTIAL_TOKENS = {
    "PhD", "Ph.D", "EdD", "Ed.D", "MEd", "M.Ed", "MA", "M.A", "MS", "M.S",
    "MBA", "M.B.A", "BA", "B.A", "BS", "B.S", "JD", "J.D", "DO", "MD",
    "M.D", "RN", "BSN", "LVN", "LPN", "NP", "OTR", "CCC", "SLP",
    "NBCT", "CPA", "Esq",
}
_CREDENTIAL_TOKENS_LOWER = {t.lower().rstrip(".") for t in _CREDENTIAL_TOKENS}


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")

def _clean(text: object) -> str:
    if text is None: return ""
    return _WS_RE.sub(" ", str(text)).strip()


def _lower_words(text: str) -> List[str]:
    return re.findall(r"[a-z]+", text.lower())


def _get_text(elem_or_text) -> str:
    """Accept a bs4 element or a plain string. Return cleaned text."""
    if elem_or_text is None: return ""
    if isinstance(elem_or_text, str): return _clean(elem_or_text)
    try:
        return _clean(elem_or_text.get_text(" "))
    except AttributeError:
        return _clean(str(elem_or_text))


def _is_rejected_name_label(text: str) -> bool:
    """Return True for common navigation/content labels, not names."""
    normalized = re.sub(r"[^a-z0-9]+", " ",
                        _clean(text).lower()).strip()
    return normalized in _NON_PERSON_LABELS or any(
        normalized.startswith(f"{label} ")
        for label in _NON_PERSON_LABELS
    )


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------

def _decode_cloudflare_email(hex_str: str) -> Optional[str]:
    """Cloudflare email obfuscation: first byte is the XOR key, rest
    are the email bytes XORed with it. See CF docs."""
    try:
        raw = bytes.fromhex(hex_str)
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except Exception:
        return None


def _decode_obfuscated_text(text: str) -> Optional[str]:
    """"jdoe at example dot org" -> "jdoe@example.org"."""
    m = _OBF_EMAIL_RE.search(text)
    if not m: return None
    local = m.group(1)
    domain_raw = m.group(2)
    # Replace ' dot ' / '(dot)' / '[dot]' with '.'
    domain = re.sub(
        r"\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*",
        ".", domain_raw).strip(".")
    candidate = f"{local}@{domain}"
    return candidate if _EMAIL_RE.fullmatch(candidate) else None


def _clean_email(raw: str) -> Optional[str]:
    """Normalize an email candidate: strip mailto:, query strings,
    leading "Email " labels, HTML entities. Return None if it doesn't
    validate."""
    if not raw: return None
    v = html_mod.unescape(str(raw)).strip()
    # Strip mailto: and any ?subject=... tail.
    if v.lower().startswith("mailto:"):
        v = v[7:]
    v = v.split("?", 1)[0].split(",", 1)[0].strip()
    # Strip common label prefixes ("Email: ", "E-mail: ")
    v = re.sub(r"^(?:e[-\s]?mail|contact)\s*[:\-]?\s*",
               "", v, flags=re.IGNORECASE)
    v = v.strip().strip("<>").lower()
    if _EMAIL_RE.fullmatch(v):
        return v
    m = _EMAIL_RE.search(v)
    return m.group(0).lower() if m else None


def extract_emails(elem) -> List[Field]:
    """Every email extraction route we know about, in order of trust.

    Returns a *deduplicated* list of Fields — same address seen via
    multiple routes keeps only the highest-confidence one.
    """
    seen: dict = {}   # email -> Field

    def add(email: Optional[str], conf: float, src: FieldSource) -> None:
        norm = _clean_email(email) if email else None
        if not norm: return
        existing = seen.get(norm)
        if existing is None or conf > existing.confidence:
            seen[norm] = Field(norm, conf, src)

    text = _get_text(elem)

    # -- Handle plain-text case: skip DOM routes ---------------------
    if isinstance(elem, str) or Tag is None or not hasattr(elem, "find_all"):
        # entity-encoded, obfuscated, plain regex
        entity_match = _ENTITY_EMAIL_RE.search(text)
        if entity_match:
            add(html_mod.unescape(entity_match.group(0)),
                Confidence.HIGH.value, FieldSource.OBFUSCATED)
        add(_decode_obfuscated_text(text),
            Confidence.HIGH.value, FieldSource.OBFUSCATED)
        for m in _EMAIL_RE.finditer(text):
            add(m.group(0), Confidence.MEDIUM.value, FieldSource.CARD_TEXT)
        return list(seen.values())

    # -- 1) mailto: hrefs (canonical) --------------------------------
    for a in elem.find_all("a", href=True):
        href = a.get("href", "")
        if href.lower().startswith("mailto:"):
            add(href, Confidence.CERTAIN.value, FieldSource.MAILTO)

    # -- 2) schema.org itemprop=email --------------------------------
    for node in elem.find_all(attrs={"itemprop": "email"}):
        val = node.get("content") or _get_text(node)
        add(val, Confidence.CERTAIN.value, FieldSource.MICRODATA)

    # -- 3) Cloudflare data-cfemail ----------------------------------
    for node in elem.select("[data-cfemail]"):
        add(_decode_cloudflare_email(node.get("data-cfemail", "")),
            Confidence.CERTAIN.value, FieldSource.CLOUDFLARE)

    # -- 4) data-email / data-mail attributes ------------------------
    for node in elem.select("[data-email], [data-mail]"):
        add(node.get("data-email") or node.get("data-mail"),
            Confidence.HIGH.value, FieldSource.DATA_ATTR)

    # -- 5) Finalsite screen-reader-only spans -----------------------
    #    Finalsite renders "at" and "dot" inside .fsStyleSROnly so that
    #    the visible text looks like "jdoe [at] example [dot] org" but
    #    screen readers hear a real email. Concatenating all descendant
    #    text with the SR-only text included resolves it.
    #
    #    Some Finalsite skins also put the plain email directly inside
    #    an SR-only span (e.g. Henry County BOE:
    #    `<span class="fsEmailIcon"><span class="fsStyleSROnly">
    #    sabney@…</span></span>`). Detecting these here — rather than
    #    letting them fall through to the low-confidence bare-regex
    #    pass at step 8 — gives them CERTAIN confidence, which
    #    matters for downstream ranking and dedupe.
    for span in elem.select(
            ".fsStyleSROnly, .sr-only, .screen-reader-text, .visuallyhidden"):
        span_text = _get_text(span)
        # Plain email hiding inside the SR span itself (common Finalsite pattern).
        for m in _EMAIL_RE.finditer(span_text):
            add(m.group(0), Confidence.CERTAIN.value, FieldSource.SR_ONLY)
        # Obfuscated "at"/"dot" reconstructed from the SR span PLUS its
        # visible siblings — the original use case for this branch.
        maybe = _decode_obfuscated_text(_get_text(span.parent) if span.parent else "")
        add(maybe, Confidence.HIGH.value, FieldSource.SR_ONLY)

    # -- 6) HTML-entity encoded @ or . ------------------------------
    for m in _ENTITY_EMAIL_RE.finditer(str(elem)):
        add(html_mod.unescape(m.group(0)),
            Confidence.HIGH.value, FieldSource.OBFUSCATED)

    # -- 7) "at / dot" text obfuscation -----------------------------
    obf = _decode_obfuscated_text(text)
    add(obf, Confidence.HIGH.value, FieldSource.OBFUSCATED)

    # -- 8) bare regex over visible text (last resort) --------------
    #      Confidence dropped because "foo@bar" appears in JS strings.
    for m in _EMAIL_RE.finditer(text):
        add(m.group(0), Confidence.MEDIUM.value, FieldSource.CARD_TEXT)

    return list(seen.values())


# ---------------------------------------------------------------------------
# Phone extraction
# ---------------------------------------------------------------------------

def normalize_phone(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (formatted, extension). Formatted is (XXX) XXX-XXXX; ext
    is stripped out and returned separately."""
    if not raw: return None, None
    s = str(raw)
    ext = None
    em = _EXT_RE.search(s)
    if em:
        ext = em.group(1)
        s = s[:em.start()]
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None, ext
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", ext


def extract_phones(elem) -> List[Field]:
    seen: dict = {}

    def add(raw: Optional[str], conf: float, src: FieldSource,
            ext_override: Optional[str] = None) -> None:
        if not raw: return
        fmt, ext = normalize_phone(raw)
        if not fmt: return
        stored = fmt if not (ext or ext_override) else \
            f"{fmt} x{ext_override or ext}"
        existing = seen.get(fmt)
        if existing is None or conf > existing.confidence:
            seen[fmt] = Field(stored, conf, src)

    text = _get_text(elem)

    if isinstance(elem, str) or Tag is None or not hasattr(elem, "find_all"):
        for m in _PHONE_RE.finditer(text):
            add(m.group(0), Confidence.MEDIUM.value, FieldSource.CARD_TEXT)
        return list(seen.values())

    # tel: links (canonical)
    for a in elem.find_all("a", href=True):
        href = a["href"].lower()
        if href.startswith("tel:"):
            add(href[4:], Confidence.CERTAIN.value, FieldSource.TEL_LINK)

    # schema.org
    for node in elem.find_all(attrs={"itemprop": "telephone"}):
        val = node.get("content") or _get_text(node)
        add(val, Confidence.CERTAIN.value, FieldSource.MICRODATA)

    # Free-text regex
    for m in _PHONE_RE.finditer(text):
        add(m.group(0), Confidence.MEDIUM.value, FieldSource.CARD_TEXT)

    return list(seen.values())


# ---------------------------------------------------------------------------
# Name extraction
# ---------------------------------------------------------------------------

def _is_probable_name_text(text: str) -> bool:
    """Cheap surface test: 2..5 tokens, mostly Capitalised, no digits,
    doesn't contain @, doesn't smell like a job title or CTA."""
    if not text: return False
    t = text.strip().strip(",;:")
    if _is_rejected_name_label(t): return False
    if len(t) < 3 or len(t) > 80: return False
    if "@" in t or any(c.isdigit() for c in t): return False
    tokens = t.split()
    if not (1 <= len(tokens) <= 6): return False
    lw = set(_lower_words(t))
    if lw & _CTA_WORDS: return False
    if lw & _JUNK_NAME_TOKENS: return False
    # At least half the tokens should look name-shaped (start Uppercase,
    # or all-caps like "MCDONALD"). Single lowercase word => not a name.
    def name_shaped(tok: str) -> bool:
        core = tok.strip(",.-'()")
        if not core: return False
        return core[0].isupper() or core.isupper()
    return sum(name_shaped(t) for t in tokens) >= max(1, (len(tokens) + 1) // 2)


def normalize_name(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Split into (first, last) after stripping honorifics + credentials.
    Handles "Last, First", "First Middle Last", credentials trailing."""
    if not name: return None, None
    n = _clean(name).rstrip(".,;")

    # "Doe, John A." -> "John A. Doe"
    if "," in n:
        left, _, right = n.partition(",")
        right = right.strip()
        # Only swap if the right side looks like a first name (short,
        # capitalised) and there are no other commas signalling
        # credentials (those we strip below).
        if right and " " not in left and _is_probable_name_text(right):
            n = f"{right} {left.strip()}"

    tokens = [t for t in n.split() if t]

    # Drop honorifics from the front.
    while tokens and tokens[0].lower().rstrip(".") in _HONORIFICS:
        tokens.pop(0)

    # Peel credentials off the end: RN, PhD, EdD, etc.
    # We stop at the first non-credential from the right.
    # NB: strip commas/periods from tokens as we go — a name like
    # "Alicia Ramirez, RN" arrives as tokens ["Alicia", "Ramirez,", "RN"]
    # so both the trailing "RN" AND the trailing comma on "Ramirez," must go.
    while len(tokens) >= 2:
        tail = tokens[-1].strip(".,;").lower()
        if tail in _CREDENTIAL_TOKENS_LOWER or (
                tail.upper() in _CREDENTIAL_TOKENS):
            tokens.pop()
        else:
            break
    tokens = [t.rstrip(",.;") for t in tokens if t.rstrip(",.;")]

    if not tokens: return None, None
    if len(tokens) == 1: return tokens[0], None
    return tokens[0], tokens[-1]


def extract_name(elem, prefer_headings: bool = True) -> Optional[Field]:
    """Find the person's name inside an element (typically a card).

    Priority (highest confidence first):
      1. schema.org itemprop=name / hCard .fn
      2. Any heading (h1..h6) whose text looks like a name
      3. Elements with class containing 'name'
      4. First bold/strong text that looks like a name
    """
    if elem is None: return None
    if isinstance(elem, str) or Tag is None or not hasattr(elem, "select_one"):
        text = _get_text(elem)
        if _is_probable_name_text(text):
            return Field(_clean(text), Confidence.MEDIUM.value,
                         FieldSource.CARD_TEXT)
        return None

    # 1) microdata / hCard
    for sel, src in (('[itemprop="name"]', FieldSource.MICRODATA),
                     ('.fn, .p-name', FieldSource.HCARD)):
        node = elem.select_one(sel)
        if node:
            txt = _clean(node.get("content") or _get_text(node))
            if _is_probable_name_text(txt):
                return Field(txt, Confidence.CERTAIN.value, src)

    # 2) headings (usually the card title = name)
    if prefer_headings:
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            for h in elem.find_all(tag):
                txt = _clean(_get_text(h))
                if _is_probable_name_text(txt):
                    return Field(txt, Confidence.HIGH.value,
                                 FieldSource.HEADING)

    # 3) explicit .name class
    for node in elem.select(
            '[class*="name" i]:not([class*="school" i]):not([class*="site" i])'):
        txt = _clean(_get_text(node))
        if _is_probable_name_text(txt):
            return Field(txt, Confidence.HIGH.value, FieldSource.HEADING)

    # 4) strong/b
    for node in elem.find_all(["strong", "b"]):
        txt = _clean(_get_text(node))
        if _is_probable_name_text(txt):
            return Field(txt, Confidence.MEDIUM.value, FieldSource.CARD_TEXT)

    # 5) very last resort: first line of visible text that looks like a name
    for line in _get_text(elem).splitlines():
        line = _clean(line)
        if _is_probable_name_text(line):
            return Field(line, Confidence.LOW.value, FieldSource.CARD_TEXT)

    return None


# ---------------------------------------------------------------------------
# Title / department extraction
# ---------------------------------------------------------------------------

# Words that make a chunk of text look like a job title. Used to
# distinguish "5th Grade Math Teacher" from "John Doe".
_TITLE_HINTS = {
    "teacher", "instructor", "professor", "faculty", "principal",
    "superintendent", "director", "coordinator", "supervisor", "manager",
    "counselor", "counseling", "coach", "nurse", "psychologist",
    "librarian", "clerk", "secretary", "aide", "paraprofessional",
    "para", "specialist", "assistant", "vice", "associate", "deputy",
    "chief", "officer", "administrator", "custodian", "chair",
    "president", "vp", "dean", "registrar", "athletic", "athletics",
    "band", "choir", "orchestra", "math", "english", "science",
    "history", "social", "reading", "esl", "sped", "spec", "grade",
    "kindergarten", "elementary", "middle", "high", "board",
    "trustee", "member", "technology", "food", "maintenance",
    "transportation", "security",
}


def _looks_like_title(text: str) -> bool:
    if not text: return False
    if "@" in text: return False
    if len(text) < 3 or len(text) > 160: return False
    lw = set(_lower_words(text))
    return bool(lw & _TITLE_HINTS)


def _profile_url_is_person_specific(profile_url: str,
                                     name: str = "") -> bool:
    """Avoid treating a generic page link as person evidence."""
    path = urlparse(profile_url or "").path.lower()
    if not path:
        return False
    if any(token in path for token in
           ("/profile", "/bio", "/person", "/employee", "/faculty",
            "/staff/")):
        return True
    name_tokens = [re.sub(r"[^a-z0-9]", "", t.lower())
                   for t in _clean(name).split()]
    return any(len(token) >= 4 and token in path
               for token in name_tokens)


def _has_person_supporting_evidence(elem, name: Optional[Field],
                                    title: Optional[Field],
                                    email: Optional[Field],
                                    phone: Optional[Field],
                                    profile_url: Optional[Field]) -> bool:
    """Require more than a vague one-token text match for a Person."""
    if not name or not name.value or _is_rejected_name_label(name.value):
        return False
    first, last = normalize_name(name.value)
    if first and last:
        return True
    if name.source in (FieldSource.JSON_LD, FieldSource.MICRODATA,
                       FieldSource.HCARD):
        return True
    if email or phone:
        return True
    if title and _looks_like_title(title.value):
        return True
    if profile_url and _profile_url_is_person_specific(
            profile_url.value, name.value):
        return True
    return False


def extract_title(elem, exclude_text: Optional[str] = None
                  ) -> Optional[Field]:
    """Find the job title inside an element. `exclude_text` (usually
    the already-extracted name) is skipped so we don't return the name
    as a title."""
    if elem is None: return None
    exclude = _clean(exclude_text or "").lower()

    if isinstance(elem, str) or Tag is None or not hasattr(elem, "select_one"):
        t = _get_text(elem)
        if _looks_like_title(t) and t.lower() != exclude:
            return Field(t, Confidence.MEDIUM.value, FieldSource.CARD_TEXT)
        return None

    # 1) microdata
    for sel, src in (('[itemprop="jobTitle"]', FieldSource.MICRODATA),
                     ('[itemprop="title"]',    FieldSource.MICRODATA),
                     ('.title, .p-job-title, .role, .position, .job-title',
                      FieldSource.HCARD)):
        node = elem.select_one(sel)
        if node:
            txt = _clean(node.get("content") or _get_text(node))
            if txt and txt.lower() != exclude and len(txt) < 160:
                return Field(txt, Confidence.CERTAIN.value if
                             src == FieldSource.MICRODATA else Confidence.HIGH.value,
                             src)

    # 2) class-based ("*title*", "*position*", "*role*") — but reject any
    #    element whose class *also* contains "name" or "school".
    for node in elem.select('[class*="title" i], [class*="position" i], '
                             '[class*="role" i], [class*="job" i]'):
        cls = " ".join(node.get("class") or []).lower()
        if "name" in cls or "school" in cls or "site" in cls:
            continue
        txt = _clean(_get_text(node))
        if txt and txt.lower() != exclude and 2 < len(txt) < 160:
            if _looks_like_title(txt) or ":" not in txt:
                return Field(txt, Confidence.HIGH.value, FieldSource.HEADING)

    # 3) Look at siblings of any element we would have identified as
    #    the name — often the first <p> or <span> right after is the title.
    #    We do this by looking through children in order.
    for child in elem.find_all(["p", "span", "div"], recursive=True, limit=25):
        txt = _clean(_get_text(child))
        if not txt or txt.lower() == exclude:
            continue
        if "@" in txt or len(txt) > 160:
            continue
        if _looks_like_title(txt):
            return Field(txt, Confidence.MEDIUM.value, FieldSource.CARD_TEXT)

    return None


def extract_department(elem) -> Optional[Field]:
    if elem is None: return None
    if isinstance(elem, str) or Tag is None or not hasattr(elem, "select_one"):
        return None
    for sel, src in (('[itemprop="department"]', FieldSource.MICRODATA),
                     ('.department, .dept, [class*="department" i]',
                      FieldSource.HCARD)):
        node = elem.select_one(sel)
        if node:
            txt = _clean(node.get("content") or _get_text(node))
            if txt and len(txt) < 100:
                return Field(txt, Confidence.HIGH.value, src)
    return None


# ---------------------------------------------------------------------------
# Media & links
# ---------------------------------------------------------------------------

def extract_photo(elem, base_url: str = "") -> Optional[Field]:
    if elem is None or Tag is None or not hasattr(elem, "find"):
        return None
    img = elem.find("img")
    if not img: return None
    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
    if not src: return None
    # Skip generic placeholder / spacer images.
    if any(bad in src.lower() for bad in
           ("blank.gif", "spacer.gif", "placeholder", "avatar-default")):
        return None
    return Field(urljoin(base_url, src), Confidence.HIGH.value,
                 FieldSource.DATA_ATTR)


def extract_profile_url(elem, base_url: str = "") -> Optional[Field]:
    """Return the first non-mailto/non-tel anchor as the profile link.
    A person's card typically wraps their name in a link to their own
    page."""
    if elem is None or Tag is None or not hasattr(elem, "find_all"):
        return None
    for a in elem.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith("#"):
            continue
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:")):
            continue
        return Field(urljoin(base_url, href), Confidence.HIGH.value,
                     FieldSource.URL_TOKEN)
    return None
