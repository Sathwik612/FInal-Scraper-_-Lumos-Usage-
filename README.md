# staffdir — school district staff-directory extractor

Discovers staff directories on K-12 school and district websites and pulls
out **name, title, email, phone, department** for every person listed. Built
to run against state-scale CSVs (500-1500+ districts) and drain each site
completely before moving on.

**Requires Python 3.10+.** Optional but strongly recommended: Playwright
+ Chromium, which unlocks JS-rendered directories (Finalsite modal
profiles, envelope-icon reveals, click-to-open detail pages).

---

## Table of Contents

1. [Install](#1-install)
2. [Which mode should I use?](#2-which-mode-should-i-use)
3. [Command reference](#3-command-reference)
   - [`single-url` — test one URL](#31-single-url--test-one-url)
   - [`site` — one row per school/site](#32-site--one-row-per-schoolsite)
   - [`district` — one row per district](#33-district--one-row-per-district-discovers-schools)
   - [`discover` — find directory URLs without extracting](#34-discover--find-directory-urls-without-extracting)
4. [Output files](#4-output-files)
5. [Typical workflow: state-scale batch](#5-typical-workflow-state-scale-batch)
6. [Reading the log](#6-reading-the-log)
7. [Troubleshooting](#7-troubleshooting)
8. [How it works (extraction tiers)](#8-how-it-works-extraction-tiers)

---

## 1. Install

```powershell
# From the project root
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS/Linux

pip install -r requirements.txt

# Playwright — optional but strongly recommended
pip install playwright
python -m playwright install chromium
```

**Verify Playwright is picked up.** When you kick off any mode below, the
log's third line should say:

```
playwright fallback: ENABLED (max_pages=500, timeout=300s). ...
```

If instead you see `REQUESTED but playwright is not installed`, the
install didn't take — check you're in the right venv and rerun the two
Playwright commands.

Run the tests to sanity-check the install:

```powershell
python -m pytest staffdir/ -q
```

You should see ~118 passing.

---

## 2. Which mode should I use?

Four subcommands, each with a different input shape:

| Your input | Use this |
|---|---|
| **One URL you want to test right now** (debugging, checking a site works) | `single-url` |
| **CSV of school-level websites** (each row is one school, no discovery) | `site` |
| **CSV of district-level websites** (each row is one district, we discover its schools) | `district` |
| **You just want to know which URL on each site holds the staff directory** — no extraction | `discover` |

**Most state-scale runs want `district` mode.** School-level URLs usually
don't host their own directories; the district's central page does. If your
CSV has hundreds of individual school URLs, deduplicate to district
domains first (see [section 5](#5-typical-workflow-state-scale-batch)).

---

## 3. Command reference

### 3.1 `single-url` — test one URL

**The fastest way to check if a specific directory page works.** No CSV
needed. Prints results to stdout as JSON, or writes them to a CSV with
`--out`.

#### Basic — just print leads

```powershell
python -m staffdir single-url https://www.enterpriseschools.net/staff-directory
```

#### Verbose — show extraction report + which strategies fired

Use this for debugging. It prints the full "extraction report" at the
end telling you exactly which static strategies and Playwright
techniques ran, how many records each produced, and any warnings.

```powershell
python -m staffdir single-url https://www.enterpriseschools.net/staff-directory `
    --verbose
```

#### Save to a CSV instead of stdout

```powershell
python -m staffdir single-url https://www.enterpriseschools.net/staff-directory `
    --out enterprise.csv
```

#### Static-tier only — skip the Playwright fallback

If Chromium isn't launching on your machine, or you want a quick sanity
check without the JS-rendered fallback:

```powershell
python -m staffdir single-url https://www.enterpriseschools.net/staff-directory `
    --no-playwright-fallback --verbose
```

#### Kitchen sink — everything on

```powershell
python -m staffdir single-url https://www.enterpriseschools.net/staff-directory `
    --out enterprise.csv --verbose `
    --playwright-timeout 600 --playwright-max-pages 1000
```

**Flags:**

| Flag | Default | What it does |
|---|---|---|
| `--out` | (stdout as JSON) | Write leads to this CSV |
| `--max-pages` | 20 | Max paginated pages to follow (static tier) |
| `--verbose` | off | DEBUG logging + full extraction report at end |
| `--no-playwright-fallback` | off | Skip the JS-rendered fallback tier |
| `--playwright-max-pages` | 500 | Safety-valve cap on pagination pages (rarely needs raising) |
| `--playwright-timeout` | 300 | Soft deadline in seconds; extends automatically while emails still appear |

**Use this before every state-scale run** to confirm one URL from the CSV
actually works, catching config problems in 60 seconds instead of after
an hour.

---

### 3.2 `site` — one row per school/site

Treat every CSV row as an independent site. Discovers each site's staff
directory URL, then extracts. Best when your input rows are individual
schools with their own staff pages (rare in K-12; more common for
private schools, universities, businesses).

**Input CSV shape:** at least two columns — a name and a URL. Column
names are configurable via `--name-col` / `--url-col`. Example CSV:

```csv
School,Website
Emma C Attales Middle School,https://attales.abseconschools.org
H Ashton Marsh Elementary,https://marsh.abseconschools.org
```

#### Smoke test — first 5 rows only

Always run this first on a new CSV. Confirms your column names are
right and the pipeline actually works before you commit to a long run.

```powershell
python -m staffdir site examples/schools.csv `
    --name-col School --url-col Website `
    --out-prefix smoke --limit 5 --verbose
```

#### Basic production run

```powershell
python -m staffdir site examples/schools.csv `
    --name-col School --url-col Website `
    --out-prefix myrun --workers 8
```

#### Default columns — if your CSV uses `name` + `website` you can drop the col flags

```powershell
python -m staffdir site examples/schools.csv --out-prefix myrun --workers 8
```

#### Resume an interrupted run

`--resume` skips rows already present in `myrun_leads.csv`. Same
`--out-prefix` as the crashed run.

```powershell
python -m staffdir site examples/schools.csv `
    --name-col School --url-col Website `
    --out-prefix myrun --workers 8 --resume
```

#### Bigger batches, more parallelism

Site mode is network-bound, so more workers is usually better up to
16. Only push higher if your bandwidth is the bottleneck.

```powershell
python -m staffdir site examples/schools.csv `
    --name-col School --url-col Website `
    --out-prefix myrun --workers 16 --max-dirs-per-site 5
```

**Flags:**

| Flag | Default | What it does |
|---|---|---|
| `--name-col` | `name` | Column containing the site/school name |
| `--url-col` | `website` | Column containing the URL |
| `--out-prefix` | `staffdir_run` | Output files: `<prefix>_directories.csv`, `<prefix>_leads.csv` |
| `--workers` | 8 | Threads processing rows in parallel |
| `--max-dirs-per-site` | 3 | Max directory URLs to try per site |
| `--max-directory-pages` | 20 | Pagination cap for static extraction (pre-Playwright) |
| `--limit` | 0 (all) | Process only the first N rows — great for smoke tests |
| `--resume` | off | Skip rows already in the output CSV (safe restart) |
| `--verify-ssl` | off | Enforce SSL cert validation (many school sites have expired certs; leave off) |
| `--verbose` | off | DEBUG logging |
| `--no-playwright-fallback` | off | Skip JS-rendered fallback tier |
| `--playwright-max-pages` | 500 | Pagination safety valve |
| `--playwright-timeout` | 300 | Per-site soft deadline (auto-extends while emails still appear) |

---

### 3.3 `district` — one row per district (discovers schools)

Treat every CSV row as a district. For each row, the pipeline:

1. Discovers the district's central staff directory
2. Discovers each of its schools (up to `--max-schools-per-district`)
3. Discovers each school's staff directory
4. Extracts from all of them

**This is the mode for state-scale K-12 runs.**

**Input CSV shape:** at least two columns — a district name and a district
homepage URL. Example CSV:

```csv
District,Website
Newark Public School District,https://www.nps.k12.nj.us
Jersey City Public Schools,https://www.jcboe.org
Elizabeth Public Schools,https://www.epsnj.org
```

#### Smoke test — 3 districts to verify config

**Always run this first on a new state CSV.** Takes 5-10 minutes; catches
column-name issues and Playwright problems before you burn hours on a
broken config.

```powershell
python -m staffdir district examples/NJ_districts.csv `
    --name-col District --url-col Website `
    --out-prefix NJ_smoke --limit 3 --verbose
```

#### Basic production run

```powershell
python -m staffdir district examples/NJ_districts.csv `
    --name-col District --url-col Website `
    --out-prefix NJ_run `
    --workers 4 --inner-workers 4 --time-budget 600
```

#### Default columns — if your CSV uses `name` + `website` you can drop the col flags

```powershell
python -m staffdir district examples/districts.csv `
    --out-prefix run --workers 4 --inner-workers 4 --time-budget 600
```

#### Resume an interrupted run

`--resume` skips districts already in `NJ_run_district_status.csv`. Same
`--out-prefix` as the run you're resuming.

```powershell
python -m staffdir district examples/NJ_districts.csv `
    --name-col District --url-col Website `
    --out-prefix NJ_run --resume `
    --workers 4 --inner-workers 4 --time-budget 600
```

#### Mega-districts — bigger time budget + more schools per district

For huge districts (LAUSD, NYC DOE, Chicago Public Schools) where a
single district can have 100+ schools and 10,000+ staff:

```powershell
python -m staffdir district examples/mega.csv `
    --name-col District --url-col Website `
    --out-prefix mega_run `
    --workers 2 --inner-workers 8 `
    --time-budget 1800 --max-schools-per-district 200
```

#### Retry only the failed districts

After a run, filter your CSV to keep only rows where `NJ_run_district_status.csv`
shows `PLEASE_CHECK` or `PARTIAL`, save as `NJ_redo.csv`, then:

```powershell
python -m staffdir district examples/NJ_redo.csv `
    --name-col District --url-col Website `
    --out-prefix NJ_redo `
    --workers 4 --inner-workers 4 --time-budget 1200
```

Bigger `--time-budget` (20 min vs 10) is the one lever that reliably
helps on retry — most PLEASE_CHECK failures are discovery running out
of time, not code bugs.

**Flags:**

| Flag | Default | What it does |
|---|---|---|
| `--name-col` | `name` | District name column |
| `--url-col` | `website` | District homepage column |
| `--out-prefix` | `staffdir_district_run` | Output files (four of them, see [section 4](#4-output-files)) |
| `--workers` | 4 | Districts processed in parallel |
| `--inner-workers` | 4 | Schools processed in parallel within one district |
| `--time-budget` | 600 | **Soft** per-district deadline (seconds). Extends automatically while new leads keep appearing — stops only if past the deadline AND 60s without new leads. Big districts with 30+ schools legitimately need 5-10 min. |
| `--max-schools-per-district` | 30 | Cap schools discovered per district (raise for mega-districts) |
| `--max-dirs-per-school` | 2 | Max directory URLs tried per school |
| `--max-directory-pages` | 20 | Static-tier pagination cap |
| `--limit` | 0 (all) | Process only the first N rows |
| `--resume` | off | Skip districts already in the output CSV |
| `--verify-ssl` | off | Enforce SSL cert validation |
| `--verbose` | off | DEBUG logging |
| `--no-playwright-fallback` | off | Skip JS-rendered fallback tier |

Playwright's own timeout/pagination flags apply too but are internally
controlled per-technique — you almost never need to set them for this
mode.

---

### 3.4 `discover` — find directory URLs without extracting

Just discovers the staff-directory URL(s) on each site. No extraction, no
Playwright — much faster. Useful when you want to build a CSV of directory
URLs to inspect manually or feed to another tool.

#### Basic discovery

```powershell
python -m staffdir discover examples/districts.csv `
    --name-col District --url-col Website `
    --out discovered.csv
```

#### Test on 20 rows first

```powershell
python -m staffdir discover examples/districts.csv `
    --name-col District --url-col Website `
    --out discovered.csv --limit 20 --verbose
```

#### Default columns

```powershell
python -m staffdir discover examples/districts.csv --out discovered.csv
```

**Output** (`discovered.csv`) has one row per input site with the top
directory candidate URL, its confidence score, and the discovery
strategy that found it. From there you can:

- Manually spot-check the URLs in a browser before committing to a
  full extraction run.
- Feed the discovered URLs into `single-url` one by one for targeted
  testing.
- Fix bad URLs in the source CSV before running `district` or `site`
  mode.

---

## 4. Output files

### `site` mode

- **`<prefix>_directories.csv`** — one row per site. Shows which directory
  URL was discovered, what CMS the site uses, what strategy extracted
  from it, and any warnings.
- **`<prefix>_leads.csv`** — one row per person. Columns: `district_name`,
  `school_name`, `staff_name`, `first_name`, `last_name`, `title`,
  `department`, `email`, `phone`, `profile_url`, `source_url`,
  `lead_class`, `lead_score`, `confidence`, and more.

### `district` mode

- **`<prefix>_schools.csv`** — one row per (district, school) pair.
  Shows what was discovered for each school within each district.
- **`<prefix>_leads.csv`** — one row per person (same schema as
  `site` mode).
- **`<prefix>_district_status.csv`** — one row per district. Total
  schools found, leads extracted, extraction status (SUCCESS /
  PARTIAL / PLEASE_CHECK / NO_LEADS).
- **`<prefix>_summary.json`** — machine-readable run summary.

**All output files are checkpointed** — rows are appended as each
site/district finishes. If the run crashes or you Ctrl+C, everything up
to that point is already saved. Use `--resume` on the next run to skip
what's already there.

---

## 5. Typical workflow: state-scale batch

Say you have a CSV of Maryland districts. The full recommended flow:

### Step 1 — Smoke test one URL

```powershell
python -m staffdir single-url https://www.bcps.org/staff-directory --verbose
```

Confirm you see leads in the output. If not, Playwright isn't installed
or the URL is wrong.

### Step 2 — Smoke test the CSV

```powershell
python -m staffdir district examples/MD_districts.csv `
    --name-col District --url-col Website `
    --out-prefix MD_smoke --limit 3 --verbose
```

Three districts should finish in 5-10 minutes. If you get leads, the
config is correct and you can move on. If not, paste the log and fix
whatever's broken before starting the big run.

### Step 3 — Full run

```powershell
python -m staffdir district examples/MD_districts.csv `
    --name-col District --url-col Website `
    --out-prefix MD_run `
    --workers 4 --inner-workers 4 --time-budget 600
```

Watch the log periodically. Districts print out as they finish — see
[section 6](#6-reading-the-log) for what to look for.

### Step 4 — Retry the misses

After the run finishes, check `MD_run_district_status.csv`. Districts
labeled `PLEASE_CHECK` with 0 leads or `PARTIAL` are candidates for a
second pass. Build a redo CSV from those rows and run again with the
same command but a bigger `--time-budget` (say 1200) to give slow
sites more headroom.

### Step 5 — Resuming an interrupted run

If the run crashes at row 200 of 500, just start it again with `--resume`:

```powershell
python -m staffdir district examples/MD_districts.csv `
    --name-col District --url-col Website `
    --out-prefix MD_run --resume `
    --workers 4 --inner-workers 4 --time-budget 600
```

It'll skip any district already in `MD_run_district_status.csv`.

---

## 6. Reading the log

Each finished district/site prints a line like:

```
15:35:27 [INFO] [6/649] Newark Public School District -> 636 leads (SUCCESS)
```

**Status codes:**

| Status | Meaning |
|---|---|
| `SUCCESS` | Extraction ran cleanly and produced records |
| `PARTIAL` | Records extracted, but some quality signal was below threshold (may still be complete — often just means low emails-per-person) |
| `PLEASE_CHECK` | Something odd happened — extraction produced 0 records, discovery ran out of time, or emails were suspiciously missing. **Usually the interesting ones to investigate.** |
| `NO_LEADS` | Extraction ran cleanly but the site had no staff directory to find |

**Other log lines worth noticing:**

- `playwright fallback: ENABLED (max_pages=500, timeout=300s)` — Playwright is installed and will be used. This is what you want at the top of every run.
- `finalsite: 45 pagination pages detected on <url> — extracting all` — Finalsite site with many pages; pipeline is scaling accordingly.
- `<district>: static tier returned 145 persons / 32 emails (113 missing) — escalating <url> to playwright fallback` — Static HTML found people but most were missing emails; Playwright is filling in the gaps.
- `<district>: playwright fallback recovered 98 emails (now 130/145 persons have email)` — The fallback rescued emails via modal clicks or profile follows. Working as intended.
- `paginate-by-url: 2 consecutive empty pages at 12 — natural end of directory` — Pagination auto-terminated. Normal.
- `finalsite: <url> declares 630 pages, extracting first 500 ... Set --playwright-max-pages 680 if you need every page.` — Only fires on truly enormous districts (LAUSD scale). For normal K-12 you'll never see this.

---

## 7. Troubleshooting

### "0 leads (PLEASE_CHECK)" for a district you're sure should work

Run just that URL in `single-url --verbose`:

```powershell
python -m staffdir single-url https://<the-district-url>/ --verbose
```

Look at the last section of the output — the "extraction report" tells
you which strategies ran and what they found. Common causes:

- **Wrong URL** — the discovery step landed on the wrong page. Check
  what the site actually shows if you visit `<url>/staff-directory`
  in a browser.
- **Playwright not installed** — no `playwright fallback: ENABLED`
  line at startup.
- **Bot detection** — the site returned a captcha or 403. Log will show
  a very short HTML response.

### The run has been "processing N districts" for over an hour with no `[1/N]` output

One or more workers is hung on a slow/broken URL. Kill (Ctrl+C) and:

1. Run `Get-Process python, chromium, chrome` — if you see chromium
   processes with recent CPU, extraction is happening slowly, not hung.
   Wait longer or reduce `--time-budget`.
2. Otherwise restart with `--workers 1 --limit 4 --verbose` to isolate
   which URL is the problem.

### Broken pipe / "There is no current event loop" errors

These come from Playwright cleanup on Windows and are harmless — they
appear during shutdown, after all your data has already been written to
disk. Update to the current codebase if you see them mid-run; that's a
bug that was fixed.

### Playwright not being detected

```powershell
python -c "from playwright.async_api import async_playwright; print('OK')"
```

Should print `OK`. If it errors, `pip install playwright` didn't complete
successfully in your venv. Reinstall:

```powershell
python -m pip install --upgrade playwright
python -m playwright install chromium
```

### Sites with pagination beyond 500 pages

Only relevant for LAUSD, NYC DOE, Chicago Public Schools scale. Raise
the flag:

```powershell
python -m staffdir district ... --playwright-max-pages 1000
```

### Some districts extract 0 leads even after retry

Not every K-12 site publishes staff emails publicly. Some districts
deliberately hide emails behind a contact form. There's no extractor
that can defeat those — they're a lost cause.

---

## 8. How it works (extraction tiers)

Every directory URL is parsed in two tiers. Small/normal sites often
finish in Tier 1; JS-heavy sites escalate to Tier 2 automatically.

### Tier 1 — Static HTML (always run)

`staffdir/directory.py` runs **16 strategies** in priority order against
the raw HTML. Every strategy that finds records contributes to a merged,
deduplicated person list.

| # | Strategy | What it detects |
|---|---|---|
| 0 | `finalsite_constituent` | Finalsite `.fsConstituentItem` cards (very common in K-12) |
| 1 | `contact_list_pro` | WordPress Contact List Pro |
| 2 | `json_ld` | `<script type="application/ld+json">` Person entries |
| 3 | `embedded_json` | `__NEXT_DATA__`, application/json state blobs |
| 4 | `microdata` | HTML `itemtype*=Person` microdata |
| 5 | `hcard` | `.vcard` / `.h-card` microformats |
| 6 | `tables` | Data tables with mailto anchors |
| 7 | `named_cards` | CSS class selectors (`.staff-member`, `.team-member`, etc.) |
| 8 | `repeating_blocks` | DOM-agnostic structural detector |
| 9 | `dl` | Definition lists |
| 10 | `mailto` | Standalone `mailto:` links with nearby name/title |
| 11 | `accordions` | Bootstrap `<details>` / accordion panels |
| 12 | `aria_lists` | ARIA `role='list'` / `role='listitem'` containers |
| 13 | `wp_rest` | WordPress REST API endpoints |
| 14 | `grid_cards` | CSS Grid/Flexbox card containers |
| 15 | `foxbright` | Foxbright CMS (`.fbcms_staffentry`, common in Michigan) |

Plus per-field decoders for **Cloudflare `data-cfemail`**, **at/dot
obfuscation** ("name at domain dot com"), and **screen-reader-only
spans** (`.fsStyleSROnly`).

### Tier 2 — Playwright fallback (JS-rendered pages)

Kicks in whenever Tier 1 returns zero persons, all persons without
emails, or a meaningful fraction (≥20%) of persons without emails.

Four techniques run in order:

| # | Technique | When it wins |
|---|---|---|
| A | `direct_mailto_urlpage` | `mailto:` anchors visible after JS render; paginates via `?page_no=N` |
| B | `hidden_icon_nextbtn` | Same anchors but styled as envelope icons; paginates via next-button click |
| C | `finalsite_click` | Finalsite modal profiles where email appears only after clicking the person's name |
| D | `profile_follow` | Foxbright/Blackboard/custom themes where each name links to a detail page with the email |

Both tiers merge into one deduped output. A static record enriched by a
Playwright-found email survives with all fields intact.

**Adaptive timing** — no fixed timeouts. Every deadline is progress-aware:
extraction keeps going past the nominal deadline as long as new emails
are still being found; it stops only when both the deadline has passed
AND no new emails have appeared for the stall window. This means big
directories complete fully and stuck sites don't hang forever.

**Adaptive pagination** — no fixed page cap. Extraction terminates
naturally when the site returns two empty pages in a row, two duplicate
pages in a row (a "clamp" — many sites clamp `?page_no=999` to the last
real page and would otherwise loop forever), or the pagination controls
run out. The `--playwright-max-pages 500` flag is a safety valve that
almost never triggers.

---

## Attribution

Built for large-scale K-12 staff directory extraction. Successfully
tested against 500+ districts across NJ, TN, AL, MD, and TX.
