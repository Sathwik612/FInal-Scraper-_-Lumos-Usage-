"""
staffdir.playwright_fallback
============================

**JS-rendered fallback tier.** When the static HTML extractors in
``staffdir.directory`` return zero persons — or persons with no emails
— for a discovered directory URL, this module drives a headless
Chromium via Playwright to see what a real browser would see.

Three techniques are tried in order, and *whichever produces persons
with emails wins*. Each one is self-contained and safe to skip if
Playwright is not installed; the pipeline degrades gracefully to
HTML-only behaviour in that case.

Technique 1 — Direct Visible Emails
-----------------------------------
The most common case: ``<a href="mailto:...">`` anchors are actually
present in the DOM but only after JavaScript renders them. We select
every mailto anchor, walk up the DOM until we hit an ancestor with
three or more text lines (that's the staff "card"), and read
name / title / school off the lines that don't contain ``@``.

Pagination is attempted three ways in this order:
  * URL parameter (``?page_no=N``) up to ``max_pages``
  * "Next" button click (``a:has-text('>')``) until it disappears
     or is marked disabled

Technique 2 — Hidden in Icon
----------------------------
Structurally identical to Technique 1 (still ``a[href^='mailto:']``),
but the anchor is styled as an envelope icon so nothing readable is
visible next to it. The DOM-walk still finds the enclosing card.
Handled by the same extractor — the ``pagination='next_button'``
mode covers the icon-heavy Ladue / Wentzville pattern.

Technique 3 — Linked to Name (Finalsite modal)
----------------------------------------------
Finalsite directories built on ``.fsConstituentItem`` frequently put
the email behind a click: you click the person's name, a modal or
side panel opens, and only *then* is ``.fsEmail`` populated. We open
each profile in turn, extract from ``.fsEmail`` /
``.fsStyleSROnly``, and close. Pagination via the ``a[data-page]``
buttons Finalsite renders.

Public entry point
------------------
::

    from staffdir.playwright_fallback import (
        extract_with_playwright, is_playwright_available)

    if is_playwright_available():
        persons, report = extract_with_playwright(
            "https://example.k12.us/staff", max_pages=10)

Every returned ``Person`` uses the same schema as
``staffdir.directory``, so pipeline-level code doesn't need to
special-case them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from .schema import (
    Confidence, ExtractionReport, Field, FieldSource, Person,
)

logger = logging.getLogger("staffdir.playwright_fallback")


# ---------------------------------------------------------------------------
# Optional-dependency guard
# ---------------------------------------------------------------------------

try:
    from playwright.async_api import async_playwright                  # type: ignore
    _PLAYWRIGHT_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:                                                # pragma: no cover
    async_playwright = None                                             # type: ignore
    _PLAYWRIGHT_IMPORT_ERROR = exc


def is_playwright_available() -> bool:
    """True iff ``playwright`` is importable in this environment.

    The pipeline calls this before scheduling a fallback pass so it
    can log a single informative warning per run instead of failing
    silently on every row.
    """
    return async_playwright is not None


# ---------------------------------------------------------------------------
# Small helpers shared across techniques
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WS_RE = re.compile(r"\s+")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def _valid_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    # Reject obvious junk: role addresses, image files, template placeholders.
    lowered = email.strip().lower()
    if any(bad in lowered for bad in (
            "example.com", "yourdomain", "@sentry",
            ".png", ".jpg", ".gif", ".svg")):
        return False
    return bool(_EMAIL_RE.fullmatch(lowered))


def _parse_name(name: str) -> Tuple[str, str]:
    """Split "Jane Q. Doe" into ("Jane", "Q. Doe"). Same rule as
    ``parse_name`` in the reference Colab notebooks so behaviour
    stays predictable when the two produce the same record."""
    parts = _clean(name).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _email_from_text(text: str) -> str:
    if not text:
        return ""
    match = _EMAIL_RE.search(text)
    return match.group(0).lower() if match else ""


def _mk_field(value: str, *, source: FieldSource,
              conf: float = Confidence.HIGH.value) -> Optional[Field]:
    value = _clean(value)
    if not value:
        return None
    return Field(value, conf, source)


def _person_from_lines(lines: List[str], email: str,
                       *, source_url: str, phone: str = "",
                       profile_url: str = "") -> Optional[Person]:
    """Assemble a Person from card lines + a mailto-derived email.

    Line 0 is the name, line 1 is the role (title), line 2 is the
    school / location if present. Rejects records with an obviously
    invalid name so we don't emit "Read More" or "Photo" as people.
    """
    clean_lines = [_clean(l) for l in lines if _clean(l) and "@" not in l]
    if not clean_lines:
        return None
    name = clean_lines[0]
    if len(name) < 2 or not re.search(r"[A-Za-z]", name):
        return None
    role = clean_lines[1] if len(clean_lines) >= 2 else ""
    school = clean_lines[2] if len(clean_lines) >= 3 else ""
    first, last = _parse_name(name)

    email_field = _mk_field(email.lower(), source=FieldSource.MAILTO,
                            conf=Confidence.CERTAIN.value) if email else None
    name_field = _mk_field(name, source=FieldSource.CARD_TEXT)
    if name_field is None:
        return None

    person = Person(
        name=name_field,
        first_name=_mk_field(first, source=FieldSource.CARD_TEXT)
            if first else None,
        last_name=_mk_field(last, source=FieldSource.CARD_TEXT)
            if last else None,
        email=email_field,
        phone=_mk_field(phone, source=FieldSource.TEL_LINK) if phone else None,
        title=_mk_field(role, source=FieldSource.CARD_TEXT) if role else None,
        department=_mk_field(school, source=FieldSource.CARD_TEXT)
            if school else None,
        profile_url=_mk_field(profile_url, source=FieldSource.CARD_TEXT,
                              conf=Confidence.MEDIUM.value)
            if profile_url else None,
        source_url=source_url,
    )
    person.compute_overall_confidence()
    return person


def _dedupe(records: List[Person]) -> List[Person]:
    """Merge by contact_id if present, else lowercased email, else
    lowercased name.

    Lightweight version of ``staffdir.directory._dedupe_persons`` —
    keying on contact_id first matches how the static tier keys, so
    when both tiers see the same Finalsite person the pipeline-level
    merge collapses them into one enriched row.
    """
    seen: dict[str, Person] = {}
    for p in records:
        key = ""
        if p.contact_id and p.contact_id.value:
            key = f"cid:{p.contact_id.value.strip().lower()}"
        elif p.email and p.email.value:
            key = f"em:{p.email.value.strip().lower()}"
        elif p.name and p.name.value:
            key = f"nm:{p.name.value.strip().lower()}"
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = p
            continue
        # Fill missing fields on the incumbent from the newcomer.
        for attr in ("email", "phone", "title", "department", "profile_url",
                     "first_name", "last_name", "contact_id"):
            if getattr(existing, attr) is None and getattr(p, attr) is not None:
                setattr(existing, attr, getattr(p, attr))
        existing.compute_overall_confidence()
    return list(seen.values())


# ---------------------------------------------------------------------------
# JS snippet: walk up the DOM until the ancestor has >= 3 text lines.
# ---------------------------------------------------------------------------

_CARD_WALKER_JS = """
(el) => {
    let node = el;
    while (node) {
        const text = node.innerText || "";
        const lines = text.split("\\n").map(x => x.trim()).filter(Boolean);
        if (lines.length >= 3) {
            return node;
        }
        node = node.parentElement;
    }
    return el.parentElement;
}
"""


# ---------------------------------------------------------------------------
# Technique 1 + 2 — mailto anchor + DOM-walk (Direct Visible / Hidden Icon)
# ---------------------------------------------------------------------------

# Selectors that reveal an email when clicked. Many school themes render
# the address as an envelope button that only exposes the mailto: href
# after a click / focus event. Walking these BEFORE the mailto scrape
# converts the "hidden in icon" case into the "direct visible" case.
_ICON_REVEAL_SELECTORS = (
    "[class*='email' i][role='button']",
    "button[class*='email' i]",
    "button[class*='mail' i]",
    "button[aria-label*='email' i]",
    "a[aria-label*='email' i]",
    "a[title*='email' i]",
    "[class*='envelope' i]",
    "i.fa-envelope",
    ".fa-envelope",
    "svg[class*='mail' i]",
    ".reveal-email, .show-email, [data-reveal-email]",
)


async def _reveal_hidden_emails(page, cap: int = 200) -> int:
    """Click every visible envelope-icon-shaped element so pages that
    hide the mailto behind a JS reveal expose it in the DOM.

    Returns the number of clicks issued (for logging). Failures are
    intentionally silent — many of these selectors match decorative
    elements too, and we don't want one broken click to abort the
    whole reveal pass.
    """
    clicks = 0
    for selector in _ICON_REVEAL_SELECTORS:
        try:
            handles = await page.query_selector_all(selector)
        except Exception:
            continue
        for handle in handles[:cap]:
            try:
                if not await handle.is_visible():
                    continue
                await handle.click(timeout=600, force=True)
                clicks += 1
                # Small yield so the reveal-script gets a chance to run
                # before we click the next one. Cheap and worth it.
                await page.wait_for_timeout(80)
            except Exception:
                continue
        if clicks >= cap:
            break
    # If we opened anything modal-shaped, close it so pagination still works.
    if clicks:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
        except Exception:
            pass
    return clicks


async def _scroll_to_bottom(page, step_px: int = 900,
                             max_steps: int = 12) -> None:
    """Trigger lazy-loading by scrolling incrementally. Some directories
    only append staff cards to the DOM as they enter the viewport."""
    try:
        for _ in range(max_steps):
            await page.evaluate(
                f"window.scrollBy(0, {step_px})")
            await page.wait_for_timeout(150)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def _extract_mailto_page(page, page_url: str) -> List[Person]:
    """Scrape every mailto anchor on the current page into a Person.

    Two enhancements over a naive scan: we scroll the page to trigger
    lazy-loaded cards, and we click every envelope-icon-shaped element
    to force JS-hidden addresses into the DOM before reading them.
    """
    # Give lazy loaders a chance to populate before we look.
    await _scroll_to_bottom(page)
    # Convert "hidden in icon" pages into "direct visible" pages.
    revealed = await _reveal_hidden_emails(page)
    if revealed:
        logger.debug("_extract_mailto_page revealed %d icon(s) on %s",
                     revealed, page_url)

    try:
        await page.wait_for_selector("a[href^='mailto:']", timeout=15000)
    except Exception:
        # Retry once after a hard reload — the notebook version does the
        # same because a lot of Finalsite / Blackboard pages return an
        # empty shell on first paint.
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            await page.wait_for_selector("a[href^='mailto:']", timeout=8000)
        except Exception:
            return []

    email_anchors = await page.query_selector_all("a[href^='mailto:']")
    records: List[Person] = []
    seen_emails: set[str] = set()

    for anchor in email_anchors:
        try:
            href = await anchor.get_attribute("href")
            if not href:
                continue
            email = href.replace("mailto:", "").split("?", 1)[0].strip().lower()
            if not _valid_email(email) or email in seen_emails:
                continue

            card_handle = await anchor.evaluate_handle(_CARD_WALKER_JS)
            if card_handle is None:
                continue
            text = await card_handle.evaluate("(el) => el.innerText || ''")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            # Collapse consecutive duplicate lines — CMSes love printing
            # the same string twice for a11y and for visual layout.
            dedup_lines: List[str] = []
            for line in lines:
                if not dedup_lines or line != dedup_lines[-1]:
                    dedup_lines.append(line)

            # Look for a tel: link inside the same card so we surface a
            # phone number when it's available.
            phone = ""
            try:
                tel = await card_handle.query_selector("a[href^='tel:']")
                if tel is not None:
                    tel_href = await tel.get_attribute("href")
                    if tel_href:
                        phone = tel_href.replace("tel:", "").strip()
            except Exception:
                pass

            person = _person_from_lines(
                dedup_lines, email,
                source_url=page_url, phone=phone)
            if person:
                seen_emails.add(email)
                records.append(person)
        except Exception as exc:
            logger.debug("mailto card extract failed: %s", exc)
            continue
    return records


def _add_page_param(base_url: str, page_no: int) -> str:
    if page_no <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page_no={page_no}"


async def _paginate_by_url(page, base_url: str, max_pages: int) -> List[Person]:
    """Technique 1: iterate ``?page_no=N``.

    Natural termination — ``max_pages`` is only a safety valve, not a
    routine tunable. We stop as soon as any of these fire:

      * Two consecutive empty pages (directory is smaller than we
        asked for).
      * Two consecutive pages returning the same emails as the
        previous page (Finalsite / Blackboard "clamp": page 999
        returns the same as the last real page, so a naive loop
        would spin forever fetching the same records).
      * Navigation error twice in a row (site down, rate-limit).

    In practice this means a 3-page directory stops at page 4, a
    30-page directory stops at page 31, and the safety cap only
    kicks in on truly pathological cases (>100 pages of unique
    staff records — vanishingly rare in K-12).
    """
    all_records: List[Person] = []
    empty_streak = 0
    duplicate_streak = 0
    last_page_signature: Optional[frozenset] = None
    for page_no in range(1, max_pages + 1):
        url = _add_page_param(base_url, page_no)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3500)
        except Exception as exc:
            logger.debug("paginate-by-url goto %s failed: %s", url, exc)
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue
        page_records = await _extract_mailto_page(page, url)
        if not page_records:
            empty_streak += 1
            if empty_streak >= 2:
                logger.info(
                    "paginate-by-url: 2 consecutive empty pages at "
                    "%d — natural end of directory", page_no)
                break
            continue
        # Signature-based duplicate detection. A site that clamps
        # page_no=999 to the last real page would otherwise never
        # trigger the empty-streak exit.
        signature = frozenset(
            (r.email.value.lower() if r.email else "",
             r.name.value.lower() if r.name else "")
            for r in page_records)
        if last_page_signature is not None and signature == last_page_signature:
            duplicate_streak += 1
            if duplicate_streak >= 2:
                logger.info(
                    "paginate-by-url: page %d returns same records "
                    "as page %d — pagination clamped, stopping",
                    page_no, page_no - 2)
                break
            # Don't extend all_records on a duplicate.
            continue
        duplicate_streak = 0
        last_page_signature = signature
        empty_streak = 0
        all_records.extend(page_records)
    else:
        # for-else fires when we exhausted range(max_pages) without break.
        # That means we hit the safety cap — worth a warning so operators
        # know to bump it if they trust the site. Only trigger when the
        # cap is genuinely restrictive (≥ default of 500); if a caller
        # deliberately set a low cap for testing they don't need us
        # nagging.
        if max_pages >= 500:
            logger.warning(
                "paginate-by-url: hit safety cap of %d pages on %s — "
                "mega-district scale; raise --playwright-max-pages if "
                "the site legitimately goes deeper",
                max_pages, base_url)
    return all_records


async def _paginate_by_next_button(page, base_url: str,
                                    max_pages: int) -> List[Person]:
    """Technique 2: click ``a:has-text('>')`` until it disappears or
    is marked as disabled."""
    all_records: List[Person] = []
    try:
        await page.goto(base_url, wait_until="domcontentloaded",
                        timeout=60000)
        await page.wait_for_timeout(4000)
    except Exception as exc:
        logger.debug("next-button initial goto failed: %s", exc)
        return []

    page_index = 1
    while page_index <= max_pages:
        page_records = await _extract_mailto_page(page, page.url)
        all_records.extend(page_records)

        try:
            next_btn = await page.query_selector("a:has-text('>')")
            if next_btn is None:
                break
            classes = (await next_btn.get_attribute("class")) or ""
            if "disabled" in classes.lower():
                break
            aria_disabled = (await next_btn.get_attribute("aria-disabled")) or ""
            if aria_disabled.lower() == "true":
                break
            await next_btn.click(force=True)
            await page.wait_for_timeout(3500)
            page_index += 1
        except Exception as exc:
            logger.debug("next-button click failed: %s", exc)
            break
    return all_records


# ---------------------------------------------------------------------------
# Technique 3 — Finalsite: click .fsConstituentItem name to open modal
# ---------------------------------------------------------------------------

async def _extract_email_from_element(element) -> str:
    """Try three ways to pull an email out of a Finalsite subtree.

    1. ``a[href^='mailto:']`` inside the element.
    2. ``.fsStyleSROnly`` screen-reader-only email text.
    3. Free-text regex over the element's ``textContent``.
    """
    try:
        anchors = await element.query_selector_all("a[href^='mailto:']")
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            if not href:
                continue
            match = re.search(r"mailto:([^?]+)", href, flags=re.I)
            if match:
                email = _clean(match.group(1)).lower()
                if _valid_email(email):
                    return email
    except Exception:
        pass

    try:
        sr_nodes = await element.query_selector_all(".fsStyleSROnly")
        for node in sr_nodes:
            text = await node.text_content()
            email = _email_from_text(text or "")
            if _valid_email(email):
                return email
    except Exception:
        pass

    try:
        text = await element.text_content()
        email = _email_from_text(text or "")
        if _valid_email(email):
            return email
    except Exception:
        pass
    return ""


async def _extract_phone_from_element(element) -> str:
    try:
        anchors = await element.query_selector_all("a[href^='tel:']")
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            if href:
                return href.replace("tel:", "").strip()
    except Exception:
        pass
    return ""


# Track which base URLs we've already emitted the mega-district cap
# warning for, so it fires at most once per URL rather than once per
# technique invocation (T1/T3/etc. each visit the same URL).
_CAP_WARNED_URLS: set = set()


async def _extract_finalsite_pages(page, base_url: str,
                                    max_pages: int) -> List[Person]:
    """Technique 3: iterate every ``.fsConstituentItem`` on every
    Finalsite pagination page, opening any card that doesn't already
    have a visible email."""
    try:
        await page.goto(base_url, wait_until="domcontentloaded",
                        timeout=60000)
        await page.wait_for_timeout(3000)
    except Exception as exc:
        logger.debug("finalsite goto failed: %s", exc)
        return []

    # Detect declared page numbers from the pagination bar so we don't
    # over-shoot small directories.
    page_numbers: List[int] = []
    try:
        pagination = await page.query_selector_all("a[data-page]")
        for anchor in pagination:
            value = await anchor.get_attribute("data-page")
            if value and value.isdigit():
                page_numbers.append(int(value))
    except Exception:
        pass
    declared_total = max(page_numbers) if page_numbers else 1
    total_pages = min(declared_total, max_pages)
    if declared_total > 1:
        if declared_total > max_pages:
            if base_url not in _CAP_WARNED_URLS:
                _CAP_WARNED_URLS.add(base_url)
                logger.warning(
                    "finalsite: %s declares %d pages, extracting "
                    "first %d (this is a mega-district-scale "
                    "directory — the safety cap kicked in). Set "
                    "--playwright-max-pages %d if you need every "
                    "page.",
                    base_url, declared_total, max_pages,
                    declared_total + 50)
        else:
            logger.info(
                "finalsite: %d pagination pages detected on %s — "
                "extracting all", declared_total, base_url)

    all_records: List[Person] = []
    for page_no in range(1, total_pages + 1):
        if page_no > 1:
            # Click the pagination button matching this page number.
            clicked = False
            try:
                selector = f"a[data-page='{page_no}']"
                btn = await page.query_selector(selector)
                if btn is not None:
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await page.wait_for_timeout(3000)
                    clicked = True
            except Exception as exc:
                logger.debug("finalsite paginate click %d failed: %s",
                             page_no, exc)
            if not clicked:
                break

        cards = await page.query_selector_all(".fsConstituentItem")
        for card in cards:
            try:
                name_el = await card.query_selector("h3.fsFullName")
                if name_el is None:
                    continue
                name = _clean(await name_el.text_content())
                if not name:
                    continue

                # Finalsite's stable per-person key. Capturing it lets
                # the pipeline's dedupe merge this record with the same
                # person from the static tier instead of emitting a
                # duplicate row.
                constituent_id = ""
                try:
                    constituent_id = (await card.get_attribute(
                        "data-constituent-id")) or ""
                    if not constituent_id:
                        link = await card.query_selector(
                            "a[data-constituent-id]")
                        if link is not None:
                            constituent_id = (await link.get_attribute(
                                "data-constituent-id")) or ""
                except Exception:
                    pass

                role = ""
                role_el = await card.query_selector(".fsTitles")
                if role_el is not None:
                    role = re.sub(r"^Titles:\s*", "",
                                  _clean(await role_el.text_content()),
                                  flags=re.I)

                location = ""
                loc_el = await card.query_selector(".fsLocations")
                if loc_el is not None:
                    location = re.sub(r"^Locations:\s*", "",
                                      _clean(await loc_el.text_content()),
                                      flags=re.I)

                phone = await _extract_phone_from_element(card)

                # 3a. Try email extraction on the card itself, using
                # every known Finalsite email-container class. This
                # catches three important patterns without needing a
                # click:
                #
                #   * Henry County shape — `.fsEmailIcon` wrapping
                #     `.fsStyleSROnly` text; no mailto: anchor exists.
                #   * Midfield shape — a fully-expanded
                #     `.fsConstituentProfile` rendered *inside* the
                #     card, with the mailto already in the DOM. The
                #     old snapshot-based click logic falsely concluded
                #     "no new mailto appeared" because the mailto
                #     was there before the click too.
                #   * Standard `.fsEmail` box with a mailto anchor.
                #
                # We probe each explicit selector first (cheap and
                # noise-free), then fall back to a whole-card scan
                # which catches any other structure a Finalsite skin
                # might dream up.
                email = ""
                for sel in (".fsEmail", ".fsEmailIcon",
                            ".fsConstituentEmail",
                            ".fsProfileSectionSpecialData.fsEmail",
                            "[class*='Email' i]"):
                    try:
                        boxes = await card.query_selector_all(sel)
                    except Exception:
                        boxes = []
                    for box in boxes:
                        email = await _extract_email_from_element(box)
                        if email:
                            break
                    if email:
                        break

                # 3a-bis: whole-card scan — the ultimate safety net
                # for any skin that puts the email in an unclassed
                # `.fsStyleSROnly` span or in a pre-expanded profile
                # section. Cheap; only runs when the targeted search
                # above returned nothing.
                if not email:
                    email = await _extract_email_from_element(card)

                # 3a-tris: page-level profile lookup by constituent_id
                # — some Finalsite skins render every `.fsConstituentProfile`
                # once at the bottom of the page, keyed by id, rather
                # than nested in the card. If we captured the
                # constituent_id from the card, jump straight to the
                # matching profile block; no click required.
                if not email and constituent_id:
                    for modal_sel in (
                            f".fsConstituentProfile"
                            f"[data-constituent-id='{constituent_id}']",
                            f"#fsEl_{constituent_id}_DialogContent",
                            f"[id*='_{constituent_id}_']"):
                        try:
                            modal = await page.query_selector(modal_sel)
                        except Exception:
                            modal = None
                        if modal is not None:
                            email = await _extract_email_from_element(modal)
                            if email:
                                break

                # 3b. If nothing, open the profile and try again.
                #
                # Finalsite renders each profile modal into the DOM
                # server-side (hidden), then reveals it when the user
                # clicks a.fsConstituentProfileLink. Naive is_visible()
                # checks are flaky because the wrapper toggles between
                # display:none and display:block; the reliable signal
                # is a mailto anchor appearing *after* the click that
                # wasn't there before it.
                if not email:
                    try:
                        link = await card.query_selector(
                            "a.fsConstituentProfileLink")
                        # Some Finalsite themes make the whole name
                        # heading clickable instead of exposing the
                        # named link class.
                        if link is None:
                            link = await card.query_selector("h3.fsFullName a")
                        if link is None:
                            link = await card.query_selector(
                                "a[data-constituent-id]")
                        if link is not None:
                            # Snapshot mailto anchors already on the
                            # page so we can spot the one the modal
                            # brings in.
                            before = set(await page.eval_on_selector_all(
                                "a[href^='mailto:']",
                                "els => els.map(e => e.href)"))

                            await link.scroll_into_view_if_needed()
                            await link.click(force=True, timeout=8000)

                            # Wait up to 3s for any *new* mailto link
                            # to appear. Finalsite modals populate the
                            # `.fsEmail a[href^=mailto:]` as they open.
                            try:
                                await page.wait_for_function(
                                    """(prev) => {
                                        const now = Array.from(
                                          document.querySelectorAll(
                                            "a[href^='mailto:']"))
                                          .map(e => e.href);
                                        return now.some(h => !prev.includes(h));
                                    }""",
                                    arg=list(before),
                                    timeout=3000)
                            except Exception:
                                # Modal may have loaded without an
                                # email — that's fine, we'll fall
                                # through the extraction paths below
                                # and just come up empty for this row.
                                await page.wait_for_timeout(600)

                            # Read the new mailto link directly. This
                            # is the strongest signal — the anchor
                            # element didn't exist as a rendered
                            # mailto: before the click, so whatever
                            # appeared is the modal's contribution.
                            try:
                                after_hrefs = await page.eval_on_selector_all(
                                    "a[href^='mailto:']",
                                    "els => els.map(e => e.href)")
                                for href in after_hrefs:
                                    if href in before:
                                        continue
                                    candidate = (href.replace("mailto:", "")
                                                 .split("?", 1)[0]
                                                 .strip().lower())
                                    if _valid_email(candidate):
                                        email = candidate
                                        break
                            except Exception:
                                pass

                            # Fallback: scan every plausible modal
                            # container Finalsite uses. `.fsDialog`
                            # matches the class exactly; the wrapper
                            # you actually see in the rendered DOM is
                            # `.fsDialogContent`, and each profile has
                            # its own `[id$='_DialogContent']` id, so
                            # we cover all three shapes here.
                            if not email:
                                for sel in (
                                        ".fsDialogContent",
                                        "[id$='_DialogContent']",
                                        ".fsModal",
                                        ".fsDialog",
                                        "[role='dialog']",
                                        ".fsConstituentProfile"):
                                    modals = await page.query_selector_all(sel)
                                    for modal in modals:
                                        try:
                                            visible = await modal.is_visible()
                                        except Exception:
                                            visible = True
                                        if not visible:
                                            continue
                                        email = await (
                                            _extract_email_from_element(modal))
                                        if email:
                                            break
                                    if email:
                                        break

                            # Close whatever we opened.
                            closed = False
                            for close_sel in (
                                    "button.fsModalClose", ".fsModalClose",
                                    "button[aria-label='Close']",
                                    "[aria-label='Close']",
                                    ".fsDialogClose"):
                                try:
                                    close_btn = await page.query_selector(
                                        close_sel)
                                    if close_btn is not None and \
                                            await close_btn.is_visible():
                                        await close_btn.click(force=True,
                                                              timeout=2000)
                                        await page.wait_for_timeout(300)
                                        closed = True
                                        break
                                except Exception:
                                    continue
                            if not closed:
                                try:
                                    await page.keyboard.press("Escape")
                                    await page.wait_for_timeout(200)
                                except Exception:
                                    pass
                    except Exception as exc:
                        logger.debug("finalsite profile click failed: %s", exc)

                first, last = _parse_name(name)
                name_field = _mk_field(
                    name, source=FieldSource.FINALSITE_CONSTITUENT)
                if name_field is None:
                    continue
                person = Person(
                    name=name_field,
                    first_name=_mk_field(
                        first, source=FieldSource.FINALSITE_CONSTITUENT)
                        if first else None,
                    last_name=_mk_field(
                        last, source=FieldSource.FINALSITE_CONSTITUENT)
                        if last else None,
                    email=_mk_field(email, source=FieldSource.MAILTO,
                                    conf=Confidence.CERTAIN.value)
                        if email else None,
                    phone=_mk_field(phone, source=FieldSource.TEL_LINK)
                        if phone else None,
                    title=_mk_field(role, source=FieldSource.FINALSITE_CONSTITUENT)
                        if role else None,
                    department=_mk_field(
                        location, source=FieldSource.FINALSITE_CONSTITUENT)
                        if location else None,
                    contact_id=_mk_field(
                        constituent_id,
                        source=FieldSource.FINALSITE_CONSTITUENT,
                        conf=Confidence.CERTAIN.value)
                        if constituent_id else None,
                    source_url=page.url,
                )
                person.compute_overall_confidence()
                all_records.append(person)
            except Exception as exc:
                logger.debug("finalsite card parse failed: %s", exc)
                continue
    return all_records


# ---------------------------------------------------------------------------
# Technique 4 — Generic profile follow (non-Finalsite click-to-detail)
#
# Foxbright, Blackboard, custom themes: list page shows names + a link
# to each person's detail page; the email lives on the detail page.
# When Tier-1 static extraction gave us profile_url values but no
# emails, or when the page has anchors that look like staff-profile
# links, we open each profile in a new tab and scrape its rendered
# body for a mailto or plain-text email.
# ---------------------------------------------------------------------------

# Patterns in an anchor's href that mark it as a "person detail page"
# rather than a generic navigation link. Keep this tight — over-eager
# matching would send us clicking every menu item.
_PROFILE_HREF_RE = re.compile(
    r"/(?:staff|faculty|teachers?|profile|bio|directory|people|user"
    r"|member|employee)/[^?#]", re.I)


async def _extract_profile_page_email(page) -> Tuple[str, str]:
    """Look for an email on the currently-loaded profile page. Returns
    ``(email, phone)`` — either may be empty. Tries mailto anchors,
    then a Cloudflare data-cfemail decode, then a page-text regex."""
    # 1. Direct mailto
    try:
        mailto = await page.query_selector("a[href^='mailto:']")
        if mailto is not None:
            href = await mailto.get_attribute("href")
            if href:
                candidate = (href.replace("mailto:", "")
                             .split("?", 1)[0].strip().lower())
                if _valid_email(candidate):
                    phone = ""
                    tel = await page.query_selector("a[href^='tel:']")
                    if tel is not None:
                        tel_href = await tel.get_attribute("href")
                        if tel_href:
                            phone = tel_href.replace("tel:", "").strip()
                    return candidate, phone
    except Exception:
        pass

    # 2. Cloudflare data-cfemail — decode using the same XOR trick as
    # staffdir.fields, so we don't need to load the browser DOM into
    # a BeautifulSoup pass just for this.
    try:
        cfhex = await page.evaluate(
            "() => (document.querySelector('[data-cfemail]')||{}).dataset"
            "?.cfemail || ''")
        if cfhex:
            r = int(cfhex[:2], 16)
            decoded = "".join(
                chr(int(cfhex[i:i + 2], 16) ^ r)
                for i in range(2, len(cfhex), 2))
            if _valid_email(decoded):
                return decoded.lower(), ""
    except Exception:
        pass

    # 3. Plain-text regex over the visible body.
    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''")
        candidate = _email_from_text(body_text or "")
        if _valid_email(candidate):
            return candidate, ""
    except Exception:
        pass
    return "", ""


async def _collect_profile_urls(page, base_url: str,
                                 cap: int = 200) -> List[Tuple[str, str]]:
    """Return up to ``cap`` (url, visible_name) pairs pointing to staff
    detail pages linked from the current directory page. Same-host only."""
    try:
        rows = await page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({
                href: e.getAttribute('href') || '',
                text: (e.innerText || e.textContent || '').trim()
            })).filter(r => r.href && r.text)"""
        )
    except Exception:
        return []

    host = urlparse(base_url).netloc.lower()
    seen: set[str] = set()
    out: List[Tuple[str, str]] = []
    for row in rows:
        href = row.get("href") or ""
        text = _clean(row.get("text") or "")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if not _PROFILE_HREF_RE.search(href):
            continue
        absolute = urljoin(base_url, href)
        # Same-host only to avoid wandering off into search-engine
        # results or other sites' bios.
        if urlparse(absolute).netloc.lower() != host:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        # Skip anchors whose text is clearly not a person's name
        # (menu labels, "Read More", pagination glyphs).
        if not text or len(text) < 3 or len(text) > 60:
            continue
        if not re.search(r"[A-Za-z]", text):
            continue
        out.append((absolute, text))
        if len(out) >= cap:
            break
    return out


async def _extract_via_profile_follow(page, base_url: str,
                                      max_profiles: int = 60
                                      ) -> List[Person]:
    """For every plausible staff-profile anchor on the directory page,
    open the profile in a new tab and pull an email out of it.

    Complements Technique 3 for non-Finalsite sites where the pattern
    is "list of names, each links to a detail page carrying the
    email" (Foxbright, Blackboard, many custom themes). Runs against
    the currently-loaded page — the orchestrator navigates before
    calling this.
    """
    try:
        await page.wait_for_timeout(500)
    except Exception:
        pass

    profile_links = await _collect_profile_urls(
        page, base_url, cap=max_profiles)
    if not profile_links:
        return []

    logger.info("profile-follow: %d candidate profile URLs on %s",
                len(profile_links), base_url)

    context = page.context
    records: List[Person] = []
    for profile_url, name in profile_links:
        profile_page = None
        try:
            profile_page = await context.new_page()
            try:
                await profile_page.goto(profile_url,
                                        wait_until="domcontentloaded",
                                        timeout=25000)
            except Exception:
                continue
            await profile_page.wait_for_timeout(600)
            # Icon-reveal on the detail page too — a lot of Foxbright
            # skins put the address behind an envelope button there.
            await _reveal_hidden_emails(profile_page, cap=20)
            email, phone = await _extract_profile_page_email(profile_page)
            if not email:
                continue

            first, last = _parse_name(name)
            person = Person(
                name=_mk_field(name, source=FieldSource.CARD_TEXT),
                first_name=_mk_field(first, source=FieldSource.CARD_TEXT)
                    if first else None,
                last_name=_mk_field(last, source=FieldSource.CARD_TEXT)
                    if last else None,
                email=_mk_field(email, source=FieldSource.MAILTO,
                                conf=Confidence.CERTAIN.value),
                phone=_mk_field(phone, source=FieldSource.TEL_LINK)
                    if phone else None,
                profile_url=_mk_field(profile_url,
                                       source=FieldSource.CARD_TEXT,
                                       conf=Confidence.HIGH.value),
                source_url=base_url,
            )
            person.compute_overall_confidence()
            records.append(person)
        except Exception as exc:
            logger.debug("profile-follow %s raised: %s", profile_url, exc)
        finally:
            if profile_page is not None:
                try:
                    await profile_page.close()
                except Exception:
                    pass
    logger.info("profile-follow: recovered %d emails from %d profiles",
                len(records), len(profile_links))
    return records


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


import time as _time


async def _run_all_techniques(url: str, *, max_pages: int,
                              headless: bool = True,
                              deadline: Optional[float] = None,
                              stuck_after: float = 90.0,
                              ) -> Tuple[List[Person], List[str]]:
    """Run every Playwright technique for one URL, merge, return whatever
    was extracted — even on partial timeout.

    Progress rule (fixes the "timeout kills active extraction" bug):

    * ``deadline`` is a soft ceiling in wall-clock ``time.time()``
      units. Between techniques we check whether we've passed it AND
      whether extraction has stalled — if we're still adding emails,
      we keep going even past the ceiling, because a big directory
      with 400+ records genuinely needs 4-5 minutes to fully drain.
    * ``stuck_after`` is the "no new emails in N seconds" cutoff. If
      that fires we're clearly not making progress and it's safe to
      stop.

    Every technique is wrapped in its own try/except so a failure
    (or a per-technique navigation timeout) can never wipe out the
    records already extracted by earlier techniques.
    """
    assert async_playwright is not None
    contributions: List[str] = []
    merged: List[Person] = []
    last_progress_at = _time.time()
    last_email_count = 0

    def _should_stop(technique_name: str) -> bool:
        """Return True only if we're both past the deadline AND stalled.
        A soft deadline that respects active extraction — the whole
        point of the fix."""
        if deadline is None:
            return False
        now = _time.time()
        current_emails = sum(1 for r in merged if r.email)
        nonlocal last_email_count, last_progress_at
        if current_emails > last_email_count:
            last_email_count = current_emails
            last_progress_at = now
        if now < deadline:
            return False
        stalled_for = now - last_progress_at
        if stalled_for >= stuck_after:
            logger.info(
                "playwright: past deadline and stalled %.0fs (no new "
                "emails since) — stopping before %s",
                stalled_for, technique_name)
            return True
        # Past deadline but still finding emails — keep going.
        logger.debug(
            "playwright: past deadline but still extracting "
            "(last progress %.0fs ago) — continuing into %s",
            _time.time() - last_progress_at, technique_name)
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=_USER_AGENT)
            page = await context.new_page()

            # ---- Technique 1: URL pagination -----------------------------
            if not _should_stop("technique 1"):
                try:
                    t1 = await _paginate_by_url(page, url, max_pages)
                except Exception as exc:
                    logger.debug("technique 1 raised: %s", exc)
                    t1 = []
                if t1:
                    contributions.append(f"direct_mailto_urlpage={len(t1)}")
                    merged.extend(t1)

            emails_so_far = sum(1 for r in merged if r.email)

            # ---- Technique 2: next-button pagination ---------------------
            # Only bother if T1 didn't already surface a lot of emails —
            # T2 revisits the site from page 1 and would be redundant.
            if emails_so_far < 3 and not _should_stop("technique 2"):
                try:
                    t2 = await _paginate_by_next_button(
                        page, url, max_pages)
                except Exception as exc:
                    logger.debug("technique 2 raised: %s", exc)
                    t2 = []
                if t2:
                    contributions.append(f"hidden_icon_nextbtn={len(t2)}")
                    merged.extend(t2)
                    emails_so_far = sum(1 for r in merged if r.email)

            # ---- Technique 3: Finalsite click-through --------------------
            # Runs whenever the page shows any Finalsite constituent
            # markup — even if T1/T2 got partial data, T3 typically
            # rescues the missing emails on profile-only cards.
            has_finalsite = False
            if not _should_stop("technique 3 probe"):
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=60000)
                    await page.wait_for_timeout(2500)
                    items = await page.query_selector_all(
                        ".fsConstituentItem")
                    has_finalsite = len(items) > 0
                except Exception as exc:
                    logger.debug("finalsite probe failed: %s", exc)

            if has_finalsite and not _should_stop("technique 3"):
                try:
                    t3 = await _extract_finalsite_pages(
                        page, url, max_pages)
                except Exception as exc:
                    logger.debug("technique 3 raised: %s", exc)
                    t3 = []
                if t3:
                    contributions.append(f"finalsite_click={len(t3)}")
                    merged.extend(t3)

            # ---- Technique 4: generic profile follow --------------------
            # For non-Finalsite sites (Foxbright, Blackboard, custom
            # themes) where the list page links out to a detail page
            # per person. Fires whenever we still don't have many
            # emails after T1-T3 — regardless of CMS. Same-host only,
            # capped, so it's cheap to try speculatively.
            emails_so_far = sum(1 for r in merged if r.email)
            if emails_so_far < 5 and not _should_stop("technique 4"):
                try:
                    # Fresh navigation so T3's modal state is discarded.
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=60000)
                    await page.wait_for_timeout(1500)
                    t4 = await _extract_via_profile_follow(
                        page, url, max_profiles=60)
                except Exception as exc:
                    logger.debug("technique 4 raised: %s", exc)
                    t4 = []
                if t4:
                    contributions.append(f"profile_follow={len(t4)}")
                    merged.extend(t4)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return _dedupe(merged), contributions


def _run_coro_in_fresh_loop(coro):
    """Run a coroutine to completion on a brand-new event loop.

    Safe to call from any thread — including a ThreadPoolExecutor
    worker where ``asyncio.get_event_loop()`` on Python 3.10+ raises
    ``RuntimeError``. Cancels and awaits any pending tasks before
    closing the loop so Playwright's driver connection tears down
    cleanly (this is what stops the ``BrokenPipeError`` /
    "Connection closed while reading from the driver" cascade you'd
    otherwise see when the pipeline runs several workers in parallel
    on Windows).
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            # Drain pending tasks so Playwright's subprocess and pipe
            # writers finish before we close the loop.
            try:
                pending = [t for t in asyncio.all_tasks(loop)
                           if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                # Let subprocess-transport finalizers run.
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
    finally:
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        loop.close()


def extract_with_playwright(
        url: str, *, max_pages: int = 500, headless: bool = True,
        timeout_seconds: int = 300,
        stuck_after_seconds: float = 90.0,
) -> Tuple[List[Person], ExtractionReport]:
    """Synchronous public entry point.

    Runs the Playwright techniques against ``url`` and returns
    ``(persons, report)``. Returns ``([], report)`` with an explanatory
    warning only if Playwright isn't installed. Never returns empty
    just because a technique failed or the deadline passed —
    whatever was extracted before the failure is kept.

    Parameters
    ----------
    url:
        Directory URL to scrape.
    max_pages:
        Upper bound on pages to visit per technique.
    headless:
        Set to False for local debugging with a visible browser.
    timeout_seconds:
        **Soft** wall-clock ceiling (default 300s). Techniques are
        allowed to keep running past this ceiling as long as they
        are still adding new emails; extraction only stops when
        we're both past the ceiling AND we've gone
        ``stuck_after_seconds`` without any new email being added.
        A big directory that legitimately needs 5-6 minutes to
        drain will finish; a stuck site that produces nothing for
        90s past the ceiling will be aborted.
    stuck_after_seconds:
        No-new-emails cutoff, only checked after the soft deadline.
    """
    report = ExtractionReport(url=url)
    report.js_rendered = True
    report.strategies_tried.append("playwright_fallback")

    if not is_playwright_available():
        report.warnings.append(
            f"playwright not installed ({_PLAYWRIGHT_IMPORT_ERROR!r}); "
            "install with `pip install playwright && playwright install chromium`")
        report.strategy_used = "playwright_unavailable"
        return [], report

    # Compute the soft deadline once here so it's stable across the whole
    # extraction — the orchestrator will honour it while still respecting
    # active-progress.
    deadline = _time.time() + timeout_seconds

    async def _bounded():
        return await _run_all_techniques(
            url, max_pages=max_pages, headless=headless,
            deadline=deadline, stuck_after=stuck_after_seconds)

    # Decide how to enter the event loop. Three possible callers:
    #   (a) top-level script / CLI     — no loop anywhere → new loop here.
    #   (b) Jupyter cell               — a loop is *running* on this
    #                                    thread → must offload to a
    #                                    fresh thread to avoid deadlock.
    #   (c) ThreadPoolExecutor worker  — no loop on this thread, but
    #                                    another one may exist on the
    #                                    main thread → new loop here.
    #
    # (a) and (c) look identical to `_run_coro_in_fresh_loop`; (b) is
    # the only case that needs offloading. Detect (b) via
    # `get_running_loop` — the only variant that doesn't spuriously
    # create a loop the way `get_event_loop` did in Python 3.10+.
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            # Case (b): jump to a fresh thread with its own loop.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=1) as ex:
                persons, contributions = ex.submit(
                    _run_coro_in_fresh_loop, _bounded()).result()
        else:
            # Cases (a) + (c): fresh loop on this thread.
            persons, contributions = _run_coro_in_fresh_loop(_bounded())
    except Exception as exc:
        # Log the failure but still return whatever the orchestrator
        # yielded before it went sideways. Because the orchestrator
        # catches per-technique exceptions internally, this path is
        # only reached on a truly catastrophic browser-launch or
        # driver-connection failure.
        report.warnings.append(f"playwright fallback raised: {exc}")
        report.strategy_used = "playwright_error"
        logger.warning("playwright fallback for %s raised: %s", url, exc)
        return [], report

    report.persons_returned = len(persons)
    report.emails_found = sum(1 for p in persons if p.email)
    report.phones_found = sum(1 for p in persons if p.phone)
    report.merged_records = len(persons)
    report.candidates_seen = len(persons)
    if contributions:
        report.strategy_used = "playwright_fallback"
        for c in contributions:
            report.strategy_results[c.split("=", 1)[0]] = c
    else:
        report.strategy_used = "playwright_no_records"
        report.warnings.append(
            "playwright fallback ran but found no records")
    return persons, report
