"""
staffdir.discover
=================

**Directory discovery** — given a school / district homepage URL,
find the actual staff-directory URL(s) as accurately as possible.

The problem: on any given district site, `/staff-directory` might
be at any of dozens of paths (`/apps/pages/staff`, `/our-district/
staff`, `/about/personnel`, or a per-school subdomain). This module
finds it robustly.

Algorithm (in priority order — the first stage that yields a
confident hit short-circuits):

  1. **CMS-aware canonical probing.** Detect the CMS from the
     homepage HTML; probe canonical paths for that CMS
     (e.g. `/apps/pages/staff` for Finalsite).
  2. **Nav-link mining.** Extract anchors from `<nav>` / `<header>`
     regions and rank by matching STAFF_LINK_KEYWORDS.
  3. **Homepage anchor scan.** Same keyword ranking but across the
     whole body — lower confidence than nav.
  4. **Sitemap.xml mining.** Parse the sitemap and match URL paths
     against STAFF_URL_TOKENS.
  5. **Common-path fallback.** Try the "unknown" CMS canonical
     paths.

Every candidate URL is then **fetched and re-classified** via
`classify_page`. Only URLs whose classification is STAFF_DIRECTORY
(or DEPARTMENT_INDEX, which we can recurse into) survive. The
ranker returns the top-N candidates with their confidence and the
reasons each was picked.

Nothing here modifies your existing find_directories — this is a
new function you can call directly.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup           # type: ignore
except Exception:                            # pragma: no cover
    BeautifulSoup = None

from .classify import classify_page
from .cms_paths import (
    CMS_CANONICAL_PATHS, STAFF_LINK_KEYWORDS, STAFF_URL_TOKENS,
)
from .schema import PageClassification, PageType


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class DirectoryCandidate:
    """One candidate staff-directory URL with the evidence for its
    ranking."""
    url: str
    score: float = 0.0                     # 0..1 (before verification)
    reasons: List[str] = field(default_factory=list)
    classification: Optional[PageClassification] = None
    # After verification we set these:
    verified: bool = False                 # actually fetched + classified
    person_count_hint: int = 0             # mailto count on the page, cheap

    def add(self, weight: float, why: str) -> None:
        self.score += weight
        self.reasons.append(f"[+{weight:.2f}] {why}")

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "score": round(self.score, 3),
            "verified": self.verified,
            "page_type": (self.classification.page_type.value
                           if self.classification else None),
            "page_type_confidence": (round(self.classification.confidence, 3)
                                      if self.classification else None),
            "person_count_hint": self.person_count_hint,
            "reasons": self.reasons[:8],
        }


@dataclass
class DiscoveryReport:
    """Everything the discoverer did, for audit."""
    homepage: str = ""
    homepage_status: str = ""              # "ok" | "unreachable" | "js_only"
    cms_detected: str = "unknown"
    candidates_scanned: int = 0
    candidates_verified: int = 0
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CMS detection (light — no dependency on legacy detect_cms)
# ---------------------------------------------------------------------------

_CMS_SIGS = {
    "finalsite":    ("finalsite", "fs-cms", "fsconstituent", "fselement",
                     "fsstyle"),
    "blackboard":   ("blackboard", "bbdn", "schoolwires", "webcommunitymanager",
                     "myschoolcdn"),
    "edlio":        ("edlio", "edl.io"),
    "apptegy":      ("apptegy", "thrillshare"),
    "schoolblocks": ("schoolblocks",),
    "gabbart":      ("gabbart",),
    "parentsquare": ("parentsquare",),
    "squarespace":  ("squarespace",),
    "wordpress":    ("wp-content", "wp-includes", "wp-json"),
}


def detect_cms(html: str, url: str = "") -> str:
    """Return a CMS name key (matches CMS_CANONICAL_PATHS keys) or
    'unknown'. This is deliberately independent of the legacy
    detect_cms() so `staffdir` has no hard import dependency."""
    hay = (html or "").lower() + " " + (url or "").lower()
    for name, sigs in _CMS_SIGS.items():
        if any(s in hay for s in sigs):
            return name
    return "unknown"


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

_SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".zip", ".jpg", ".jpeg",
                    ".png", ".gif", ".svg", ".mp4", ".xls", ".xlsx",
                    ".ico", ".css", ".js")

# Anchor text that means "this link is definitely not the staff directory"
_NEGATIVE_TEXT_HINTS = (
    "login", "sign in", "log in", "register", "translate", "search",
    "calendar", "menu", "back to", "facebook", "twitter", "youtube",
    "instagram", "linkedin",
)


def _norm_url(url: str) -> str:
    """Normalize a URL for de-duplication: drop fragment, lowercase host,
    collapse trailing slash on non-empty paths."""
    p = urlparse(url)
    scheme = (p.scheme or "http").lower()
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    q = f"?{p.query}" if p.query else ""
    return f"{scheme}://{host}{path}{q}"


def _dir_form(url: str) -> str:
    """Return the URL with a trailing slash appended IF the last path
    segment doesn't look like a file (no dot in it). This is critical
    before passing to ``urljoin`` with a relative href — without the
    trailing slash, ``urljoin('/a/b', 'c')`` returns ``/a/c``, not
    ``/a/b/c``."""
    p = urlparse(url)
    path = p.path or "/"
    if path.endswith("/"): return url
    last = path.rsplit("/", 1)[-1]
    # If the last segment has an extension (contains a dot), leave it —
    # that IS a file. Otherwise treat as directory and add trailing /.
    if "." in last: return url
    q = f"?{p.query}" if p.query else ""
    f = f"#{p.fragment}" if p.fragment else ""
    return f"{p.scheme}://{p.netloc}{path}/{q}{f}"


def _same_site(candidate: str, base: str) -> bool:
    ch = urlparse(candidate).netloc.lower()
    bh = urlparse(base).netloc.lower()
    if not ch or not bh: return False
    # Strip common subdomain "www"
    def strip_www(h): return h[4:] if h.startswith("www.") else h
    ch2, bh2 = strip_www(ch), strip_www(bh)
    return ch == bh or ch2 == bh2 or ch.endswith("." + bh2) or \
           bh.endswith("." + ch2)


def _looks_skippable(url: str) -> bool:
    low = url.lower()
    if any(low.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return True
    if any(low.startswith(p) for p in ("mailto:", "tel:", "javascript:", "#")):
        return True
    return False


# ---------------------------------------------------------------------------
# Scoring by link text / URL tokens
# ---------------------------------------------------------------------------

def _score_link_text(text: str) -> Tuple[float, str]:
    """Return (best_weight, reason). Non-zero only if the text matches
    any keyword. Case-insensitive."""
    tl = (text or "").lower().strip()
    if not tl: return 0.0, ""
    # Longest match wins (so "staff directory" beats "staff").
    best_w = 0.0
    best_reason = ""
    for kw, w in STAFF_LINK_KEYWORDS:
        if kw in tl and w > best_w:
            best_w, best_reason = w, f"link text matches {kw!r}"
    # Penalty for anchor-text that's clearly not a directory link.
    if any(neg in tl for neg in _NEGATIVE_TEXT_HINTS):
        return 0.0, ""
    return best_w, best_reason


def _score_url_tokens(url: str) -> Tuple[float, str]:
    low = url.lower()
    path = urlparse(low).path
    best_w = 0.0
    best_reason = ""
    for tok, w in STAFF_URL_TOKENS:
        if tok in path and w > best_w:
            best_w, best_reason = w, f"URL path contains {tok!r}"
    return best_w, best_reason


# ---------------------------------------------------------------------------
# Stage: nav-link mining
# ---------------------------------------------------------------------------

def _mine_nav_links(soup, base_url: str) -> List[DirectoryCandidate]:
    """Extract candidates from <nav>, <header>, and anchors with
    role='navigation'. Nav links get a small confidence boost — a link
    to the staff directory that sits in the site's main navigation is
    almost certainly the real one."""
    candidates: List[DirectoryCandidate] = []
    if soup is None: return candidates

    nav_regions = list(soup.find_all("nav")) + \
        list(soup.find_all(attrs={"role": "navigation"})) + \
        list(soup.find_all("header"))
    seen = set()
    base_for_join = _dir_form(base_url)
    for region in nav_regions:
        for a in region.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or _looks_skippable(href): continue
            abs_url = urljoin(base_for_join, href)
            key = _norm_url(abs_url)
            if key in seen: continue
            seen.add(key)
            if not _same_site(abs_url, base_url): continue
            text = a.get_text(" ", strip=True)
            tw, treason = _score_link_text(text)
            uw, ureason = _score_url_tokens(abs_url)
            base_score = max(tw, uw)
            if base_score <= 0: continue
            c = DirectoryCandidate(url=key)
            c.add(base_score, treason or ureason)
            c.add(0.15, "found in <nav>/<header>")
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Stage: homepage anchor scan
# ---------------------------------------------------------------------------

def _mine_body_links(soup, base_url: str,
                     already: Set[str]) -> List[DirectoryCandidate]:
    candidates: List[DirectoryCandidate] = []
    if soup is None: return candidates
    base_for_join = _dir_form(base_url)
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or _looks_skippable(href): continue
        abs_url = urljoin(base_for_join, href)
        key = _norm_url(abs_url)
        if key in already: continue
        already.add(key)
        if not _same_site(abs_url, base_url): continue
        text = a.get_text(" ", strip=True)
        tw, treason = _score_link_text(text)
        uw, ureason = _score_url_tokens(abs_url)
        base_score = max(tw, uw)
        if base_score <= 0: continue
        c = DirectoryCandidate(url=key)
        # Body-link is worth slightly less than nav-link at the same text.
        c.add(base_score * 0.85, treason or ureason)
        candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Stage: sitemap.xml mining
# ---------------------------------------------------------------------------

_NS_SM = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _mine_sitemap(fetch: Callable[[str], Optional[str]], base_url: str,
                  already: Set[str], max_depth: int = 1
                  ) -> List[DirectoryCandidate]:
    """Fetch and parse sitemap.xml (following one level of sitemap
    index nesting). Return candidates whose URL matches STAFF_URL_TOKENS."""
    p = urlparse(base_url)
    root_sitemap = f"{p.scheme}://{p.netloc}/sitemap.xml"
    to_visit: List[Tuple[str, int]] = [(root_sitemap, 0)]
    candidates: List[DirectoryCandidate] = []
    tried = set()

    while to_visit:
        sm_url, depth = to_visit.pop(0)
        if sm_url in tried or depth > max_depth: continue
        tried.add(sm_url)
        xml = fetch(sm_url)
        if not xml: continue
        try:
            root = ET.fromstring(xml)
        except Exception:
            continue
        # Nested sitemap indexes:
        for sm in root.findall(".//sm:sitemap/sm:loc", _NS_SM):
            if sm.text: to_visit.append((sm.text.strip(), depth + 1))
        # URL entries:
        for loc in root.findall(".//sm:url/sm:loc", _NS_SM):
            url = (loc.text or "").strip()
            if not url or not _same_site(url, base_url): continue
            key = _norm_url(url)
            if key in already: continue
            uw, ureason = _score_url_tokens(url)
            if uw <= 0: continue
            already.add(key)
            c = DirectoryCandidate(url=key)
            c.add(uw, ureason)
            c.add(0.1, "found in sitemap.xml")
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Stage: CMS-canonical path probing
# ---------------------------------------------------------------------------

def _probe_cms_paths(fetch: Callable[[str], Optional[str]],
                     base_url: str, cms: str,
                     already: Set[str]) -> List[DirectoryCandidate]:
    """Try known canonical paths for the detected CMS. Each becomes a
    DirectoryCandidate; whether the page actually exists is decided
    in the verification pass.

    When ``base_url`` has a non-trivial path (e.g. a school at
    ``/schools/lincoln-elementary``), probe **both** origin-relative
    (``/staff``) and school-path-relative
    (``/schools/lincoln-elementary/staff``). The school-path version
    gets a higher score because it's much more likely to be that
    school's own directory rather than the district's.
    """
    paths = CMS_CANONICAL_PATHS.get(cms, []) or \
            CMS_CANONICAL_PATHS.get("unknown", [])
    candidates: List[DirectoryCandidate] = []
    p = urlparse(base_url)
    origin = f"{p.scheme}://{p.netloc}"
    # The school subpath itself, e.g. "/schools/lincoln-elementary".
    school_path = (p.path or "").rstrip("/")

    def _add(url: str, weight: float, reason: str) -> None:
        url = _norm_url(url)
        if url in already: return
        already.add(url)
        c = DirectoryCandidate(url=url)
        c.add(weight, reason)
        candidates.append(c)

    for path in paths:
        # 1) Origin-relative (works for site roots, subdomain schools).
        _add(urljoin(origin, path), 0.4,
              f"canonical path for CMS {cms!r}: {path}")
        # 2) School-path-relative (works for path-based per-school pages
        #    under a district). Higher score — matches the *school*, not
        #    the district, so it beats the origin variant on ties.
        if school_path and school_path != "":
            rel = path.lstrip("/")
            _add(f"{origin}{school_path}/{rel}", 0.55,
                  f"canonical path for CMS {cms!r} at school subpath: "
                  f"{school_path}/{rel}")
    return candidates


# ---------------------------------------------------------------------------
# Verification: fetch each candidate and classify_page
# ---------------------------------------------------------------------------

_MAILTO_RE = re.compile(r"^mailto:", re.I)


def _verify_candidate(fetch: Callable[[str], Optional[str]],
                      cand: DirectoryCandidate) -> DirectoryCandidate:
    """Fetch the candidate URL and update it with classification +
    person-count hint. The classification decides whether we trust
    this candidate at all."""
    html = fetch(cand.url)
    if not html:
        cand.add(-0.5, "fetch failed / non-HTML response")
        return cand
    if BeautifulSoup is None:
        cand.verified = True
        return cand
    soup = BeautifulSoup(html, "lxml")
    cand.classification = classify_page(soup, cand.url)
    cand.verified = True
    # Cheap person-count hint (mailto count).
    cand.person_count_hint = len(soup.find_all("a", href=_MAILTO_RE))

    ptype = cand.classification.page_type
    conf = cand.classification.confidence
    if ptype == PageType.STAFF_DIRECTORY:
        cand.add(0.4 * conf, f"classified STAFF_DIRECTORY (conf {conf:.2f})")
    elif ptype == PageType.STAFF_PROFILE:
        cand.add(0.05, "classified STAFF_PROFILE (single person; not a "
                       "directory but keep as fallback)")
    elif ptype == PageType.DEPARTMENT_INDEX:
        cand.add(0.15, f"classified DEPARTMENT_INDEX (conf {conf:.2f}) — "
                       "may recurse")
    elif ptype in (PageType.LOGIN, PageType.ERROR):
        cand.add(-0.7, f"classified {ptype.value} — reject")
    else:
        cand.add(-0.2, f"classified {ptype.value} (not a directory)")

    if cand.person_count_hint >= 5:
        cand.add(0.15, f"page has {cand.person_count_hint} mailto links")

    return cand


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def discover_staff_directories(
    homepage_url: str,
    fetch: Callable[[str], Optional[str]],
    *,
    max_candidates: int = 20,
    max_verify: int = 8,
    min_verified_score: float = 0.35,
    top_n: int = 3,
    follow_department_index: bool = True,
) -> Tuple[List[DirectoryCandidate], DiscoveryReport]:
    """Discover staff-directory URLs starting from a homepage.

    Parameters
    ----------
    homepage_url
        District or school homepage URL.
    fetch
        Callable ``fetch(url) -> Optional[str]``. Plug in your existing
        Fetcher.get, a CachedFetcher, or plain requests.
    max_candidates
        Cap on candidates gathered from the mining stages (before
        verification). Prevents explosion on sitemap-heavy sites.
    max_verify
        Cap on candidates we actually HTTP-fetch to verify.
    min_verified_score
        Verified candidates below this score are dropped.
    top_n
        Return at most this many candidates, highest-score first.
    follow_department_index
        If the top candidate is DEPARTMENT_INDEX, mine its links for
        per-department directories and add them as candidates.
    """
    report = DiscoveryReport(homepage=homepage_url)

    # Fetch homepage.
    html = fetch(homepage_url)
    if not html:
        report.homepage_status = "unreachable"
        report.warnings.append("homepage fetch returned nothing")
        return [], report
    report.homepage_status = "ok"

    if BeautifulSoup is None:
        raise RuntimeError("bs4 is required")
    soup = BeautifulSoup(html, "lxml")

    # Detect CMS from the homepage.
    cms = detect_cms(html, homepage_url)
    report.cms_detected = cms

    # Gather candidates from all stages.
    seen_urls: Set[str] = set()
    all_candidates: List[DirectoryCandidate] = []
    all_candidates.extend(_probe_cms_paths(fetch, homepage_url, cms,
                                            seen_urls))
    all_candidates.extend(_mine_nav_links(soup, homepage_url))
    for c in _mine_body_links(soup, homepage_url, seen_urls):
        all_candidates.append(c)
    # Nav mining doesn't populate `seen_urls` — collapse now.
    all_candidates = _collapse_dupes(all_candidates)

    # Sitemap (best-effort; many sites have it).
    all_candidates.extend(_mine_sitemap(fetch, homepage_url, seen_urls))
    all_candidates = _collapse_dupes(all_candidates)

    # Homepage is itself sometimes the "directory" (small schools).
    _include_homepage_as_candidate(all_candidates, homepage_url, soup)

    all_candidates = _collapse_dupes(all_candidates)
    report.candidates_scanned = len(all_candidates)

    # Cap before we start paying for HTTP.
    all_candidates.sort(key=lambda c: -c.score)
    all_candidates = all_candidates[:max_candidates]

    # Verify by fetching + classify_page.
    verified: List[DirectoryCandidate] = []
    for cand in all_candidates[:max_verify]:
        v = _verify_candidate(fetch, cand)
        verified.append(v)
    report.candidates_verified = len(verified)

    # Optionally recurse into a top-scoring DEPARTMENT_INDEX.
    if follow_department_index:
        for v in list(verified):
            if v.classification and \
                    v.classification.page_type == PageType.DEPARTMENT_INDEX \
                    and v.score >= 0.4:
                extras = _recurse_department_index(fetch, v.url, seen_urls)
                for e in extras:
                    verified.append(_verify_candidate(fetch, e))
                    report.candidates_verified += 1

    # Filter + sort.
    kept = [v for v in verified if v.score >= min_verified_score]
    kept.sort(key=lambda c: -c.score)

    if not kept and verified:
        report.warnings.append(
            f"no candidate scored >= {min_verified_score}; "
            f"returning best-effort top {min(top_n, len(verified))}")
        verified.sort(key=lambda c: -c.score)
        return verified[:top_n], report
    return kept[:top_n], report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collapse_dupes(cands: List[DirectoryCandidate]) -> List[DirectoryCandidate]:
    """Same URL from multiple stages -> sum scores, merge reasons."""
    by_url: dict = {}
    for c in cands:
        key = _norm_url(c.url)
        if key in by_url:
            e = by_url[key]
            e.score += c.score
            e.reasons.extend(c.reasons)
        else:
            c.url = key
            by_url[key] = c
    return list(by_url.values())


def _include_homepage_as_candidate(cands: List[DirectoryCandidate],
                                    homepage_url: str, soup) -> None:
    """Small-school sites sometimes put the staff list directly on the
    homepage. Add the homepage as a low-baseline candidate — the
    verification pass will reject it if it's not classified as one."""
    if not soup: return
    mailto_count = len(soup.find_all("a", href=_MAILTO_RE))
    if mailto_count < 3: return
    key = _norm_url(homepage_url)
    if any(c.url == key for c in cands): return
    c = DirectoryCandidate(url=key)
    c.add(0.15, f"homepage has {mailto_count} mailto links; "
                "may itself be the directory")
    cands.append(c)


def _recurse_department_index(fetch: Callable[[str], Optional[str]],
                               dept_url: str,
                               already: Set[str]
                               ) -> List[DirectoryCandidate]:
    html = fetch(dept_url)
    if not html or BeautifulSoup is None: return []
    soup = BeautifulSoup(html, "lxml")
    # Grab links whose text/URL still mentions staff/faculty/directory.
    extras: List[DirectoryCandidate] = []
    base_for_join = _dir_form(dept_url)
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or _looks_skippable(href): continue
        abs_url = _norm_url(urljoin(base_for_join, href))
        if abs_url in already: continue
        text = a.get_text(" ", strip=True)
        tw, treason = _score_link_text(text)
        uw, ureason = _score_url_tokens(abs_url)
        s = max(tw, uw) * 0.9
        if s < 0.5: continue
        already.add(abs_url)
        c = DirectoryCandidate(url=abs_url)
        c.add(s, treason or ureason)
        c.add(0.1, "linked from a DEPARTMENT_INDEX page")
        extras.append(c)
    return extras[:12]
