"""
staffdir.profile
================

Extract one Person from a *single-person* page (a bio / profile page).

Used to enrich a Person found on a directory listing when the listing
didn't have their email or full title — you follow ``person.profile_url``
and merge what the profile page yields into the original record.

Strategy is much simpler than the directory extractor:
  1. JSON-LD Person → done.
  2. Microdata Person → done.
  3. hCard → done.
  4. Heuristic: the biggest `main`/`article` element on the page,
     treated as one big card — run the field extractors on it.
"""

from __future__ import annotations

from typing import Optional, Tuple

try:
    from bs4 import BeautifulSoup           # type: ignore
except Exception:                            # pragma: no cover
    BeautifulSoup = None

from .directory import (
    _dedupe_persons, _persons_from_hcard, _persons_from_json_ld,
    _persons_from_microdata, _person_from_element, _strip_noise,
)
from .schema import ExtractionReport, Person


def _pick_main_container(soup):
    for sel in ("main", "article", "#content", ".content", "#main",
                ".main", "#page", ".page-content"):
        el = soup.select_one(sel)
        if el is not None: return el
    return soup.body or soup


def extract_staff_profile(html_or_soup, url: str = ""
                          ) -> Tuple[Optional[Person], ExtractionReport]:
    """Return the single Person on a bio / profile page, or None if we
    couldn't identify anyone."""
    report = ExtractionReport(url=url)
    if BeautifulSoup is None:
        raise RuntimeError("bs4 is required")
    if isinstance(html_or_soup, (str, bytes)):
        soup = BeautifulSoup(html_or_soup, "lxml")
    else:
        soup = html_or_soup

    for name, fn in (("json_ld", _persons_from_json_ld),
                     ("microdata", _persons_from_microdata),
                     ("hcard", _persons_from_hcard)):
        report.strategies_tried.append(name)
        found = _dedupe_persons(fn(soup, url) or [])
        if found:
            report.strategy_used = name
            report.persons_returned = 1
            return found[0], report

    _strip_noise(soup)
    container = _pick_main_container(soup)
    report.strategies_tried.append("main_container_heuristic")
    p = _person_from_element(container, url)
    if p:
        report.strategy_used = "main_container_heuristic"
        report.persons_returned = 1
        return p, report

    report.warnings.append("no Person identified on this page")
    return None, report


def enrich_with_profile(base: Person, profile: Person) -> Person:
    """Merge a profile-page Person into an already-known list-page
    Person. Profile-page values usually win because profile pages are
    typically the canonical single-record source.

    The merge is field-by-field via `Field.better_of`.
    """
    if profile is None: return base
    for attr in ("name", "first_name", "last_name", "email", "phone",
                 "extension", "title", "department", "grade", "room",
                 "photo_url", "profile_url", "biography"):
        b = getattr(base, attr); n = getattr(profile, attr)
        if b is None and n is not None:
            setattr(base, attr, n)
        elif b is not None and n is not None:
            setattr(base, attr, __import__("staffdir.schema",
                fromlist=["Field"]).Field.better_of(b, n))
    if not base.subjects and profile.subjects:
        base.subjects = list(profile.subjects)
    base.compute_overall_confidence()
    return base
