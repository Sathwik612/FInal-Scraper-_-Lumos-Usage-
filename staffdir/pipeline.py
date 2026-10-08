"""
staffdir.pipeline
=================

**End-to-end orchestrator.** Input: an iterable of ``(name, website)``
records (or a CSV with those columns). Output: two CSV files —

  * ``<out_prefix>_directories.csv`` — one row per input site with the
    discovered staff-directory URL(s), page classification, and any
    warnings. Useful audit trail and lets you fix bad rows and re-run.
  * ``<out_prefix>_leads.csv`` — one row per extracted person, with
    the standard schema your existing pipeline expects
    (district, school, first_name, last_name, email, title,
    lead_type, department, ...).

Key properties:
  * **Checkpointed** — rows are appended to disk as they finish, so a
    kill / crash / Ctrl-C never loses completed work.
  * **Parallel** — uses ThreadPoolExecutor with a configurable worker
    count. Per-host rate limiting is handled by the underlying
    fetcher.
  * **Resumable** — pass ``resume=True`` and rows already in the
    output CSV are skipped.
  * **Pluggable fetcher** — bring your own callable ``fetch(url) ->
    Optional[str]``. Defaults to a sensible ``requests.Session``.

Usage
-----
::

    from staffdir.pipeline import run_pipeline
    run_pipeline("districts.csv",
                 out_prefix="texas_run1",
                 name_col="district_name",
                 url_col="website",
                 workers=16)

Or programmatically::

    records = [("Foo ISD", "https://foo.edu"),
               ("Bar School District", "https://bar.k12.us"),]
    run_pipeline(records, out_prefix="test", workers=4)
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set, Tuple, Union

try:
    import requests                            # type: ignore
except Exception:                               # pragma: no cover
    requests = None

from .directory import extract_staff_directory, _dedupe_persons
from .playwright_fallback import (
    extract_with_playwright, is_playwright_available,
)
from .discover  import (
    DirectoryCandidate, DiscoveryReport,
    discover_staff_directories,
)
from .schema    import Person
from .schools   import SchoolLink, discover_schools
from .leads     import classify_lead, deduplicate_leads, normalize_title, score_lead


logger = logging.getLogger("staffdir.pipeline")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# URL dedup cache — parse each unique directory URL only once
# ---------------------------------------------------------------------------

class _ExtractionCache:
    """Thread-safe cache for directory extraction results.

    When multiple districts or schools point at the same directory URL (which
    is common — many district sites have a single central staff page linked
    from every school's nav), parsing the HTML for every reference wastes
    significant time.  This cache ensures the expensive extraction runs once
    per unique URL; subsequent callers get a shallow copy of the result list.
    """
    def __init__(self):
        self._lock = threading.Lock()
        # url (normalized) -> (persons list, extraction report)
        self._cache: dict = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _norm(url: str) -> str:
        """Normalize URL for cache lookup — lowercase host, strip trailing
        slash, remove fragment."""
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        return urlunsplit((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            p.query,
            "",   # drop fragment
        ))

    def get(self, url: str):
        """Return cached (persons, report) or None."""
        key = self._norm(url)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._hits += 1
                # Return shallow copies so callers can't mutate shared state
                persons, report = entry
                return list(persons), report
            self._misses += 1
            return None

    def put(self, url: str, persons, report):
        key = self._norm(url)
        with self._lock:
            self._cache[key] = (list(persons), report)

    @property
    def stats(self):
        with self._lock:
            return {"hits": self._hits, "misses": self._misses,
                    "cached_urls": len(self._cache)}


# ---------------------------------------------------------------------------
# Column schemas (kept compatible with existing exporters)
# ---------------------------------------------------------------------------

DIRECTORY_COLS = [
    "input_name", "input_website",
    "directory_url", "directory_score", "page_type",
    "page_type_confidence", "person_count_hint",
    "cms_detected", "n_candidates_scanned", "n_candidates_verified",
    "reasons", "warnings",
]

LEAD_COLS = [
    "district_name", "district_url", "school_name", "school_url",
    "staff_name", "first_name", "last_name", "title", "normalized_title",
    "department", "lead_class", "lead_score", "lead_priority", "email", "phone",
    "extension", "photo_url", "profile_url", "directory_url",
    "contact_id", "contact_name", "source_url", "source_type",
    "confidence", "organization_level",
    "district_status", "district_issue",
    # Legacy aliases retained for downstream consumers of the additive
    # package.
    "district", "school", "overall_confidence", "lead_type",
]

# District-pipeline schemas — one row per (district, school) with what
# was discovered + what was extracted.
SCHOOL_COLS = [
    "district_name", "district_website",
    "school_name", "school_url", "school_score", "is_subdomain",
    "directory_url", "directory_score", "page_type",
    "person_count_hint", "cms_detected", "n_persons_extracted",
    "organization_level", "district_status", "district_issue", "warnings",
]

DISTRICT_STATUS_COLS = [
    "district_name", "district_url", "district_status", "district_issue",
    "schools_discovered", "schools_with_directories",
    "district_directories_found", "total_directories_found",
    "people_extracted", "valid_leads", "high_value_leads",
    "decision_makers", "teaching_staff", "support_staff",
    "leads_with_email", "leads_with_phone", "runtime_seconds",
]


# ---------------------------------------------------------------------------
# Default fetcher (requests) — plug in your own for anything more serious
# ---------------------------------------------------------------------------

def default_fetcher(user_agent: str = "staffdir/1.0",
                     timeout: int = 20, verify_ssl: bool = False
                     ) -> Callable[[str], Optional[str]]:
    """Build a threadsafe fetch(url) callable backed by a requests
    session. Returns None on any failure, so callers don't need to
    handle exceptions.

    For production crawls consider passing a wrapper around the legacy
    Fetcher (retries + rate limits + robots.txt awareness) or a
    CachedFetcher (for dev iteration).
    """
    if requests is None:
        raise RuntimeError("requests must be installed for default_fetcher")

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.verify = verify_ssl
    if not verify_ssl:
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass

    def fetch(url: str) -> Optional[str]:
        if not url or not url.startswith(("http://", "https://")):
            return None
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400: return None
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            return r.text
        except Exception:
            return None

    return fetch


# ---------------------------------------------------------------------------
# Per-row work
# ---------------------------------------------------------------------------

@dataclass
class RowResult:
    input_name: str
    input_website: str
    directory_rows: List[dict] = field(default_factory=list)
    lead_rows: List[dict] = field(default_factory=list)
    error: Optional[str] = None


def _classify_lead_if_available(title, role, emp_type, district, school):
    """Best-effort classify_lead using legacy scraper if importable."""
    try:
        from master_staff_directory_scraper import classify_lead   # type: ignore
        return classify_lead(title, role, emp_type, district, school)
    except Exception:
        # Cheap standalone fallback: 4-bucket by title keywords.
        t = (title or "").lower()
        if "superintendent" in t or "trustee" in t or "board" in t:
            return "Dist"
        if "principal" in t or "dean" in t:
            return "School"
        if any(w in t for w in ("teacher", "instructor", "professor",
                                 "faculty")):
            return "Teacher"
        return "Other"


def _person_to_lead_row(
        p: Person, *, district: str, school: str, directory_url: str,
        district_url: str = "", school_url: str = "",
        organization_level: str = "SCHOOL",
        district_status: str = "", district_issue: str = "") -> dict:
    # Try legacy infer_role for a title-role split; fall back gracefully.
    role, dept_inferred, emp_type = "", "", "OTHER"
    try:
        from master_staff_directory_scraper import (              # type: ignore
            infer_role, EmployeeType)
        role, dept_inferred, emp_type_enum = infer_role(
            p.name.value if p.name else "",
            p.title.value if p.title else "",
            p.email.value if p.email else None,
            directory_url, school)
        emp_type = emp_type_enum.value if hasattr(emp_type_enum, "value") \
            else str(emp_type_enum)
    except Exception:
        pass

    title = p.title.value if p.title else role
    lead_class = classify_lead(title)
    row = {
        "district_name": district,
        "district_url": district_url,
        "school_name": school if organization_level == "SCHOOL" else "",
        "school_url": school_url if organization_level == "SCHOOL" else "",
        "staff_name": p.name.value if p.name else "",
        "first_name": p.first_name.value if p.first_name else "",
        "last_name": p.last_name.value if p.last_name else "",
        "title": title,
        "normalized_title": normalize_title(title),
        "lead_class": lead_class,
        "lead_score": 0,
        "lead_priority": "C",
        "email": p.email.value if p.email else "",
        "phone": p.phone.value if p.phone else "",
        "extension": p.extension.value if p.extension else "",
        "photo_url": p.photo_url.value if p.photo_url else "",
        "profile_url": p.profile_url.value if p.profile_url else "",
        "directory_url": directory_url,
        "source_url": p.source_url or directory_url,
        "contact_id": p.contact_id.value if p.contact_id else "",
        "contact_name": p.contact_name.value if p.contact_name else "",
        "source_type": (
            "contact_list_pro"
            if p.name and p.name.source.value == "contact_list_pro"
            else "finalsite_constituent"
            if p.name and p.name.source.value == "finalsite_constituent"
            else "staff_directory"
        ),
        "confidence": round(p.overall_confidence, 3),
        "organization_level": organization_level,
        "district_status": district_status,
        "district_issue": district_issue,
        # Legacy aliases retained for existing consumers.
        "district": district,
        "school": school if organization_level == "SCHOOL" else "",
        "overall_confidence": round(p.overall_confidence, 3),
        "lead_type": _classify_lead_if_available(
            title, role, emp_type, district, school),
        "department": p.department.value if p.department else dept_inferred,
    }
    row["lead_score"] = score_lead(row, confirmed_directory=True)
    row["lead_priority"] = (
        "A" if row["lead_score"] >= 80 else
        "B" if row["lead_score"] >= 60 else "C"
    )
    return row


def _needs_playwright_fallback(persons: List[Person],
                                *, min_missing_emails: int = 2,
                                min_missing_ratio: float = 0.20
                                ) -> bool:
    """Trigger rule for the JS-rendered fallback tier.

    We escalate to Playwright in any of these cases:

    1. The static extractor returned nothing at all — page is JS-only.
    2. Every static record is missing an email — the classic "list of
       names with no contact info" signal.
    3. A meaningful fraction of static records are missing emails —
       typical Finalsite pattern where some people publish their
       email on the card (visible mailto:) and others hide it behind
       a profile-modal click. Without this rule the fallback silently
       skipped every such site because rule #2 was `False`.

    The fraction thresholds (30% missing AND at least 3 people missing)
    are tuned to avoid firing on directories where one or two staff
    genuinely have no published email — the browser cost isn't worth
    it for that.
    """
    if not persons:
        return True
    missing = sum(1 for p in persons if not p.email)
    if missing == 0:
        return False
    if missing == len(persons):
        # Rule #2 — every record missing.
        return True
    # Rule #3 — meaningful fraction missing.
    return (missing >= min_missing_emails and
            missing / len(persons) >= min_missing_ratio)


def _merge_playwright_records(base: List[Person],
                              extra: List[Person]) -> List[Person]:
    """Union of ``base`` (static) + ``extra`` (playwright) via the
    canonical dedupe from ``staffdir.directory``. When both tiers
    saw the same person, ``Field.better_of`` keeps the higher-
    confidence values, so a static record enriched by a JS-found
    email survives with both fields intact."""
    if not extra:
        return base
    return _dedupe_persons(list(base) + list(extra))


def _process_one(name: str, website: str,
                 fetch: Callable[[str], Optional[str]],
                 *, max_dirs_per_site: int = 3,
                 max_pages_per_dir: int = 1,
                 max_directory_pages: int = 20,
                 extraction_cache: Optional[_ExtractionCache] = None,
                 use_playwright_fallback: bool = True,
                 playwright_max_pages: int = 500,
                 playwright_timeout: int = 300,
                 ) -> RowResult:
    result = RowResult(input_name=name, input_website=website)
    if not website or not website.startswith(("http://", "https://")):
        result.error = "no valid website"
        result.directory_rows.append(_dir_row(name, website, None, None, None))
        return result

    try:
        candidates, disc_report = discover_staff_directories(
            website, fetch, top_n=max_dirs_per_site)
    except Exception as exc:
        result.error = f"discover raised: {exc}"
        result.directory_rows.append(_dir_row(name, website, None, None,
                                               error=str(exc)))
        return result

    if not candidates:
        result.directory_rows.append(_dir_row(name, website, None,
                                               disc_report, None))
        return result

    playwright_available = (use_playwright_fallback
                            and is_playwright_available())

    for cand in candidates:
        # Check the extraction cache first — if another row already
        # parsed this exact directory URL, reuse the results.
        cached = extraction_cache.get(cand.url) if extraction_cache else None
        if cached is not None:
            persons, extract_report = cached
            logger.debug("%s: cache hit for %s (%d persons)",
                         name, cand.url, len(persons))
        else:
            # Fetch + extract.
            html = fetch(cand.url)
            if not html:
                result.directory_rows.append(_dir_row(name, website, cand,
                                                        disc_report,
                                                        error="fetch failed"))
                continue
            try:
                persons, extract_report = extract_staff_directory(
                    html, cand.url, fetch=fetch,
                    max_directory_pages=max_directory_pages)
            except Exception as exc:
                result.directory_rows.append(_dir_row(name, website, cand,
                                                        disc_report,
                                                        error=str(exc)))
                continue

            # ---- Playwright fallback tier -------------------------------
            # Fires when the HTML tier came up empty, missed every
            # email, or missed a meaningful fraction. All three
            # Playwright techniques are attempted inside
            # `extract_with_playwright`; results are merged with the
            # static ones via `_dedupe_persons` so a static record
            # gains an email without becoming a duplicate row.
            if playwright_available and _needs_playwright_fallback(persons):
                static_count = len(persons)
                static_emails = sum(1 for p in persons if p.email)
                logger.info(
                    "%s: static tier returned %d persons / %d emails "
                    "(%d missing) — escalating %s to playwright fallback",
                    name, static_count, static_emails,
                    static_count - static_emails, cand.url)
                pw_persons, pw_report = extract_with_playwright(
                    cand.url, max_pages=playwright_max_pages,
                    timeout_seconds=playwright_timeout)
                if pw_persons:
                    persons = _merge_playwright_records(persons, pw_persons)
                    new_emails = sum(1 for p in persons if p.email)
                    logger.info(
                        "%s: playwright fallback recovered %d emails "
                        "(now %d / %d persons have email)",
                        name, new_emails - static_emails,
                        new_emails, len(persons))
                    extract_report.strategies_tried.extend(
                        pw_report.strategies_tried)
                    for k, v in pw_report.strategy_results.items():
                        extract_report.strategy_results[k] = v
                    extract_report.warnings.extend(pw_report.warnings)
                    extract_report.js_rendered = True
                    extract_report.persons_returned = len(persons)
                    extract_report.emails_found = sum(
                        1 for p in persons if p.email)
                    extract_report.phones_found = sum(
                        1 for p in persons if p.phone)
                    extract_report.merged_records = len(persons)
                    # Reflect the rescue in strategy_used so audit CSV
                    # readers can tell which rows needed the JS tier.
                    if extract_report.strategy_used in (
                            "best_effort", "", "rejected_page_type"):
                        extract_report.strategy_used = "playwright_fallback"
                    else:
                        extract_report.strategy_used = (
                            f"{extract_report.strategy_used}+"
                            "playwright_fallback")
                else:
                    for w in pw_report.warnings:
                        extract_report.warnings.append(w)

            # Store in cache for other rows that discover the same URL.
            # Cache the enriched result so parallel rows don't re-pay
            # the Playwright cost.
            if extraction_cache is not None:
                extraction_cache.put(cand.url, persons, extract_report)

        result.directory_rows.append(_dir_row(name, website, cand,
                                                disc_report,
                                                extract_report=extract_report))
        for p in persons:
            result.lead_rows.append(_person_to_lead_row(
                p, district=name, school=name, directory_url=cand.url))
    return result


def _dir_row(name, website, cand: Optional[DirectoryCandidate],
             disc_report: Optional[DiscoveryReport],
             extract_report=None, error: Optional[str] = None) -> dict:
    return {
        "input_name":    name,
        "input_website": website,
        "directory_url": cand.url if cand else "",
        "directory_score": round(cand.score, 3) if cand else 0.0,
        "page_type":     (cand.classification.page_type.value
                          if cand and cand.classification else ""),
        "page_type_confidence": (round(cand.classification.confidence, 3)
                                  if cand and cand.classification else 0.0),
        "person_count_hint": cand.person_count_hint if cand else 0,
        "cms_detected":  disc_report.cms_detected if disc_report else "",
        "n_candidates_scanned":  disc_report.candidates_scanned if disc_report else 0,
        "n_candidates_verified": disc_report.candidates_verified if disc_report else 0,
        "reasons":  " | ".join(cand.reasons[:5]) if cand else "",
        "warnings": " | ".join(filter(None, [
            error or "",
            " | ".join(disc_report.warnings) if disc_report else "",
            " | ".join(extract_report.warnings) if extract_report else "",
        ])),
    }


# ---------------------------------------------------------------------------
# CSV output — checkpointed, thread-safe
# ---------------------------------------------------------------------------

class _CsvSink:
    """Append-only CSV writer with a lock. Writes header on first
    open, then appends rows as they arrive."""
    def __init__(self, path: str, columns: List[str]):
        self.path = path
        self.columns = columns
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".",
                    exist_ok=True)
        need_header = not os.path.exists(path) or os.path.getsize(path) == 0
        if need_header:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                csv.DictWriter(fh, fieldnames=columns).writeheader()

    def append(self, rows: List[dict]) -> None:
        if not rows: return
        with self._lock, open(self.path, "a", newline="",
                                encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=self.columns,
                               extrasaction="ignore")
            for r in rows:
                w.writerow(r)

    def existing_keys(self, key_cols: List[str]) -> Set[Tuple[str, ...]]:
        """Read back the CSV to find already-processed keys (used for
        resume)."""
        keys: Set[Tuple[str, ...]] = set()
        if not os.path.exists(self.path): return keys
        try:
            with open(self.path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    keys.add(tuple((row.get(c) or "") for c in key_cols))
        except Exception:
            pass
        return keys


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def _load_input(source: Union[str, Iterable[Tuple[str, str]]],
                name_col: str, url_col: str
                ) -> List[Tuple[str, str]]:
    if isinstance(source, str):
        rows: List[Tuple[str, str]] = []
        with open(source, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in (name_col, url_col)
                       if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"CSV {source!r} missing columns: "
                                 f"{missing}. Available: "
                                 f"{reader.fieldnames}")
            for r in reader:
                name = (r.get(name_col) or "").strip()
                url  = (r.get(url_col)  or "").strip()
                if not name and not url: continue
                if url and not url.startswith(("http://", "https://")):
                    url = "https://" + url
                rows.append((name, url))
        return rows
    return [(n, u) for (n, u) in source]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    source: Union[str, Iterable[Tuple[str, str]]],
    *,
    out_prefix: str = "staffdir_run",
    name_col: str = "name",
    url_col: str = "website",
    workers: int = 8,
    fetch: Optional[Callable[[str], Optional[str]]] = None,
    max_dirs_per_site: int = 3,
    max_directory_pages: int = 20,
    resume: bool = False,
    verify_ssl: bool = False,
    limit: int = 0,
    log_every: int = 5,
    use_playwright_fallback: bool = True,
    playwright_max_pages: int = 500,
    playwright_timeout: int = 300,
) -> Tuple[str, str]:
    """Run discovery + extraction for every input row. Returns
    ``(directories_csv_path, leads_csv_path)``.

    Parameters
    ----------
    source:
        Either a CSV path or an iterable of ``(name, website)`` tuples.
    out_prefix:
        Output CSVs are ``<out_prefix>_directories.csv`` and
        ``<out_prefix>_leads.csv``.
    name_col / url_col:
        Column names in the input CSV.
    workers:
        Thread count. Discovery + extraction are network-bound; 8-16
        is usually a good range.
    fetch:
        Callable ``fetch(url) -> Optional[str]``. If None, a
        ``default_fetcher(verify_ssl=verify_ssl)`` is built.
    max_dirs_per_site:
        A single site can legitimately have several directory pages
        (per department, per campus). Cap how many to process per row.
    resume:
        Skip input rows already present in ``<out_prefix>_directories.csv``.
    limit:
        Process only the first N input rows (0 = all).
    """
    records = _load_input(source, name_col, url_col)
    if limit: records = records[:limit]
    if fetch is None:
        fetch = default_fetcher(verify_ssl=verify_ssl)

    dirs_path  = f"{out_prefix}_directories.csv"
    leads_path = f"{out_prefix}_leads.csv"
    dirs_sink  = _CsvSink(dirs_path,  DIRECTORY_COLS)
    leads_sink = _CsvSink(leads_path, LEAD_COLS)

    already: Set[Tuple[str, str]] = set()
    if resume:
        already = {(k[0], k[1])
                   for k in dirs_sink.existing_keys(["input_name",
                                                      "input_website"])}
        if already:
            records = [(n, u) for (n, u) in records if (n, u) not in already]
            logger.info("resume: skipping %d rows already processed",
                        len(already))

    # --- URL dedup: pre-scan input to log duplicate websites -----------
    from collections import Counter
    url_counts = Counter(u.rstrip("/").lower() for _, u in records if u)
    dupes = {u: c for u, c in url_counts.items() if c > 1}
    if dupes:
        logger.info("url dedup: %d unique URLs across %d input rows "
                     "(%d URLs appear more than once — extraction will be "
                     "cached)", len(url_counts), len(records), len(dupes))
    extraction_cache = _ExtractionCache()

    logger.info("processing %d rows (workers=%d) -> %s + %s",
                len(records), workers, dirs_path, leads_path)
    if use_playwright_fallback:
        if is_playwright_available():
            logger.info(
                "playwright fallback: ENABLED (max_pages=%d, timeout=%ds). "
                "Sites where the static HTML tier finds no emails will be "
                "re-scraped with headless Chromium using direct-mailto, "
                "hidden-icon and finalsite-modal techniques.",
                playwright_max_pages, playwright_timeout)
        else:
            logger.warning(
                "playwright fallback: REQUESTED but playwright is not "
                "installed. Install with `pip install playwright && "
                "playwright install chromium` to catch JS-rendered "
                "directories.")
    else:
        logger.info("playwright fallback: DISABLED by caller")

    started = time.time()
    done = 0
    total_leads = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(_process_one, n, u, fetch,
                              max_dirs_per_site=max_dirs_per_site,
                              max_directory_pages=max_directory_pages,
                              extraction_cache=extraction_cache,
                              use_playwright_fallback=use_playwright_fallback,
                              playwright_max_pages=playwright_max_pages,
                              playwright_timeout=playwright_timeout): (n, u)
                   for (n, u) in records}
        for fut in as_completed(futures):
            n, u = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                logger.warning("%s (%s): unhandled exception %s", n, u, exc)
                dirs_sink.append([_dir_row(n, u, None, None, error=str(exc))])
                done += 1
                continue
            dirs_sink.append(result.directory_rows)
            leads_sink.append(result.lead_rows)
            total_leads += len(result.lead_rows)
            done += 1
            if done % log_every == 0 or done == len(records):
                rate = done / max(1e-6, time.time() - started)
                logger.info("progress %d/%d (%.1f rows/s) — %d leads so far",
                            done, len(records), rate, total_leads)

    cache_stats = extraction_cache.stats
    logger.info("done in %.1fs. %d rows -> %d leads. "
                "extraction cache: %d unique URLs, %d hits, %d misses. "
                "outputs:\n  %s\n  %s",
                time.time() - started, len(records), total_leads,
                cache_stats["cached_urls"], cache_stats["hits"],
                cache_stats["misses"],
                os.path.abspath(dirs_path), os.path.abspath(leads_path))
    return dirs_path, leads_path


# ===========================================================================
# District-level pipeline: district -> schools -> per-school directories
# ===========================================================================

@dataclass
class _DistrictResult:
    district_name: str
    district_website: str
    school_rows: List[dict] = field(default_factory=list)
    lead_rows: List[dict] = field(default_factory=list)
    district_status: str = "NO_LEADS"
    district_issue: str = ""
    schools_discovered: int = 0
    schools_with_directories: int = 0
    district_directories_found: int = 0
    total_directories_found: int = 0
    people_extracted: int = 0
    duplicates_removed: int = 0
    runtime_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)


def _process_district(district_name: str, district_website: str,
                      fetch: Callable[[str], Optional[str]],
                      *, max_schools_per_district: int,
                      max_dirs_per_school: int,
                      inner_workers: int = 8,
                      per_district_time_budget: float = 600.0
                      ) -> _DistrictResult:
    """Discover schools in a district; then, IN PARALLEL, per school:
    discover its staff directory and extract people. Returns rows for
    two CSVs.

    ``per_district_time_budget`` is a soft wall-clock cap. Once
    exceeded, we stop scheduling new schools; already-running ones
    finish. Prevents one broken district from stalling the whole run.
    """
    result = _DistrictResult(district_name=district_name,
                              district_website=district_website)
    if not district_website or not district_website.startswith(("http://",
                                                                  "https://")):
        result.school_rows.append(_school_row(district_name, district_website,
            None, None, None, warnings="no valid website"))
        return result

    # ---- Stage A: find the schools -------------------------------------
    try:
        schools, sreport = discover_schools(
            district_website, fetch,
            max_schools=max_schools_per_district)
    except Exception as exc:
        result.school_rows.append(_school_row(
            district_name, district_website, None, None, None,
            warnings=f"school-discovery raised: {exc}"))
        return result

    if not schools:
        result.school_rows.append(_school_row(
            district_name, district_website, None, None, None,
            warnings=" | ".join(sreport.warnings) or
                     "no schools discovered"))
        return result

    # ---- Stage B: per-school discovery + extraction, IN PARALLEL -------
    started_at = time.time()

    def _one_school(school: SchoolLink) -> Tuple[SchoolLink, List[dict], List[dict]]:
        srows: List[dict] = []
        lrows: List[dict] = []
        try:
            cands, dreport = discover_staff_directories(
                school.url, fetch, top_n=max_dirs_per_school)
        except Exception as exc:
            srows.append(_school_row(district_name, district_website,
                school, None, None,
                warnings=f"directory-discovery raised: {exc}"))
            return school, srows, lrows

        if not cands:
            srows.append(_school_row(district_name, district_website,
                school, None, dreport,
                warnings=" | ".join(dreport.warnings) or
                         "no directory candidates"))
            return school, srows, lrows

        for cand in cands:
            html = fetch(cand.url)
            if not html:
                srows.append(_school_row(district_name, district_website,
                    school, cand, dreport,
                    warnings="directory fetch failed"))
                continue
            try:
                persons, ereport = extract_staff_directory(
                    html, cand.url, fetch=fetch)
            except Exception as exc:
                srows.append(_school_row(district_name, district_website,
                    school, cand, dreport,
                    warnings=f"extraction raised: {exc}"))
                continue

            # Same escalation rule as _process_one: if static tier
            # yielded nothing usable, run the Playwright fallback tier.
            if is_playwright_available() and \
                    _needs_playwright_fallback(persons):
                pw_persons, pw_report = extract_with_playwright(
                    cand.url, max_pages=500, timeout_seconds=300)
                if pw_persons:
                    persons = _merge_playwright_records(persons, pw_persons)
                    ereport.warnings.extend(pw_report.warnings)
                    ereport.js_rendered = True

            srows.append(_school_row(district_name, district_website,
                school, cand, dreport,
                n_persons=len(persons),
                warnings=" | ".join(ereport.warnings)))

            for p in persons:
                lrows.append(_person_to_lead_row(
                    p, district=district_name,
                    school=school.name, directory_url=cand.url))
        return school, srows, lrows

    with ThreadPoolExecutor(max_workers=max(1, inner_workers)) as ex:
        futures = {ex.submit(_one_school, s): s for s in schools}
        # Progress-aware soft deadline. Mirrors the Playwright fallback
        # rule: pass the deadline only closes the door if we've also
        # stopped making progress. A single big district (Baldwin,
        # Birmingham, LAUSD) legitimately needs 5-10 minutes to fully
        # drain across 30+ schools; the old hard-90s cap was cutting
        # those runs off just as they were succeeding.
        last_progress_at = started_at
        last_lead_count = 0
        # No-new-leads window after the deadline before we give up.
        # 60s is enough for one big school to finish; anything longer
        # is either legitimately stuck or genuinely slow enough that
        # the school-level Playwright budget is also failing.
        stuck_after = 60.0

        def _time_up() -> bool:
            nonlocal last_progress_at, last_lead_count
            now = time.time()
            current = len(result.lead_rows)
            if current > last_lead_count:
                last_lead_count = current
                last_progress_at = now
            elapsed = now - started_at
            if elapsed <= per_district_time_budget:
                return False
            stalled = now - last_progress_at
            if stalled >= stuck_after:
                return True
            # Past the deadline but still adding leads — keep going.
            return False

        for fut in as_completed(futures):
            # Progress-aware time check: only cancel if we're both past
            # the deadline AND stalled.
            if _time_up():
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                elapsed = time.time() - started_at
                result.school_rows.append(_school_row(
                    district_name, district_website, None, None, None,
                    warnings=f"per-district budget "
                             f"({per_district_time_budget:.0f}s) exceeded "
                             f"and extraction stalled for {stuck_after:.0f}s "
                             f"(total {elapsed:.0f}s); "
                             f"remaining schools cancelled"))
                break
            try:
                _sc, srows, lrows = fut.result()
            except Exception as exc:
                result.school_rows.append(_school_row(
                    district_name, district_website, None, None, None,
                    warnings=f"school worker raised: {exc}"))
                continue
            result.school_rows.extend(srows)
            result.lead_rows.extend(lrows)

    return result


def _school_row(district_name: str, district_website: str,
                school: Optional[SchoolLink],
                cand: Optional[DirectoryCandidate],
                dreport: Optional[DiscoveryReport],
                *, n_persons: int = 0,
                organization_level: str = "SCHOOL",
                district_status: str = "",
                district_issue: str = "",
                warnings: str = "") -> dict:
    return {
        "district_name":    district_name,
        "district_website": district_website,
        "school_name":      school.name if school else (
            "(District Office)" if organization_level == "DISTRICT" else ""),
        "school_url":       school.url if school else "",
        "school_score":     round(school.score, 3) if school else 0.0,
        "is_subdomain":     bool(school.is_subdomain) if school else False,
        "directory_url":    cand.url if cand else "",
        "directory_score":  round(cand.score, 3) if cand else 0.0,
        "page_type":        (cand.classification.page_type.value
                             if cand and cand.classification else ""),
        "person_count_hint": cand.person_count_hint if cand else 0,
        "cms_detected":     dreport.cms_detected if dreport else "",
        "n_persons_extracted": n_persons,
        "organization_level": organization_level,
        "district_status": district_status,
        "district_issue": district_issue,
        "warnings":         warnings or "",
    }


def _process_district_production(
        district_name: str, district_website: str,
        fetch: Callable[[str], Optional[str]], *,
        max_schools_per_district: int, max_dirs_per_school: int,
        inner_workers: int, per_district_time_budget: float,
         request_timeout: int = 5, max_directory_pages: int = 20,
         extraction_cache: Optional[_ExtractionCache] = None) -> _DistrictResult:
    """Production district run with independent district and school scopes."""
    result = _DistrictResult(district_name, district_website)
    problems: List[str] = []
    started_at = time.time()
    deadline = started_at + per_district_time_budget
    response_cache: dict[str, Optional[str]] = {}

    def budget_fetch(url: str) -> Optional[str]:
        if time.time() >= deadline:
            return None
        cache_key = url.rstrip("/") or url
        if cache_key in response_cache:
            return response_cache[cache_key]
        value = fetch(url)
        response_cache[cache_key] = value
        return value

    if not district_website or not district_website.startswith(
            ("http://", "https://")):
        result.district_status = "PLEASE_CHECK"
        result.district_issue = "District website is missing or invalid"
        result.school_rows.append(_school_row(
            district_name, district_website, None, None, None,
            organization_level="DISTRICT",
            district_status=result.district_status,
            district_issue=result.district_issue,
            warnings=result.district_issue))
        return result

    # District and school discovery are independent. Run them concurrently so
    # a slow district-level probe does not consume the whole school budget.
    district_candidates: List[DirectoryCandidate] = []
    district_report: Optional[DiscoveryReport] = None
    schools: List[SchoolLink] = []
    school_report = None
    with ThreadPoolExecutor(max_workers=2) as scope_ex:
        district_future = scope_ex.submit(
            discover_staff_directories, district_website, budget_fetch,
            top_n=max_dirs_per_school)
        schools_future = scope_ex.submit(
            discover_schools, district_website, budget_fetch,
            max_schools=max_schools_per_district,
            include_district_itself=False)
        try:
            district_candidates, district_report = district_future.result()
            result.district_directories_found = len(district_candidates)
        except Exception as exc:
            problems.append(f"District directory discovery failed: {exc}")
        try:
            schools, school_report = schools_future.result()
        except Exception as exc:
            problems.append(f"School discovery failed: {exc}")

    for cand in district_candidates:
        cached = extraction_cache.get(cand.url) if extraction_cache else None
        if cached is not None:
            persons, ereport = cached
            logger.debug("%s: cache hit for district dir %s",
                         district_name, cand.url)
        else:
            html = budget_fetch(cand.url)
            if not html:
                problems.append("District directory could not be accessed")
                result.school_rows.append(_school_row(
                    district_name, district_website, None, cand, district_report,
                    organization_level="DISTRICT",
                    warnings="district directory fetch failed"))
                continue
            try:
                persons, ereport = extract_staff_directory(
                    html, cand.url, fetch=budget_fetch,
                    max_directory_pages=max_directory_pages)
            except Exception as exc:
                problems.append(f"District directory extraction failed: {exc}")
                result.school_rows.append(_school_row(
                    district_name, district_website, None, cand, district_report,
                    organization_level="DISTRICT",
                    warnings=f"district directory extraction failed: {exc}"))
                continue
            # Same JS-rendered escalation as the site pipeline. The
            # district office is where the highest-value leads live, so
            # never skip the fallback here.
            if is_playwright_available() and \
                    _needs_playwright_fallback(persons):
                pw_persons, pw_report = extract_with_playwright(
                    cand.url, max_pages=500, timeout_seconds=300)
                if pw_persons:
                    persons = _merge_playwright_records(persons, pw_persons)
                    ereport.warnings.extend(pw_report.warnings)
                    ereport.js_rendered = True
                    if ereport.strategy_used in ("best_effort", "",
                                                  "rejected_page_type"):
                        ereport.strategy_used = "playwright_fallback"
            if extraction_cache is not None:
                extraction_cache.put(cand.url, persons, ereport)
        if ereport.strategy_used == "best_effort":
            problems.append(
                "District staff directory had no strategy meeting the "
                "minimum validated-person threshold")
            persons = []
        result.people_extracted += len(persons)
        result.school_rows.append(_school_row(
            district_name, district_website, None, cand, district_report,
            n_persons=len(persons), organization_level="DISTRICT",
            warnings=" | ".join(ereport.warnings)))
        if (not persons and
                ereport.strategy_used != "rejected_page_type"):
            problems.append("District staff directory found but no staff could be extracted")
        for person in persons:
            result.lead_rows.append(_person_to_lead_row(
                person, district=district_name, school="",
                district_url=district_website, school_url="",
                directory_url=cand.url, organization_level="DISTRICT"))

    district_directory_urls = {
        cand.url.rstrip("/").lower() for cand in district_candidates
    }

    result.schools_discovered = len(schools)
    if not schools:
        if school_report is not None and not school_report.homepage_ok:
            problems.append("District website inaccessible")
        else:
            problems.append("No schools discovered from district website")

    if time.time() >= deadline:
        problems.append(
            f"District processing timed out after "
            f"{per_district_time_budget:.0f}s")

    def process_school(school: SchoolLink):
        srows: List[dict] = []
        lrows: List[dict] = []
        raw_people = 0
        has_directory = False
        try:
            candidates, report = discover_staff_directories(
                school.url, budget_fetch, top_n=max_dirs_per_school)
        except Exception as exc:
            return (school, [_school_row(
                district_name, district_website, school, None, None,
                organization_level="SCHOOL",
                warnings=f"directory discovery failed: {exc}")],
                    lrows, raw_people, has_directory,
                    f"School directory discovery failed: {exc}")
        has_directory = bool(candidates)
        # A district-wide directory is commonly linked from every school
        # page. It already ran in the district scope; reusing it here would
        # manufacture school attribution and multiply identical leads.
        candidates = [
            cand for cand in candidates
            if cand.url.rstrip("/").lower() not in district_directory_urls
        ]
        has_directory = bool(candidates)
        if not candidates:
            srows.append(_school_row(
                district_name, district_website, school, None, report,
                organization_level="SCHOOL",
                warnings=" | ".join(report.warnings) or
                         "no school-specific directory candidates"))
            return school, srows, lrows, raw_people, has_directory, ""
        for cand in candidates:
            cached = extraction_cache.get(cand.url) if extraction_cache else None
            if cached is not None:
                persons, ereport = cached
            else:
                html = budget_fetch(cand.url)
                if not html:
                    srows.append(_school_row(
                        district_name, district_website, school, cand, report,
                        organization_level="SCHOOL",
                        warnings="directory fetch failed"))
                    continue
                try:
                    persons, ereport = extract_staff_directory(
                        html, cand.url, fetch=budget_fetch,
                        max_directory_pages=max_directory_pages)
                except Exception as exc:
                    srows.append(_school_row(
                        district_name, district_website, school, cand, report,
                        organization_level="SCHOOL",
                        warnings=f"extraction failed: {exc}"))
                    continue
                if is_playwright_available() and \
                        _needs_playwright_fallback(persons):
                    pw_persons, pw_report = extract_with_playwright(
                        cand.url, max_pages=500, timeout_seconds=300)
                    if pw_persons:
                        persons = _merge_playwright_records(
                            persons, pw_persons)
                        ereport.warnings.extend(pw_report.warnings)
                        ereport.js_rendered = True
                        if ereport.strategy_used in ("best_effort", "",
                                                      "rejected_page_type"):
                            ereport.strategy_used = "playwright_fallback"
                if extraction_cache is not None:
                    extraction_cache.put(cand.url, persons, ereport)
            if ereport.strategy_used == "best_effort":
                persons = []
            raw_people += len(persons)
            srows.append(_school_row(
                district_name, district_website, school, cand, report,
                n_persons=len(persons), organization_level="SCHOOL",
                warnings=" | ".join(ereport.warnings)))
            for person in persons:
                lrows.append(_person_to_lead_row(
                    person, district=district_name, school=school.name,
                    district_url=district_website, school_url=school.url,
                    directory_url=cand.url, organization_level="SCHOOL"))
        return school, srows, lrows, raw_people, has_directory, ""

    if schools:
        with ThreadPoolExecutor(max_workers=max(1, inner_workers)) as ex:
            futures = {ex.submit(process_school, school): school
                       for school in schools}
            # Progress-aware soft deadline — see _process_district for
            # the rationale. The old hard-cap was cutting big districts
            # off just as extraction was finishing.
            last_progress_at = started_at
            last_lead_count = 0
            stuck_after = 60.0

            def _time_up() -> bool:
                nonlocal last_progress_at, last_lead_count
                now = time.time()
                current = len(result.lead_rows)
                if current > last_lead_count:
                    last_lead_count = current
                    last_progress_at = now
                if now - started_at <= per_district_time_budget:
                    return False
                stalled = now - last_progress_at
                return stalled >= stuck_after

            for future in as_completed(futures):
                if _time_up():
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    elapsed = time.time() - started_at
                    problems.append(
                        f"District processing budget "
                        f"({per_district_time_budget:.0f}s) exceeded "
                        f"and extraction stalled for {stuck_after:.0f}s "
                        f"(total {elapsed:.0f}s)")
                    break
                try:
                    _, srows, lrows, raw_count, has_dir, error = future.result()
                except Exception as exc:
                    problems.append(f"School worker failed: {exc}")
                    continue
                result.school_rows.extend(srows)
                result.lead_rows.extend(lrows)
                result.people_extracted += raw_count
                result.total_directories_found += sum(
                    1 for row in srows if row.get("directory_url"))
                if has_dir:
                    result.schools_with_directories += 1
                if error:
                    problems.append(error)

    result.total_directories_found += result.district_directories_found
    if result.total_directories_found == 0:
        problems.append("No usable staff directory candidates found")
    raw_lead_count = len(result.lead_rows)
    result.lead_rows = deduplicate_leads(result.lead_rows)
    result.duplicates_removed = raw_lead_count - len(result.lead_rows)
    if any("timed out" in problem.lower() for problem in problems):
        result.district_status = "PLEASE_CHECK"
    elif result.lead_rows and problems:
        result.district_status = "PARTIAL"
    elif result.lead_rows:
        result.district_status = "SUCCESS"
    elif problems:
        result.district_status = "PLEASE_CHECK"
    else:
        result.district_status = "NO_LEADS"
    result.district_issue = "; ".join(dict.fromkeys(problems))[:500]
    result.runtime_seconds = round(time.time() - started_at, 3)
    for row in result.school_rows:
        row["district_status"] = result.district_status
        row["district_issue"] = result.district_issue
    for row in result.lead_rows:
        row["district_status"] = result.district_status
        row["district_issue"] = result.district_issue
    return result


def _run_district_pipeline_legacy(
    source: Union[str, Iterable[Tuple[str, str]]],
    *,
    out_prefix: str = "staffdir_district_run",
    name_col: str = "name",
    url_col: str = "website",
    workers: int = 4,
    inner_workers: int = 8,
    fetch: Optional[Callable[[str], Optional[str]]] = None,
    max_schools_per_district: int = 30,
    max_dirs_per_school: int = 2,
    per_district_time_budget: float = 600.0,
    resume: bool = False,
    verify_ssl: bool = False,
    limit: int = 0,
    log_every: int = 1,
) -> Tuple[str, str]:
    """Run: **district -> schools -> per-school staff directories ->
    extraction**. Returns ``(schools_csv_path, leads_csv_path)``.

    Concurrency model:
      * ``workers`` districts run in parallel (default 4)
      * within each district, ``inner_workers`` schools are processed
        in parallel (default 8)
      * effective simultaneous HTTP connections = workers × inner_workers
      * ``per_district_time_budget`` caps wall-clock time on a single
        district so a slow/broken site can't stall the whole run

    Output CSVs (both appended-to as work completes, so a Ctrl-C or
    disconnect doesn't lose progress):
      * ``<out_prefix>_schools.csv`` — audit trail: one row per
        (district, school, directory) attempted
      * ``<out_prefix>_leads.csv`` — the deliverable: one row per
        extracted person, stamped with district + school
    """
    records = _load_input(source, name_col, url_col)
    if limit: records = records[:limit]
    if fetch is None:
        fetch = default_fetcher(verify_ssl=verify_ssl)

    schools_path = f"{out_prefix}_schools.csv"
    leads_path   = f"{out_prefix}_leads.csv"
    schools_sink = _CsvSink(schools_path, SCHOOL_COLS)
    leads_sink   = _CsvSink(leads_path,   LEAD_COLS)

    already: Set[Tuple[str, str]] = set()
    if resume:
        already = {(k[0], k[1])
                    for k in schools_sink.existing_keys(
                        ["district_name", "district_website"])}
        if already:
            records = [(n, u) for (n, u) in records if (n, u) not in already]
            logger.info("resume: skipping %d districts already processed",
                        len(already))

    logger.info("processing %d districts (outer=%d workers, "
                 "inner=%d workers, time budget=%.0fs/district)",
                 len(records), workers, inner_workers,
                 per_district_time_budget)
    started = time.time()
    done = 0
    total_leads = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(_process_district, n, u, fetch,
                              max_schools_per_district=max_schools_per_district,
                              max_dirs_per_school=max_dirs_per_school,
                              inner_workers=inner_workers,
                              per_district_time_budget=per_district_time_budget
                              ): (n, u)
                    for (n, u) in records}
        for fut in as_completed(futures):
            n, u = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                logger.warning("%s (%s): unhandled exception %s", n, u, exc)
                schools_sink.append([_school_row(n, u, None, None, None,
                                                  warnings=f"exc:{exc}")])
                done += 1
                continue
            schools_sink.append(res.school_rows)
            leads_sink.append(res.lead_rows)
            total_leads += len(res.lead_rows)
            done += 1
            if done % log_every == 0 or done == len(records):
                elapsed = time.time() - started
                rate = done / max(1e-6, elapsed)
                eta = (len(records) - done) / max(1e-6, rate)
                logger.info("[%d/%d, %.2f dist/s, ETA %.0fs] %s -> "
                             "%d schools, %d leads (total %d)",
                             done, len(records), rate, eta, n[:40],
                             len(res.school_rows), len(res.lead_rows),
                             total_leads)

    logger.info("done in %.1fs. %d districts -> %d total leads.",
                 time.time() - started, len(records), total_leads)
    return schools_path, leads_path


def run_district_pipeline(
    source: Union[str, Iterable[Tuple[str, str]]],
    *,
    out_prefix: str = "staffdir_district_run",
    name_col: str = "name",
    url_col: str = "website",
    workers: int = 4,
    inner_workers: int = 4,
    fetch: Optional[Callable[[str], Optional[str]]] = None,
    max_schools_per_district: int = 30,
    max_dirs_per_school: int = 2,
    max_directory_pages: int = 20,
    per_district_time_budget: float = 600.0,
    request_timeout: int = 5,
    resume: bool = False,
    verify_ssl: bool = False,
    limit: int = 0,
    log_every: int = 1,
) -> Tuple[str, str]:
    """Run the production District -> School -> Staff -> Lead pipeline.

    Returns the established ``(schools_csv, leads_csv)`` pair. The required
    district status CSV and JSON summary are emitted alongside them.
    """
    records = _load_input(source, name_col, url_col)
    if limit:
        records = records[:limit]
    if fetch is None:
        fetch = default_fetcher(timeout=request_timeout, verify_ssl=verify_ssl)

    schools_path = f"{out_prefix}_schools.csv"
    leads_path = f"{out_prefix}_leads.csv"
    status_path = f"{out_prefix}_district_status.csv"
    if not resume:
        for output_path in (schools_path, leads_path, status_path,
                            f"{out_prefix}_summary.json"):
            if os.path.exists(output_path):
                os.remove(output_path)
    schools_sink = _CsvSink(schools_path, SCHOOL_COLS)
    leads_sink = _CsvSink(leads_path, LEAD_COLS)
    status_sink = _CsvSink(status_path, DISTRICT_STATUS_COLS)

    already: Set[Tuple[str, str]] = set()
    if resume:
        already = {(row[0], row[1]) for row in
                   status_sink.existing_keys(["district_name",
                                              "district_url"])}
        records = [(name, url) for name, url in records
                   if (name, url) not in already]
        if already:
            logger.info("resume: skipping %d districts already processed",
                        len(already))

    started = time.time()
    totals = {
        "districts_processed": 0, "schools_discovered": 0,
        "schools_with_staff_directories": 0, "district_directories_found": 0,
        "school_directories_found": 0, "directories_found": 0,
        "people_extracted": 0, "valid_leads": 0, "high_value_leads": 0,
        "decision_makers": 0, "teaching_staff": 0, "support_staff": 0,
        "leads_with_email": 0, "leads_with_phone": 0,
        "leads_with_profile": 0, "duplicates_removed": 0,
        "errors": 0,
    }
    statuses: dict[str, int] = {}
    failure_reasons: dict[str, int] = {}
    extraction_reasons: dict[str, int] = {}

    # Shared extraction cache across all districts — when multiple districts
    # point schools at the same directory URL, extraction runs once.
    extraction_cache = _ExtractionCache()

    logger.info("processing %d districts (workers=%d, inner=%d, budget=%.0fs)",
                len(records), workers, inner_workers, per_district_time_budget)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {
            ex.submit(
                _process_district_production, name, url, fetch,
                max_schools_per_district=max_schools_per_district,
                max_dirs_per_school=max_dirs_per_school,
                inner_workers=inner_workers,
                per_district_time_budget=per_district_time_budget,
                request_timeout=request_timeout,
                max_directory_pages=max_directory_pages,
                extraction_cache=extraction_cache,
            ): (name, url)
            for name, url in records
        }
        for future in as_completed(futures):
            name, url = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.warning("%s: unhandled district exception: %s",
                               name, exc)
                result = _DistrictResult(
                    district_name=name, district_website=url,
                    district_status="ERROR",
                    district_issue="Pipeline software error",
                    errors=[str(exc)],
                )
                result.school_rows.append(_school_row(
                    name, url, None, None, None,
                    district_status="ERROR",
                    district_issue=result.district_issue,
                    warnings=str(exc)))

            schools_sink.append(result.school_rows)
            leads_sink.append(result.lead_rows)
            status_sink.append([{
                "district_name": result.district_name,
                "district_url": result.district_website,
                "district_status": result.district_status,
                "district_issue": result.district_issue,
                "schools_discovered": result.schools_discovered,
                "schools_with_directories": result.schools_with_directories,
                "district_directories_found": result.district_directories_found,
                "total_directories_found": result.total_directories_found,
                "people_extracted": result.people_extracted,
                "valid_leads": len(result.lead_rows),
                "high_value_leads": sum(
                    1 for row in result.lead_rows
                    if row.get("lead_class") in (
                        "HIGH_VALUE", "DECISION_MAKER", "DISTRICT_ADMIN")),
                "decision_makers": sum(
                    1 for row in result.lead_rows
                    if row.get("lead_class") == "DECISION_MAKER"),
                "teaching_staff": sum(
                    1 for row in result.lead_rows
                    if row.get("lead_class") == "TEACHING_STAFF"),
                "support_staff": sum(
                    1 for row in result.lead_rows
                    if row.get("lead_class") == "SUPPORT_STAFF"),
                "leads_with_email": sum(
                    1 for row in result.lead_rows if row.get("email")),
                "leads_with_phone": sum(
                    1 for row in result.lead_rows if row.get("phone")),
                "runtime_seconds": result.runtime_seconds,
            }])
            totals["districts_processed"] += 1
            totals["schools_discovered"] += result.schools_discovered
            totals["schools_with_staff_directories"] += (
                result.schools_with_directories)
            totals["district_directories_found"] += (
                result.district_directories_found)
            totals["directories_found"] += result.total_directories_found
            totals["school_directories_found"] += (
                result.total_directories_found -
                result.district_directories_found)
            totals["people_extracted"] += result.people_extracted
            totals["valid_leads"] += len(result.lead_rows)
            totals["duplicates_removed"] += result.duplicates_removed
            totals["high_value_leads"] += sum(
                1 for row in result.lead_rows
                if row.get("lead_class") in (
                    "HIGH_VALUE", "DECISION_MAKER", "DISTRICT_ADMIN"))
            totals["decision_makers"] += sum(
                1 for row in result.lead_rows
                if row.get("lead_class") == "DECISION_MAKER")
            totals["teaching_staff"] += sum(
                1 for row in result.lead_rows
                if row.get("lead_class") == "TEACHING_STAFF")
            totals["support_staff"] += sum(
                1 for row in result.lead_rows
                if row.get("lead_class") == "SUPPORT_STAFF")
            totals["leads_with_email"] += sum(
                1 for row in result.lead_rows if row.get("email"))
            totals["leads_with_phone"] += sum(
                1 for row in result.lead_rows if row.get("phone"))
            totals["leads_with_profile"] += sum(
                1 for row in result.lead_rows if row.get("profile_url"))
            totals["errors"] += len(result.errors)
            statuses[result.district_status] = (
                statuses.get(result.district_status, 0) + 1)
            for problem in result.district_issue.split("; "):
                if problem:
                    failure_reasons[problem] = failure_reasons.get(problem, 0) + 1
            if result.district_status == "ERROR":
                totals["errors"] += 1
            if totals["districts_processed"] % max(1, log_every) == 0:
                logger.info("[%d/%d] %s -> %d leads (%s)",
                            totals["districts_processed"], len(records),
                            name[:40], len(result.lead_rows),
                            result.district_status)

    cache_stats = extraction_cache.stats
    summary = {
        **totals,
        "total_districts": len(records),
        "districts_success": statuses.get("SUCCESS", 0),
        "districts_partial": statuses.get("PARTIAL", 0),
        "districts_no_leads": statuses.get("NO_LEADS", 0),
        "districts_please_check": statuses.get("PLEASE_CHECK", 0),
        "districts_error": statuses.get("ERROR", 0),
        "extraction_cache": cache_stats,
        "runtime_seconds": round(time.time() - started, 3),
        "top_district_failure_reasons": sorted(
            failure_reasons.items(), key=lambda pair: -pair[1])[:10],
        "top_extraction_failure_reasons": sorted(
            extraction_reasons.items(), key=lambda pair: -pair[1])[:10],
        "outputs": {
            "schools": os.path.abspath(schools_path),
            "leads": os.path.abspath(leads_path),
            "district_status": os.path.abspath(status_path),
        },
    }
    with open(f"{out_prefix}_summary.json", "w", encoding="utf-8") as fh:
        import json
        json.dump(summary, fh, indent=2)
    logger.info("done in %.1fs: %d districts -> %d valid leads "
                "(extraction cache: %d unique URLs, %d hits saved)",
                summary["runtime_seconds"], len(records), totals["valid_leads"],
                cache_stats["cached_urls"], cache_stats["hits"])
    return schools_path, leads_path
