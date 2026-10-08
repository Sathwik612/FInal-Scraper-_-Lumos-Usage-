# `staffdir.decision_makers` — buyer-role lead extractor

Turns a district's public staff pages into a short, contactable lead
list scoped to the roles that actually buy things, with a hard rule:
**every lead has a *published* email.**

Drop `decision_makers.py` into the existing `staffdir/` package
(alongside `directory.py`, `leads.py`, …). It imports only from
`staffdir`; nothing else in the package changes.

## What it does

1. **Filters to buyer roles** using an ordered title taxonomy
   (`ROLE_RULES`): superintendent, assistant/associate/deputy
   superintendent, business administrator, curriculum & instruction,
   technology — plus (tier 2) special/student services and principals,
   and (tier 3) generic directors/supervisors. A teacher, aide, or
   secretary never matches, so it never appears.
2. **Requires a published email.** A lead is kept only if the address
   came from a real route on the page the district put up — `mailto:`,
   `itemprop=email`, JSON-LD, a Cloudflare-decoded span, etc.
   (`PUBLISHED_EMAIL_SOURCES`). Addresses are **never** synthesized from
   a name + domain pattern; if the district didn't publish it, the row
   is dropped, not guessed. Turn this off with `require_email=False`
   (or `--allow-missing-email`) if you want the emailless buyers too.

Output rows use the pipeline's exact `LEAD_COLS` schema, so they flow
through `deduplicate_leads` and the existing exporters unchanged, plus
three diagnostic columns: `dm_role`, `dm_tier`, `email_source`.

## CLI

```bash
python -m staffdir.decision_makers nj_districts.csv \
    --name-col name --url-col website \
    --out nj_dm_leads.csv \
    --workers 6 --max-dirs 3 --max-tier 3
```

Input CSV needs a name column and a website column (same format the
pipeline already uses). `--max-tier 1` gives you cabinet only;
`3` (default) includes principals and generic leadership.

## Library

```python
from staffdir import default_fetcher
from staffdir.decision_makers import harvest_decision_makers

fetch = default_fetcher(verify_ssl=False)
rows, report = harvest_decision_makers(
    "https://www.example.k12.nj.us/", fetch,
    district_name="Example Township Public Schools")
# rows: buyer-role leads, each with a published email
```

Given HTML you already fetched:

```python
from staffdir import extract_staff_directory
from staffdir.decision_makers import extract_decision_makers

persons, _ = extract_staff_directory(html, url)
rows = extract_decision_makers(
    persons, district_name="...", district_url="...", directory_url=url)
```

## Tests

```bash
python -m staffdir.tests_decision_makers   # 6 tests, offline (stub fetcher)
```

Covers role tiers, the athletic/secretary vetoes, the acronym
substring guard (`cto` must not match inside `director`), the
published-email gate (including rejecting a high-confidence but
*guessed* address), and a full harvest through the real extractor.

## Design notes

- **Role match is token-aware.** Multi-word terms match as substrings;
  single-word terms and acronyms (`cto`, `cio`, `cfo`) match whole
  tokens, so they don't collide inside longer words.
- **Two veto classes.** Non-buyer *function* words (athletic, band,
  cafeteria…) veto only the generic tiers. Support-staff qualifiers
  (administrative assistant to…, secretary to…) veto every tier — a
  secretary to the superintendent is not the buyer.
- **Scoring reuses `leads.score_lead`.** A cabinet role with a
  published email, district scope, and a title lands at priority A.
