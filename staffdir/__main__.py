"""
staffdir command-line runner.

Usage
-----
    python -m staffdir <mode> path/to/input.csv [options]

Modes
-----
    site         Process each row as a standalone site (school or org).
    district     Each row is a district: discover schools first, then
                 find and extract staff directories per school.
    single-url   Extract staff from a single URL (no CSV needed).
    discover     Run directory discovery only — find the staff-directory
                 URL(s) on each site but don't extract people.

Run ``python -m staffdir <mode> --help`` for mode-specific options.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .pipeline import (
    run_pipeline, run_district_pipeline, default_fetcher,
    _load_input,
)
from .directory import extract_staff_directory
from .discover import discover_staff_directories


def _site_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "site",
        help="Process each input row as a standalone site (school / org).",
        description="Discover + extract staff directories for a CSV of "
                    "school or organisation websites. Each row is treated "
                    "independently.")
    p.add_argument("input_csv",
                   help="CSV with at least the two columns given by "
                        "--name-col and --url-col.")
    p.add_argument("--name-col", default="name",
                   help="Column with the site/school name "
                        "(default: 'name').")
    p.add_argument("--url-col", default="website",
                   help="Column with the site URL (default: 'website').")
    p.add_argument("--out-prefix", default="staffdir_run",
                   help="Output files: <prefix>_directories.csv + "
                        "<prefix>_leads.csv.")
    p.add_argument("--workers", type=int, default=8,
                   help="Thread count (default 8). Network-bound; "
                        "8-16 is a good range.")
    p.add_argument("--max-dirs-per-site", type=int, default=3,
                   help="Max directory URLs to extract per site (default 3).")
    p.add_argument("--max-directory-pages", type=int, default=20,
                   help="Max paginated pages to follow per directory "
                        "(default 20).")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only the first N input rows (0 = all).")
    p.add_argument("--resume", action="store_true",
                   help="Skip rows already in the output CSV.")
    p.add_argument("--verify-ssl", action="store_true",
                   help="Verify SSL certs (default: OFF).")
    p.add_argument("--verbose", action="store_true",
                   help="DEBUG logging.")
    # JS-rendered fallback tier — off only if you know your inputs are
    # all server-rendered and want to skip the Chromium install.
    p.add_argument("--no-playwright-fallback", action="store_true",
                   help="Disable the headless-Chromium fallback tier "
                        "(default: ENABLED when playwright is installed).")
    p.add_argument("--playwright-max-pages", type=int, default=500,
                   help="Safety-valve cap on pagination pages per directory. Natural termination (empty page / duplicate page / no more next-buttons) handles small sites automatically; you almost never need to raise this (default 500).")
    p.add_argument("--playwright-timeout", type=int, default=300,
                   help="Soft deadline in seconds. Extraction only stops if we both pass this deadline AND go 90s with no new email found — so a legitimately-slow big site keeps extracting past the deadline. Raise only if you see stuck-site aborts (default 300).",)
    return p


def _district_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "district",
        help="Each input row is a DISTRICT — discover schools, then "
             "extract staff per school.",
        description="District-level pipeline: for each district row, "
                    "discover schools, discover staff directories per "
                    "school, and extract every staff member.")
    p.add_argument("input_csv",
                   help="CSV with district name + website columns.")
    p.add_argument("--name-col", default="name",
                   help="District name column (default: 'name').")
    p.add_argument("--url-col", default="website",
                   help="District website column (default: 'website').")
    p.add_argument("--out-prefix", default="staffdir_district_run",
                   help="Output files: <prefix>_schools.csv + "
                        "<prefix>_leads.csv + <prefix>_district_status.csv "
                        "+ <prefix>_summary.json.")
    p.add_argument("--workers", type=int, default=4,
                   help="Districts processed in parallel (default 4).")
    p.add_argument("--inner-workers", type=int, default=4,
                   help="Schools processed in parallel within one "
                        "district (default 4).")
    p.add_argument("--time-budget", type=float, default=600.0,
                   help="Soft per-district deadline in seconds. Extraction only stops if we pass this deadline AND go 60s without new leads — so big districts with 30+ schools keep extracting past the deadline (default 600).")
    p.add_argument("--max-schools-per-district", type=int, default=30,
                   help="Cap schools discovered per district (default 30).")
    p.add_argument("--max-dirs-per-school", type=int, default=2,
                   help="Max directory URLs per school (default 2).")
    p.add_argument("--max-directory-pages", type=int, default=20,
                   help="Max paginated pages per directory (default 20).")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only the first N districts (0 = all).")
    p.add_argument("--resume", action="store_true",
                   help="Skip districts already in the output CSV.")
    p.add_argument("--verify-ssl", action="store_true",
                   help="Verify SSL certs (default: OFF).")
    p.add_argument("--verbose", action="store_true",
                   help="DEBUG logging.")
    p.add_argument("--no-playwright-fallback", action="store_true",
                   help="Disable the headless-Chromium fallback tier "
                        "(default: ENABLED when playwright is installed). "
                        "The fallback runs per-school when the static "
                        "HTML tier returns no emails.")
    return p


def _single_url_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "single-url",
        help="Extract staff from a single URL (quick test, no CSV).",
        description="Fetch one URL, run extraction, print results. "
                    "Useful for debugging or verifying a specific page.")
    p.add_argument("url", help="The staff-directory URL to extract.")
    p.add_argument("--out", default=None,
                   help="Write leads CSV to this path (default: print "
                        "to stdout as JSON).")
    p.add_argument("--max-pages", type=int, default=20,
                   help="Max paginated pages to follow (default 20).")
    p.add_argument("--verbose", action="store_true",
                   help="DEBUG logging + full extraction report.")
    p.add_argument("--no-playwright-fallback", action="store_true",
                   help="Disable the headless-Chromium fallback tier "
                        "(default: ENABLED when playwright is installed).")
    p.add_argument("--playwright-max-pages", type=int, default=500,
                   help="Safety-valve cap on pagination pages. Natural termination handles small sites; almost never need to raise (default 500).")
    p.add_argument("--playwright-timeout", type=int, default=300,
                   help="Soft deadline in seconds — extends automatically while the site is still yielding new emails (default 300).")
    return p


def _discover_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "discover",
        help="Discover staff-directory URL(s) on each site "
             "(no extraction).",
        description="Run only the directory-discovery phase. Outputs a "
                    "CSV of discovered directory URLs with scores and "
                    "classifications. Useful for auditing which URLs "
                    "would be scraped before committing to a full run.")
    p.add_argument("input_csv",
                   help="CSV with name + website columns.")
    p.add_argument("--name-col", default="name",
                   help="Name column (default: 'name').")
    p.add_argument("--url-col", default="website",
                   help="Website column (default: 'website').")
    p.add_argument("--out", default="discovered_directories.csv",
                   help="Output CSV path (default: "
                        "discovered_directories.csv).")
    p.add_argument("--top-n", type=int, default=3,
                   help="Max directory candidates per site (default 3).")
    p.add_argument("--workers", type=int, default=8,
                   help="Thread count (default 8).")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only the first N rows (0 = all).")
    p.add_argument("--verify-ssl", action="store_true",
                   help="Verify SSL certs (default: OFF).")
    p.add_argument("--verbose", action="store_true",
                   help="DEBUG logging.")
    return p


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m staffdir",
        description="Discover + extract staff directories from school "
                    "/ district websites.",
    )
    sub = p.add_subparsers(dest="mode", required=False)
    _site_parser(sub)
    _district_parser(sub)
    _single_url_parser(sub)
    _discover_parser(sub)

    # --- Legacy flat flags (backwards-compatible) ---
    # NOTE: this positional is named differently from the "input_csv"
    # positional defined inside each subcommand's own parser
    # (_site_parser, _district_parser, _discover_parser). If it shared
    # the same dest, argparse would set it *after* the chosen
    # subcommand finishes parsing (as part of matching this top-level
    # parser's own positional pattern) and silently overwrite the
    # correct value with this positional's default of None — which is
    # exactly what happened before this fix: `python -m staffdir
    # district some.csv --out-prefix x` parsed fine, but
    # args.input_csv came back None because this line clobbered it.
    p.add_argument("legacy_input_csv", nargs="?", default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--district", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--name-col", default="name",
                   help=argparse.SUPPRESS)
    p.add_argument("--url-col", default="website",
                   help=argparse.SUPPRESS)
    p.add_argument("--out-prefix", default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--workers", type=int, default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--inner-workers", type=int, default=4,
                   help=argparse.SUPPRESS)
    p.add_argument("--time-budget", type=float, default=600.0,
                   help=argparse.SUPPRESS)
    p.add_argument("--limit", type=int, default=0,
                   help=argparse.SUPPRESS)
    p.add_argument("--max-dirs-per-site", type=int, default=2,
                   help=argparse.SUPPRESS)
    p.add_argument("--max-schools-per-district", type=int, default=30,
                   help=argparse.SUPPRESS)
    p.add_argument("--resume", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--verify-ssl", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--verbose", action="store_true",
                   help=argparse.SUPPRESS)
    return p


def _run_single_url(args) -> int:
    """Extract staff from a single URL and print/write results."""
    logging.getLogger("staffdir.pipeline").setLevel(
        logging.DEBUG if args.verbose else logging.INFO)
    fetch = default_fetcher(verify_ssl=False)
    html = fetch(args.url)
    if not html:
        print(f"error: could not fetch {args.url}", file=sys.stderr)
        return 1
    persons, report = extract_staff_directory(
        html, args.url, fetch=fetch,
        max_directory_pages=args.max_pages)

    # Playwright fallback tier — same trigger rule as the batch pipeline.
    use_pw = not getattr(args, "no_playwright_fallback", False)
    if use_pw:
        from .playwright_fallback import (
            extract_with_playwright, is_playwright_available)
        from .pipeline import (
            _needs_playwright_fallback, _merge_playwright_records)
        if is_playwright_available() and _needs_playwright_fallback(persons):
            print(f"static tier: {len(persons)} persons / "
                  f"{sum(1 for p in persons if p.email)} emails — "
                  "escalating to playwright fallback...", file=sys.stderr)
            pw_persons, pw_report = extract_with_playwright(
                args.url,
                max_pages=getattr(args, "playwright_max_pages", 100),
                timeout_seconds=getattr(args, "playwright_timeout", 300))
            if pw_persons:
                persons = _merge_playwright_records(persons, pw_persons)
                report.warnings.extend(pw_report.warnings)
                report.js_rendered = True
                report.strategies_tried.extend(pw_report.strategies_tried)
        elif not is_playwright_available() and _needs_playwright_fallback(persons):
            print("hint: install playwright to catch JS-rendered "
                  "directories (`pip install playwright && "
                  "playwright install chromium`)", file=sys.stderr)

    if args.verbose:
        print(json.dumps(report.to_dict(), indent=2))
    if args.out:
        import csv
        from .pipeline import LEAD_COLS, _person_to_lead_row
        rows = [_person_to_lead_row(
            p, district="", school="", directory_url=args.url)
            for p in persons]
        with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=LEAD_COLS,
                               extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"{len(persons)} persons -> {args.out}")
    else:
        for p in persons:
            d = p.to_dict()
            print(json.dumps(
                {k: v for k, v in d.items() if v}, indent=2))
    return 0


def _run_discover(args) -> int:
    """Run discovery-only and write candidates CSV."""
    import csv
    from concurrent.futures import ThreadPoolExecutor, as_completed

    logging.getLogger("staffdir.pipeline").setLevel(
        logging.DEBUG if args.verbose else logging.INFO)
    logger = logging.getLogger("staffdir.pipeline")
    fetch = default_fetcher(verify_ssl=args.verify_ssl)
    records = _load_input(args.input_csv, args.name_col, args.url_col)
    if args.limit:
        records = records[:args.limit]

    cols = ["name", "website", "directory_url", "score", "page_type",
            "confidence", "person_count_hint", "cms_detected", "reasons"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()

    def process(name, url):
        try:
            cands, report = discover_staff_directories(
                url, fetch, top_n=args.top_n)
        except Exception as exc:
            return [(name, url, "", 0, "", 0, 0,
                     "", f"error: {exc}")]
        if not cands:
            return [(name, url, "", 0, "", 0, 0,
                     report.cms_detected,
                     " | ".join(report.warnings) or "no candidates")]
        rows = []
        for c in cands:
            rows.append((
                name, url, c.url, round(c.score, 3),
                c.classification.page_type.value if c.classification else "",
                round(c.classification.confidence, 3)
                    if c.classification else 0,
                c.person_count_hint, report.cms_detected,
                " | ".join(c.reasons[:5])))
        return rows

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(process, n, u): (n, u)
                   for n, u in records}
        for fut in as_completed(futures):
            rows = fut.result()
            with open(args.out, "a", newline="",
                      encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=cols)
                for row in rows:
                    writer.writerow(dict(zip(cols, row)))
            done += 1
            if done % 5 == 0 or done == len(records):
                logger.info("discover: %d/%d", done, len(records))

    print(f"discovery complete: {done} sites -> {args.out}")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --- Handle subcommand modes ---
    if args.mode == "site":
        logging.getLogger("staffdir.pipeline").setLevel(
            logging.DEBUG if args.verbose else logging.INFO)
        run_pipeline(
            args.input_csv,
            out_prefix=args.out_prefix,
            name_col=args.name_col, url_col=args.url_col,
            workers=args.workers,
            max_dirs_per_site=args.max_dirs_per_site,
            max_directory_pages=args.max_directory_pages,
            resume=args.resume, verify_ssl=args.verify_ssl,
            limit=args.limit,
            use_playwright_fallback=not args.no_playwright_fallback,
            playwright_max_pages=args.playwright_max_pages,
            playwright_timeout=args.playwright_timeout)
        return 0

    if args.mode == "district":
        logging.getLogger("staffdir.pipeline").setLevel(
            logging.DEBUG if args.verbose else logging.INFO)
        run_district_pipeline(
            args.input_csv,
            out_prefix=args.out_prefix,
            name_col=args.name_col, url_col=args.url_col,
            workers=args.workers,
            inner_workers=args.inner_workers,
            per_district_time_budget=args.time_budget,
            max_schools_per_district=args.max_schools_per_district,
            max_dirs_per_school=args.max_dirs_per_school,
            max_directory_pages=args.max_directory_pages,
            resume=args.resume, verify_ssl=args.verify_ssl,
            limit=args.limit)
        return 0

    if args.mode == "single-url":
        return _run_single_url(args)

    if args.mode == "discover":
        return _run_discover(args)

    # --- Legacy flat-flag fallback (no subcommand) ---
    if not args.legacy_input_csv:
        parser.print_help()
        return 0

    logging.getLogger("staffdir.pipeline").setLevel(
        logging.DEBUG if args.verbose else logging.INFO)
    try:
        if args.district:
            run_district_pipeline(
                args.legacy_input_csv,
                out_prefix=args.out_prefix or "staffdir_district_run",
                name_col=args.name_col, url_col=args.url_col,
                workers=args.workers or 4,
                inner_workers=args.inner_workers,
                per_district_time_budget=args.time_budget,
                max_schools_per_district=args.max_schools_per_district,
                max_dirs_per_school=args.max_dirs_per_site,
                resume=args.resume, verify_ssl=args.verify_ssl,
                limit=args.limit)
        else:
            run_pipeline(
                args.legacy_input_csv,
                out_prefix=args.out_prefix or "staffdir_run",
                name_col=args.name_col, url_col=args.url_col,
                workers=args.workers or 8,
                max_dirs_per_site=args.max_dirs_per_site,
                resume=args.resume, verify_ssl=args.verify_ssl,
                limit=args.limit)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
