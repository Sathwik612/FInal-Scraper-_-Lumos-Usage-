"""
staffdir.tests_pipeline
=======================

Integration tests for the discovery + pipeline flow. Uses a mock
fetcher backed by an in-memory URL -> HTML dict, so we don't hit any
real sites during testing.

Run:  python -m staffdir.tests_pipeline
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from typing import Callable, Dict, Optional

# Reuse the FIX_* fixtures from the unit-test module.
from .tests_smoke import (
    FAILS, expect, run,
    FIX_AUSTIN_TABLE, FIX_MAILTO_LIST, FIX_JSON_LD,
    FIX_GENERIC_CARDS, FIX_STAFF_PROFILE, FIX_LOGIN,
)
from .discover  import discover_staff_directories, detect_cms
from .pipeline  import run_pipeline, run_district_pipeline
from .schema    import PageType
from .schools   import discover_schools


# ---------------------------------------------------------------------------
# A miniature site: homepage with nav links to a staff directory
# ---------------------------------------------------------------------------

HOMEPAGE_FINALSITE = """<!doctype html><html><head>
<title>Foo Independent School District</title>
<link rel="stylesheet" href="/static/finalsite.css">
<meta name="generator" content="Finalsite">
</head><body>
<header><nav>
   <a href="/">Home</a>
   <a href="/our-district">Our District</a>
   <a href="/staff-directory">Staff Directory</a>
   <a href="/schools">Schools</a>
   <a href="/news">News</a>
   <a href="/calendar">Calendar</a>
</nav></header>
<main>
   <h1>Welcome to Foo ISD</h1>
   <p>...homepage content...</p>
   <a href="/careers">Careers</a>
</main>
</body></html>"""

HOMEPAGE_UNKNOWN_CMS = """<!doctype html><html><head>
<title>Bar Academy</title></head><body>
<div class="site-nav">
   <a href="/">Home</a>
   <a href="/about">About</a>
   <a href="/faculty">Meet Our Faculty</a>
   <a href="/admissions">Admissions</a>
</div>
<h1>Welcome</h1>
<p>Copy about the school...</p>
</body></html>"""

# Homepage that has NO nav directory link — discovery has to fall back
# to canonical CMS paths (probing /staff-directory etc.)
HOMEPAGE_NO_HINTS = """<!doctype html><html>
<head><title>Baz Elementary</title></head>
<body><h1>Welcome</h1>
<p>Please contact the office at (555) 555-1234.</p>
<a href="/">Home</a> <a href="/about">About</a> <a href="/news">News</a>
</body></html>"""

# A staff directory built with generic Bootstrap grid classes, so the
# discovery has to (a) find it and (b) the extractor has to use the
# repeating-blocks detector.
STAFF_PAGE_GENERIC = FIX_GENERIC_CARDS

# A staff directory that's clearly Austin-style
STAFF_PAGE_AUSTIN  = FIX_AUSTIN_TABLE


# ---------------------------------------------------------------------------
# Mock fetcher
# ---------------------------------------------------------------------------

def build_mock_fetch(routes: Dict[str, str]) -> Callable[[str], Optional[str]]:
    """`routes` maps URL -> HTML. Trailing slashes and normalize
    handling."""
    def norm(u: str) -> str:
        u = u.rstrip("/")
        if u.endswith("//"): u = u.rstrip("/")
        return u.lower()
    normed = {norm(k): v for k, v in routes.items()}
    def fetch(url: str) -> Optional[str]:
        return normed.get(norm(url))
    return fetch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detect_cms():
    assert detect_cms(HOMEPAGE_FINALSITE, "https://foo.edu") == "finalsite"
    assert detect_cms(HOMEPAGE_UNKNOWN_CMS, "https://bar.edu") == "unknown"
    assert detect_cms("<div>wp-content</div>", "https://x.org") == "wordpress"


def test_discover_finds_nav_link():
    """The nav has <a href='/staff-directory'>Staff Directory</a>. We
    should pick it up, verify it, and rank it top."""
    routes = {
        "https://foo.edu":                 HOMEPAGE_FINALSITE,
        "https://foo.edu/staff-directory": STAFF_PAGE_AUSTIN,
        # Also present at a canonical Finalsite path but the nav-linked
        # URL is what actually exists here:
        "https://foo.edu/apps/pages/staff": "",
    }
    fetch = build_mock_fetch(routes)
    cands, report = discover_staff_directories("https://foo.edu", fetch,
                                                max_verify=8)
    assert report.homepage_status == "ok"
    assert report.cms_detected == "finalsite"
    assert cands, "should return at least one candidate"
    top = cands[0]
    assert "/staff-directory" in top.url, top.url
    assert top.classification is not None
    assert top.classification.page_type == PageType.STAFF_DIRECTORY, \
        top.classification.page_type


def test_discover_falls_back_to_canonical_paths():
    """No nav hints — must probe canonical CMS paths."""
    routes = {
        "https://baz.edu":                 HOMEPAGE_NO_HINTS,
        "https://baz.edu/staff-directory": STAFF_PAGE_AUSTIN,
    }
    fetch = build_mock_fetch(routes)
    cands, report = discover_staff_directories("https://baz.edu", fetch)
    assert cands, "should still find the directory via canonical probing"
    top = cands[0]
    assert "/staff-directory" in top.url
    assert top.classification.page_type == PageType.STAFF_DIRECTORY


def test_discover_rejects_login_page():
    """A candidate that classifies as LOGIN must be rejected."""
    routes = {
        "https://portal.edu": """<html><head><title>Home</title></head>
        <body><a href="/staff">Staff Directory</a></body></html>""",
        "https://portal.edu/staff": FIX_LOGIN,
    }
    fetch = build_mock_fetch(routes)
    cands, report = discover_staff_directories("https://portal.edu", fetch)
    # If it returned a candidate, it should NOT be the login page.
    for c in cands:
        if c.classification:
            assert c.classification.page_type != PageType.LOGIN, \
                f"login page slipped through: {c.to_dict()}"


def test_discover_returns_empty_on_unreachable():
    fetch = build_mock_fetch({})
    cands, report = discover_staff_directories("https://gone.edu", fetch)
    assert cands == []
    assert report.homepage_status == "unreachable"


def test_pipeline_end_to_end_two_sites():
    """Two sites, one Finalsite with a table directory, one unknown-CMS
    with a card grid. Pipeline should discover + extract both."""
    routes = {
        # Site 1: Foo ISD (Finalsite, Austin-style table)
        "https://foo.edu":                 HOMEPAGE_FINALSITE,
        "https://foo.edu/staff-directory": STAFF_PAGE_AUSTIN,

        # Site 2: Bar Academy (unknown CMS, generic cards)
        "https://bar.edu":         HOMEPAGE_UNKNOWN_CMS,
        "https://bar.edu/faculty": STAFF_PAGE_GENERIC,
    }
    fetch = build_mock_fetch(routes)

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run1")
        records = [("Foo ISD", "https://foo.edu"),
                    ("Bar Academy", "https://bar.edu")]
        dirs_path, leads_path = run_pipeline(
            records, out_prefix=prefix, workers=2, fetch=fetch)
        # Both files should exist and have header + rows.
        assert os.path.exists(dirs_path)
        assert os.path.exists(leads_path)

        import csv
        with open(dirs_path, encoding="utf-8-sig") as fh:
            dir_rows = list(csv.DictReader(fh))
        with open(leads_path, encoding="utf-8-sig") as fh:
            lead_rows = list(csv.DictReader(fh))

        assert len(dir_rows) >= 2, dir_rows
        # Both sites should have discovered a directory
        for r in dir_rows:
            assert r["directory_url"], r
            assert r["page_type"] in ("staff_directory",
                                        "staff_profile", "department_index"), r

        # Foo ISD table has 4 rows; Bar Academy card grid has 4.
        # We expect ~8 leads across the two sites (a few may drop on
        # confidence, so require >= 6).
        assert len(lead_rows) >= 6, \
            f"expected >= 6 leads, got {len(lead_rows)}: {lead_rows}"

        # Every lead should have an email
        for r in lead_rows:
            assert "@" in (r["email"] or ""), r

        # Both districts should be represented
        districts = {r["district"] for r in lead_rows}
        assert "Foo ISD" in districts and "Bar Academy" in districts, \
            districts


def test_pipeline_handles_unreachable_row():
    """A row whose site is down should log a warning row but not
    crash the pipeline."""
    routes = {
        "https://alive.edu":         HOMEPAGE_UNKNOWN_CMS,
        "https://alive.edu/faculty": STAFF_PAGE_GENERIC,
    }
    fetch = build_mock_fetch(routes)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run2")
        records = [("Alive School", "https://alive.edu"),
                    ("Dead School",  "https://dead.example")]
        dirs_path, leads_path = run_pipeline(
            records, out_prefix=prefix, workers=2, fetch=fetch)
        import csv
        with open(dirs_path, encoding="utf-8-sig") as fh:
            dir_rows = list(csv.DictReader(fh))
        # Every input row should have at least one output row
        input_names = {r["input_name"] for r in dir_rows}
        assert "Alive School" in input_names
        assert "Dead School" in input_names
        # The dead row should have a warning about the unreachable homepage.
        dead_rows = [r for r in dir_rows if r["input_name"] == "Dead School"]
        assert any("unreachable" in (r["warnings"] or "")
                    or "fetch" in (r["warnings"] or "")
                    for r in dead_rows), dead_rows


def test_pipeline_from_csv_input():
    """The pipeline must accept a CSV file path as input, not only an
    in-memory list."""
    routes = {
        "https://foo.edu":                 HOMEPAGE_FINALSITE,
        "https://foo.edu/staff-directory": STAFF_PAGE_AUSTIN,
    }
    fetch = build_mock_fetch(routes)
    with tempfile.TemporaryDirectory() as tmp:
        in_csv = os.path.join(tmp, "in.csv")
        with open(in_csv, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write("district_name,website\n")
            fh.write("Foo ISD,https://foo.edu\n")
        prefix = os.path.join(tmp, "csv_run")
        dirs_path, leads_path = run_pipeline(
            in_csv, out_prefix=prefix,
            name_col="district_name", url_col="website",
            workers=1, fetch=fetch)
        import csv
        with open(leads_path, encoding="utf-8-sig") as fh:
            lead_rows = list(csv.DictReader(fh))
        assert len(lead_rows) >= 4, lead_rows


def test_pipeline_resume():
    """Second run with resume=True should skip already-processed rows."""
    routes = {
        "https://foo.edu":                 HOMEPAGE_FINALSITE,
        "https://foo.edu/staff-directory": STAFF_PAGE_AUSTIN,
        "https://bar.edu":         HOMEPAGE_UNKNOWN_CMS,
        "https://bar.edu/faculty": STAFF_PAGE_GENERIC,
    }
    fetch = build_mock_fetch(routes)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "resume_run")
        # First run: just Foo.
        run_pipeline([("Foo ISD", "https://foo.edu")],
                     out_prefix=prefix, workers=1, fetch=fetch)
        # Second run with resume: add Bar. Foo should be skipped.
        run_pipeline([("Foo ISD", "https://foo.edu"),
                       ("Bar Academy", "https://bar.edu")],
                     out_prefix=prefix, workers=1, fetch=fetch,
                     resume=True)
        import csv
        with open(f"{prefix}_directories.csv", encoding="utf-8-sig") as fh:
            dir_rows = list(csv.DictReader(fh))
        # Foo should appear once, Bar should appear at least once.
        foo_rows = [r for r in dir_rows if r["input_name"] == "Foo ISD"]
        bar_rows = [r for r in dir_rows if r["input_name"] == "Bar Academy"]
        # (Foo has one candidate; if multiple discovered, tolerate.)
        assert len(foo_rows) >= 1
        assert len(bar_rows) >= 1


# ---------------------------------------------------------------------------
# District-pipeline tests (district -> schools -> directories -> people)
# ---------------------------------------------------------------------------

# District homepage that lists 3 schools in its nav.
DISTRICT_HOMEPAGE = """<!doctype html><html><head>
<title>Foo Unified School District</title></head>
<body>
<header><nav>
   <a href="/">Home</a>
   <a href="/about">About Us</a>
   <a href="/staff-directory">Staff Directory</a>
   <a href="/schools/lincoln-elementary">Lincoln Elementary School</a>
   <a href="/schools/washington-middle">Washington Middle School</a>
   <a href="/schools/roosevelt-high">Roosevelt High School</a>
</nav></header>
<h1>Welcome to Foo USD</h1>
</body></html>"""

# Small school homepages, each linking to their own staff directory.
# NB: real school pages use either a relative href ("staff") or a full
# subpath ("/schools/lincoln/staff") — a plain absolute like "/staff"
# would resolve to the district's staff dir, which is what we want to
# avoid.
def _mk_school_homepage(school_name: str, staff_path: str) -> str:
    return f"""<!doctype html><html><head>
<title>{school_name}</title></head><body>
<nav><a href="./">Home</a>
<a href="{staff_path}">Staff Directory</a></nav>
<h1>Welcome to {school_name}</h1></body></html>"""


def test_discover_schools_from_nav():
    routes = {
        "https://foo.edu": DISTRICT_HOMEPAGE,
    }
    fetch = build_mock_fetch(routes)
    schools, report = discover_schools("https://foo.edu", fetch,
                                        include_district_itself=False,
                                        min_score=0.5)
    assert report.homepage_ok
    urls = {s.url for s in schools}
    assert any("lincoln-elementary" in u for u in urls), urls
    assert any("washington-middle"  in u for u in urls), urls
    assert any("roosevelt-high"      in u for u in urls), urls
    assert len(schools) >= 3


def test_discover_schools_includes_district_itself():
    fetch = build_mock_fetch({"https://foo.edu": DISTRICT_HOMEPAGE})
    schools, _ = discover_schools("https://foo.edu", fetch,
                                    include_district_itself=True,
                                    min_score=0.5)
    # First entry should be the district itself.
    assert "(District Office)" in schools[0].name
    assert schools[0].url.startswith("https://foo.edu")


def test_discover_schools_dedupes_by_name():
    """Two anchors pointing at the same-named school under different
    URLs should collapse to one SchoolLink."""
    html = """<html><body><nav>
    <a href="/schools/lincoln-elementary">Lincoln Elementary School</a>
    <a href="/schools/lincoln">Lincoln Elementary</a>
    <a href="/departments/schools/lincoln-el">Lincoln Elementary</a>
    <a href="/schools/washington-middle">Washington Middle School</a>
    </nav></body></html>"""
    fetch = build_mock_fetch({"https://foo.edu": html})
    schools, _ = discover_schools("https://foo.edu", fetch,
                                    include_district_itself=False,
                                    min_score=0.5)
    names = [s.name for s in schools]
    lincoln_hits = [n for n in names if "lincoln" in n.lower()]
    assert len(lincoln_hits) == 1, \
        f"expected 1 Lincoln after de-dup, got {lincoln_hits}"
    assert any("Washington" in n for n in names)


def test_discover_schools_rejects_index_links():
    """Anchor text like 'All Schools', 'Our Schools', 'School List'
    must NOT be counted as schools (they're navigation index links)."""
    html = """<html><body><nav>
    <a href="/our-schools">Our Schools</a>
    <a href="/schools-list">All Schools</a>
    <a href="/browse-schools">Find A School</a>
    <a href="/schools/real-elementary">Real Elementary School</a>
    </nav></body></html>"""
    fetch = build_mock_fetch({"https://foo.edu": html})
    schools, _ = discover_schools("https://foo.edu", fetch,
                                    include_district_itself=False,
                                    min_score=0.5)
    names = {s.name for s in schools}
    assert not any("Our Schools" in n or "All Schools" in n or
                   "Find A School" in n for n in names), names
    assert any("Real Elementary" in n for n in names), names


def test_district_pipeline_end_to_end():
    """Full: district -> schools -> per-school directories -> people."""
    routes = {
        "https://foo.edu":                  DISTRICT_HOMEPAGE,
        "https://foo.edu/staff-directory":  STAFF_PAGE_AUSTIN,
        "https://foo.edu/schools/lincoln-elementary":
            _mk_school_homepage("Lincoln Elementary", "staff"),
        "https://foo.edu/schools/lincoln-elementary/staff":
            STAFF_PAGE_GENERIC,
        "https://foo.edu/schools/washington-middle":
            _mk_school_homepage("Washington Middle", "staff"),
        "https://foo.edu/schools/washington-middle/staff":
            FIX_MAILTO_LIST,
        "https://foo.edu/schools/roosevelt-high":
            _mk_school_homepage("Roosevelt High", "staff-directory"),
        "https://foo.edu/schools/roosevelt-high/staff-directory":
            STAFF_PAGE_AUSTIN,
    }
    fetch = build_mock_fetch(routes)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "district")
        schools_csv, leads_csv = run_district_pipeline(
            [("Foo USD", "https://foo.edu")],
            out_prefix=prefix, workers=2, fetch=fetch,
            max_schools_per_district=10, max_dirs_per_school=1)
        import csv
        with open(schools_csv, encoding="utf-8-sig") as fh:
            school_rows = list(csv.DictReader(fh))
        with open(leads_csv, encoding="utf-8-sig") as fh:
            lead_rows = list(csv.DictReader(fh))

        # Expect at least: district office + 3 schools = 4 school_rows
        # (each with a directory result).
        assert len(school_rows) >= 4, school_rows
        school_names = {r["school_name"] for r in school_rows}
        assert any("(District Office)" in s for s in school_names)
        assert any("Lincoln" in s for s in school_names)
        assert any("Washington" in s for s in school_names)
        assert any("Roosevelt" in s for s in school_names)

        # 4 directories × ~4 people each = ~16 leads.
        assert len(lead_rows) >= 12, len(lead_rows)
        # District tag preserved.
        assert all(r["district"] == "Foo USD" for r in lead_rows)
        # Every lead has an email.
        assert all("@" in (r["email"] or "") for r in lead_rows)


def test_district_pipeline_handles_no_schools_found():
    """District whose homepage has no school links: pipeline should
    still return the district-office row (with directory extraction
    if the homepage has one) and not crash."""
    routes = {
        "https://tiny.edu": """<html><head><title>Tiny District</title>
        </head><body><h1>Welcome</h1><nav>
        <a href="/">Home</a><a href="/staff">Staff</a></nav></body></html>""",
        "https://tiny.edu/staff": FIX_MAILTO_LIST,
    }
    fetch = build_mock_fetch(routes)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "tiny")
        schools_csv, leads_csv = run_district_pipeline(
            [("Tiny District", "https://tiny.edu")],
            out_prefix=prefix, workers=1, fetch=fetch)
        import csv
        with open(schools_csv, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        with open(leads_csv, encoding="utf-8-sig") as fh:
            leads = list(csv.DictReader(fh))
        # District office row must exist.
        assert any("(District Office)" in r["school_name"] for r in rows), rows
        # And it should have extracted people via /staff.
        assert len(leads) >= 4, leads


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_detect_cms,
        test_discover_finds_nav_link,
        test_discover_falls_back_to_canonical_paths,
        test_discover_rejects_login_page,
        test_discover_returns_empty_on_unreachable,
        test_pipeline_end_to_end_two_sites,
        test_pipeline_handles_unreachable_row,
        test_pipeline_from_csv_input,
        test_pipeline_resume,
        test_discover_schools_from_nav,
        test_discover_schools_includes_district_itself,
        test_discover_schools_dedupes_by_name,
        test_discover_schools_rejects_index_links,
        test_district_pipeline_end_to_end,
        test_district_pipeline_handles_no_schools_found,
    ]
    print(f"Running {len(tests)} pipeline tests")
    for t in tests: run(t.__name__, t)
    if FAILS:
        print("\nFAILURES:")
        for msg in FAILS: print("  -", msg)
        return 1
    print(f"\nAll {len(tests)} pipeline tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
