"""
staffdir.directory
==================

Top-level orchestrator for extracting a full list of people from a
staff-directory page. This is the *replacement* (or, if you prefer,
*upgrade*) for the ``parse_soup`` dispatch in the legacy scraper.

Strategy order (matches the priority already established in the
legacy code but each stage is its own function and returns a report
of what happened):

  0. schema.org / JSON-LD `Person` blocks (highest trust)
  1. schema.org microdata `[itemscope][itemtype*=Person]`
  2. hCard / vCard microformats
  3. HTML tables with mailto in them
  4. Named-class card selectors  (staff / faculty / person / …)
  5. **Repeating-block detector** (structural, DOM-agnostic — new)
  6. Definition lists
  7. Mailto fallback

Each stage returns a list of `Person`. The orchestrator keeps the
first stage that produced >= `min_persons` results and falls through
otherwise. If nothing meets the threshold, the largest bag we found
is returned anyway, with a warning attached to the report.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from bs4 import BeautifulSoup, Tag           # type: ignore
except Exception:                                 # pragma: no cover
    BeautifulSoup = Tag = None

from .blocks import find_repeating_blocks
from .classify import classify_page
from .fields import (
    extract_department, extract_emails, extract_name, extract_phones,
    extract_photo, extract_profile_url, extract_title, normalize_name,
    _get_text, _clean, _is_probable_name_text,
    _has_person_supporting_evidence, _is_rejected_name_label,
)
from .schema import (
    Confidence, ExtractionReport, Field, FieldSource, PageType, Person,
)


# ---------------------------------------------------------------------------
# Helper: build a Person from one card element
# ---------------------------------------------------------------------------

def _person_from_element(elem, source_url: str,
                         name_hint: Optional[str] = None) -> Optional[Person]:
    """Run the atomic field extractors on one card and assemble a
    Person. Returns None if we couldn't even find a name."""
    name = extract_name(elem)
    if not name and name_hint:
        # Sometimes the name is *outside* the card element (e.g. next to it in
        # a table row). Callers can pass an override.
        if _is_probable_name_text(name_hint):
            name = Field(_clean(name_hint), Confidence.MEDIUM.value,
                         FieldSource.CARD_TEXT)
    if not name:
        return None

    emails = extract_emails(elem)
    email = max(emails, key=lambda f: f.confidence) if emails else None

    phones = extract_phones(elem)
    phone = max(phones, key=lambda f: f.confidence) if phones else None
    ext_field = None
    if phone and " x" in phone.value:
        base, _, ext = phone.value.partition(" x")
        phone = Field(base, phone.confidence, phone.source)
        ext_field = Field(ext, phone.confidence, phone.source)

    title = extract_title(elem, exclude_text=name.value if name else None)
    dept  = extract_department(elem)
    photo = extract_photo(elem, base_url=source_url)
    profile = extract_profile_url(elem, base_url=source_url)

    if not _has_person_supporting_evidence(
            elem, name, title, email, phone, profile):
        return None

    first, last = normalize_name(name.value)
    person = Person(
        name=name,
        first_name=Field(first, name.confidence, name.source) if first else None,
        last_name=Field(last,  name.confidence, name.source) if last  else None,
        email=email,
        phone=phone,
        extension=ext_field,
        title=title,
        department=dept,
        photo_url=photo,
        profile_url=profile,
        source_url=source_url,
    )
    person.compute_overall_confidence()
    return person


# ---------------------------------------------------------------------------
# Stage 0 — JSON-LD
# ---------------------------------------------------------------------------

def _persons_from_json_ld(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip(): continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some sites embed multiple JSON objects concatenated.
            try:
                data = json.loads("[" + raw.replace("}{", "},{") + "]")
            except Exception:
                continue
        for obj in _walk_json_persons(data):
            p = _person_from_json_dict(obj, source_url,
                                       source=FieldSource.JSON_LD)
            if p: people.append(p)
    return people


def _walk_embedded_records(node):
    """Yield JSON objects that look like embedded staff records.

    Modern CMSs frequently render the visible cards from ``__NEXT_DATA__`` or
    an ``application/json`` state blob instead of putting contact fields in
    the card HTML. Keep this deliberately narrow: a record must have a
    plausible name plus a title, contact field, or person-specific URL.
    """
    if isinstance(node, dict):
        keys = {str(k).lower() for k in node}
        identity_keys = {
            "name", "fullname", "displayname", "givenname", "firstname",
            "familyname", "lastname",
        }
        supporting_keys = {
            "title", "jobtitle", "role", "position", "email", "phone",
            "telephone", "profileurl", "profile_url", "url", "href",
        }
        if keys & identity_keys and keys & supporting_keys:
            yield node
        for value in node.values():
            yield from _walk_embedded_records(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_embedded_records(value)


def _persons_from_embedded_json(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    seen = set()
    for script in soup.find_all("script"):
        script_id = (script.get("id") or "").lower()
        script_type = (script.get("type") or "").lower()
        if script_type != "application/json" and script_id not in {
                "__next_data__", "staff-data", "directory-data"}:
            continue
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for record in _walk_embedded_records(data):
            def value(*names):
                for name in names:
                    found = record.get(name)
                    if found not in (None, ""):
                        return str(found).strip()
                return ""

            name = value("name", "fullName", "displayName")
            if not name:
                name = " ".join(filter(None, (
                    value("givenName", "firstName"),
                    value("familyName", "lastName"),
                )))
            if not _is_probable_name_text(name):
                continue
            title = value("jobTitle", "title", "role", "position")
            email = value("email", "emailAddress")
            phone = value("telephone", "phone", "phoneNumber")
            profile_url = value("profileUrl", "profile_url", "url", "href")
            if not any((title, email, phone, profile_url)):
                continue
            identity = (name.lower(), email.lower(), profile_url.lower())
            if identity in seen:
                continue
            seen.add(identity)
            first, last = normalize_name(name)
            person = Person(
                name=Field(name, Confidence.HIGH.value,
                           FieldSource.JSON_EMBEDDED),
                first_name=Field(first, Confidence.HIGH.value,
                                 FieldSource.JSON_EMBEDDED) if first else None,
                last_name=Field(last, Confidence.HIGH.value,
                                FieldSource.JSON_EMBEDDED) if last else None,
                title=Field(title, Confidence.HIGH.value,
                            FieldSource.JSON_EMBEDDED) if title else None,
                email=(
                    max(extract_emails(email),
                        key=lambda f: f.confidence)
                    if extract_emails(email) else None),
                phone=(
                    max(extract_phones(phone),
                        key=lambda f: f.confidence)
                    if extract_phones(phone) else None),
                profile_url=(
                    Field(urljoin(source_url, profile_url),
                          Confidence.HIGH.value,
                          FieldSource.JSON_EMBEDDED)
                    if profile_url and not profile_url.startswith(
                        ("#", "javascript:")) else None),
                source_url=source_url,
            )
            person.compute_overall_confidence()
            people.append(person)
    return people


def _walk_json_persons(node):
    """Yield every dict where @type is Person (or list containing Person)."""
    if isinstance(node, dict):
        t = node.get("@type")
        if t == "Person" or (isinstance(t, list) and "Person" in t):
            yield node
        for v in node.values():
            yield from _walk_json_persons(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json_persons(v)


def _person_from_json_dict(d: dict, source_url: str,
                           source: FieldSource) -> Optional[Person]:
    def s(*keys):
        for k in keys:
            v = d.get(k)
            if v: return str(v)
        return None
    name = s("name") or " ".join(filter(None,
        (s("givenName"), s("familyName"))))
    if not name or _is_rejected_name_label(name): return None
    email = s("email"); phone = s("telephone"); title = s("jobTitle", "title")
    dept  = s("department"); photo = s("image"); url = s("url")
    first, last = normalize_name(name)
    p = Person(
        name=Field(name, Confidence.CERTAIN.value, source),
        first_name=Field(first, Confidence.CERTAIN.value, source) if first else None,
        last_name=Field(last,  Confidence.CERTAIN.value, source) if last  else None,
        email=Field(email, Confidence.CERTAIN.value, source) if email else None,
        phone=Field(phone, Confidence.CERTAIN.value, source) if phone else None,
        title=Field(title, Confidence.CERTAIN.value, source) if title else None,
        department=Field(dept, Confidence.CERTAIN.value, source) if dept else None,
        photo_url=Field(photo, Confidence.CERTAIN.value, source) if photo else None,
        profile_url=Field(url, Confidence.CERTAIN.value, source) if url else None,
        source_url=source_url,
    )
    p.compute_overall_confidence()
    return p


# ---------------------------------------------------------------------------
# Stage 1 — microdata Person
# ---------------------------------------------------------------------------

def _persons_from_microdata(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for scope in soup.select('[itemscope][itemtype*="Person" i]'):
        # extract_name / extract_title already know about itemprop
        p = _person_from_element(scope, source_url)
        if p: people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 2 — hCard
# ---------------------------------------------------------------------------

def _persons_from_hcard(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for card in soup.select(".vcard, .h-card"):
        p = _person_from_element(card, source_url)
        if p: people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 3 — tables with mailto
# ---------------------------------------------------------------------------

def _persons_from_tables(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3: continue
        # Skip tables that don't reference contact info at all.
        if not table.find("a", href=re.compile(r"^(mailto|tel):", re.I)) \
                and not re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                                  table.get_text(" ")):
            continue
        # Detect header row.
        header = None
        if rows and rows[0].find(["th"]):
            header = [_clean(th.get_text()).lower()
                      for th in rows[0].find_all(["th", "td"])]
            data_rows = rows[1:]
        else:
            data_rows = rows
        for row in data_rows:
            p = _person_from_table_row(row, header, source_url)
            if p: people.append(p)
    return people


def _person_from_table_row(row, header, source_url):
    cells = row.find_all(["td", "th"])
    if len(cells) < 2: return None

    # Try to pull from named columns if we have a header.
    def col(name_hints):
        if not header: return None
        for i, h in enumerate(header):
            if any(hint in h for hint in name_hints) and i < len(cells):
                return cells[i]
        return None

    name_cell  = col(("name", "employee", "person", "staff"))
    title_cell = col(("title", "position", "role", "job"))
    dept_cell  = col(("department", "dept", "grade", "subject"))

    if name_cell is None:
        # No header: assume the first cell whose text looks name-shaped.
        for c in cells:
            if _is_probable_name_text(_get_text(c)):
                name_cell = c; break
    if name_cell is None: return None

    # Run the atomic extractors over the whole row (emails/phones may be
    # in later cells) but seed the name from the identified name cell.
    name_hint = _get_text(name_cell)
    p = _person_from_element(row, source_url, name_hint=name_hint)
    if p and title_cell is not None and p.title is None:
        t = _clean(_get_text(title_cell))
        if t: p.title = Field(t, Confidence.HIGH.value, FieldSource.TABLE_CELL)
    if p and dept_cell is not None and p.department is None:
        d = _clean(_get_text(dept_cell))
        if d: p.department = Field(d, Confidence.HIGH.value, FieldSource.TABLE_CELL)
    return p


# ---------------------------------------------------------------------------
# Stage 4 — named-class cards
# ---------------------------------------------------------------------------

_CARD_SELECTORS = (
    ".staff-directory .staff-member", ".staff-directory .staff",
    ".directory .directory-item", ".staff-list li", ".faculty-list li",
    ".staff-card", ".employee-card", ".person-card", ".team-member",
    ".teammember", ".bio-card",
    "[class*='staff-member']", "[class*='faculty-member']",
    "[class*='employee']", "[class*='person']", "[class*='team-member']",
    "[class*='directory-item']",
)

def _persons_from_named_cards(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    seen_ids = set()
    for sel in _CARD_SELECTORS:
        try:
            elements = soup.select(sel)
        except Exception:
            continue
        for el in elements:
            if id(el) in seen_ids: continue
            seen_ids.add(id(el))
            p = _person_from_element(el, source_url)
            if p: people.append(p)
        if len(people) >= 3:
            return people
    return people


# ---------------------------------------------------------------------------
# Stage 5 — repeating-block detector (the new one)
# ---------------------------------------------------------------------------

def _persons_from_repeating_blocks(soup, source_url: str) -> List[Person]:
    clusters = find_repeating_blocks(soup, min_repeats=3,
                                     min_person_likeness=0.35, top_n=2)
    people: List[Person] = []
    for cluster in clusters:
        for el in cluster.elements:
            p = _person_from_element(el, source_url)
            if p: people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 6 — definition lists
# ---------------------------------------------------------------------------

def _persons_from_dl(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            name = _get_text(dt)
            if not _is_probable_name_text(name):
                continue
            p = _person_from_element(dd, source_url, name_hint=name)
            if p: people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 7 — mailto fallback
# ---------------------------------------------------------------------------

def _persons_from_mailto(soup, source_url: str) -> List[Person]:
    people: List[Person] = []
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        # extract_emails intentionally scans descendants, so pass the
        # immediate wrapper rather than the anchor itself.
        email = extract_emails(a.parent or a)
        email_f = email[0] if email else None
        if not email_f: continue

        # Try to identify a name — link text if it looks name-shaped,
        # else the nearest heading walking up the tree, else the parent
        # element minus the email text.
        link_text = _clean(a.get_text())
        name_field = None
        title_field = None
        if link_text and _is_probable_name_text(link_text):
            name_field = Field(link_text, Confidence.MEDIUM.value,
                               FieldSource.MAILTO)
        else:
            node = a
            for _ in range(4):
                node = node.parent
                if node is None: break
                h = node.find(["h1","h2","h3","h4","h5","h6"])
                if h:
                    ht = _clean(_get_text(h))
                    if _is_probable_name_text(ht):
                        name_field = Field(ht, Confidence.MEDIUM.value,
                                           FieldSource.HEADING)
                        break
        if name_field is None and a.parent is not None:
            ptxt = _clean(_get_text(a.parent))
            ptxt = ptxt.replace(email_f.value, "").replace(link_text, "").strip()
            if _is_probable_name_text(ptxt):
                name_field = Field(ptxt, Confidence.LOW.value,
                                   FieldSource.CARD_TEXT)

        # Some CMSs place the mailto in a button/link wrapper while the
        # person's name and title live in a nearby heading or sibling. Walk
        # only small, unambiguous ancestors; never scan the whole page.
        if name_field is None:
            ancestor = a
            for _ in range(6):
                ancestor = ancestor.parent
                if ancestor is None:
                    break
                text = _get_text(ancestor)
                if len(text) < 8 or len(text) > 1200:
                    continue
                candidates = []
                for node in ancestor.find_all(
                        ["h1", "h2", "h3", "h4", "h5", "h6",
                         "strong", "b"]):
                    candidate = _clean(_get_text(node))
                    if _is_probable_name_text(candidate):
                        candidates.append(candidate)
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    name_field = Field(candidates[0], Confidence.MEDIUM.value,
                                       FieldSource.HEADING)
                    if title_field is None:
                        title_field = extract_title(
                            ancestor, exclude_text=candidates[0])
                    break
        if not name_field: continue
        first, last = normalize_name(name_field.value)
        if a.parent is not None:
            nearby_title = extract_title(
                a.parent, exclude_text=name_field.value)
            if nearby_title is not None:
                title_field = nearby_title
        p = Person(
            name=name_field,
            first_name=Field(first, name_field.confidence, name_field.source) if first else None,
            last_name=Field(last,  name_field.confidence, name_field.source) if last  else None,
            email=email_f,
            title=title_field,
            source_url=source_url,
        )
        p.compute_overall_confidence()
        people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 8 — WordPress Contact List Pro
# ---------------------------------------------------------------------------

def _persons_from_contact_list_pro(soup, source_url: str) -> List[Person]:
    """Extract the server-rendered Contact List Pro record shape.

    Contact List Pro commonly duplicates the same record for responsive
    layouts, so the orchestrator's normal name/email dedupe is supplemented
    here with the plugin's data-id/data-name identity. A Send message link
    or data-name without a visible name element is never sufficient.
    """
    people: List[Person] = []
    seen = set()
    selectors = ("li[id^='cl-']", ".contact-list-contact-container")
    for selector in selectors:
        for elem in soup.select(selector):
            name_node = elem.select_one(
                ".contact-list-contact-name, "
                "[class*='contact-list-contact-name' i]"
            )
            name_text = _clean(_get_text(name_node)) if name_node else ""
            if not _is_probable_name_text(name_text):
                continue

            message = elem.find(
                "a", attrs={"data-name": True}, href=True)
            data_name = (
                (message.get("data-name") if message else None)
                or (elem.get("data-name") if hasattr(elem, "get") else None)
                or name_text
            )
            data_id = (
                (message.get("data-id") if message else None)
                or (elem.get("data-id") if hasattr(elem, "get") else None)
                or (elem.get("id") if hasattr(elem, "get") else None)
            )
            identity = (str(data_id or ""), str(data_name or name_text).lower())
            if identity in seen:
                continue
            seen.add(identity)

            person = _person_from_element(elem, source_url,
                                          name_hint=name_text)
            if not person:
                continue

            first, last = normalize_name(name_text)
            person.name = Field(name_text, Confidence.HIGH.value,
                                FieldSource.CONTACT_LIST_PRO)
            person.first_name = (
                Field(first, Confidence.HIGH.value,
                      FieldSource.CONTACT_LIST_PRO) if first else None)
            person.last_name = (
                Field(last, Confidence.HIGH.value,
                      FieldSource.CONTACT_LIST_PRO) if last else None)

            title_node = elem.select_one(
                ".contact-list-job-title, "
                "[class*='contact-list-job-title' i]"
            )
            title_text = _clean(_get_text(title_node)) if title_node else ""
            if title_text:
                person.title = Field(title_text, Confidence.HIGH.value,
                                     FieldSource.CONTACT_LIST_PRO)
            if data_id:
                person.contact_id = Field(str(data_id), Confidence.HIGH.value,
                                          FieldSource.CONTACT_LIST_PRO)
            if data_name:
                person.contact_name = Field(str(data_name),
                                            Confidence.HIGH.value,
                                            FieldSource.CONTACT_LIST_PRO)
            person.compute_overall_confidence()
            people.append(person)
    return people


# ---------------------------------------------------------------------------
# Stage 9 — Finalsite Constituent Manager
# ---------------------------------------------------------------------------

_FINALSITE_INSERT_EMAIL_RE = re.compile(
    r"FS\.util\.insertEmail\(\s*['\"]([^'\"]+)['\"]\s*,\s*"
    r"['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)

_COMMON_EMAIL_TLDS = {
    "com", "edu", "gov", "mil", "net", "org", "biz", "info", "name",
    "pro", "school", "us", "uk", "ca", "au", "nz", "io", "co",
}


def _finalsite_insert_email(first: str, second: str) -> Optional[str]:
    """Decode both Finalsite insertEmail argument orders.

    Older Finalsite pages pass reversed local-part then reversed domain.
    Current Constituent Manager pages, including Sossaman, pass the reversed
    domain then reversed local-part.  Prefer the candidate with a recognizable
    domain suffix and retain the older order as a conservative fallback.
    """
    decoded_first, decoded_second = first[::-1], second[::-1]
    candidates = [
        f"{decoded_first}@{decoded_second}",
        f"{decoded_second}@{decoded_first}",
    ]
    parsed = [extract_emails(candidate) for candidate in candidates]
    valid = [items[0].value.lower() for items in parsed if items]
    if not valid:
        return None
    for candidate in valid:
        domain = candidate.rsplit("@", 1)[-1]
        if domain.rsplit(".", 1)[-1].lower() in _COMMON_EMAIL_TLDS:
            return candidate
    return valid[0]


def _finalsite_email_fields(elem) -> List[Field]:
    """Extract Finalsite mailto/obfuscated emails from a bounded element."""
    fields = list(extract_emails(elem))
    seen = {f.value.lower() for f in fields}
    for script in elem.find_all("script") if hasattr(elem, "find_all") else []:
        raw = script.get_text() or ""
        for _, first, second in _FINALSITE_INSERT_EMAIL_RE.findall(raw):
            # Do not treat an unpaired page email as a constituent email;
            # callers establish that relationship.
            email = _finalsite_insert_email(first, second)
            if not email:
                continue
            if email not in seen:
                seen.add(email)
                fields.append(Field(
                    email, Confidence.CERTAIN.value,
                    FieldSource.FINALSITE_CONSTITUENT))
    return fields


def _finalsite_email_nodes(soup):
    """Return only constituent-style email nodes, excluding location emails.

    Covers three Finalsite email-container shapes seen in the wild:

      * ``.fsEmail`` — the classic labelled block (``School Email`` /
        ``Primary``) containing a mailto anchor.
      * ``.fsEmailIcon`` — the icon-only variant where the readable
        email lives inside a nested ``.fsStyleSROnly`` span (e.g.
        Henry County BOE), with no mailto anchor at all.
      * ``[id^='fsEmail-']`` — the wrapper Finalsite emits for each
        constituent's primary email regardless of skin.
    """
    nodes = []
    for node in soup.select(".fsEmail, .fsEmailIcon, [id^='fsEmail-']"):
        if node.find_parent(class_="fsConstituentItem"):
            continue
        if _finalsite_email_fields(node):
            nodes.append(node)
    return nodes


def _persons_from_finalsite(
        soup, source_url: str,
        hydrated_email_by_id: Optional[Dict[str, Field]] = None,
        ) -> List[Person]:
    """Extract visible Finalsite constituent records conservatively."""
    items = list(soup.select(".fsConstituentItem"))
    if not items:
        return []

    global_email_nodes = _finalsite_email_nodes(soup)
    email_by_id = dict(hydrated_email_by_id or {})
    email_values = []
    for node in global_email_nodes:
        fields = _finalsite_email_fields(node)
        if not fields:
            continue
        email_field = max(fields, key=lambda f: f.confidence)
        email_values.append(email_field)
        raw_attrs = " ".join(str(v) for v in node.attrs.values())
        for item in items:
            constituent_id = (
                item.get("data-constituent-id")
                or item.get("data-id")
                or "")
            if constituent_id and str(constituent_id) in raw_attrs:
                email_by_id[str(constituent_id)] = email_field

    # DOM ordering is used only when the page exposes exactly one usable
    # email node per visible constituent. Otherwise an unassociated email
    # remains blank rather than being assigned to a neighbor.
    ordered_emails = (
        email_values if len(email_values) == len(items) else [])

    people: List[Person] = []
    for index, item in enumerate(items):
        name_node = item.select_one(".fsFullName")
        name_text = _clean(_get_text(name_node)) if name_node else ""
        if not _is_probable_name_text(name_text):
            continue
        person = _person_from_element(item, source_url, name_hint=name_text)
        if not person:
            continue

        person.name = Field(name_text, Confidence.CERTAIN.value,
                            FieldSource.FINALSITE_CONSTITUENT)
        first, last = normalize_name(name_text)
        person.first_name = (
            Field(first, Confidence.CERTAIN.value,
                  FieldSource.FINALSITE_CONSTITUENT) if first else None)
        person.last_name = (
            Field(last, Confidence.CERTAIN.value,
                  FieldSource.FINALSITE_CONSTITUENT) if last else None)

        title_node = item.select_one(".fsTitles")
        title_text = _clean(_get_text(title_node)) if title_node else ""
        if title_text:
            person.title = Field(title_text, Confidence.CERTAIN.value,
                                 FieldSource.FINALSITE_CONSTITUENT)

        constituent_id = (
            item.get("data-constituent-id")
            or item.get("data-id")
            or "")
        if constituent_id:
            person.contact_id = Field(
                str(constituent_id), Confidence.CERTAIN.value,
                FieldSource.FINALSITE_CONSTITUENT)
            person.contact_name = Field(
                name_text, Confidence.CERTAIN.value,
                FieldSource.FINALSITE_CONSTITUENT)

        profile = item.select_one(".fsConstituentProfileLink[href]")
        href = profile.get("href", "").strip() if profile else ""
        if href and href not in {"#", "javascript:void(0)",
                                 "javascript:;"}:
            person.profile_url = Field(
                urljoin(source_url, href), Confidence.HIGH.value,
                FieldSource.FINALSITE_CONSTITUENT)

        email = _finalsite_email_fields(item)
        if not email and constituent_id:
            email = [email_by_id[str(constituent_id)]] \
                if str(constituent_id) in email_by_id else []
        if not email and ordered_emails:
            email = [ordered_emails[index]]
        if email:
            person.email = max(email, key=lambda f: f.confidence)
        person.compute_overall_confidence()
        people.append(person)
    return people


def _finalsite_hydration_targets(soup) -> Tuple[Optional[str], List[str]]:
    """Return the Finalsite element ID and modal-profile constituent IDs.

    Finalsite's newer Constituent Manager directory leaves profile links at
    ``#`` and loads each profile into a popup.  The element ID is required to
    reproduce the request made by the vendor's own JavaScript; requiring it
    keeps this opt-in path from probing arbitrary URLs on ordinary pages.
    """
    element_id = None
    for element in soup.select(".fsConstituent[id]"):
        if not element.select_one(".fsConstituentItem"):
            continue
        match = re.fullmatch(r"fsEl_(\d+)", (element.get("id") or "").strip())
        if match:
            element_id = match.group(1)
            break
    if not element_id:
        return None, []

    targets: List[str] = []
    for item in soup.select(".fsConstituentItem"):
        constituent_id = (
            item.get("data-constituent-id")
            or item.get("data-id")
            or "")
        profile = item.select_one(".fsConstituentProfileLink")
        href = (profile.get("href") or "").strip() if profile else ""
        modal_link = (
            profile is not None and
            (href in {"", "#", "javascript:void(0)", "javascript:;"}
             or profile.name == "button"))
        if modal_link and constituent_id and str(constituent_id) not in targets:
            targets.append(str(constituent_id))
    return element_id, targets


def _finalsite_profile_hydration_url(
        source_url: str, element_id: str, constituent_id: str) -> str:
    """Build the bounded profile request used by Finalsite's modal handler."""
    endpoint = urljoin(source_url, f"/fs/elements/{element_id}")
    return endpoint + "?" + urlencode({
        "const_id": constituent_id,
        "show_profile": "true",
    })


def _finalsite_hydrate_emails(
        soup, source_url: str, fetch,
        max_requests: int = 50,
        ) -> Tuple[Dict[str, Field], Dict[str, object]]:
    """Fetch only signaled Finalsite profile popups and recover their emails.

    The returned map is keyed by the constituent ID from the directory page.
    A response is accepted only when its profile wrapper repeats that same ID,
    so a generic or malformed response cannot donate an email to a neighbor.
    """
    element_id, targets = _finalsite_hydration_targets(soup)
    diagnostics: Dict[str, object] = {
        "signaled": bool(element_id and targets),
        "attempted": False,
        "requests": 0,
        "succeeded": 0,
        "failed": 0,
        "failures": [],
    }
    hydrated: Dict[str, Field] = {}
    if not element_id or not targets:
        return hydrated, diagnostics
    if fetch is None:
        diagnostics["failures"] = ["fetch unavailable"]
        return hydrated, diagnostics

    diagnostics["attempted"] = True
    for constituent_id in targets[:max(0, max_requests)]:
        profile_url = _finalsite_profile_hydration_url(
            source_url, element_id, constituent_id)
        diagnostics["requests"] = int(diagnostics["requests"]) + 1
        try:
            response = fetch(profile_url)
        except Exception as exc:
            diagnostics["failed"] = int(diagnostics["failed"]) + 1
            diagnostics["failures"].append(
                f"{constituent_id}: fetch raised {exc}")
            continue
        if not response:
            diagnostics["failed"] = int(diagnostics["failed"]) + 1
            diagnostics["failures"].append(
                f"{constituent_id}: empty response")
            continue
        try:
            profile_soup = BeautifulSoup(response, "lxml")
            wrappers = profile_soup.select(
                f".fsConstituentCustom[data-constituent-id='{constituent_id}'], "
                f".fsConstituentProfile[data-constituent-id='{constituent_id}']")
            email_fields = []
            for wrapper in wrappers:
                for node in wrapper.select(".fsEmail, [id^='fsEmail-']"):
                    email_fields.extend(_finalsite_email_fields(node))
            if not email_fields:
                diagnostics["failed"] = int(diagnostics["failed"]) + 1
                diagnostics["failures"].append(
                    f"{constituent_id}: no safely associated email")
                continue
            hydrated[constituent_id] = max(
                email_fields, key=lambda f: f.confidence)
            diagnostics["succeeded"] = int(diagnostics["succeeded"]) + 1
        except Exception as exc:
            diagnostics["failed"] = int(diagnostics["failed"]) + 1
            diagnostics["failures"].append(
                f"{constituent_id}: response parse failed ({exc})")

    if len(targets) > max_requests:
        diagnostics["failed"] = int(diagnostics["failed"]) + (
            len(targets) - max_requests)
        diagnostics["failures"].append(
            f"hydration capped at {max_requests} profile requests")
    return hydrated, diagnostics


# ---------------------------------------------------------------------------
# Dedupe within a page (same person appearing via multiple stages)
# ---------------------------------------------------------------------------

def _dedupe_persons(people: List[Person]) -> List[Person]:
    """Merge duplicates within a single page:
      key = (lowercased email) if email else (lowercased name)
    When two records collide, keep the higher-confidence one and fill
    in any missing fields from the loser."""
    by_key: dict = {}
    key_to_person: dict = {}

    def identity_keys(person: Person) -> List[Tuple[str, str]]:
        keys = []
        for attr in ("contact_id", "email", "profile_url", "name"):
            field = getattr(person, attr, None)
            if field and field.value:
                if attr == "profile_url":
                    # Contact List Pro often points every record at the same
                    # generic "send message" endpoint. It is not identity
                    # evidence and must not collapse distinct people.
                    profile_path = field.value.split("?", 1)[0].rstrip("/")
                    if profile_path.lower().endswith(("/contact", "/send-message")):
                        continue
                value = field.value.strip().lower()
                keys.append((attr, value))
                # Some school templates render "First Last" in the
                # Contact List Pro block and "Last First" in a generic
                # repeating block. For two-token names, the unordered token
                # key is safe enough to merge those two renderings while
                # retaining the exact-name key for normal cases.
                if attr == "name" and 2 <= len(value.split()) <= 4:
                    keys.append(("name_tokens", " ".join(sorted(value.split()))))
        return keys

    for p in people:
        keys = identity_keys(p)
        if not keys:
            continue
        existing = next(
            (key_to_person[key] for key in keys if key in key_to_person),
            None)
        if existing is not None:
            # Fill fields on the winner from the loser.
            for attr in ("email", "phone", "extension", "title", "department",
                         "photo_url", "profile_url", "first_name", "last_name",
                         "biography", "grade", "room", "contact_id",
                         "contact_name"):
                if getattr(existing, attr) is None and \
                        getattr(p, attr) is not None:
                    setattr(existing, attr, getattr(p, attr))
                else:
                    ex = getattr(existing, attr)
                    ne = getattr(p, attr)
                    if ex is not None and ne is not None:
                        setattr(existing, attr, Field.better_of(ex, ne))
            existing.compute_overall_confidence()
        else:
            by_key[len(by_key)] = p
            existing = p
        for key in identity_keys(existing):
            key_to_person[key] = existing
    return list(by_key.values())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 10 — Accordion / Collapsible sections
# ---------------------------------------------------------------------------

def _persons_from_accordions(soup, source_url: str) -> List[Person]:
    """Extract people from accordion / collapsible sections.

    Many school sites organize staff by department in Bootstrap-style
    accordions or details/summary elements. Each collapsed panel typically
    contains one or more staff entries with name, title, and contact info.
    """
    people: List[Person] = []
    # HTML5 <details> elements
    for details in soup.find_all("details"):
        summary = details.find("summary")
        if not summary:
            continue
        # Each details block may contain multiple people
        for a in details.find_all("a", href=re.compile(r"^mailto:", re.I)):
            p = _person_from_element(
                a.parent.parent if a.parent else a, source_url)
            if p:
                people.append(p)
        # Also try to find card-like children inside the details body
        for child in details.find_all(
                ["div", "li", "article", "tr"],
                recursive=True):
            if child.find("a", href=re.compile(r"^mailto:", re.I)):
                p = _person_from_element(child, source_url)
                if p:
                    people.append(p)

    # Bootstrap / jQuery UI accordion panels
    accordion_selectors = (
        ".accordion .accordion-body",
        ".accordion .panel-body",
        ".accordion .collapse",
        "[data-toggle='collapse']",
        "[data-bs-toggle='collapse']",
        ".ui-accordion-content",
    )
    seen_ids = set()
    for sel in accordion_selectors:
        try:
            panels = soup.select(sel)
        except Exception:
            continue
        for panel in panels:
            if id(panel) in seen_ids:
                continue
            seen_ids.add(id(panel))
            p = _person_from_element(panel, source_url)
            if p:
                people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 11 — ARIA role list items
# ---------------------------------------------------------------------------

def _persons_from_aria_lists(soup, source_url: str) -> List[Person]:
    """Extract people from ARIA-annotated lists.

    Accessibility-forward sites use ``role="list"`` and ``role="listitem"``
    on their staff directories rather than (or in addition to) semantic
    ``<ul>`` / ``<li>`` elements. These are often ``<div>`` based and
    escape the named-card selector net because they lack staff-specific
    class names.
    """
    people: List[Person] = []
    list_containers = soup.find_all(attrs={"role": "list"})
    for container in list_containers:
        items = container.find_all(attrs={"role": "listitem"})
        if len(items) < 3:
            continue
        # Quick check: does the list contain any contact information?
        container_text = container.get_text(" ", strip=True)
        has_contact = bool(
            re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                       container_text)
            or container.find("a", href=re.compile(r"^(mailto|tel):", re.I)))
        if not has_contact:
            continue
        for item in items:
            p = _person_from_element(item, source_url)
            if p:
                people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 12 — WordPress REST API / wp:embed records
# ---------------------------------------------------------------------------

def _persons_from_wp_rest(soup, source_url: str) -> List[Person]:
    """Extract people from WordPress REST API JSON embedded in the page.

    Some WordPress themes embed staff CPT (Custom Post Type) data via
    ``wp:embed`` or ``wp:restapi`` script blocks, or store the JSON in
    a data attribute on the staff container.
    """
    people: List[Person] = []
    seen = set()

    # Look for data-attributes that embed JSON staff data
    for elem in soup.find_all(
            attrs={"data-staff": True}) + soup.find_all(
            attrs={"data-directory": True}) + soup.find_all(
            attrs={"data-people": True}):
        raw = (elem.get("data-staff")
               or elem.get("data-directory")
               or elem.get("data-people")
               or "")
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for record in items:
            if not isinstance(record, dict):
                continue
            for embedded in _walk_embedded_records(record):
                _add_wp_record(embedded, source_url, people, seen)

    # Look for wp:collection links in <script type="application/json">
    for script in soup.find_all("script", type="application/json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, (dict, list)):
            continue
        for record in _walk_embedded_records(data):
            _add_wp_record(record, source_url, people, seen)

    return people


def _add_wp_record(record: dict, source_url: str,
                   people: List[Person], seen: set) -> None:
    """Helper to add a WordPress-style staff record avoiding dupes."""
    def val(*keys):
        for k in keys:
            v = record.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    name = val("name", "full_name", "display_name", "post_title")
    if not name or not _is_probable_name_text(name):
        return
    email = val("email", "staff_email", "user_email")
    title = val("title", "job_title", "position", "staff_title")
    phone = val("phone", "telephone", "staff_phone")
    if not any((email, title, phone)):
        return
    identity = (name.lower(), email.lower())
    if identity in seen:
        return
    seen.add(identity)
    first, last = normalize_name(name)
    person = Person(
        name=Field(name, Confidence.HIGH.value, FieldSource.JSON_EMBEDDED),
        first_name=Field(first, Confidence.HIGH.value,
                         FieldSource.JSON_EMBEDDED) if first else None,
        last_name=Field(last, Confidence.HIGH.value,
                        FieldSource.JSON_EMBEDDED) if last else None,
        title=Field(title, Confidence.HIGH.value,
                    FieldSource.JSON_EMBEDDED) if title else None,
        email=(max(extract_emails(email), key=lambda f: f.confidence)
               if extract_emails(email) else None),
        phone=(max(extract_phones(phone), key=lambda f: f.confidence)
               if extract_phones(phone) else None),
        source_url=source_url,
    )
    person.compute_overall_confidence()
    people.append(person)


# ---------------------------------------------------------------------------
# Stage 13 — Grid/Flex card detector
# ---------------------------------------------------------------------------

def _persons_from_grid_cards(soup, source_url: str) -> List[Person]:
    """Detect staff cards laid out via CSS Grid or Flexbox containers.

    Modern school themes (Apptegy/Thrillshare, Edlio, SchoolPointe) use
    generic ``<div>`` grids with no staff-specific class names. This
    strategy looks for containers with 3+ child ``<div>`` or ``<article>``
    siblings whose inline styles or classes mention grid/flex, then tries
    to extract a person from each child.
    """
    people: List[Person] = []
    grid_flex_re = re.compile(
        r"(display\s*:\s*(grid|flex)|grid-template|flex-wrap"
        r"|staff-grid|people-grid|team-grid|directory-grid"
        r"|staff-flex|people-flex|team-flex|card-grid|card-list)", re.I)

    candidates = []
    for div in soup.find_all(["div", "section", "ul"]):
        style = div.get("style") or ""
        classes = " ".join(div.get("class") or [])
        if grid_flex_re.search(style) or grid_flex_re.search(classes):
            candidates.append(div)

    seen_ids = set()
    for container in candidates:
        children = [
            c for c in container.find_all(
                ["div", "article", "li"], recursive=False)
            if len(c.get_text(strip=True)) > 5
        ]
        if len(children) < 3:
            continue
        # Quick contact-info check across the container
        container_text = container.get_text(" ", strip=True)
        has_contact = bool(
            re.search(
                r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                container_text)
            or container.find("a", href=re.compile(r"^(mailto|tel):", re.I)))
        if not has_contact:
            continue
        for child in children:
            if id(child) in seen_ids:
                continue
            seen_ids.add(id(child))
            p = _person_from_element(child, source_url)
            if p:
                people.append(p)
    return people


# ---------------------------------------------------------------------------
# Stage 16 — Foxbright CMS (Michigan school-district platform, fxbrt.com CDN,
# footer "Powered by Foxbright"). Directory list pages hold
# name/building/position/phone; each row often links to a detail page that
# carries the email. Because Foxbright is server-rendered, this static
# strategy catches it before the Playwright tier is even considered.
# ---------------------------------------------------------------------------

def _persons_from_foxbright(soup, source_url: str) -> List[Person]:
    """Extract staff from ``.fbcms_staffentry`` cards.

    Structure observed across Foxbright installs::

        <div class="fbcms_staffentry">
          <div class="fbcms_staffinfo">
            <div class="name"><a href="/staff/1234">Jane Doe</a></div>
            <div class="row2 building">Central Elementary</div>
            <div class="row3 position">Kindergarten Teacher</div>
            [optional mailto anchor OR link to detail page carrying it]
          </div>
        </div>
    """
    people: List[Person] = []
    entries = soup.select(".fbcms_staffentry, .fbcms_staffinfo")
    if not entries:
        return people
    # Deduplicate wrappers — some skins nest .fbcms_staffinfo inside
    # .fbcms_staffentry and both would match otherwise.
    seen: set = set()
    for entry in entries:
        if id(entry) in seen:
            continue
        # Prefer the outermost wrapper we saw.
        parent = entry.find_parent(class_="fbcms_staffentry")
        if parent is not None and id(parent) in seen:
            continue
        anchor = parent if parent is not None else entry
        seen.add(id(anchor))

        name_el = anchor.select_one(".name")
        if not name_el:
            continue
        name = _clean(_get_text(name_el))
        if not name or len(name) < 3 or not _is_probable_name_text(name):
            continue

        building_el = anchor.select_one(".building, .row2")
        position_el = anchor.select_one(".position, .row3")
        building = _clean(_get_text(building_el)) if building_el else ""
        position = _clean(_get_text(position_el)) if position_el else ""

        # Grab email off the card if it's there (mailto or Cloudflare).
        emails = extract_emails(anchor)
        email_field = emails[0] if emails else None

        # Detail-page URL — the profile that carries the email when the
        # list page doesn't. Pipeline hydration follows this later.
        detail_url = ""
        det = name_el.find("a", href=True) or anchor.find(
            "a", href=re.compile(r"(staff|profile|detail|/user)", re.I))
        if det and det.get("href"):
            detail_url = urljoin(source_url, det["href"])

        first, last = normalize_name(name)
        person = Person(
            name=Field(name, Confidence.HIGH.value, FieldSource.CARD_TEXT),
            first_name=Field(first, Confidence.HIGH.value,
                             FieldSource.CARD_TEXT) if first else None,
            last_name=Field(last, Confidence.HIGH.value,
                            FieldSource.CARD_TEXT) if last else None,
            email=email_field,
            title=Field(position, Confidence.HIGH.value,
                        FieldSource.CARD_TEXT) if position else None,
            department=Field(building, Confidence.HIGH.value,
                             FieldSource.CARD_TEXT) if building else None,
            profile_url=Field(detail_url, Confidence.HIGH.value,
                              FieldSource.CARD_TEXT) if detail_url else None,
            source_url=source_url,
        )
        person.compute_overall_confidence()
        people.append(person)
    return people


_STRATEGIES = [
    ("finalsite_constituent", _persons_from_finalsite),
    ("contact_list_pro",  _persons_from_contact_list_pro),
    ("json_ld",           _persons_from_json_ld),
    ("embedded_json",     _persons_from_embedded_json),
    ("microdata",         _persons_from_microdata),
    ("hcard",             _persons_from_hcard),
    ("tables",            _persons_from_tables),
    ("named_cards",       _persons_from_named_cards),
    ("repeating_blocks",  _persons_from_repeating_blocks),
    ("dl",                _persons_from_dl),
    ("mailto",            _persons_from_mailto),
    ("accordions",        _persons_from_accordions),
    ("aria_lists",        _persons_from_aria_lists),
    ("wp_rest",           _persons_from_wp_rest),
    ("grid_cards",        _persons_from_grid_cards),
    ("foxbright",         _persons_from_foxbright),
]


def _strip_noise(soup) -> None:
    """Remove chrome that pollutes person extraction. **Preserves**
    <script type="application/ld+json"> because the JSON-LD extractor
    needs them."""
    for tag in soup.find_all(["script", "style", "noscript", "select",
                              "option", "datalist"]):
        try:
            if tag.name == "script" and (
                    (tag.get("type") or "").lower() == "application/ld+json"
                    or (tag.get("type") or "").lower() == "application/json"
                    or (tag.get("id") or "").lower() == "__next_data__"
                    or "FS.util.insertEmail" in (tag.get_text() or "")):
                continue
            tag.decompose()
        except Exception: pass
    # nav/footer only if they don't contain mailto (Some sites list
    # admin emails in the footer — don't discard them).
    for tag in soup.find_all(["nav", "footer"]):
        try:
            if tag.select_one(
                    "a[rel='next'], .pagination a, .pager a, "
                    "[class*='page-nav' i] a, [class*='paging' i] a, "
                    "[aria-label*='pagination' i] a, "
                    "[aria-label*='paging' i] a"):
                continue
            if not tag.find("a", href=re.compile(r"^mailto:", re.I)):
                tag.decompose()
        except Exception: pass


def _finalsite_pagination_urls(soup, source_url: str) -> List[str]:
    """Find bounded Finalsite constituent page links only."""
    search_params = []
    search_form = soup.select_one("form.fsConstituentSearchForm")
    if search_form:
        for input_node in search_form.select("input[name]"):
            input_type = (input_node.get("type") or "").lower()
            name = (input_node.get("name") or "").strip()
            if (not name or input_type in {"button", "submit", "reset"} or
                    not name.startswith("const_")):
                continue
            search_params.append((name, input_node.get("value") or ""))

    urls = []
    for a in soup.select(
            "a.fsNextPageLink, a.fsLastPageLink, "
            "a[href*='const_page'], a[data-page]"):
        href = (a.get("href") or "").strip()
        raw_page = a.get("data-page") or ""
        match = re.search(r"(?:^|[?&])const_page=(\d+)", href)
        page = int(raw_page) if str(raw_page).isdigit() else (
            int(match.group(1)) if match else 0)
        if page <= 1 or not href:
            continue
        full = urljoin(source_url, href)
        if search_params:
            parsed = urlsplit(full)
            existing = parse_qsl(parsed.query, keep_blank_values=True)
            existing_names = {key for key, _ in existing}
            existing.extend(
                (key, value) for key, value in search_params
                if key not in existing_names)
            full = urlunsplit(parsed._replace(
                query=urlencode(existing, doseq=True)))
        if full not in urls:
            urls.append(full)
    return urls


def _normalize_pagination_url(url: str) -> str:
    """Normalize a URL for pagination dedup — strip trailing slashes,
    remove page=1 / const_page=1, lowercase the host."""
    parsed = urlsplit(url)
    # Lowercase the host
    normalized = parsed._replace(netloc=parsed.netloc.lower())
    # Strip trailing slash from path (but keep root /)
    path = normalized.path.rstrip("/") or "/"
    normalized = normalized._replace(path=path)
    # Remove page=1 style params that are aliases for the base page
    params = parse_qsl(normalized.query, keep_blank_values=True)
    page_1_keys = {"page", "p", "pg", "paged", "const_page"}
    filtered = [
        (k, v) for k, v in params
        if not (k.lower() in page_1_keys and v in ("1", "0"))
    ]
    normalized = normalized._replace(query=urlencode(filtered, doseq=True))
    return urlunsplit(normalized)


def _generic_pagination_urls(soup, source_url: str) -> List[str]:
    """Find same-site numbered/next links from a real pagination control.

    The legacy browser scraper handled both numbered controls and Next
    buttons. This static equivalent intentionally ignores arbitrary numeric
    links and only accepts anchors inside pagination containers, ``rel=next``,
    or links whose URL carries a page-like parameter.

    Also detects load-more buttons and data-attribute pagination used by
    modern CMS themes (Apptegy, SchoolPointe, Edlio).
    """
    pagination_classes = re.compile(
        r"(pagination|pager|page-nav|page-navigation|paging"
        r"|load-more|loadmore|view-more)", re.I)
    numbered: List[Tuple[int, str]] = []
    next_urls: List[str] = []
    source = urlsplit(source_url)
    source_norm = _normalize_pagination_url(source_url)
    seen = {source_norm}

    # --- Standard <a href> pagination links ---
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(source_url, href)
        parsed = urlsplit(url)
        if parsed.netloc and parsed.netloc.lower() != source.netloc.lower():
            continue
        norm = _normalize_pagination_url(url)
        rel = " ".join(anchor.get("rel") or []).lower()
        text = _clean(_get_text(anchor))
        aria = _clean(anchor.get("aria-label") or "")
        title = _clean(anchor.get("title") or "")
        combined = f"{text} {aria} {title}".lower()
        anchor_classes = " ".join(anchor.get("class") or [])
        parent = (
            anchor if pagination_classes.search(anchor_classes)
            else anchor.find_parent(attrs={"class": pagination_classes}))
        url_has_page = bool(re.search(
            r"(?:[?&](?:page|p|pg|paged|offset|start)=\d+|/page/\d+)",
            url, re.I))
        is_number = text.isdigit() and parent is not None
        is_next = (
            rel == "next" or
            bool(re.fullmatch(r"(?:next|next page|>|›|»|→|>>)", text, re.I)) or
            bool(re.search(r"\bnext\s*(?:page|»|>|→)?\b", combined))
        )
        # Load-more / View-more buttons that are actually links
        is_load_more = bool(re.search(
            r"\b(load\s*more|view\s*more|show\s*more|see\s*all)\b",
            combined)) and url_has_page
        if not (is_number or is_next or is_load_more
                or (url_has_page and parent is not None)):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        if is_number:
            number = int(text)
            if number > 1:
                numbered.append((number, url))
        elif is_next or url_has_page or is_load_more:
            next_urls.append(url)

    # --- data-href / data-url / data-page-url pagination buttons ---
    for elem in soup.select(
            "[data-href], [data-url], [data-page-url], "
            "button[data-page], [data-next-page-url]"):
        data_url = (
            elem.get("data-href") or elem.get("data-url")
            or elem.get("data-page-url") or elem.get("data-next-page-url")
            or "")
        if not data_url:
            continue
        url = urljoin(source_url, data_url)
        parsed = urlsplit(url)
        if parsed.netloc and parsed.netloc.lower() != source.netloc.lower():
            continue
        norm = _normalize_pagination_url(url)
        if norm in seen:
            continue
        seen.add(norm)
        next_urls.append(url)

    numbered.sort(key=lambda item: item[0])
    return [url for _, url in numbered] + next_urls


def _extract_single_page(
        html_or_soup, url: str, *,
        min_persons: int, keep_confidence_below: float,
        classify_first: bool, fetch=None,
        max_profile_hydrations: int = 50,
        ) -> Tuple[List[Person], ExtractionReport, object]:
    report = ExtractionReport(url=url)
    if BeautifulSoup is None:
        raise RuntimeError("bs4 is required")
    if isinstance(html_or_soup, (str, bytes)):
        soup = BeautifulSoup(html_or_soup, "lxml")
    else:
        soup = html_or_soup

    if classify_first:
        pc = classify_page(soup, url)
        report.page_classification = pc
        if pc.page_type not in (PageType.STAFF_DIRECTORY,
                                PageType.DEPARTMENT_INDEX):
            report.warnings.append(
                f"page classified as {pc.page_type.value} "
                f"(conf {pc.confidence:.2f}); normal directory extraction "
                "rejected")
            report.strategy_used = "rejected_page_type"
            return [], report, soup

    report.js_rendered = bool(
        soup.select_one("[aria-busy='true'], .fsLoadingDotsOverlay") or
        (soup.select(".fsConstituent") and
         not soup.select(".fsConstituentItem")))

    hydrated_email_by_id: Dict[str, Field] = {}
    hydration_diagnostics = {
        "signaled": False, "attempted": False, "requests": 0,
        "succeeded": 0, "failed": 0, "failures": [],
    }
    # A page with modal-only profile links and no statically rendered
    # constituent emails is the narrow signal for this optional path.
    if (soup.select(".fsConstituentItem") and
            not _finalsite_email_nodes(soup)):
        hydrated_email_by_id, hydration_diagnostics = (
            _finalsite_hydrate_emails(
                soup, url, fetch, max_requests=max_profile_hydrations))
    report.js_hydration_attempted = bool(
        hydration_diagnostics["attempted"])
    report.js_hydration_requests = int(hydration_diagnostics["requests"])
    report.js_hydration_succeeded = int(hydration_diagnostics["succeeded"])
    report.js_hydration_failed = int(hydration_diagnostics["failed"])
    if hydration_diagnostics["signaled"]:
        if hydration_diagnostics["attempted"]:
            report.strategy_results["finalsite_hydration"] = (
                f"{report.js_hydration_succeeded}/"
                f"{len(_finalsite_hydration_targets(soup)[1])} profiles hydrated")
        else:
            report.strategy_results["finalsite_hydration"] = (
                "skipped: fetch unavailable")
        for failure in hydration_diagnostics["failures"]:
            report.warnings.append(
                f"Finalsite JS-only profile hydration: {failure}")

    _strip_noise(soup)
    all_people: List[Person] = []
    strategy_counts = {}
    for name, fn in _STRATEGIES:
        report.strategies_tried.append(name)
        try:
            if name == "finalsite_constituent":
                people = _dedupe_persons(
                    fn(soup, url, hydrated_email_by_id))
            else:
                people = _dedupe_persons(fn(soup, url))
            report.strategy_results[name] = "ok"
        except Exception as exc:
            report.strategy_results[name] = f"error: {exc}"
            report.warnings.append(f"strategy {name} raised: {exc}")
            people = []
        strategy_counts[name] = len(people)
        report.candidates_by_strategy[name] = len(people)
        report.validated_by_strategy[name] = len(people)
        if people:
            report.recovered_by_strategy[name] = len(people)
        all_people.extend(people)

    merged = _dedupe_persons(all_people)
    report.candidates_seen = len(all_people)
    report.merged_records = len(merged)
    report.emails_found = sum(bool(p.email) for p in merged)
    report.phones_found = sum(bool(p.phone) for p in merged)

    if merged:
        best_strategy = max(
            strategy_counts, key=lambda name: strategy_counts[name])
        contributors = sum(v > 0 for v in strategy_counts.values())
        if (best_strategy in {"finalsite_constituent", "contact_list_pro"} and
                strategy_counts[best_strategy] == len(merged)):
            report.strategy_used = best_strategy
        elif strategy_counts[best_strategy] >= min_persons:
            report.strategy_used = (
                "multi_pass" if contributors > 1 else best_strategy)
        elif best_strategy in {"finalsite_constituent", "contact_list_pro"}:
            report.strategy_used = best_strategy
        elif len(merged) >= min_persons:
            report.strategy_used = "multi_pass"
        else:
            report.strategy_used = "best_effort"
            report.warnings.append(
                f"no strategy hit min_persons={min_persons}; "
                f"returning best-effort bag of {len(merged)}")
    else:
        report.strategy_used = "best_effort"

    kept = []
    for person in merged:
        if (keep_confidence_below > 0 and
                person.overall_confidence < keep_confidence_below):
            report.drop(f"below_conf<{keep_confidence_below}")
        kept.append(person)
    report.persons_returned = len(kept)
    return kept, report, soup


def extract_staff_directory(
        html_or_soup, url: str = "", *,
        min_persons: int = 2,
        keep_confidence_below: float = 0.4,
        classify_first: bool = True,
        fetch=None,
        max_directory_pages: int = 20,
        max_profile_hydrations: int = 50,
) -> Tuple[List[Person], ExtractionReport]:
    """Extract and merge all validated strategies for a directory page."""
    people, report, soup = _extract_single_page(
        html_or_soup, url, min_persons=min_persons,
        keep_confidence_below=keep_confidence_below,
        classify_first=classify_first, fetch=fetch,
        max_profile_hydrations=max_profile_hydrations)

    if (fetch is None or max_directory_pages <= 1 or
            not report.page_classification or
            report.page_classification.page_type != PageType.STAFF_DIRECTORY):
        return people, report

    pages_seen = 1
    report.pagination_urls = []
    is_finalsite = bool(
        report.candidates_by_strategy.get("finalsite_constituent"))
    pending = _finalsite_pagination_urls(soup, url) if is_finalsite else []
    for page_url in _generic_pagination_urls(soup, url):
        if page_url not in pending:
            pending.append(page_url)
    # Use normalized URLs for the visited set so that page=1,
    # trailing-slash variants, and case differences don't cause
    # the same page to be fetched twice.
    visited = {_normalize_pagination_url(url)}
    while pending and pages_seen < max_directory_pages:
        page_url = pending.pop(0)
        page_norm = _normalize_pagination_url(page_url)
        if page_norm in visited:
            continue
        visited.add(page_norm)
        try:
            page_html = fetch(page_url)
        except Exception as exc:
            report.warnings.append(
                f"pagination fetch raised for {page_url}: {exc}")
            continue
        if not page_html:
            report.warnings.append(f"pagination fetch failed: {page_url}")
            continue
        report.pagination_urls.append(page_url)
        page_people, page_report, page_soup = _extract_single_page(
            page_html, page_url, min_persons=min_persons,
            keep_confidence_below=keep_confidence_below,
            # The first page has already established a valid Finalsite
            # directory. A final page can contain one remaining constituent,
            # which is not independently enough to pass the conservative
            # multi-person classifier.
            classify_first=False, fetch=fetch,
            max_profile_hydrations=max_profile_hydrations)
        pages_seen += 1
        people = _dedupe_persons(people + page_people)
        next_page_urls = _finalsite_pagination_urls(
            page_soup, page_url) if is_finalsite else []
        for candidate in _generic_pagination_urls(page_soup, page_url):
            if candidate not in next_page_urls:
                next_page_urls.append(candidate)
        pending_norms = {_normalize_pagination_url(p) for p in pending}
        pending.extend(
            p for p in next_page_urls
            if _normalize_pagination_url(p) not in visited
            and _normalize_pagination_url(p) not in pending_norms)
        report.strategies_tried = list(dict.fromkeys(
            report.strategies_tried + page_report.strategies_tried))
        report.warnings.extend(page_report.warnings)
        report.candidates_seen += page_report.candidates_seen
        for key, value in page_report.candidates_by_strategy.items():
            report.candidates_by_strategy[key] = (
                report.candidates_by_strategy.get(key, 0) + value)
        for key, value in page_report.validated_by_strategy.items():
            report.validated_by_strategy[key] = (
                report.validated_by_strategy.get(key, 0) + value)
        for key, value in page_report.recovered_by_strategy.items():
            report.recovered_by_strategy[key] = (
                report.recovered_by_strategy.get(key, 0) + value)
        report.js_rendered = report.js_rendered or page_report.js_rendered
        report.js_hydration_attempted = (
            report.js_hydration_attempted or
            page_report.js_hydration_attempted)
        report.js_hydration_requests += page_report.js_hydration_requests
        report.js_hydration_succeeded += page_report.js_hydration_succeeded
        report.js_hydration_failed += page_report.js_hydration_failed
        if report.js_hydration_attempted:
            report.strategy_results["finalsite_hydration"] = (
                f"{report.js_hydration_succeeded} succeeded, "
                f"{report.js_hydration_failed} failed across "
                f"{report.js_hydration_requests} requests")
        report.emails_found = sum(bool(p.email) for p in people)
        report.phones_found = sum(bool(p.phone) for p in people)
        report.merged_records = len(people)
    report.pagination_pages = pages_seen
    report.persons_returned = len(people)
    return people, report
