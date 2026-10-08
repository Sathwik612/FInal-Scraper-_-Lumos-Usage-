"""
staffdir.classify
=================

Page-type classifier. Given a parsed HTML page + its URL, decide
what *kind* of page it is (staff directory, individual staff profile,
department index, homepage, contact page, etc.).

Design choices:
  * The output is a `PageClassification`, not just an enum. It carries
    the confidence, the structural signals the classifier looked at,
    and a human-readable list of reasons — so a wrong decision can be
    audited without re-parsing.
  * Every rule is a "signal" that contributes weight toward one page
    type. The winning type = highest total weight; ties resolved by a
    fixed priority.
  * This replaces the binary `is_directory_page()` in the legacy code,
    which lumped "many mailto links" and "list of person cards" into
    one boolean.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup           # type: ignore
except Exception:                            # pragma: no cover
    BeautifulSoup = None

try:
    from .blocks import find_repeating_blocks
except Exception:                            # pragma: no cover
    find_repeating_blocks = None

from .schema import PageClassification, PageType


# ---------------------------------------------------------------------------
# URL-token vocabularies
# ---------------------------------------------------------------------------

_URL_TOKENS = {
    PageType.STAFF_DIRECTORY:
        ("staff", "directory", "faculty", "employees", "our-team",
         "our_team", "staff-directory", "staff_directory", "personnel",
         "meet-the-team", "administration", "leadership", "people",
         "who-we-are", "district-directory", "school-directory"),
    PageType.STAFF_PROFILE:
        ("bio", "biography", "profile", "person", "user"),
    PageType.DEPARTMENT_INDEX:
        ("departments", "programs", "grade-levels", "schools", "campuses",
         "school-list", "department-list"),
    PageType.CONTACT:
        ("contact", "contact-us", "get-in-touch", "reach-us"),
    PageType.NEWS:
        ("news", "press", "articles", "announcements", "blog"),
    PageType.CALENDAR:
        ("calendar", "events", "schedule"),
    PageType.LOGIN:
        ("login", "signin", "sign-in", "portal", "auth"),
}

_PATH_TAIL_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

# A page can mention staff without being a directory.  These are matched
# against the normalized path, so e.g. ``staff-awards-recognition`` also
# matches ``staff-award``.
_NEGATIVE_STAFF_URL_PATTERNS = (
    "staff-award", "staff-recognition", "staff-news", "staff-spotlight",
    "staff-announcement", "staff-event", "employee-spotlight",
    "employee-news", "teacher-spotlight", "teacher-news", "faculty-news",
    "faculty-spotlight", "/news/staff", "/blog/staff",
)


def _normalized_path(path: str) -> str:
    return re.sub(r"/+", "/", (path or "").lower().replace("_", "-"))


def _is_negative_staff_url(path: str) -> bool:
    normalized = _normalized_path(path)
    return any(pattern in normalized
               for pattern in _NEGATIVE_STAFF_URL_PATTERNS)


def _url_signals(url: str) -> Dict[str, Any]:
    p = urlparse(url or "")
    path_tokens = [t.lower() for t in _PATH_TAIL_RE.findall(p.path or "")]
    return {
        "path": p.path,
        "path_tokens": path_tokens,
        "path_depth": len([t for t in (p.path or "").split("/") if t]),
        "query": p.query,
    }


# ---------------------------------------------------------------------------
# Structural signals from the DOM
# ---------------------------------------------------------------------------

_MAILTO_RE = re.compile(r"^mailto:", re.I)
_TEL_RE    = re.compile(r"^tel:",   re.I)


def _count_valid_repeating_people(soup, clusters) -> int:
    """Count person-supported blocks, not merely repeated DOM blocks.

    Repeating-block detection is intentionally broad and can match
    navigation/content grids. Keep those clusters as diagnostics, but only
    use blocks with a plausible name plus independent person evidence for
    directory classification.
    """
    try:
        from .fields import (
            _has_person_supporting_evidence, extract_emails, extract_name,
            extract_phones, extract_profile_url, extract_title,
        )
        count = 0
        for cluster in clusters:
            for elem in cluster.elements:
                name = extract_name(elem)
                title = extract_title(
                    elem, exclude_text=name.value if name else None)
                emails = extract_emails(elem)
                phones = extract_phones(elem)
                profile = extract_profile_url(elem)
                if _has_person_supporting_evidence(
                        elem, name, title,
                        emails[0] if emails else None,
                        phones[0] if phones else None, profile):
                    count += 1
        return count
    except Exception:
        return 0


def _structural_signals(soup) -> Dict[str, Any]:
    """Cheap scans of the DOM used by multiple rules below."""
    if soup is None:
        return {"empty": True}

    # Total mailto links.
    mailto = soup.find_all("a", href=_MAILTO_RE)
    tel    = soup.find_all("a", href=_TEL_RE)

    # Biggest table + whether it has any mailto in it.
    best_rows, table_has_mail = 0, False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) > best_rows:
            best_rows = len(rows)
            table_has_mail = bool(table.find("a", href=_MAILTO_RE))

    # Person-card selectors (loose union).
    cards = soup.select(
        '[class*="staff" i], [class*="employee" i], [class*="faculty" i], '
        '[class*="person" i], [class*="teammember" i], [class*="team-member" i], '
        '[class*="member" i], [class*="directory-item" i], [class*="bio" i]')

    # Repeated <li> children under the same parent, at least 4 in a row
    # — cheap "list-like" test used to catch mailto-list directories.
    max_li = 0
    for ul in soup.find_all(["ul", "ol"]):
        lis = ul.find_all("li", recursive=False)
        if len(lis) > max_li:
            max_li = len(lis)

    # Login-page fingerprint: password field + submit button.
    has_login = bool(soup.select_one('input[type="password"]'))

    # schema.org Person present?
    person_scope = bool(soup.select('[itemscope][itemtype*="Person" i]'))
    person_ld = 0
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.get_text() or ""
        if '"@type"' in txt and "Person" in txt:
            person_ld += len(re.findall(r'"@type"\s*:\s*"Person"', txt))
    embedded_staff_scripts = 0
    for s in soup.select(
            'script[type="application/json"], script#__NEXT_DATA__'):
        txt = s.get_text() or ""
        if re.search(
                r'"(?:name|fullName|displayName)"\s*:', txt,
                re.IGNORECASE) and re.search(
                r'"(?:title|jobTitle|role|position|email)"\s*:', txt,
                re.IGNORECASE):
            embedded_staff_scripts += 1

    contact_names = soup.select(
        ".contact-list-contact-name, [class*='contact-list-contact-name' i]"
    )
    contact_titles = soup.select(
        ".contact-list-job-title, [class*='contact-list-job-title' i]"
    )
    contact_items = soup.select("li[id^='cl-'], .contact-list-contact-container")

    # Finalsite Constituent Manager renders the visible list independently
    # from profile/email details. Require repeated items plus both the
    # dedicated name and title fields; a lone class in navigation is not
    # directory evidence.
    finalsite_items = soup.select(".fsConstituentItem")
    finalsite_names = soup.select(".fsConstituentItem .fsFullName")
    finalsite_titles = soup.select(".fsConstituentItem .fsTitles")

    # Generic Bootstrap/React card grids have no useful class names.  Reuse
    # the existing structural detector as a classifier signal, but keep the
    # threshold conservative.
    repeating_clusters = 0
    repeating_valid_people = 0
    if find_repeating_blocks is not None:
        try:
            clusters = find_repeating_blocks(
                soup, min_repeats=3, min_person_likeness=0.35, top_n=2)
            repeating_clusters = len(clusters)
            repeating_valid_people = _count_valid_repeating_people(
                soup, clusters)
        except Exception:
            repeating_clusters = 0

    # Article / news markers.
    is_article = bool(soup.find("article")) or \
                 bool(soup.select('meta[property="og:type"][content*="article" i]'))

    # Calendar / event markers.
    is_event = bool(soup.select('[itemtype*="Event" i], .fc-event, .calendar'))

    # Contact-form fingerprint: form with a subject/message textarea.
    has_contact_form = False
    for form in soup.find_all("form"):
        if form.find("textarea") and form.find("input", attrs={"type":
                lambda v: v and v.lower() in ("email", "text")}):
            has_contact_form = True
            break

    # Body text length (kept for the "soft 404 / empty stub" heuristic).
    body_len = 0
    body = soup.find("body")
    if body:
        body_len = len(re.sub(r"\s+", " ", body.get_text(" ")))

    # Title & first h1 (for keyword scans).
    title_text = soup.title.string if (soup.title and soup.title.string) else ""
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ") if h1 else ""

    return {
        "n_mailto": len(mailto),
        "n_tel": len(tel),
        "biggest_table_rows": best_rows,
        "table_has_mail": table_has_mail,
        "n_person_cards": len(cards),
        "max_li_in_list": max_li,
        "has_login_form": has_login,
        "has_person_microdata": person_scope,
        "has_person_json_ld": bool(person_ld),
        "n_person_json_ld": person_ld,
        "n_embedded_staff_scripts": embedded_staff_scripts,
        "n_contact_list_names": len(contact_names),
        "n_contact_list_titles": len(contact_titles),
        "n_contact_list_items": len(contact_items),
        "n_finalsite_items": len(finalsite_items),
        "n_finalsite_names": len(finalsite_names),
        "n_finalsite_titles": len(finalsite_titles),
        "n_repeating_person_clusters": repeating_clusters,
        "n_repeating_valid_people": repeating_valid_people,
        "is_article": is_article,
        "is_event": is_event,
        "has_contact_form": has_contact_form,
        "body_text_length": body_len,
        "title": (title_text or "").strip()[:200],
        "h1": (h1_text or "").strip()[:200],
    }


# ---------------------------------------------------------------------------
# Individual rules — each returns (page_type, weight, reason) or None
# ---------------------------------------------------------------------------

def _rule_url_tokens(url_sig, sig):
    """URL path tokens map cleanly to page types."""
    fired = []
    tokens = set(url_sig["path_tokens"])
    for ptype, vocab in _URL_TOKENS.items():
        matches = [t for t in vocab if t.replace("-", "").replace("_", "") in
                   {tok.replace("-", "").replace("_", "") for tok in tokens}
                   or t in url_sig["path"].lower()]
        if matches:
            if ptype == PageType.STAFF_DIRECTORY:
                # URL vocabulary is only a prior.  A directory verdict needs
                # independent person/list evidence from the page.
                if _is_negative_staff_url(url_sig["path"]) or \
                        not _has_directory_person_evidence(sig):
                    continue
                normalized_matches = {
                    m.replace("-", "").replace("_", "") for m in matches}
                generic_only = normalized_matches == {"staff"}
                url_weight = 0.12 if generic_only else 0.35
            else:
                url_weight = 0.35
            fired.append((ptype, url_weight,
                          f"URL contains {ptype.value} tokens: {matches[:3]}"))
    return fired


def _rule_login(url_sig, sig):
    if sig.get("has_login_form"):
        return [(PageType.LOGIN, 0.9, "page has a password input")]
    return []


def _rule_error(url_sig, sig):
    hints = []
    title = (sig.get("title", "") + " " + sig.get("h1", "")).lower()
    if any(k in title for k in ("404", "not found", "page not found",
                                 "access denied", "forbidden")):
        return [(PageType.ERROR, 0.9, f"title/h1 says: {title.strip()[:60]}")]
    # Soft-404: extremely thin body, no forms, no lists.
    if sig.get("body_text_length", 0) < 200 and \
            not sig.get("has_contact_form") and sig.get("n_mailto", 0) == 0 \
            and sig.get("n_person_cards", 0) == 0:
        hints.append((PageType.ERROR, 0.4,
                      f"body has only {sig.get('body_text_length')} chars"))
    return hints


def _rule_staff_directory(url_sig, sig):
    fired = []
    if _is_negative_staff_url(url_sig["path"]):
        return fired
    # (a) Repeated person cards + emails
    if sig.get("n_person_cards", 0) >= 3 and sig.get("n_mailto", 0) >= 3:
        fired.append((PageType.STAFF_DIRECTORY, 0.7,
                      f"{sig['n_person_cards']} person-class cards & "
                      f"{sig['n_mailto']} mailto links"))
    elif sig.get("n_person_cards", 0) >= 6:
        fired.append((PageType.STAFF_DIRECTORY, 0.55,
                      f"{sig['n_person_cards']} person-class cards"))
    # (a2) A repeated list of contact-bearing items.  This covers the common
    # four-person <ul> directory without allowing a bare /staff URL to win.
    if sig.get("n_mailto", 0) >= 3 and sig.get("max_li_in_list", 0) >= 3:
        fired.append((PageType.STAFF_DIRECTORY, 0.55,
                      f"{sig['n_mailto']} mailto links in a repeated list"))
    # (a3) Multiple structured Person objects are directory evidence rather
    # than the single-Person profile signal below.
    if sig.get("n_person_json_ld", 0) >= 2:
        fired.append((PageType.STAFF_DIRECTORY, 0.75,
                      f"{sig['n_person_json_ld']} JSON-LD Person objects"))
    if (sig.get("n_embedded_staff_scripts", 0) >= 1 and
            any(token in url_sig["path"].lower()
                for token in ("/staff", "/faculty", "/directory",
                              "/employees", "/people"))):
        fired.append((PageType.STAFF_DIRECTORY, 0.7,
                      "embedded JSON contains repeated staff-like fields"))
    if sig.get("n_repeating_valid_people", 0) >= 2:
        fired.append((PageType.STAFF_DIRECTORY, 0.65,
                      f"{sig['n_repeating_valid_people']} validated "
                      "people in repeating block(s)"))
    if (sig.get("n_contact_list_names", 0) >= 1 and
            sig.get("n_contact_list_titles", 0) >= 1):
        fired.append((PageType.STAFF_DIRECTORY, 0.7,
                      f"{sig['n_contact_list_names']} Contact List Pro "
                      "name entries"))
    if (sig.get("n_finalsite_items", 0) >= 2 and
            sig.get("n_finalsite_names", 0) >= 2 and
            sig.get("n_finalsite_titles", 0) >= 2):
        fired.append((PageType.STAFF_DIRECTORY, 0.8,
                      f"{sig['n_finalsite_items']} Finalsite constituent "
                      "items with names and titles"))
    # (b) A big table that contains mailto (Austin-style)
    if sig.get("biggest_table_rows", 0) >= 5 and sig.get("table_has_mail"):
        fired.append((PageType.STAFF_DIRECTORY, 0.75,
                      f"table with {sig['biggest_table_rows']} rows & mailto"))
    # (c) Just a lot of mailto links
    elif sig.get("n_mailto", 0) >= 8:
        fired.append((PageType.STAFF_DIRECTORY, 0.6,
                      f"{sig['n_mailto']} mailto links on page"))
    # (d) Long <ul>/<ol> that mentions staff/faculty in title
    if sig.get("max_li_in_list", 0) >= 8:
        title = (sig.get("title", "") + " " + sig.get("h1", "")).lower()
        if any(w in title for w in
               ("staff", "faculty", "directory", "team", "people",
                "personnel", "administration")):
            fired.append((PageType.STAFF_DIRECTORY, 0.55,
                          f"long <li> list ({sig['max_li_in_list']}) "
                          f"in a staff/faculty-titled page"))
    return fired


def _has_directory_person_evidence(sig) -> bool:
    """Return whether the DOM contains independent list/person evidence.

    This deliberately mirrors the conservative directory rules rather than
    treating a URL token as evidence in its own right.
    """
    return bool(
        sig.get("n_person_cards", 0) >= 3 or
        sig.get("n_person_json_ld", 0) >= 2 or
        sig.get("n_embedded_staff_scripts", 0) >= 1 or
        sig.get("n_repeating_valid_people", 0) >= 2 or
        (sig.get("biggest_table_rows", 0) >= 3 and
         sig.get("table_has_mail")) or
        (sig.get("n_mailto", 0) >= 3 and
         sig.get("max_li_in_list", 0) >= 3) or
        (sig.get("n_contact_list_names", 0) >= 1 and
         sig.get("n_contact_list_titles", 0) >= 1) or
        (sig.get("n_finalsite_items", 0) >= 2 and
         sig.get("n_finalsite_names", 0) >= 2 and
         sig.get("n_finalsite_titles", 0) >= 2) or
        (sig.get("max_li_in_list", 0) >= 8)
    )


def _rule_staff_profile(url_sig, sig):
    fired = []
    # Only one person on the page + microdata says so
    if (sig.get("has_person_microdata") or sig.get("has_person_json_ld")) \
            and sig.get("n_person_cards", 0) <= 1 \
            and sig.get("n_mailto", 0) <= 2:
        fired.append((PageType.STAFF_PROFILE, 0.85,
                      "Person microdata/JSON-LD, no multi-person layout"))
    # One heading that looks like a name + one mailto + short body
    if sig.get("n_mailto", 0) == 1 and sig.get("biggest_table_rows", 0) < 3 \
            and sig.get("n_person_cards", 0) <= 1:
        h1 = sig.get("h1", "")
        # crude: 2-4 word Title-Case string
        if h1 and 2 <= len(h1.split()) <= 5 and h1[:1].isupper():
            fired.append((PageType.STAFF_PROFILE, 0.55,
                          f"one mailto + name-shaped h1: {h1!r}"))
    return fired


def _rule_department_index(url_sig, sig):
    fired = []
    title = (sig.get("title", "") + " " + sig.get("h1", "")).lower()
    if sig.get("max_li_in_list", 0) >= 6 and \
            sig.get("n_person_cards", 0) < 3 and \
            sig.get("n_mailto", 0) < 3 and \
            any(w in title for w in
                ("departments", "schools", "programs", "campuses")):
        fired.append((PageType.DEPARTMENT_INDEX, 0.7,
                      "long list under a departments/schools title"))
    return fired


def _rule_contact(url_sig, sig):
    fired = []
    if sig.get("has_contact_form") and sig.get("n_mailto", 0) <= 2 \
            and sig.get("n_person_cards", 0) <= 1:
        fired.append((PageType.CONTACT, 0.65, "contact form present, no "
                      "multi-person layout"))
    return fired


def _rule_news(url_sig, sig):
    if sig.get("is_article") and sig.get("n_person_cards", 0) <= 1:
        return [(PageType.NEWS, 0.7, "<article> element or og:type=article")]
    return []


def _rule_calendar(url_sig, sig):
    if sig.get("is_event"):
        return [(PageType.CALENDAR, 0.7, "calendar/event markup")]
    return []


def _rule_homepage(url_sig, sig):
    """The root path with rich body content but few staff signals."""
    if url_sig.get("path_depth", 0) <= 1 \
            and sig.get("body_text_length", 0) > 400 \
            and sig.get("n_person_cards", 0) < 3 \
            and sig.get("n_mailto", 0) < 3:
        return [(PageType.HOMEPAGE, 0.45,
                 "shallow URL, rich body, no directory markers")]
    return []


_ALL_RULES = [_rule_login, _rule_error, _rule_url_tokens,
              _rule_staff_directory, _rule_staff_profile,
              _rule_department_index, _rule_contact,
              _rule_news, _rule_calendar, _rule_homepage]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify_page(soup, url: str = "") -> PageClassification:
    """Return a PageClassification for a parsed HTML page.

    Accepts a BeautifulSoup instance OR raw HTML string OR None
    (returned as ERROR).
    """
    result = PageClassification()
    if soup is None:
        result.page_type = PageType.ERROR
        result.add_reason(0.9, "no HTML returned")
        return result

    if isinstance(soup, (bytes, str)):
        if BeautifulSoup is None:
            raise RuntimeError("bs4 is required for classify_page")
        soup = BeautifulSoup(soup, "lxml")

    url_sig = _url_signals(url)
    struct_sig = _structural_signals(soup)
    struct_sig["negative_staff_url"] = _is_negative_staff_url(
        url_sig["path"])
    result.signals = {**{"url_" + k: v for k, v in url_sig.items()},
                       **struct_sig}

    # Accumulate weight per page type.
    weights: Dict[PageType, float] = {}
    reasons_by_type: Dict[PageType, List[str]] = {}
    for rule in _ALL_RULES:
        for (ptype, w, why) in rule(url_sig, struct_sig) or []:
            weights[ptype] = weights.get(ptype, 0.0) + w
            reasons_by_type.setdefault(ptype, []).append(
                f"[+{w:.2f}] ({rule.__name__}) {why}")

    if not weights:
        result.page_type = PageType.OTHER
        result.confidence = 0.2
        result.reasons.append("[+0.20] no rule fired; defaulted to OTHER")
        return result

    # Tie-breaker priority — the more actionable page types win ties.
    tie_priority = [
        PageType.STAFF_DIRECTORY, PageType.STAFF_PROFILE,
        PageType.DEPARTMENT_INDEX, PageType.LOGIN, PageType.ERROR,
        PageType.CONTACT, PageType.CALENDAR, PageType.NEWS,
        PageType.HOMEPAGE, PageType.OTHER,
    ]
    ranked = sorted(weights.items(),
                    key=lambda kv: (-kv[1], tie_priority.index(kv[0])))
    winner, winner_w = ranked[0]
    result.page_type = winner
    result.confidence = min(1.0, winner_w)
    result.reasons = reasons_by_type[winner]

    # If a second candidate is within 0.15, log it as a warning.
    if len(ranked) > 1 and (winner_w - ranked[1][1]) < 0.15:
        runner_up, ru_w = ranked[1]
        result.reasons.append(
            f"[!] runner-up {runner_up.value} at {ru_w:.2f} — "
            f"consider inspecting")

    return result
