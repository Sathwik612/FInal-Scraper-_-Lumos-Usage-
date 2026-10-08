"""
staffdir.schools
================

**School discovery within a district.** Given a district homepage
URL, find the URLs of individual schools/campuses in the district.

Approach mirrors staffdir.discover:
  1. **Nav-link mining** — anchors in <nav>/<header> whose text or
     URL mentions school-ish words (elementary, middle, high, etc.).
  2. **"Our Schools" index probing** — try canonical paths like
     `/schools`, `/our-schools`, `/campuses`. If any exists AND is
     classified as DEPARTMENT_INDEX or similar, mine its links.
  3. **Sitemap.xml mining** — parse the sitemap and pick URLs whose
     path matches school patterns (`/schools/foo-elementary`, etc.).
  4. **Body-anchor scan** — same keyword scoring but across the whole
     homepage. Lower confidence than nav.
  5. **Subdomain detection** — `lincoln.footown.k12.az.us` under
     `footown.k12.az.us` is a strong signal.
  6. **Playwright fallback** — when the four static stages combined
     yield fewer than ``MIN_SCHOOLS_BEFORE_PLAYWRIGHT`` candidates,
     re-render the homepage AND canonical schools-index paths in
     headless Chromium and mine the resulting DOM. This is the only
     way to catch JS-rendered school lists (Finalsite school finders,
     React-based "find a school" widgets, etc.). Denver Public
     Schools' site-finder is a canonical example: the static homepage
     is a shell, the school list is client-side.

Every candidate is de-duped by normalized URL. Optionally each is
verified by a quick HEAD/GET check.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup           # type: ignore
except Exception:                            # pragma: no cover
    BeautifulSoup = None


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Longest phrases first so "high school" beats "school" when both match.
SCHOOL_TEXT_TOKENS: List[Tuple[str, float]] = [
    ("elementary school", 1.0),
    ("middle school",     1.0),
    ("high school",       1.0),
    ("junior high",       1.0),
    ("senior high",       1.0),
    ("primary school",    1.0),
    ("intermediate school", 1.0),
    ("early childhood",   0.95),
    ("preschool",         0.9),
    ("learning center",   0.9),
    ("school of",         0.85),
    ("academy",           0.85),
    ("preparatory",       0.85),
    ("collegiate",        0.8),
    ("magnet",            0.8),
    ("charter",           0.7),
    ("campus",            0.75),
    ("elementary",        0.85),
    ("k-12",              0.85),
    ("k-8",               0.85),
    ("k-5",               0.85),
    # NB: "middle" and "high" alone are too generic — deliberately not
    # listed. They only fire when part of a longer phrase above.
]

# URL path fragments that suggest school landing pages.
SCHOOL_URL_PATTERNS: List[Tuple[str, float]] = [
    (r"/schools?/",         0.8),
    (r"/campus(?:es)?/",    0.8),
    (r"/sites?/",           0.5),
    (r"/locations?/",       0.5),
    (r"-elementary",        0.9),
    (r"-middle",            0.85),
    (r"-high(?:-?school)?", 0.85),
    (r"-academy",           0.85),
    (r"-primary",           0.85),
    (r"-preparatory",       0.85),
    (r"/elementary",        0.85),
    (r"/middle",            0.7),
    (r"/high",              0.7),
]

# Paths to probe when the homepage has no direct school links.
CANONICAL_SCHOOLS_INDEX_PATHS = [
    "/schools",
    "/our-schools",
    "/schools/",
    "/our-schools/",
    "/campuses",
    "/campuses/",
    "/our-campuses",
    "/schools-list",
    "/school-list",
    "/directory/schools",
    "/about/schools",
]

# Anchor text that is obviously not a school link. Whole-phrase match
# (with word boundaries) — a single occurrence in the anchor text
# rejects the link outright.
_NEGATIVE_TEXT_PHRASES = (
    "home page", "district home", "our district", "district office",
    "central office", "our team", "our staff", "our schools",
    "all schools", "school list", "list of schools", "browse schools",
    "schools directory", "school directory", "find a school",
    "select a school", "choose a school", "search schools",
    "login", "sign in", "log in", "register", "sign up",
    "translate", "language", "search", "calendar", "events",
    "menu", "back to", "return to", "close menu", "toggle",
    "facebook", "twitter", "youtube", "instagram", "linkedin",
    "tiktok", "pinterest",
    "privacy", "sitemap", "accessibility", "terms",
    "news", "employment", "careers", "jobs", "employees",
    "board of", "school board", "board meeting",
    "about us", "about", "contact us", "contact",
    "parent portal", "student portal", "staff portal",
    "faq", "help", "support",
)

# URL path segments that mean "this is an index/browse page, not a
# specific school".
_NEGATIVE_URL_TOKENS = (
    "/all-schools", "/schools-list", "/school-list",
    "/browse-schools", "/find-a-school", "/select-school",
    "/schools/index", "/schools.html", "/schools/", "/our-schools/",
    "/directory-of-schools", "/school-directory",
    "/board", "/board-of", "/employment", "/careers", "/jobs",
    "/news", "/calendar", "/events", "/parent-portal",
    "/student-portal", "/staff-portal", "/login", "/signin",
    "/privacy", "/sitemap", "/accessibility",
    "/venue/", "/venues/", "/event/", "/events/", "/calendar-event/",
    "/facility/",
    "/facilities/", "/page/districtleadership",
)

_SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".zip", ".jpg", ".jpeg", ".png",
                    ".gif", ".svg", ".mp4", ".xls", ".xlsx", ".ico",
                    ".css", ".js")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class SchoolLink:
    """One discovered school with its evidence."""
    name: str
    url: str
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    is_subdomain: bool = False

    def add(self, weight: float, why: str) -> None:
        self.score += weight
        self.reasons.append(f"[+{weight:.2f}] {why}")

    def to_dict(self) -> dict:
        return {
            "name": self.name, "url": self.url,
            "score": round(self.score, 3),
            "is_subdomain": self.is_subdomain,
            "reasons": self.reasons[:6],
        }


@dataclass
class SchoolDiscoveryReport:
    district_url: str = ""
    homepage_ok: bool = False
    candidates_scanned: int = 0
    schools_returned: int = 0
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_url(url: str) -> str:
    p = urlparse(url)
    scheme = (p.scheme or "http").lower()
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"): path = path[:-1]
    q = f"?{p.query}" if p.query else ""
    return f"{scheme}://{host}{path}{q}"


def _dir_form(url: str) -> str:
    """URL with trailing slash if the last path segment doesn't look
    like a file. Ensures ``urljoin(base, 'staff')`` resolves under
    the current directory, not one level up."""
    p = urlparse(url)
    path = p.path or "/"
    if path.endswith("/"): return url
    last = path.rsplit("/", 1)[-1]
    if "." in last: return url
    q = f"?{p.query}" if p.query else ""
    return f"{p.scheme}://{p.netloc}{path}/{q}"


def _root_domain(host: str) -> str:
    """Very rough eTLD+1 fallback (no PSL). Good enough for schools:
    ``lincoln.foo.k12.az.us`` -> ``foo.k12.az.us``."""
    host = host.lower().lstrip(".")
    parts = host.split(".")
    if len(parts) <= 2: return host
    # Common school TLD patterns: k12.<st>.us, <st>.us, .edu, .org
    if parts[-1] == "us" and parts[-2] in ("us",) or \
       (len(parts) >= 4 and parts[-1] == "us" and parts[-2] in
        ("az", "tx", "ca", "ny", "fl", "oh", "mi", "il", "nc", "md")
        and parts[-3] == "k12"):
        # e.g. "foo.k12.az.us"
        return ".".join(parts[-4:])
    return ".".join(parts[-2:])


def _same_root(candidate: str, base: str) -> bool:
    ch = urlparse(candidate).netloc.lower()
    bh = urlparse(base).netloc.lower()
    if not ch or not bh: return False
    def strip_www(h): return h[4:] if h.startswith("www.") else h
    ch2, bh2 = strip_www(ch), strip_www(bh)
    if ch2 == bh2: return True
    if ch2.endswith("." + bh2) or bh2.endswith("." + ch2): return True
    return _root_domain(ch2) == _root_domain(bh2)


def _is_subdomain(candidate: str, base: str) -> bool:
    ch = urlparse(candidate).netloc.lower()
    bh = urlparse(base).netloc.lower()
    def strip_www(h): return h[4:] if h.startswith("www.") else h
    ch2, bh2 = strip_www(ch), strip_www(bh)
    return ch2 != bh2 and ch2.endswith("." + bh2)


def _looks_skippable(url: str) -> bool:
    low = url.lower()
    if any(low.endswith(ext) for ext in _SKIP_EXTENSIONS): return True
    if low.startswith(("mailto:", "tel:", "javascript:", "#")): return True
    return False


def _score_link_text(text: str) -> Tuple[float, str]:
    tl = (text or "").lower().strip()
    if not tl or len(tl) > 100: return 0.0, ""
    # Reject if any negative phrase appears as a whole word substring.
    padded = f" {tl} "
    for neg in _NEGATIVE_TEXT_PHRASES:
        if f" {neg} " in padded or padded.startswith(f" {neg} ") \
                or padded.endswith(f" {neg} ") or tl == neg:
            return 0.0, ""
    best_w = 0.0; best_reason = ""
    for tok, w in SCHOOL_TEXT_TOKENS:
        if tok in tl and w > best_w:
            best_w, best_reason = w, f"link text contains {tok!r}"
    return best_w, best_reason


def _score_url_path(url: str) -> Tuple[float, str]:
    path = urlparse(url).path.lower()
    # Calendar-event pages often contain the name of a real opponent school
    # (and therefore score highly on "-academy"/"-school"). They are event
    # records, never school landing pages.
    if "/calendar-event/" in path or path.startswith("/calendar-event"):
        return 0.0, ""
    # Reject index / non-school paths outright.
    for neg in _NEGATIVE_URL_TOKENS:
        if neg in path and not any(sig in path for sig in
                                    ("-elementary", "-middle", "-high",
                                     "-academy", "-primary")):
            return 0.0, ""
    best_w = 0.0; best_reason = ""
    for pat, w in SCHOOL_URL_PATTERNS:
        if re.search(pat, path) and w > best_w:
            best_w, best_reason = w, f"URL path matches {pat!r}"
    return best_w, best_reason


def _guess_school_name(text: str, url: str) -> str:
    """Prefer the anchor text; if that's blank/generic, derive from the
    URL's last path segment."""
    t = (text or "").strip()
    if 3 <= len(t) <= 100 and t.lower() not in ("", "home", "click here",
                                                 "more"):
        return t
    seg = urlparse(url).path.rstrip("/").split("/")[-1]
    if seg:
        return seg.replace("-", " ").replace("_", " ").title()
    return urlparse(url).netloc


# ---------------------------------------------------------------------------
# Stage 1: nav / header links
# ---------------------------------------------------------------------------

def _mine_nav(soup, district_url: str) -> List[SchoolLink]:
    schools: List[SchoolLink] = []
    if soup is None: return schools
    regions = list(soup.find_all("nav")) + \
              list(soup.find_all(attrs={"role": "navigation"})) + \
              list(soup.find_all("header"))
    seen = set()
    for region in regions:
        for a in region.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or _looks_skippable(href): continue
            abs_url = urljoin(_dir_form(district_url), href)
            key = _norm_url(abs_url)
            if key in seen: continue
            seen.add(key)
            if not _same_root(abs_url, district_url): continue
            text = a.get_text(" ", strip=True)
            tw, treason = _score_link_text(text)
            uw, ureason = _score_url_path(abs_url)
            base = max(tw, uw)
            if base < 0.6: continue
            s = SchoolLink(name=_guess_school_name(text, abs_url),
                            url=key,
                            is_subdomain=_is_subdomain(abs_url, district_url))
            s.add(base, treason or ureason)
            s.add(0.1, "found in <nav>/<header>")
            if s.is_subdomain: s.add(0.15, "subdomain of district")
            schools.append(s)
    return schools


# ---------------------------------------------------------------------------
# Stage 2: probe canonical "Our Schools" index paths
# ---------------------------------------------------------------------------

def _mine_schools_index(fetch: Callable[[str], Optional[str]],
                        district_url: str,
                        already: Set[str]) -> List[SchoolLink]:
    p = urlparse(district_url)
    origin = f"{p.scheme}://{p.netloc}"
    schools: List[SchoolLink] = []
    probed: Set[str] = set()
    for path in CANONICAL_SCHOOLS_INDEX_PATHS:
        url = _norm_url(urljoin(origin, path))
        if url in probed:
            continue
        probed.add(url)
        html = fetch(url)
        if not html or BeautifulSoup is None: continue
        soup = BeautifulSoup(html, "lxml")
        # Grab every same-root anchor and score it.
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or _looks_skippable(href): continue
            abs_url = urljoin(_dir_form(url), href)
            key = _norm_url(abs_url)
            if key in already: continue
            if not _same_root(abs_url, district_url): continue
            text = a.get_text(" ", strip=True)
            tw, treason = _score_link_text(text)
            uw, ureason = _score_url_path(abs_url)
            base = max(tw, uw)
            if base < 0.5: continue
            already.add(key)
            s = SchoolLink(name=_guess_school_name(text, abs_url),
                            url=key,
                            is_subdomain=_is_subdomain(abs_url, district_url))
            s.add(base, treason or ureason)
            s.add(0.1, f"found on schools-index page {path}")
            if s.is_subdomain: s.add(0.15, "subdomain of district")
            schools.append(s)
        if schools:  # first index that yielded something wins
            break
    return schools


# ---------------------------------------------------------------------------
# Stage 3: sitemap.xml
# ---------------------------------------------------------------------------

_NS_SM = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _mine_sitemap(fetch: Callable[[str], Optional[str]],
                  district_url: str,
                  already: Set[str],
                  max_depth: int = 1) -> List[SchoolLink]:
    p = urlparse(district_url)
    root = f"{p.scheme}://{p.netloc}/sitemap.xml"
    to_visit: List[Tuple[str, int]] = [(root, 0)]
    tried = set()
    schools: List[SchoolLink] = []
    while to_visit:
        sm_url, depth = to_visit.pop(0)
        if sm_url in tried or depth > max_depth: continue
        tried.add(sm_url)
        xml = fetch(sm_url)
        if not xml: continue
        try:
            root_el = ET.fromstring(xml)
        except Exception:
            continue
        for sm in root_el.findall(".//sm:sitemap/sm:loc", _NS_SM):
            if sm.text: to_visit.append((sm.text.strip(), depth + 1))
        for loc in root_el.findall(".//sm:url/sm:loc", _NS_SM):
            url = (loc.text or "").strip()
            if not url or not _same_root(url, district_url): continue
            key = _norm_url(url)
            if key in already: continue
            uw, ureason = _score_url_path(url)
            if uw < 0.7: continue
            already.add(key)
            s = SchoolLink(name=_guess_school_name("", url), url=key,
                            is_subdomain=_is_subdomain(url, district_url))
            s.add(uw, ureason)
            s.add(0.1, "found in sitemap.xml")
            if s.is_subdomain: s.add(0.15, "subdomain of district")
            schools.append(s)
    return schools


# ---------------------------------------------------------------------------
# Stage 4: body-anchor scan (fallback)
# ---------------------------------------------------------------------------

def _mine_body(soup, district_url: str,
               already: Set[str]) -> List[SchoolLink]:
    schools: List[SchoolLink] = []
    if soup is None: return schools
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or _looks_skippable(href): continue
        abs_url = urljoin(_dir_form(district_url), href)
        key = _norm_url(abs_url)
        if key in already: continue
        if not _same_root(abs_url, district_url): continue
        text = a.get_text(" ", strip=True)
        tw, treason = _score_link_text(text)
        uw, ureason = _score_url_path(abs_url)
        base = max(tw, uw)
        if base < 0.7: continue        # stricter than nav — noisier region
        already.add(key)
        s = SchoolLink(name=_guess_school_name(text, abs_url),
                        url=key,
                        is_subdomain=_is_subdomain(abs_url, district_url))
        s.add(base * 0.85, treason or ureason)
        if s.is_subdomain: s.add(0.15, "subdomain of district")
        schools.append(s)
    return schools


# ---------------------------------------------------------------------------
# De-dupe (same school appearing via multiple stages)
# ---------------------------------------------------------------------------

def _normalize_school_name(name: str) -> str:
    """Collapse variants of the same school name. 'Lincoln Elementary
    School', 'Lincoln Elementary', 'Lincoln El. School' all -> 'lincoln'
    for de-dup purposes."""
    n = (name or "").lower().strip()
    # Drop generic school-noise words
    n = re.sub(r"\b(school|elementary|middle|high|junior|senior|primary|"
               r"intermediate|academy|preparatory|prep|k-?\d+|campus|"
               r"center|the|of|and|our|district|charter|magnet)\b",
               " ", n)
    # Drop parenthetical qualifiers and non-word chars
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n or (name or "").lower().strip()


def _url_path_depth(url: str) -> int:
    return len([p for p in urlparse(url).path.split("/") if p])


def _collapse(schools: List[SchoolLink]) -> List[SchoolLink]:
    """De-dup in two passes: first by URL, then by normalized school
    name (so 'Lincoln Elementary' and 'Lincoln Elementary School' at
    two different URLs collapse into one entry)."""
    # Pass 1: same URL -> merge scores + reasons.
    by_url: dict = {}
    for s in schools:
        key = _norm_url(s.url)
        if key in by_url:
            existing = by_url[key]
            existing.score += s.score
            existing.reasons.extend(s.reasons)
            if len(s.name) > len(existing.name) and \
                    not existing.name.lower().startswith(s.name.lower()[:8]):
                existing.name = s.name
        else:
            s.url = key
            by_url[key] = s
    by_url_list = list(by_url.values())

    # Pass 2: same normalized name -> keep highest-scoring URL.
    by_name: dict = {}
    for s in by_url_list:
        nm = _normalize_school_name(s.name)
        if not nm: continue    # empty after normalization = suspicious
        if nm in by_name:
            existing = by_name[nm]
            if s.score > existing.score:
                # Keep the higher-scoring one; record the dupe URL.
                s.reasons.append(f"absorbed dup URL: {existing.url}")
                by_name[nm] = s
            else:
                existing.reasons.append(f"absorbed dup URL: {s.url}")
        else:
            by_name[nm] = s
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Stage 6: Playwright fallback for JS-rendered school lists
#
# Fires when stages 1-4 combined yielded fewer than
# MIN_SCHOOLS_BEFORE_PLAYWRIGHT candidates — the classic signature of
# a district site whose school list is client-rendered (Finalsite
# school finder, React map, etc.). We launch a real browser, load
# the homepage AND canonical schools-index paths, and mine the DOM
# for same-root or subdomain anchors that scored below the static
# threshold OR that require JS to appear at all.
# ---------------------------------------------------------------------------

logger = logging.getLogger("staffdir.schools")

# Static discovery below this count triggers the Playwright fallback.
# For any real K-12 district this is a very safe floor — mega-districts
# have 100+ schools, small rural districts have 3-5. Anything under 5
# strongly suggests we missed a JS-rendered list.
MIN_SCHOOLS_BEFORE_PLAYWRIGHT = 5

# Additional URL paths to probe in a rendered browser. Superset of
# CANONICAL_SCHOOLS_INDEX_PATHS because rendering also unlocks
# JS-hydrated finders that live at deep or app-style paths.
_PLAYWRIGHT_SCHOOL_PATHS = list(dict.fromkeys(
    CANONICAL_SCHOOLS_INDEX_PATHS + [
        "/find-a-school", "/find-schools", "/find_a_school",
        "/school-finder", "/schools-finder", "/school_finder",
        "/schools/find", "/schools/finder", "/schools/directory",
        "/district/schools", "/district/our-schools",
        "/our-district/schools", "/our-district/our-schools",
        "/browse-schools", "/schools/browse",
    ]))

# Sibling subdomains often host school finders / school lists that
# `www.<district>` doesn't. Denver Public Schools is the canonical
# case: `www.dpsk12.org` has no /schools path at all, but
# `schoolfinder.dpsk12.org` has the entire school list and individual
# schools live at `<slug>.dpsk12.org`. Probing these sibling
# subdomains during discovery catches this whole family of setups.
_SIBLING_SUBDOMAIN_PREFIXES = [
    "schools", "schoolfinder", "school-finder", "schoolsearch",
    "sites", "finder", "directory", "portal",
]


def _sibling_subdomain_urls(district_url: str) -> List[str]:
    """Return probable sibling-subdomain URLs for a district.

    Example: for `https://www.dpsk12.org` this yields
    `https://schoolfinder.dpsk12.org`, `https://schools.dpsk12.org`,
    etc. Callers should try each; most will 404, but the one or two
    that respond can unlock the entire school list.
    """
    p = urlparse(district_url)
    if not p.netloc:
        return []
    parts = p.netloc.split(".")
    # Strip common leading labels so we sit at the parent domain.
    if parts[0] in ("www", "web"):
        parent = ".".join(parts[1:])
    else:
        parent = ".".join(parts)
    if not parent or "." not in parent:
        return []
    scheme = p.scheme or "https"
    return [f"{scheme}://{prefix}.{parent}"
            for prefix in _SIBLING_SUBDOMAIN_PREFIXES]


def _looks_like_school_url(url: str, district_url: str) -> bool:
    """Loose accept rule for a URL to be treated as a candidate school
    home. Broader than the static ``_score_url_path`` because a real
    browser confirmed the anchor is a live link, so we can trust
    subdomain and other-domain signals more.
    """
    if _looks_skippable(url):
        return False
    parsed = urlparse(url)
    if not parsed.netloc or not parsed.scheme.startswith("http"):
        return False
    if _url_path_depth(url) > 5:
        return False
    # Same-root subdomain: strong signal (east.dpsk12.org, samuels.dpsk12.org).
    if _is_subdomain(url, district_url):
        # But not the BARE ROOTS of sibling-hub subdomains — those
        # are index pages, not school home pages. A path-bearing
        # URL on the same subdomain (schools.dpsk12.org/east-hs)
        # IS a specific school and stays accepted.
        host_first = parsed.netloc.lower().split(".")[0]
        if host_first in _SIBLING_SUBDOMAIN_PREFIXES:
            return _url_path_depth(url) >= 1
        return True
    # Same root: only trust if the path has a school-ish marker.
    if _same_root(url, district_url):
        path = parsed.path.lower()
        for neg in _NEGATIVE_URL_TOKENS:
            if neg in path and not any(sig in path for sig in
                                        ("-elementary", "-middle", "-high",
                                         "-academy", "-primary")):
                return False
        for pat, _ in SCHOOL_URL_PATTERNS:
            if re.search(pat, path):
                return True
        # No path marker → require the URL path have enough structure
        # to look like a landing page, not a nav link.
        if _url_path_depth(url) == 0:
            return False
        return False
    # Different root domain — do not accept from generic mining
    # (charter networks like dsstpublicschools.org live off-domain,
    # but chasing every off-domain link explodes candidate count).
    return False


async def _mine_with_playwright_async(district_url: str,
                                       paths_to_probe: List[str],
                                       timeout_seconds: int
                                       ) -> List[Tuple[str, str]]:
    """Async worker. Returns [(url, anchor_text), ...] from every
    probed page's rendered DOM.

    Probes three families of URLs:
      1. The district homepage itself (rendered).
      2. Every canonical schools-index path off the district's host.
      3. Every sibling subdomain (schoolfinder.<dist>, schools.<dist>, ...).

    On each visited page, mines both ``<a href>`` anchors AND
    ``<option>``/``<select>`` values — a lot of school-finder
    widgets are dropdown-based and their options carry the school
    URL in a ``value``, ``data-url``, or ``data-href`` attribute.
    """
    from playwright.async_api import async_playwright

    p_url = urlparse(district_url)
    origin = f"{p_url.scheme}://{p_url.netloc}"
    urls_to_visit = [district_url]
    urls_to_visit.extend(_sibling_subdomain_urls(district_url))
    urls_to_visit.extend(origin.rstrip("/") + path
                          for path in paths_to_probe)
    # De-dup, preserve order.
    seen_urls: Set[str] = set()
    ordered = []
    for u in urls_to_visit:
        key = _norm_url(u)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        ordered.append(u)

    collected: List[Tuple[str, str]] = []
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/125.0.0.0 Safari/537.36"))
            page = await ctx.new_page()
            for probe_url in ordered:
                if asyncio.get_event_loop().time() >= deadline:
                    break
                try:
                    resp = await page.goto(
                        probe_url, wait_until="domcontentloaded",
                        timeout=30000)
                    # 404-style pages don't help us — skip.
                    if resp is not None and resp.status >= 400:
                        continue
                    # Allow client-side JS to hydrate the school list.
                    await page.wait_for_timeout(3000)
                    # Nudge lazy-loaders.
                    try:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(500)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.debug("playwright school-mine goto %s failed: %s",
                                 probe_url, exc)
                    continue
                # ---- anchor scan ----
                try:
                    anchors = await page.eval_on_selector_all(
                        "a[href]",
                        """els => els.map(e => ({
                            href: e.getAttribute('href') || '',
                            text: (e.innerText || e.textContent || '').trim()
                        })).filter(r => r.href)"""
                    )
                except Exception:
                    anchors = []
                # ---- dropdown / option scan ----
                # Many school-finder widgets are dropdown-based and
                # store the school URL in `value`, `data-url`, or
                # `data-href` on `<option>` (or option-shaped divs).
                try:
                    options = await page.eval_on_selector_all(
                        "option, li[data-url], li[data-href], "
                        "[role='option']",
                        """els => els.map(e => ({
                            href: (e.getAttribute('data-url')
                                    || e.getAttribute('data-href')
                                    || e.getAttribute('value')
                                    || ''),
                            text: (e.innerText || e.textContent || '').trim()
                        })).filter(r =>
                            r.href && r.href.startsWith('http'))"""
                    )
                except Exception:
                    options = []
                for row in anchors + options:
                    href = (row.get("href") or "").strip()
                    text = (row.get("text") or "").strip()
                    if not href or href.startswith(("#", "mailto:",
                                                     "tel:", "javascript:")):
                        continue
                    absolute = urljoin(_dir_form(probe_url), href)
                    collected.append((absolute, text))
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return collected


def _run_coro_sync(coro):
    """Run a coroutine synchronously from any thread — same helper
    the Playwright fallback in playwright_fallback.py uses. Avoids
    the get_event_loop() RuntimeError on 3.10+ background threads.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
    finally:
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        loop.close()


def _mine_with_playwright(district_url: str, already: Set[str],
                           timeout_seconds: int = 180) -> List[SchoolLink]:
    """JS-rendered fallback for school discovery. Only fires when the
    static stages under-found. Cheap failure mode: if playwright is
    not installed, return empty and log once.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception as exc:
        logger.info(
            "school discovery: playwright unavailable (%s); "
            "skipping JS-rendered fallback for %s", exc, district_url)
        return []

    logger.info(
        "school discovery: static stages under-found on %s — "
        "escalating to playwright fallback", district_url)

    try:
        anchors = _run_coro_sync(_mine_with_playwright_async(
            district_url, _PLAYWRIGHT_SCHOOL_PATHS, timeout_seconds))
    except Exception as exc:
        logger.warning(
            "school discovery: playwright fallback raised: %s", exc)
        return []

    # Convert anchors into SchoolLink records, using the LOOSE accept
    # rule (rendered browser saw the anchor is real).
    found: List[SchoolLink] = []
    seen_local: Set[str] = set()
    for absolute, text in anchors:
        key = _norm_url(absolute)
        if key in already or key in seen_local:
            continue
        if not _looks_like_school_url(absolute, district_url):
            continue
        seen_local.add(key)
        # Score conservatively — we still want to demote nav clutter
        # even though a real browser confirmed the link exists.
        tw, treason = _score_link_text(text)
        uw, ureason = _score_url_path(absolute)
        base = max(tw, uw)
        # Subdomains get a boost because they're an extremely reliable
        # school signal (east.dpsk12.org).
        is_sub = _is_subdomain(absolute, district_url)
        if is_sub:
            base = max(base, 0.7)
        if base < 0.5:
            # Even loose: below this the link is very likely a footer
            # or utility link that happened to survive the accept
            # rule.
            continue
        s = SchoolLink(name=_guess_school_name(text, absolute),
                        url=key, is_subdomain=is_sub)
        s.add(base, treason or ureason or "playwright-mined anchor")
        s.add(0.1, "confirmed live by rendered browser")
        if is_sub:
            s.add(0.15, "subdomain of district")
        found.append(s)
    logger.info(
        "school discovery: playwright fallback found %d additional "
        "candidates on %s", len(found), district_url)
    return found


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def discover_schools(
    district_url: str,
    fetch: Callable[[str], Optional[str]],
    *,
    max_schools: int = 30,
    min_score: float = 0.7,
    max_path_depth: int = 4,
    include_district_itself: bool = True,
) -> Tuple[List[SchoolLink], SchoolDiscoveryReport]:
    """Return a ranked list of school URLs found within a district's
    web presence.

    Parameters
    ----------
    district_url
        District homepage URL.
    fetch
        Callable ``fetch(url) -> Optional[str]``.
    max_schools
        Cap on results. Default 30 is generous — most AZ districts
        have 5-20 schools; huge urban districts (Phoenix Union, Mesa)
        can override this.
    min_score
        Drop candidates below this score. 0.7 is intentionally strict
        to avoid picking up navigation/index links; loosen to 0.5 if
        you're missing schools on non-standard sites.
    max_path_depth
        Reject URLs with more than this many path segments. Real
        school landing pages are usually at /schools/<name> (depth 2)
        or subdomain root (depth 0-1), not /district/programs/<x>/
        <y>/school (depth 5+).
    include_district_itself
        Include the district homepage as a "school" record so the
        downstream pipeline extracts the *district-level* staff
        directory (superintendents, board) too.
    """
    report = SchoolDiscoveryReport(district_url=district_url)
    html = fetch(district_url)
    if not html:
        # Homepage unreachable via requests — usually Cloudflare bot
        # protection, an SSL problem the fetcher can't handle, or a
        # transient network error. A real browser routinely defeats
        # all three, so we escalate to the Playwright fallback tier
        # instead of giving up entirely. This is critical for
        # mega-districts on hardened Finalsite installs (DPS, LAUSD,
        # many Ohio urban districts).
        report.warnings.append(
            "district homepage unreachable via static fetch — "
            "escalating directly to playwright")
        seen_early: Set[str] = set()
        pw_only = _mine_with_playwright(district_url, seen_early)
        if not pw_only:
            report.warnings.append(
                "playwright also could not find schools on "
                "the district homepage")
            return [], report
        # We got schools from the browser. Skip the static stages
        # (they can't run without HTML) and jump straight to the
        # dedup + cap logic. include_district_itself still applies.
        candidates = pw_only
        report.homepage_ok = False  # note the static path failed
        candidates = [c for c in candidates
                      if _url_path_depth(c.url) <= max_path_depth]
        candidates = _collapse(candidates)
        report.candidates_scanned = len(candidates)
        kept = [c for c in candidates if c.score >= min_score]
        kept.sort(key=lambda c: -c.score)
        kept = kept[:max_schools]
        if include_district_itself:
            base = SchoolLink(
                name="(District Office)",
                url=_norm_url(district_url),
                score=1.0,
                is_subdomain=False,
            )
            base.reasons.append(
                "[+1.00] district homepage (added by default)")
            kept = [base] + [k for k in kept
                              if _norm_url(k.url) != _norm_url(district_url)]
        return kept, report
    report.homepage_ok = True
    if BeautifulSoup is None:
        raise RuntimeError("bs4 is required")
    soup = BeautifulSoup(html, "lxml")

    seen: Set[str] = set()
    candidates: List[SchoolLink] = []

    # Stage 1 — nav
    candidates.extend(_mine_nav(soup, district_url))
    for c in candidates: seen.add(_norm_url(c.url))

    # Stage 2 — body links are cheap and often identify the schools directly.
    # Prefer them before probing many absent CMS paths; this matters for the
    # production district time budget.
    body_candidates = _mine_body(soup, district_url, seen)
    candidates.extend(body_candidates)
    base_key = _norm_url(district_url)
    has_usable_body_candidate = any(
        _norm_url(c.url) != base_key and not any(
            token in urlparse(c.url).path.lower()
            for token in ("/venue/", "/venues/", "/event/", "/events/",
                          "/facility/", "/facilities/",
                          "/page/districtleadership"))
        for c in body_candidates
    )

    # Stage 3 — probe canonical schools-index paths only when body links
    # did not provide a usable school candidate.
    if not has_usable_body_candidate:
        candidates.extend(_mine_schools_index(fetch, district_url, seen))

        # Stage 4 — sitemap
        candidates.extend(_mine_sitemap(fetch, district_url, seen))

    # Stage 6 — Playwright fallback for JS-rendered school lists.
    # Fires when the static stages (1-4) combined found too few real
    # schools. "Real" = anything except the district homepage itself,
    # since that's always injected for the district-office extraction
    # regardless of discovery. Denver Public Schools is the canonical
    # trigger case: a Finalsite school-finder that renders ~200 school
    # links only after JS runs, so the static stages see nothing.
    district_key = _norm_url(district_url)
    real_static_count = sum(
        1 for c in candidates if _norm_url(c.url) != district_key)
    if real_static_count < MIN_SCHOOLS_BEFORE_PLAYWRIGHT:
        pw_candidates = _mine_with_playwright(district_url, seen)
        if pw_candidates:
            candidates.extend(pw_candidates)
            report.warnings.append(
                f"static discovery under-found ({real_static_count} "
                f"schools); playwright rescued {len(pw_candidates)}")

    # Depth filter BEFORE de-dup so we don't waste dedup time.
    candidates = [c for c in candidates
                  if _url_path_depth(c.url) <= max_path_depth]
    if not include_district_itself:
        district_key = _norm_url(district_url)
        candidates = [
            c for c in candidates
            if _norm_url(c.url) != district_key
            and not any(token in urlparse(c.url).path.lower()
                        for token in (
                            "/venue/", "/venues/", "/event/", "/events/",
                            "/facility/", "/facilities/",
                            "/page/districtleadership",
                        ))
        ]

    candidates = _collapse(candidates)
    report.candidates_scanned = len(candidates)

    # Filter + sort.
    kept = [c for c in candidates if c.score >= min_score]
    kept.sort(key=lambda c: -c.score)
    kept = kept[:max_schools]

    # Optionally prepend the district itself.
    if include_district_itself:
        base = SchoolLink(
            name="(District Office)",
            url=_norm_url(district_url),
            score=1.0,
            is_subdomain=False,
        )
        base.reasons.append("[+1.00] district homepage (added by default)")
        kept = [base] + [k for k in kept
                          if _norm_url(k.url) != _norm_url(district_url)]

    report.schools_returned = len(kept)
    return kept, report
