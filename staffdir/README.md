# staffdir — school staff-directory pipeline

Additive companion package for the Universal Staff-Directory Scraper.
Ships an end-to-end pipeline (**CSV in → discovered directories +
extracted people CSVs out**) plus the primitives it's built from
(page classifier, field extractors, repeating-block detector,
adapters for the legacy `Employee` schema).

The existing modules (`master_staff_directory_scraper.py`,
`finalsite_scraper_async.py`, `profile_district_directories.py`,
etc.) are not touched. `staffdir` sits alongside them.

## Install and run in one minute

Prereqs:
```bash
pip install beautifulsoup4 lxml requests
```

Drop the `staffdir/` folder inside your existing scraper repo (next
to `master_staff_directory_scraper.py`). Prepare a CSV with **two
columns** — a name (district or school) and a website URL:

```csv
name,website
Foo ISD,https://foo.edu
Bar Academy,https://bar.k12.us
```

Run it:
```bash
python -m staffdir input.csv --out-prefix run1 --workers 16
```

You get two output CSVs:

- `run1_directories.csv` — one row per input site: the discovered
  staff-directory URL(s), the page classification, CMS detected,
  scoring reasons, warnings. This is your audit trail.
- `run1_leads.csv` — one row per extracted person:
  `district, school, first_name, last_name, email, title,
  department, phone, ..., lead_type, overall_confidence`.

Both are appended to as work completes, so Ctrl-C never loses progress.
Re-run with `--resume` to pick up where you left off.

## What "as accurate as possible" means here

Directory discovery (`discover_staff_directories`) runs five stages
in order, each cheap-to-run-first:

1. **CMS-aware canonical probing.** Detect the CMS from the homepage;
   probe the paths that CMS actually uses (`/apps/pages/staff` for
   Finalsite, etc.).
2. **Nav-link mining.** Extract anchors from `<nav>` / `<header>`
   regions. A "Staff Directory" link in the site's main navigation is
   almost always the real thing.
3. **Homepage anchor scan.** Same keyword-based scoring but across
   the whole body — lower confidence than nav.
4. **Sitemap.xml mining.** Parse the sitemap, match paths against
   staff-directory tokens.
5. **Common-path fallback.** Try `/staff-directory`, `/faculty`, etc.
   for sites where the CMS is unknown.

Every candidate URL is then **fetched and re-classified with
`classify_page`**. Only URLs that come back as `STAFF_DIRECTORY` (or
`DEPARTMENT_INDEX`, which we recurse into) survive. A page that looks
promising but classifies as `LOGIN`, `NEWS`, or `ERROR` is dropped
with a reason so you can see why.

Ranking outputs a top-N list, each with a score and a reasons trail:

```python
from staffdir import discover_staff_directories, default_fetcher
fetch = default_fetcher()
cands, report = discover_staff_directories("https://foo.edu", fetch)
for c in cands:
    print(c.url, c.score)
    for r in c.reasons: print("   -", r)
```

## Extraction handles different directory types

`extract_staff_directory` runs all applicable strategies, merges their
validated records, and keeps the strongest fields by provenance:

| Order | Strategy | Handles |
|---|---|---|
| 1 | Finalsite constituents | Visible names/titles, IDs, safe email association |
| 2 | Contact List Pro | WordPress contact records with visible identity evidence |
| 3 | JSON-LD `Person` | Modern sites; highest trust |
| 4 | Embedded JSON | `__NEXT_DATA__` and `application/json` staff state |
| 5 | schema.org microdata `Person` | Sites with structured data |
| 6 | hCard / vCard | Older microformat sites |
| 7 | Tables + mailto | Austin-style; also generic table directories |
| 8 | Named-class cards | `.staff-member`, `.faculty-card`, etc. |
| 9 | **Repeating-block detector** | Generic templates (Bootstrap grids, hashed React classes) |
| 10 | Definition lists | Older static sites |
| 11 | Mailto context fallback | Visible emails whose name is in a nearby heading |

The repeating-block detector is what materially improves yield: it
finds person cards by structural similarity (tag-path signature +
descendant-flag clustering) instead of relying on class names.
On the FIX_GENERIC_CARDS test (Bootstrap `col-md-4` with obfuscated
"user at domain dot com" emails), the fixed-selector approach returns
zero; the repeating-block detector returns all four.

Pagination is bounded by `max_directory_pages` and follows only same-site
numbered controls or explicit Next links from a real pagination container.
Finalsite pagination remains ID-aware. Dynamic/static recovery is visible in
`ExtractionReport.recovered_by_strategy`, `pagination_pages`,
`pagination_urls`, `js_rendered`, and `dropped`.

Obfuscation handled inline by `extract_emails`:
- `mailto:` (highest trust)
- schema.org `itemprop="email"`
- Cloudflare `data-cfemail` XOR decoding
- `data-email` / `data-mail` attributes
- Finalsite `fsStyleSROnly` screen-reader-only spans
- HTML-entity encoded (`&#64;`, `&#46;`)
- `"name at domain dot com"` obfuscation
- Bare regex over visible text (lowest trust)

## Confidence + provenance on every field

Every scalar on a `Person` is a `Field(value, confidence, source)`.
Filter or route by these downstream:

```python
persons, report = extract_staff_directory(html, url)
# Only keep records where the email came from a canonical route
# (mailto:, itemprop=email, or Cloudflare-decoded):
from staffdir import FieldSource
canonical = {FieldSource.MAILTO, FieldSource.MICRODATA,
             FieldSource.CLOUDFLARE}
filtered = [p for p in persons
            if p.email and p.email.source in canonical]
```

## Package layout

```
staffdir/
├── __init__.py         # public API
├── __main__.py         # `python -m staffdir <input.csv>` CLI
├── schema.py           # Field, Person, PageType, Confidence, ExtractionReport
├── fields.py           # extract_emails / phones / name / title / department
├── classify.py         # classify_page -> PageClassification + reasons
├── blocks.py           # find_repeating_blocks (DOM-agnostic)
├── directory.py        # extract_staff_directory orchestrator
├── profile.py          # extract_staff_profile + enrich_with_profile
├── discover.py         # discover_staff_directories (the URL finder)
├── cms_paths.py        # canonical paths per CMS + link-text vocab
├── pipeline.py         # run_pipeline (CSV in -> CSV out)
├── cache.py            # SQLite HTTP cache (optional)
├── adapt.py            # persons_to_employees — bridge to legacy Employee
├── tests_smoke.py      # 18 unit tests (fields, classify, extract)
└── tests_pipeline.py   # 9 integration tests (discover, pipeline, resume)
```

## Programmatic use

Full pipeline from a list of tuples:

```python
from staffdir import run_pipeline

records = [("Foo ISD",     "https://foo.edu"),
           ("Bar Academy", "https://bar.k12.us")]
dirs_csv, leads_csv = run_pipeline(
    records, out_prefix="test_run",
    workers=8, max_dirs_per_site=3)
```

Or feed a CSV directly:

```python
run_pipeline("districts.csv", out_prefix="tx_run1",
             name_col="district_name", url_col="website",
             workers=16, resume=True)
```

Just discovery (no extraction):

```python
from staffdir import discover_staff_directories, default_fetcher
fetch = default_fetcher(verify_ssl=False)
cands, report = discover_staff_directories("https://foo.edu", fetch,
                                            top_n=3)
```

Just extraction, given HTML you already fetched:

```python
from staffdir import extract_staff_directory
persons, report = extract_staff_directory(html, url)
```

Feed extracted persons through the legacy `Employee` pipeline
unchanged (so `classify_lead` etc. behave as before):

```python
from staffdir import persons_to_employees
from master_staff_directory_scraper import export_results, deduplicate

employees = persons_to_employees(
    persons, district="Foo ISD", school="Lincoln HS",
    directory_url=url, school_url="https://foo.edu/lincoln")
export_results(deduplicate(employees), schools=[...], out_dir="out")
```

## Running the tests

```bash
python -m staffdir.tests_smoke     # 18 unit tests
python -m staffdir.tests_pipeline  # 9 integration tests (mock fetcher)
```

## CLI reference

```
python -m staffdir INPUT_CSV [options]

--name-col NAME       Column with school/district name (default: 'name')
--url-col URL         Column with site URL (default: 'website')
--out-prefix PREFIX   Output goes to <prefix>_directories.csv + _leads.csv
--workers N           Thread count (default 8)
--max-dirs-per-site N Cap directory pages processed per site (default 3)
--limit N             Process only the first N input rows (0 = all)
--resume              Skip rows already in the output CSV
--verify-ssl          Verify SSL certs (default off — many school sites
                      have bad certs; the legacy README noted this too)
--verbose             DEBUG logging
```

## Design principles

- **Every field carries provenance.** `Field.source` says which route
  produced the value; `Field.confidence` says how strong the evidence
  was. Downstream can filter, log, or route by these.
- **Every decision has a reason.** Discovery, classification, and
  extraction all attach `reasons` / `warnings` lists. Debugging bad
  output means reading the reasons, not re-running with printfs.
- **Rules over ML.** Everything is a small readable rule with a
  weight. Adding a new CMS = one entry in `cms_paths.py`.
- **Additive over disruptive.** Nothing in the existing scraper
  imports from `staffdir`. Drop it in and see zero behaviour change
  until you call it. `persons_to_employees` uses your existing
  `classify_lead` / `infer_role` for lead bucketing so downstream is
  unchanged.
- **Fail loudly, keep going.** A single dead site or malformed page
  doesn't abort the pipeline — it becomes a warning row in the
  directories CSV.
