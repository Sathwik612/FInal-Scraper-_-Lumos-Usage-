"""
staffdir.decision_makers
=========================

**Decision-maker lead extractor.** Turns the people found on a
district's public staff/leadership pages into a short, high-value lead
list scoped to the roles that actually buy things: superintendents,
business administrators, curriculum & instruction leads, technology
directors, special/student-services directors, and building principals.

Why this exists
---------------
The full directory scrape returns *everyone* — thousands of teachers
and aides per district — and only ~54% of them had a published email
in the Arizona run. For vendor / edtech outreach that list is both too
big and too sparse. The buyers are a handful of named administrators
per district, and those roles almost always publish their email,
because being reachable is part of the job.

So this module does two things on top of ``extract_staff_directory``:

  1. **Filters to buyer roles** via a small, ordered title taxonomy
     (:data:`ROLE_RULES`). A teacher never matches, so it never
     appears.
  2. **Requires a *published* email** (``require_email=True`` by
     default). "Published" means the address came from a real route on
     the page the district put up — a ``mailto:``, ``itemprop=email``,
     JSON-LD, a Cloudflare-decoded span, etc. (see
     :data:`PUBLISHED_EMAIL_SOURCES`). We never synthesize an address
     from a name + domain pattern; if the district didn't publish it,
     the row is dropped, not guessed.

Output rows match the pipeline's ``LEAD_COLS`` schema exactly (so they
flow through ``deduplicate_leads`` and the existing exporters), plus a
few diagnostic columns — ``dm_role``, ``dm_tier``, ``email_source`` —
that downstream consumers can ignore or sort on.

Quick use
---------
::

    from staffdir import default_fetcher
    from staffdir.decision_makers import harvest_decision_makers

    fetch = default_fetcher(verify_ssl=False)
    rows, report = harvest_decision_makers(
        "https://www.example.k12.nj.us/",
        fetch, district_name="Example Township Public Schools")
    # every row in `rows` has a published email and a buyer-role title

Given HTML you already have::

    from staffdir import extract_staff_directory
    from staffdir.decision_makers import extract_decision_makers

    persons, _ = extract_staff_directory(html, url)
    rows = extract_decision_makers(
        persons, district_name="Example Township Public Schools",
        district_url="https://www.example.k12.nj.us/",
        directory_url=url)
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .schema import Confidence, FieldSource, Person
from .directory import extract_staff_directory
from .discover import discover_staff_directories
from .leads import (
    classify_lead, deduplicate_leads, normalize_title,
    priority_for_score, score_lead,
)

logger = logging.getLogger("staffdir.decision_makers")


# ---------------------------------------------------------------------------
# What counts as a *published* email
# ---------------------------------------------------------------------------

#: Email is only trusted if it came from one of these routes — i.e. the
#: district actually put the address on the page. There is deliberately
#: no "inferred / pattern" source in this set: we do not manufacture
#: addresses from name + domain guesses.
PUBLISHED_EMAIL_SOURCES = frozenset({
    FieldSource.MAILTO,
    FieldSource.MICRODATA,
    FieldSource.JSON_LD,
    FieldSource.JSON_EMBEDDED,
    FieldSource.CLOUDFLARE,
    FieldSource.DATA_ATTR,
    FieldSource.OBFUSCATED,
    FieldSource.SR_ONLY,
    FieldSource.FINALSITE_CONSTITUENT,
    FieldSource.CONTACT_LIST_PRO,
    FieldSource.HCARD,
})


# ---------------------------------------------------------------------------
# Buyer-role taxonomy
# ---------------------------------------------------------------------------
#
# Ordered most-specific -> most-generic. First matching rule wins, so
# "assistant superintendent" is labelled before the plain
# "superintendent" rule can grab it. Each rule matches if ANY of its
# substrings appears in the normalized title (lowercased, punctuation
# stripped to single spaces by ``normalize_title``).
#
# tier 1 = primary buyer (owns budget and/or the edtech decision)
# tier 2 = secondary buyer / strong influencer
# tier 3 = generic leadership (kept, but lowest priority)

@dataclass(frozen=True)
class RoleRule:
    canonical: str
    tier: int
    terms: Tuple[str, ...]


ROLE_RULES: Tuple[RoleRule, ...] = (
    # --- tier 1: district cabinet / budget / tech ---------------------
    RoleRule("Assistant/Associate/Deputy Superintendent", 1,
             ("assistant superintendent", "associate superintendent",
              "deputy superintendent")),
    RoleRule("Superintendent", 1,
             ("superintendent", "chief school administrator",
              "chief education officer")),
    RoleRule("Business Administrator", 1,
             ("business administrator", "school business administrator",
              "business manager", "board secretary",
              "chief financial officer", "chief financial", "cfo",
              "director of finance", "director of business")),
    RoleRule("Curriculum & Instruction", 1,
             ("curriculum", "instruction", "teaching and learning",
              "academic officer", "academics", "chief academic")),
    RoleRule("Technology", 1,
             ("technology", "chief information", "chief technology",
              "cio", "cto", "director of it", "director of i t",
              "information technology", "instructional technology",
              "technology integration", "data systems")),

    # --- tier 2: student services / buildings -------------------------
    RoleRule("Special Education / Student Services", 2,
             ("special education", "special services", "student services",
              "pupil services", "pupil personnel", "director of guidance")),
    RoleRule("Human Resources", 2,
             ("human resources", "personnel", "chief talent",
              "director of hr")),
    RoleRule("Principal", 2,
             ("principal", "head of school", "headmaster",
              "headmistress")),

    # --- tier 3: generic leadership -----------------------------------
    RoleRule("Director / Supervisor / Coordinator", 3,
             ("executive director", "director", "supervisor",
              "coordinator", "administrator", "dean",
              "chief", "president")),
)

#: Non-buyer *function* words. A leadership title bolted onto one of
#: these (e.g. "Athletic Director") is not a buyer — but these veto only
#: the generic tiers (2/3); a real cabinet title never contains them.
ROLE_FUNCTION_NEGATIVES: Tuple[str, ...] = (
    "athletic", "athletics", "activities", "activity", "club",
    "cafeteria", "food service", "nutrition", "custodial", "custodian",
    "maintenance", "grounds", "transportation", "bus", "security",
    "band", "choir", "chorus", "drama", "theater", "theatre", "music",
    "yearbook", "booster", "pto", "pta", "class advisor",
    "student council", "honor society", "graduation",
)

#: Support-staff qualifiers. A "secretary to" / "administrative
#: assistant to" the superintendent is support staff, not the buyer, no
#: matter whose office it is — so these veto EVERY tier, including the
#: cabinet rules.
ROLE_SUPPORT_NEGATIVES: Tuple[str, ...] = (
    "administrative assistant", "assistant to the", "secretary to",
    "confidential secretary", "executive assistant", "secretary for",
    "aide to", "clerk",
)

# Back-compat alias (superset) for anything importing the old name.
ROLE_NEGATIVE_TERMS: Tuple[str, ...] = (
    ROLE_FUNCTION_NEGATIVES + ROLE_SUPPORT_NEGATIVES)


def _term_matches(term: str, norm: str, tokens: frozenset) -> bool:
    """Multi-word terms match as substrings of the normalized title;
    single-word terms (and acronyms like cto/cio/cfo) must match a whole
    token, so ``"cto"`` no longer matches inside ``"director"``."""
    if " " in term:
        return term in norm
    return term in tokens


@dataclass(frozen=True)
class RoleMatch:
    canonical: str
    tier: int
    matched_term: str


def match_role(title: Optional[str]) -> Optional[RoleMatch]:
    """Return the buyer-role a title maps to, or ``None`` if it is not a
    decision-maker role (a teacher, aide, secretary, etc.)."""
    norm = normalize_title(title or "")
    if not norm:
        return None
    tokens = frozenset(norm.split())
    # Support-staff qualifiers ("administrative assistant to the ...",
    # "secretary to ...") disqualify a title at every tier.
    if any(neg in norm for neg in ROLE_SUPPORT_NEGATIVES):
        return None
    function_vetoed = any(neg in norm for neg in ROLE_FUNCTION_NEGATIVES)
    for rule in ROLE_RULES:
        for term in rule.terms:
            if _term_matches(term, norm, tokens):
                # Non-buyer function words veto the generic tiers only;
                # a real cabinet role stands.
                if rule.tier >= 2 and function_vetoed:
                    return None
                return RoleMatch(rule.canonical, rule.tier, term)
    return None


# ---------------------------------------------------------------------------
# Person -> lead row
# ---------------------------------------------------------------------------

# Kept identical to pipeline.LEAD_COLS so rows are drop-in compatible,
# with three diagnostic columns appended.
LEAD_COLS: Tuple[str, ...] = (
    "district_name", "district_url", "school_name", "school_url",
    "staff_name", "first_name", "last_name", "title", "normalized_title",
    "department", "lead_class", "lead_score", "lead_priority", "email", "phone",
    "extension", "photo_url", "profile_url", "directory_url",
    "contact_id", "contact_name", "source_url", "source_type",
    "confidence", "organization_level",
    "district_status", "district_issue",
    # Legacy aliases retained for downstream consumers.
    "district", "school", "overall_confidence", "lead_type",
    # Decision-maker diagnostics (new).
    "dm_role", "dm_tier", "email_source",
)

_SCHOOL_LEVEL_ROLES = frozenset({"Principal"})


def _fval(field) -> Optional[str]:
    return field.value if field else None


def _split_name(person: Person) -> Tuple[Optional[str], Optional[str]]:
    first = _fval(person.first_name)
    last = _fval(person.last_name)
    if first or last:
        return first, last
    full = _fval(person.name)
    if not full:
        return None, None
    parts = full.replace(",", " ").split()
    # Drop a leading honorific so "Dr. Jane Doe" -> ("Jane", "Doe").
    if parts and parts[0].lower().rstrip(".") in (
            "dr", "mr", "mrs", "ms", "miss", "mx"):
        parts = parts[1:]
    if len(parts) == 1:
        return parts[0], None
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return None, None


def _published_email(person: Person, min_conf: float) -> Tuple[
        Optional[str], Optional[str]]:
    """Return ``(email, source)`` only if the email came from a
    published route at/above ``min_conf``. Otherwise ``(None, None)``."""
    f = person.email
    if not f or not f.value:
        return None, None
    if f.source not in PUBLISHED_EMAIL_SOURCES:
        return None, None
    if float(f.confidence) < float(min_conf):
        return None, None
    return f.value, f.source.value


def person_to_lead_row(
    person: Person,
    role: RoleMatch,
    *,
    district_name: str,
    district_url: str = "",
    directory_url: str = "",
    email: Optional[str] = None,
    email_source: Optional[str] = None,
) -> dict:
    """Build one lead row (``LEAD_COLS`` schema) from a matched person."""
    first, last = _split_name(person)
    title = _fval(person.title)
    org_level = "SCHOOL" if role.canonical in _SCHOOL_LEVEL_ROLES else "DISTRICT"
    confidence = person.compute_overall_confidence()

    row: dict = {c: "" for c in LEAD_COLS}
    row.update({
        "district_name": district_name,
        "district_url": district_url,
        "school_name": person.school or "",
        "school_url": "",
        "staff_name": _fval(person.name) or f"{first or ''} {last or ''}".strip(),
        "first_name": first or "",
        "last_name": last or "",
        "title": title or "",
        "normalized_title": normalize_title(title or ""),
        "department": _fval(person.department) or "",
        "lead_class": classify_lead(title),
        "email": email or "",
        "phone": _fval(person.phone) or "",
        "extension": _fval(person.extension) or "",
        "photo_url": _fval(person.photo_url) or "",
        "profile_url": _fval(person.profile_url) or "",
        "directory_url": directory_url or (person.source_url or ""),
        "contact_id": _fval(person.contact_id) or "",
        "contact_name": _fval(person.contact_name) or "",
        "source_url": person.source_url or directory_url or "",
        "source_type": "staff_directory",
        "confidence": confidence,
        "organization_level": org_level,
        "district_status": "",
        "district_issue": "",
        # diagnostics
        "dm_role": role.canonical,
        "dm_tier": role.tier,
        "email_source": email_source or "",
    })
    score = score_lead(row, confirmed_directory=bool(directory_url))
    row["lead_score"] = score
    row["lead_priority"] = priority_for_score(score)
    # Legacy aliases.
    row["district"] = district_name
    row["school"] = row["school_name"]
    row["overall_confidence"] = confidence
    row["lead_type"] = row["lead_class"]
    return row


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

def extract_decision_makers(
    persons: List[Person],
    *,
    district_name: str,
    district_url: str = "",
    directory_url: str = "",
    require_email: bool = True,
    min_email_confidence: float = Confidence.MEDIUM.value,
    max_tier: int = 3,
) -> List[dict]:
    """Filter a list of extracted ``Person`` records down to buyer-role
    leads.

    Parameters
    ----------
    require_email
        When ``True`` (default) a person is kept only if they have a
        *published* email (see :data:`PUBLISHED_EMAIL_SOURCES`). This is
        the "every lead is contactable" bar — and it is satisfied by the
        district's own published address, never a generated one.
    max_tier
        Drop roles above this tier (1 = cabinet only, 2 = + student
        services / principals, 3 = + generic leadership).
    """
    rows: List[dict] = []
    for person in persons:
        role = match_role(_fval(person.title))
        if role is None or role.tier > max_tier:
            continue
        email, email_source = _published_email(person, min_email_confidence)
        if require_email and not email:
            continue
        rows.append(person_to_lead_row(
            person, role,
            district_name=district_name, district_url=district_url,
            directory_url=directory_url,
            email=email, email_source=email_source))
    return rows


# ---------------------------------------------------------------------------
# End-to-end for one district
# ---------------------------------------------------------------------------

def harvest_decision_makers(
    homepage_url: str,
    fetch: Callable[[str], Optional[str]],
    *,
    district_name: str,
    max_dirs: int = 3,
    require_email: bool = True,
    min_email_confidence: float = Confidence.MEDIUM.value,
    max_tier: int = 3,
) -> Tuple[List[dict], Dict[str, object]]:
    """Discover a district's staff/leadership directories, extract the
    people, and return deduplicated decision-maker leads plus a small
    report dict.

    ``fetch`` is any callable ``url -> Optional[str]`` (use
    :func:`staffdir.default_fetcher`). Nothing here hits the network on
    its own, so it is fully testable with a stub fetcher.
    """
    report: Dict[str, object] = {
        "district_name": district_name,
        "homepage_url": homepage_url,
        "directories_scanned": 0,
        "directory_urls": [],
        "persons_seen": 0,
        "leads_before_dedup": 0,
        "leads": 0,
        "warnings": [],
    }

    candidates, disc_report = discover_staff_directories(
        homepage_url, fetch, top_n=max_dirs)
    if not candidates:
        report["warnings"].append("no staff-directory candidates found")
        return [], report

    all_rows: List[dict] = []
    persons_seen = 0
    for cand in candidates[:max_dirs]:
        html = fetch(cand.url)
        if not html:
            report["warnings"].append(f"could not fetch {cand.url}")
            continue
        report["directories_scanned"] = int(report["directories_scanned"]) + 1
        report["directory_urls"].append(cand.url)  # type: ignore[attr-defined]
        persons, _ = extract_staff_directory(html, cand.url)
        persons_seen += len(persons)
        all_rows.extend(extract_decision_makers(
            persons,
            district_name=district_name, district_url=homepage_url,
            directory_url=cand.url,
            require_email=require_email,
            min_email_confidence=min_email_confidence,
            max_tier=max_tier))

    report["persons_seen"] = persons_seen
    report["leads_before_dedup"] = len(all_rows)
    deduped = deduplicate_leads(all_rows)
    # deduplicate_leads copies unknown keys through, but re-apply
    # priority in case score changed during the merge.
    for r in deduped:
        r["lead_priority"] = priority_for_score(int(r.get("lead_score") or 0))
    report["leads"] = len(deduped)
    return deduped, report


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_dm_leads(rows: List[dict], path: str) -> None:
    """Write decision-maker leads to CSV using the ``LEAD_COLS`` header."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(LEAD_COLS),
                                extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in LEAD_COLS})


# ---------------------------------------------------------------------------
# CLI:  python -m staffdir.decision_makers districts.csv --out dm_leads.csv
# ---------------------------------------------------------------------------

def _read_input_csv(path: str, name_col: str, url_col: str
                    ) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        if name_col not in cols or url_col not in cols:
            raise ValueError(
                f"input CSV must have columns {name_col!r} and {url_col!r}; "
                f"found {cols}")
        for row in reader:
            name = (row.get(name_col) or "").strip()
            url = (row.get(url_col) or "").strip()
            if name and url:
                out.append((name, url))
    return out


def main(argv=None) -> int:
    import argparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .pipeline import default_fetcher

    p = argparse.ArgumentParser(
        prog="python -m staffdir.decision_makers",
        description="Extract buyer-role staff leads (superintendent, business "
                    "administrator, curriculum, technology, principals, ...) "
                    "with PUBLISHED emails from a CSV of district websites.")
    p.add_argument("input_csv", help="CSV with district name + website columns.")
    p.add_argument("--name-col", default="name")
    p.add_argument("--url-col", default="website")
    p.add_argument("--out", default="dm_leads.csv",
                   help="Output CSV path (default: dm_leads.csv).")
    p.add_argument("--workers", type=int, default=6,
                   help="Districts processed in parallel (default 6).")
    p.add_argument("--max-dirs", type=int, default=3,
                   help="Directory pages to scan per district (default 3).")
    p.add_argument("--max-tier", type=int, default=3, choices=(1, 2, 3),
                   help="1=cabinet only, 2=+principals/services, "
                        "3=+generic leadership (default 3).")
    p.add_argument("--allow-missing-email", action="store_true",
                   help="Keep buyer-role people even without a published "
                        "email (default: drop them).")
    p.add_argument("--verify-ssl", action="store_true",
                   help="Verify SSL certs (default OFF; many school sites "
                        "have bad certs).")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    records = _read_input_csv(args.input_csv, args.name_col, args.url_col)
    fetch = default_fetcher(verify_ssl=args.verify_ssl)
    require_email = not args.allow_missing_email

    all_rows: List[dict] = []

    def _one(rec: Tuple[str, str]) -> Tuple[str, List[dict], Dict[str, object]]:
        name, url = rec
        try:
            rows, report = harvest_decision_makers(
                url, fetch, district_name=name,
                max_dirs=args.max_dirs, require_email=require_email,
                max_tier=args.max_tier)
            return name, rows, report
        except Exception as exc:                       # fail loud, keep going
            logger.warning("district failed: %s (%s)", name, exc)
            return name, [], {"warnings": [str(exc)]}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_one, r): r for r in records}
        for fut in as_completed(futures):
            name, rows, report = fut.result()
            all_rows.extend(rows)
            logger.info("%-45s %3d buyer leads", name[:45], len(rows))

    write_dm_leads(all_rows, args.out)
    by_tier: Dict[int, int] = {}
    for r in all_rows:
        by_tier[int(r.get("dm_tier") or 0)] = by_tier.get(
            int(r.get("dm_tier") or 0), 0) + 1
    logger.info("wrote %d leads to %s  (tier1=%d tier2=%d tier3=%d)",
                len(all_rows), args.out,
                by_tier.get(1, 0), by_tier.get(2, 0), by_tier.get(3, 0))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
