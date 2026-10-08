#!/usr/bin/env python3
"""
build_nj_seed.py
================

Generate ``nj_districts.csv`` — the ``name,website`` seed the pipeline
runs on — for all New Jersey operating districts + charter LEAs.

It pulls from authoritative, no-auth sources and merges them:

  1. **Spine + attributes** — the Urban Institute's Education Data API,
     which wraps the federal NCES Common Core of Data district directory.
     Filtered to New Jersey (FIPS 34). Gives every LEA's name, county,
     NCES id, enrollment, agency type, and operating status. This is the
     authoritative *list*.
  2. **Websites (best-effort)** — CCD's own website field where present,
     then **Wikidata** (official website, property P856) for districts
     the federal file leaves blank.
  3. **Websites (authoritative override)** — an optional CSV you supply
     (e.g. an NJDOE School Directory export). Highest precedence.

Website precedence:  merge-file  >  CCD  >  Wikidata.

Run it where you have normal internet (VS Code, your laptop). It uses
only the Python standard library — no extra installs.

Examples
--------
    # full pull (CCD + Wikidata), latest data year
    python tools/build_nj_seed.py --out examples/nj_districts.csv

    # CCD only, specific year, skip Wikidata
    python tools/build_nj_seed.py --year 2023 --no-wikidata \
        --out examples/nj_districts.csv

    # fold in an NJDOE directory export for authoritative websites
    python tools/build_nj_seed.py \
        --merge-websites njdoe_directory.csv \
        --merge-name-col "District Name" --merge-url-col "Website" \
        --out examples/nj_districts.csv

Output columns: name, website, county, nces_leaid, enrollment, type,
charter, website_source.  The pipeline only needs name + website; the
rest are useful for prioritizing leads later.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

NJ_FIPS = 34
USER_AGENT = "nj-staff-pipeline seed-builder (contact: you@example.com)"

# Urban Institute Education Data API — CCD school-district directory.
CCD_DIRECTORY_URL = (
    "https://educationdata.urban.org/api/v1/"
    "school-districts/ccd/directory/{year}/?fips={fips}"
)
# Years to try, newest first, if --year is not given.
CCD_YEAR_CANDIDATES = (2023, 2022, 2021, 2020)

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
# NJ = wd:Q1408 ; "school district" = wd:Q13539806 (via P31/P279*).
WIKIDATA_QUERY = """
SELECT ?item ?itemLabel ?website ?countyLabel WHERE {
  ?item wdt:P31/wdt:P279* wd:Q13539806 .
  ?item wdt:P131* wd:Q1408 .
  OPTIONAL { ?item wdt:P856 ?website. }
  OPTIONAL { ?item wdt:P131 ?county. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


# ---------------------------------------------------------------------------
# HTTP (pluggable so the logic is testable offline)
# ---------------------------------------------------------------------------

def http_get_json(url: str, *, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 60) -> dict:
    """GET a URL and parse JSON. Raises on HTTP/parse error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


JsonFetcher = Callable[[str], dict]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

_STOP = re.compile(
    r"\b(school district|public schools|board of education|"
    r"schools|school|district|charter|academy|unified|community|"
    r"township|borough|boro|city of|city|town of|town|"
    r"the|of|county)\b")
# NB: 'elementary', 'middle', 'high', 'regional' are deliberately kept —
# they distinguish co-located districts (an elementary district vs the
# regional high-school district in the same town).
_NONWORD = re.compile(r"[^a-z0-9]+")


def normalize_district_key(name: str) -> str:
    """Collapse a district name to a comparison key so the same district
    from two sources matches despite 'Township Public Schools' vs
    'School District' style differences."""
    s = (name or "").lower()
    s = _STOP.sub(" ", s)
    s = _NONWORD.sub(" ", s)
    return " ".join(s.split())


def normalize_county_key(county: str) -> str:
    s = (county or "").lower().replace("county", "")
    return _NONWORD.sub(" ", s).strip()


class SiteIndex:
    """County-aware name->website lookup. NJ has many identically named
    districts across counties (several 'Franklin Township' etc.), so a
    website is only auto-joined when the county matches, or when a name
    is unambiguous statewide."""

    def __init__(self) -> None:
        self.by_full: Dict[Tuple[str, str], str] = {}
        self.by_name: Dict[str, set] = {}

    def add(self, name: str, county: str, site: str) -> None:
        site = clean_website(site)
        if not site:
            return
        nk = normalize_district_key(name)
        if not nk:
            return
        ck = normalize_county_key(county)
        self.by_full.setdefault((nk, ck), site)
        self.by_name.setdefault(nk, set()).add(site)

    def get(self, name: str, county: str) -> Optional[str]:
        nk = normalize_district_key(name)
        ck = normalize_county_key(county)
        if (nk, ck) in self.by_full:
            return self.by_full[(nk, ck)]
        sites = self.by_name.get(nk)
        if sites and len(sites) == 1:   # unambiguous statewide
            return next(iter(sites))
        return None                     # ambiguous -> refuse to guess

    def __len__(self) -> int:
        return len(self.by_full)


def clean_website(url: Optional[str]) -> Optional[str]:
    """Normalize a website to a fetchable https URL, or None."""
    if not url:
        return None
    u = url.strip()
    if not u or u.lower() in ("n/a", "na", "none", "-"):
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    # Strip stray whitespace / trailing punctuation.
    u = u.split()[0].rstrip(".,;")
    return u


# CCD ``agency_type`` (NCES): 1 regular local, 2 local component of a
# supervisory union, 3 regional education service agency, 4 state
# agency, 5 federal, 6 other/indeterminate, 7 independent charter LEA.
CCD_TYPE_LABELS = {
    1: "regular", 2: "regular_component", 3: "services_agency",
    4: "state_agency", 5: "federal", 6: "other", 7: "charter",
}
# Keep the ones that actually operate schools + take vendors.
CCD_TYPES_KEEP = {1, 2, 3, 7}


def _get(rec: dict, *keys, default=None):
    """First present, non-empty value among keys (CCD field names drift
    a little across years)."""
    for k in keys:
        if k in rec and rec[k] not in (None, "", -1, -2, -3):
            return rec[k]
    return default


def ccd_website(rec: dict) -> Optional[str]:
    return clean_website(_get(rec, "website", "lea_website", "url"))


def ccd_is_operating(rec: dict) -> bool:
    """Drop closed/inactive LEAs when the status field is present.
    CCD ``sy_status``: 1 open, 3 new, 4 reopened are 'operating';
    2 closed, 5 changed-boundary, 6 inactive, 7 future are not."""
    status = _get(rec, "sy_status", "sy_status_text")
    if isinstance(status, str):
        return status.strip().lower() in (
            "open", "new", "reopened", "added", "1", "3", "4")
    if isinstance(status, (int, float)):
        return int(status) in (1, 3, 4)
    return True  # unknown -> keep, filter later by enrollment/type


def ccd_type(rec: dict) -> Tuple[str, bool, bool]:
    """Return (type_label, is_charter, keep)."""
    at = _get(rec, "agency_type", default=None)
    try:
        at = int(at)
    except (TypeError, ValueError):
        at = None
    label = CCD_TYPE_LABELS.get(at, "unknown")
    is_charter = at == 7
    keep = at in CCD_TYPES_KEEP if at is not None else True
    return label, is_charter, keep


# ---------------------------------------------------------------------------
# Source pulls
# ---------------------------------------------------------------------------

def fetch_ccd_directory(year: int, *, fetch: JsonFetcher,
                        fips: int = NJ_FIPS,
                        sleep: float = 0.0) -> List[dict]:
    """Pull all CCD directory records for a state, following pagination."""
    url: Optional[str] = CCD_DIRECTORY_URL.format(year=year, fips=fips)
    out: List[dict] = []
    pages = 0
    while url:
        payload = fetch(url)
        out.extend(payload.get("results", []))
        url = payload.get("next")
        pages += 1
        if sleep and url:
            time.sleep(sleep)
    return out


def resolve_ccd_year(explicit: Optional[int], *, fetch: JsonFetcher
                     ) -> Tuple[int, List[dict]]:
    """Return (year, records). If year not given, try newest first."""
    years = [explicit] if explicit else list(CCD_YEAR_CANDIDATES)
    last_err: Optional[Exception] = None
    for y in years:
        try:
            recs = fetch_ccd_directory(y, fetch=fetch)
            if recs:
                return y, recs
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            continue
    if last_err:
        raise last_err
    raise RuntimeError("no CCD data returned for any candidate year")


def parse_wikidata_websites(payload: dict) -> SiteIndex:
    """Build a county-aware SiteIndex from a Wikidata SPARQL response."""
    idx = SiteIndex()
    for b in payload.get("results", {}).get("bindings", []):
        label = b.get("itemLabel", {}).get("value", "")
        site = b.get("website", {}).get("value", "")
        county = b.get("countyLabel", {}).get("value", "")
        if label and site:
            idx.add(label, county, site)
    return idx


def fetch_wikidata_websites(*, fetch: Optional[JsonFetcher] = None
                            ) -> SiteIndex:
    if fetch is not None:
        payload = fetch(WIKIDATA_SPARQL_URL)
        return parse_wikidata_websites(payload)
    q = urllib.parse.urlencode({"query": WIKIDATA_QUERY, "format": "json"})
    url = f"{WIKIDATA_SPARQL_URL}?{q}"
    payload = http_get_json(url, headers={"Accept": "application/sparql-results+json"})
    return parse_wikidata_websites(payload)


def load_merge_websites(path: str, name_col: str, url_col: str,
                        county_col: Optional[str] = None) -> SiteIndex:
    """Load an authoritative name->website override CSV (e.g. an NJDOE
    directory export). County column optional but recommended for NJ."""
    idx = SiteIndex()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or name_col not in reader.fieldnames \
                or url_col not in reader.fieldnames:
            raise ValueError(
                f"merge CSV needs columns {name_col!r} and {url_col!r}; "
                f"found {reader.fieldnames}")
        has_county = bool(county_col and county_col in reader.fieldnames)
        for row in reader:
            county = row.get(county_col, "") if has_county else ""
            idx.add(row.get(name_col, ""), county, row.get(url_col, ""))
    return idx


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

SEED_COLS = ("name", "website", "county", "nces_leaid", "enrollment",
             "type", "charter", "website_source")


def assemble_rows(
    ccd_records: List[dict],
    *,
    wikidata: Optional[SiteIndex] = None,
    merge: Optional[SiteIndex] = None,
    keep_types: bool = True,
    require_enrollment: bool = False,
) -> Tuple[List[dict], Dict[str, int]]:
    """Merge sources into seed rows. Returns (rows, stats)."""
    wikidata = wikidata if wikidata is not None else SiteIndex()
    merge = merge if merge is not None else SiteIndex()
    rows: List[dict] = []
    seen: set = set()
    stats = {"ccd_records": len(ccd_records), "dropped_type": 0,
             "dropped_closed": 0, "dropped_dup": 0, "no_website": 0,
             "web_from_merge": 0, "web_from_ccd": 0, "web_from_wikidata": 0}

    for rec in ccd_records:
        name = _get(rec, "lea_name", "leaname", "name")
        if not name:
            continue
        if not ccd_is_operating(rec):
            stats["dropped_closed"] += 1
            continue
        type_label, is_charter, keep = ccd_type(rec)
        if keep_types and not keep:
            stats["dropped_type"] += 1
            continue

        leaid = _get(rec, "leaid", "lea_id", default="")
        key = normalize_district_key(name)
        dedup_key = str(leaid) or key
        if dedup_key in seen:
            stats["dropped_dup"] += 1
            continue

        enrollment = _get(rec, "enrollment", "enrollment_fall", default="")
        if require_enrollment and not enrollment:
            continue
        county = _get(rec, "county_name", "county", default="")

        # Website precedence: merge > CCD > Wikidata (county-aware joins).
        website, source = None, ""
        merged = merge.get(name, county)
        wiki = wikidata.get(name, county)
        if merged:
            website, source = merged, "merge"
            stats["web_from_merge"] += 1
        elif ccd_website(rec):
            website, source = ccd_website(rec), "ccd"
            stats["web_from_ccd"] += 1
        elif wiki:
            website, source = wiki, "wikidata"
            stats["web_from_wikidata"] += 1
        else:
            stats["no_website"] += 1

        seen.add(dedup_key)
        rows.append({
            "name": name,
            "website": website or "",
            "county": county,
            "nces_leaid": leaid,
            "enrollment": enrollment,
            "type": type_label,
            "charter": "Y" if is_charter else "N",
            "website_source": source,
        })

    rows.sort(key=lambda r: (r["county"], r["name"]))
    stats["rows"] = len(rows)
    return rows, stats


def write_seed(rows: List[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(SEED_COLS))
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="build_nj_seed.py",
        description="Build nj_districts.csv (name,website + attrs) for all "
                    "NJ operating districts + charters.")
    p.add_argument("--out", default="examples/nj_districts.csv")
    p.add_argument("--year", type=int, default=None,
                   help="CCD data year (default: newest that returns data).")
    p.add_argument("--no-wikidata", action="store_true",
                   help="Skip the Wikidata website enrichment.")
    p.add_argument("--merge-websites", metavar="CSV",
                   help="Authoritative name->website override CSV "
                        "(e.g. an NJDOE directory export). Highest priority.")
    p.add_argument("--merge-name-col", default="name")
    p.add_argument("--merge-url-col", default="website")
    p.add_argument("--merge-county-col", default=None,
                   help="County column in the merge CSV (recommended for NJ; "
                        "disambiguates same-named districts).")
    p.add_argument("--keep-all-types", action="store_true",
                   help="Do not drop state/federal/other agency types.")
    p.add_argument("--require-website", action="store_true",
                   help="Only write rows that ended up with a website.")
    args = p.parse_args(argv)

    print("Pulling CCD district directory for New Jersey (FIPS 34)...")
    try:
        year, ccd = resolve_ccd_year(args.year, fetch=http_get_json)
    except Exception as exc:                          # noqa: BLE001
        print(f"error: could not fetch CCD directory: {exc}", file=sys.stderr)
        return 2
    print(f"  got {len(ccd)} LEA records (CCD year {year}).")

    wikidata = SiteIndex()
    if not args.no_wikidata:
        print("Pulling official websites from Wikidata...")
        try:
            wikidata = fetch_wikidata_websites()
            print(f"  got {len(wikidata)} district websites from Wikidata.")
        except Exception as exc:                      # noqa: BLE001
            print(f"  warning: Wikidata pull failed ({exc}); continuing.",
                  file=sys.stderr)

    merge = SiteIndex()
    if args.merge_websites:
        merge = load_merge_websites(
            args.merge_websites, args.merge_name_col, args.merge_url_col,
            args.merge_county_col)
        print(f"  loaded {len(merge)} override websites from "
              f"{args.merge_websites}.")

    rows, stats = assemble_rows(
        ccd, wikidata=wikidata, merge=merge,
        keep_types=not args.keep_all_types)

    if args.require_website:
        rows = [r for r in rows if r["website"]]

    write_seed(rows, args.out)

    with_web = sum(1 for r in rows if r["website"])
    print(f"\nWrote {len(rows)} districts to {args.out}")
    print(f"  with website : {with_web}  "
          f"(merge={stats['web_from_merge']} ccd={stats['web_from_ccd']} "
          f"wikidata={stats['web_from_wikidata']})")
    print(f"  missing site : {len(rows) - with_web}")
    print(f"  dropped      : closed={stats['dropped_closed']} "
          f"type={stats['dropped_type']} dup={stats['dropped_dup']}")
    if len(rows) - with_web:
        print("\nFill remaining websites by re-running with "
              "--merge-websites <NJDOE directory export>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
