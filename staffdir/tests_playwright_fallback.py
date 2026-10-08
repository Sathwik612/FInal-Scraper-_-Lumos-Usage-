"""Unit tests for staffdir.playwright_fallback.

These tests exercise the pure-Python parts of the fallback tier —
the trigger rule, the line-to-Person assembler, the email validator
and the local dedupe. They do NOT launch a browser; the browser
path is smoke-tested by hand against real school sites.
"""

from __future__ import annotations

import pytest

from staffdir.playwright_fallback import (
    _dedupe, _email_from_text, _parse_name, _person_from_lines,
    _valid_email, extract_with_playwright, is_playwright_available,
)
from staffdir.pipeline import (
    _merge_playwright_records, _needs_playwright_fallback,
)
from staffdir.schema import Confidence, Field, FieldSource, Person


# --------------------------------------------------------------- helpers ---

def _named(name: str, email: str | None = None,
           title: str | None = None) -> Person:
    return Person(
        name=Field(name, Confidence.HIGH.value, FieldSource.HEADING),
        email=(Field(email, Confidence.CERTAIN.value, FieldSource.MAILTO)
               if email else None),
        title=(Field(title, Confidence.HIGH.value, FieldSource.HEADING)
               if title else None),
    )


# --------------------------------------------------------------- trigger ---

class TestFallbackTrigger:
    def test_empty_list_triggers(self):
        assert _needs_playwright_fallback([]) is True

    def test_all_missing_emails_triggers(self):
        # A page-full of name-only cards is the classic JS-only signal.
        assert _needs_playwright_fallback(
            [_named("Jane Doe"), _named("John Roe"),
             _named("Sam Roe")]) is True

    def test_all_have_emails_suppresses(self):
        # HTML tier is working perfectly — no need to pay JS cost.
        assert _needs_playwright_fallback(
            [_named("Jane", email="j@x.edu"),
             _named("John", email="jr@x.edu")]) is False

    def test_meaningful_fraction_missing_triggers(self):
        # Enterprise-Schools pattern: many people, some emails visible
        # on the card, others hidden behind a modal click. The old
        # trigger let this through unchecked — new rule catches it.
        people = ([_named(f"P{i}", email=f"p{i}@x.edu") for i in range(6)]
                  + [_named(f"Q{i}") for i in range(4)])
        # 10 people, 4 missing → 40% ≥ 20% threshold, ≥2 missing.
        assert _needs_playwright_fallback(people) is True

    def test_single_missing_email_does_not_trigger(self):
        # One staffer without a published email is legitimate; not
        # worth the browser cost.
        people = ([_named(f"P{i}", email=f"p{i}@x.edu") for i in range(10)]
                  + [_named("Q")])
        assert _needs_playwright_fallback(people) is False

    def test_below_ratio_does_not_trigger(self):
        # 10% missing — below the 20% ratio threshold.
        people = ([_named(f"P{i}", email=f"p{i}@x.edu") for i in range(9)]
                  + [_named("Q")])
        assert _needs_playwright_fallback(people) is False

    def test_custom_thresholds_can_loosen(self):
        # Callers can dial the trigger tighter or looser.
        people = ([_named("A", email="a@x.edu"), _named("B")])
        assert _needs_playwright_fallback(
            people, min_missing_emails=1,
            min_missing_ratio=0.1) is True


# --------------------------------------------------------------- email ------

class TestEmailValidator:
    @pytest.mark.parametrize("email", [
        "jane.doe@valley.k12.mo.us",
        "jsmith@school-district.org",
        "principal@my.school.edu",
    ])
    def test_accepts_real(self, email):
        assert _valid_email(email) is True

    @pytest.mark.parametrize("email", [
        "", "not-an-email", "a@b",
        "photo.png@example.edu",    # image file
        "user@example.com",         # placeholder domain
        "help@yourdomain.com",      # template placeholder
    ])
    def test_rejects_junk(self, email):
        assert _valid_email(email) is False

    def test_extract_from_text(self):
        assert _email_from_text(
            "Contact: jane@school.edu for questions"
        ) == "jane@school.edu"
        assert _email_from_text("no email here") == ""


# --------------------------------------------------------------- name -------

class TestNameParser:
    def test_first_last(self):
        assert _parse_name("Jane Doe") == ("Jane", "Doe")

    def test_middle(self):
        assert _parse_name("Jane Q. Doe") == ("Jane", "Q. Doe")

    def test_single(self):
        assert _parse_name("Cher") == ("Cher", "")

    def test_empty(self):
        assert _parse_name("") == ("", "")
        assert _parse_name("   ") == ("", "")


# --------------------------------------------------------------- assembler --

class TestPersonFromLines:
    def test_full_card(self):
        # Realistic Direct-Mailto scrape: 3 lines from the walked-up card.
        p = _person_from_lines(
            ["Jane Doe", "5th Grade Teacher", "Valley Elementary"],
            "jane.doe@valley.k12.mo.us",
            source_url="https://valley.edu/staff")
        assert p is not None
        assert p.name.value == "Jane Doe"
        assert p.title.value == "5th Grade Teacher"
        assert p.department.value == "Valley Elementary"
        assert p.email.value == "jane.doe@valley.k12.mo.us"
        assert p.first_name.value == "Jane"
        assert p.last_name.value == "Doe"
        assert p.source_url == "https://valley.edu/staff"

    def test_drops_email_lines(self):
        # Lines containing '@' must be skipped so email doesn't leak
        # into the name slot.
        p = _person_from_lines(
            ["Jane Doe", "jane.doe@valley.edu", "Principal"],
            "jane.doe@valley.edu",
            source_url="https://x")
        assert p is not None
        assert p.name.value == "Jane Doe"
        assert p.title.value == "Principal"

    def test_rejects_short_name(self):
        assert _person_from_lines(["J"], "j@x.edu", source_url="") is None

    def test_rejects_non_alpha_name(self):
        # A card whose first line is a number or icon glyph should be
        # dropped, not sent onward as a "person".
        assert _person_from_lines(
            ["12345", "Some role"], "j@x.edu", source_url="") is None

    def test_no_lines_returns_none(self):
        assert _person_from_lines([], "j@x.edu", source_url="") is None


# --------------------------------------------------------------- dedupe -----

class TestDedupe:
    def test_merges_by_email(self):
        # Same email seen twice — enrichment fields from #2 should fill
        # holes on #1.
        base = _named("J D", email="j@x.edu")
        enriched = _named("Jane Doe", email="j@x.edu", title="Principal")
        merged = _dedupe([base, enriched])
        assert len(merged) == 1
        assert merged[0].title.value == "Principal"

    def test_keys_on_name_without_email(self):
        # Two nameless-email-less records are dropped; same name twice
        # collapses.
        a = _named("Jane Doe")
        b = _named("Jane Doe", title="Teacher")
        merged = _dedupe([a, b])
        assert len(merged) == 1
        assert merged[0].title.value == "Teacher"

    def test_distinct_people_survive(self):
        merged = _dedupe([_named("Jane Doe", email="jane@x.edu"),
                          _named("John Roe", email="john@x.edu")])
        assert len(merged) == 2


# --------------------------------------------------------------- merge ------

class TestMergePlaywrightRecords:
    def test_static_and_playwright_merge(self):
        # Static tier found the name but no email; playwright found
        # both — the merged record should carry both.
        static = _named("Jane Doe")
        pw = _named("Jane Doe", email="j@x.edu", title="Principal")
        merged = _merge_playwright_records([static], [pw])
        assert len(merged) == 1
        assert merged[0].email.value == "j@x.edu"
        assert merged[0].title.value == "Principal"

    def test_empty_extra_returns_base(self):
        base = [_named("A"), _named("B")]
        assert _merge_playwright_records(base, []) is base


# --------------------------------------------------------------- public -----

class TestPublicEntry:
    def test_returns_report_without_playwright(self, monkeypatch):
        # Force the "playwright missing" path even if it IS installed
        # here so the test is deterministic.
        import staffdir.playwright_fallback as mod
        monkeypatch.setattr(mod, "async_playwright", None)
        monkeypatch.setattr(mod, "_PLAYWRIGHT_IMPORT_ERROR",
                            ImportError("simulated"))

        persons, report = extract_with_playwright("https://example.com")
        assert persons == []
        assert report.strategy_used == "playwright_unavailable"
        assert report.js_rendered is True
        assert any("playwright not installed" in w for w in report.warnings)

    def test_is_available_boolean(self):
        # Just asserts the API contract — result depends on install.
        assert isinstance(is_playwright_available(), bool)


# --------------------------------------------------------------- icon reveal

class TestIconRevealSelectors:
    """These constants matter for pipeline coverage — a typo silently
    reduces reveal power in production. Keep them locked down."""

    def test_covers_common_button_patterns(self):
        from staffdir.playwright_fallback import _ICON_REVEAL_SELECTORS
        # A small sample of shapes seen on live sites — if any of
        # these disappear the reveal pass loses coverage.
        must_cover = [
            "button[class*='email' i]",
            "button[aria-label*='email' i]",
            "i.fa-envelope",
            ".fa-envelope",
            "svg[class*='mail' i]",
        ]
        for pattern in must_cover:
            assert pattern in _ICON_REVEAL_SELECTORS, \
                f"lost coverage for {pattern!r}"


# --------------------------------------------------------------- profile URL

class TestProfileHrefPattern:
    """Guard rails on the URL pattern used by Technique 4. Over-eager
    matching sends the fallback clicking every navigation link;
    under-eager misses whole CMSes."""

    def test_matches_common_staff_paths(self):
        from staffdir.playwright_fallback import _PROFILE_HREF_RE
        for href in ("/staff/1234-jane-doe",
                     "/faculty/john-smith",
                     "/directory/12345/",
                     "/people/jsmith",
                     "/teachers/johnson",
                     "/user/1002",
                     "/employee/1001-doe"):
            assert _PROFILE_HREF_RE.search(href), \
                f"should match {href!r}"

    def test_rejects_navigation_links(self):
        from staffdir.playwright_fallback import _PROFILE_HREF_RE
        for href in ("/", "/about", "/contact",
                     "/news/press-release",
                     "/staff", "/staff/",
                     "/departments/curriculum",
                     "/calendar/2025"):
            assert not _PROFILE_HREF_RE.search(href), \
                f"should NOT match {href!r}"


# --------------------------------------------------------------- foxbright

class TestFoxbrightStrategy:
    """The Foxbright extractor is Michigan-specific but there are enough
    Foxbright installs (~200 districts) that a regression here would
    cost real emails."""

    def test_extracts_from_fbcms_structure(self):
        from bs4 import BeautifulSoup
        from staffdir.directory import _persons_from_foxbright
        html = """
        <html><body>
          <div class="fbcms_staffentry">
            <div class="fbcms_staffinfo">
              <div class="name"><a href="/staff/1234">Jane Doe</a></div>
              <div class="row2 building">Central Elementary</div>
              <div class="row3 position">Kindergarten Teacher</div>
              <a href="mailto:jdoe@dist.k12.mi.us">jdoe@dist.k12.mi.us</a>
            </div>
          </div>
          <div class="fbcms_staffentry">
            <div class="fbcms_staffinfo">
              <div class="name"><a href="/staff/5678">John Roe</a></div>
              <div class="row2 building">High School</div>
              <div class="row3 position">Principal</div>
            </div>
          </div>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        people = _persons_from_foxbright(
            soup, "https://dist.k12.mi.us/staff/")
        assert len(people) == 2
        by_name = {p.name.value: p for p in people}
        assert by_name["Jane Doe"].email is not None
        assert by_name["Jane Doe"].email.value == "jdoe@dist.k12.mi.us"
        assert by_name["Jane Doe"].title.value == "Kindergarten Teacher"
        assert by_name["Jane Doe"].department.value == "Central Elementary"
        # John Roe has no email on the list — profile_url should carry
        # over so the pipeline can hydrate via detail-page fetch.
        assert by_name["John Roe"].email is None
        assert by_name["John Roe"].profile_url is not None
        assert by_name["John Roe"].profile_url.value.endswith("/staff/5678")

    def test_ignores_page_without_fbcms(self):
        from bs4 import BeautifulSoup
        from staffdir.directory import _persons_from_foxbright
        html = "<html><body><p>Just a normal page.</p></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _persons_from_foxbright(soup, "https://x") == []

    def test_registered_in_strategies(self):
        from staffdir.directory import _STRATEGIES
        names = [n for n, _ in _STRATEGIES]
        assert "foxbright" in names


# --------------------------------------------------------------- SR-only email

class TestSROnlyPlainEmail:
    """Henry County BOE pattern:
        <span class="fsEmailIcon">
          <span class="fsStyleSROnly">sabney@henrycountyboe.org</span>
        </span>
    No mailto anchor exists — the address is plain text inside a
    screen-reader-only span nested in .fsEmailIcon (NOT .fsEmail).
    """

    def test_extract_emails_finds_plain_email_in_sr_only(self):
        from bs4 import BeautifulSoup
        from staffdir.fields import extract_emails
        html = """
        <div class="fsConstituentItem">
          <h3 class="fsFullName">Sarah Abney</h3>
          <span class="fsEmailIcon">
            <span class="fsStyleSROnly">sabney@henrycountyboe.org</span>
          </span>
        </div>
        """
        elem = BeautifulSoup(html, "lxml").select_one(".fsConstituentItem")
        emails = extract_emails(elem)
        assert emails, "no email extracted from Henry County pattern"
        values = [f.value for f in emails]
        assert "sabney@henrycountyboe.org" in values, \
            f"got {values!r}"
        # Should be CERTAIN confidence now (was falling through to
        # low-confidence bare-regex path).
        top = max(emails, key=lambda f: f.confidence)
        assert top.confidence >= 0.99, \
            f"SR-only plain email should be CERTAIN, got {top.confidence}"

    def test_finalsite_extractor_captures_fsemailicon(self):
        from bs4 import BeautifulSoup
        from staffdir.directory import _persons_from_finalsite
        html = """
        <div class="fsConstituent">
          <div class="fsConstituentItem" data-constituent-id="42">
            <h3 class="fsFullName">Sarah Abney</h3>
            <div class="fsTitles">Superintendent</div>
            <span class="fsEmailIcon">
              <span class="fsStyleSROnly">sabney@henrycountyboe.org</span>
            </span>
          </div>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        people = _persons_from_finalsite(
            soup, "https://www.henrycountyboe.org/staff/")
        assert people, "no persons extracted"
        p = people[0]
        assert p.name.value == "Sarah Abney"
        assert p.email is not None, "email missing — Henry County regression"
        assert p.email.value == "sabney@henrycountyboe.org"


# --------------------------------------------------------------- inline modal

class TestInlineExpandedProfile:
    """Midfield BOE pattern:
        <div class="fsConstituentItem">
          <div class="fsConstituentProfile ...">
            <div class="fsEmail">
              <a href="mailto:ladams@midfield.k12.al.us">...</a>
            </div>
          </div>
        </div>
    The full profile is rendered inline inside the card (server-side
    expanded), not lazily via a click. The old T3 snapshot-based
    click logic would falsely conclude "no new mailto appeared" because
    the mailto was already in the DOM.
    """

    def test_finalsite_extractor_reads_inline_profile(self):
        from bs4 import BeautifulSoup
        from staffdir.directory import _persons_from_finalsite
        html = """
        <div class="fsConstituent">
          <div class="fsConstituentItem" data-constituent-id="17202">
            <div class="fsConstituentProfile fsHasPhoto">
              <h3 class="fsFullName">
                <span class="fsFullNameFirst">LaTonja</span>
                <span class="fsFullNameLast">Adams</span>
              </h3>
              <div class="fsProfileSection">
                <div class="fsProfileSectionData fsLocation">
                  <div class="fsProfileSectionFieldValue">
                    Midfield High School Faculty, Midfield High
                  </div>
                </div>
              </div>
              <div class="fsProfileSectionData fsContacts">
                <div class="fsProfileSectionSpecialData fsEmail fsEmailSchool">
                  <div>School Email</div>
                  <div>
                    <div id="fsEmail-17202-0">
                      <a href="mailto:ladams@midfield.k12.al.us">
                        ladams@midfield.k12.al.us
                      </a>
                    </div>
                    (Primary)
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        people = _persons_from_finalsite(
            soup, "https://www.midfield.k12.al.us/staff/")
        assert people, "no persons extracted from inline-profile pattern"
        p = people[0]
        assert p.name.value in ("LaTonja Adams", "LaTonja  Adams"), \
            f"got name {p.name.value!r}"
        assert p.email is not None, \
            "email missing — Midfield regression (inline profile)"
        assert p.email.value == "ladams@midfield.k12.al.us"
        assert p.contact_id and p.contact_id.value == "17202"


# --------------------------------------------------------------- T3 selector

class TestT3SelectorBroadening:
    """The Playwright T3 code searches these selectors before clicking.
    Locking them down prevents a silent regression that would send us
    back to clicking modals we don't need to click."""

    def test_card_email_selectors_cover_all_shapes(self):
        # Read the code text and assert the selectors are present —
        # cheap smoke test, would catch anyone deleting a selector.
        from pathlib import Path
        source = Path(__file__).parent.joinpath("playwright_fallback.py").read_text()
        for selector in (".fsEmail", ".fsEmailIcon",
                         ".fsConstituentEmail"):
            assert f'"{selector}"' in source, \
                f"lost T3 card-email selector {selector!r}"


# --------------------------------------------------------------- thread safety

class TestThreadPoolExecutorEntry:
    """The pipeline calls extract_with_playwright from
    ThreadPoolExecutor workers. On Python 3.10+ background threads
    have no event loop and `asyncio.get_event_loop()` RAISES rather
    than creating one. The old code caught the RuntimeError but the
    subsequent asyncio.run() left driver cleanup work running past
    loop close, producing the BrokenPipeError cascade the user saw.

    _run_coro_in_fresh_loop is the fix — a fresh loop per call, tasks
    drained on the way out. This test proves it survives being
    called from a worker thread."""

    def test_runs_from_worker_thread_without_event_loop(self):
        import asyncio
        import concurrent.futures
        from staffdir.playwright_fallback import _run_coro_in_fresh_loop

        async def _tiny():
            # Simulate a bit of async work — enough that a botched
            # loop-cleanup would leave a pending task behind.
            await asyncio.sleep(0.01)
            return "ok"

        # Confirm the target thread has NO event loop before we call —
        # this is the exact condition that triggered the bug in prod.
        def _worker():
            try:
                asyncio.get_event_loop()
                return "had_loop"     # bug reproduction failed
            except RuntimeError:
                pass
            return _run_coro_in_fresh_loop(_tiny())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            result = ex.submit(_worker).result(timeout=10)
        assert result == "ok"

    def test_repeat_calls_dont_leak_loop_state(self):
        # Regression guard: many workers calling the extractor in
        # sequence must not cause loop cross-contamination.
        from staffdir.playwright_fallback import _run_coro_in_fresh_loop
        import asyncio

        async def _make_num(n):
            await asyncio.sleep(0)
            return n

        results = [_run_coro_in_fresh_loop(_make_num(i))
                   for i in range(5)]
        assert results == [0, 1, 2, 3, 4]


# --------------------------------------------------------------- progress-aware timeout

class TestProgressAwareDeadline:
    """The old design wrapped the whole orchestrator in
    asyncio.wait_for(timeout=120) — hitting the ceiling cancelled the
    coroutine and threw away every record extracted so far. For a big
    site legitimately taking 3-4 minutes, this silently dropped the
    whole run.

    New design: deadline is soft. Extraction only stops when we're
    both past the deadline AND haven't found a new email for the
    stall window."""

    def test_default_timeout_is_generous(self):
        # Guard against someone quietly dropping this back to 120.
        # 300s = 5 min matches the largest Baldwin County / Bessemer
        # style directories seen in real Alabama runs.
        from staffdir.playwright_fallback import extract_with_playwright
        import inspect
        sig = inspect.signature(extract_with_playwright)
        assert sig.parameters["timeout_seconds"].default >= 300, \
            "default deadline must be at least 5 minutes"

    def test_pipeline_forwards_generous_default(self):
        # The pipeline entry point should agree with the extractor.
        from staffdir.pipeline import run_pipeline
        import inspect
        sig = inspect.signature(run_pipeline)
        assert sig.parameters["playwright_timeout"].default >= 300

    def test_orchestrator_accepts_deadline_kwarg(self):
        # The deadline hook must be plumbed through — a typo here
        # would silently disable the progress-aware behaviour.
        from staffdir.playwright_fallback import _run_all_techniques
        import inspect
        sig = inspect.signature(_run_all_techniques)
        assert "deadline" in sig.parameters
        assert "stuck_after" in sig.parameters
        assert sig.parameters["deadline"].default is None, \
            "deadline must be optional (None means unbounded)"


# --------------------------------------------------------------- adaptive defaults

class TestAdaptiveDefaults:
    """The two Playwright knobs (max_pages, timeout_seconds) exist as
    safety valves — the pipeline is supposed to auto-scale via
    natural termination (empty-page / duplicate-page / stall-detect).
    These tests lock in the "safety valve, not routine tunable"
    defaults so nobody quietly drops them back to a small number.
    """

    def test_max_pages_default_is_safety_valve(self):
        # Was 10 in early versions — that would clip any real school
        # district with more than 100 staff (very common). 100 is
        # high enough that natural termination almost always fires
        # first; treat any lower value as a regression.
        from staffdir.playwright_fallback import extract_with_playwright
        import inspect
        sig = inspect.signature(extract_with_playwright)
        assert sig.parameters["max_pages"].default >= 500, \
            "max_pages default must be a safety valve (≥500), not a routine cap"

    def test_pipeline_forwards_safety_valve_default(self):
        from staffdir.pipeline import run_pipeline
        import inspect
        sig = inspect.signature(run_pipeline)
        assert sig.parameters["playwright_max_pages"].default >= 500


# --------------------------------------------------------------- natural termination

class TestPaginateByUrlNaturalTermination:
    """_paginate_by_url must self-terminate on real-site conditions
    (empty page, duplicate page, error) so raising the safety cap
    doesn't turn into a runaway loop. This is what makes the
    adaptive behavior safe."""

    def test_empty_streak_logic_still_present(self):
        # Cheap sanity check: source contains the terminator patterns.
        # A refactor that accidentally deleted them would send big
        # sites into infinite loops.
        from pathlib import Path
        source = Path(__file__).parent.joinpath(
            "playwright_fallback.py").read_text()
        assert "empty_streak >= 2" in source, \
            "lost empty-page terminator"
        assert "duplicate_streak >= 2" in source, \
            "lost duplicate-page terminator (clamp detection)"

    def test_signature_dedup_uses_email_and_name(self):
        # The clamp detector keys on (email, name). If someone
        # changes it to just email, duplicates without email get
        # counted as always-different and the terminator never fires.
        from pathlib import Path
        source = Path(__file__).parent.joinpath(
            "playwright_fallback.py").read_text()
        assert "frozenset(" in source
        # Both fields referenced in the signature loop.
        assert "r.email.value.lower()" in source
        assert "r.name.value.lower()" in source


# --------------------------------------------------------------- district budget

class TestDistrictBudgetAdaptive:
    """The per-district time budget is the outermost gate. If it stays
    at 90s (the old default) then even a perfect Playwright fallback
    gets cut off by the district-level cancel loop before it can
    return leads. Locking down the "safety valve, not routine cap"
    default here too."""

    def test_run_district_pipeline_default_is_generous(self):
        from staffdir.pipeline import run_district_pipeline
        import inspect
        sig = inspect.signature(run_district_pipeline)
        # 600s = 10 min. A big district (Baldwin: 401 leads across
        # 30+ schools) legitimately needs 5-7 minutes to drain.
        assert sig.parameters["per_district_time_budget"].default >= 600, \
            "district time budget must be a safety valve, not a hard cap"

    def test_process_district_default_matches(self):
        from staffdir.pipeline import _process_district
        import inspect
        sig = inspect.signature(_process_district)
        assert sig.parameters["per_district_time_budget"].default >= 600

    def test_budget_check_is_progress_aware(self):
        # Source-level guard: the enforcement loop must consult a
        # stall detector, not do a raw `if elapsed > budget: break`.
        # A refactor that reverts to the old hard-cap would silently
        # break big districts again.
        from pathlib import Path
        src = Path(__file__).parent.joinpath("pipeline.py").read_text()
        # Both places that enforce the budget must now use _time_up().
        assert src.count("def _time_up()") >= 2, \
            "expected progress-aware _time_up() in both enforcement sites"
        assert "stalled >= stuck_after" in src, \
            "stall detector missing from district budget check"


# --------------------------------------------------------------- school discovery

class TestSchoolDiscoveryPlaywrightFallback:
    """The school-discovery layer used to be 100% static HTML. On
    Denver Public Schools (Finalsite-based, JS-rendered "find a
    school" widget) that returned 1 lead — the district homepage.
    A JS-rendered fallback is now the escalation path when static
    stages under-find schools."""

    def test_trigger_threshold_is_generous(self):
        # ≥5 means static discovery of 3-4 schools still triggers the
        # fallback. Very few real K-12 districts have <5 schools.
        from staffdir.schools import MIN_SCHOOLS_BEFORE_PLAYWRIGHT
        assert MIN_SCHOOLS_BEFORE_PLAYWRIGHT >= 5

    def test_wired_into_discover_schools(self):
        # Source-level guard against a refactor that quietly removes
        # the escalation call.
        from pathlib import Path
        src = Path(__file__).parent.joinpath("schools.py").read_text()
        assert "_mine_with_playwright(" in src, \
            "playwright fallback function must exist"
        assert "MIN_SCHOOLS_BEFORE_PLAYWRIGHT" in src, \
            "trigger threshold constant missing"
        # And the trigger check must count NON-district candidates, not
        # total. The district homepage is always injected regardless.
        assert "_norm_url(c.url) != district_key" in src, \
            "trigger must exclude district homepage from the count"

    def test_playwright_paths_include_dps_signature(self):
        # The extended path list must cover the shapes real districts
        # use for JS-rendered school finders — not just the classic
        # /schools path.
        from staffdir.schools import _PLAYWRIGHT_SCHOOL_PATHS
        must_cover = ("/find-a-school", "/school-finder",
                      "/schools/find", "/schools/directory")
        for path in must_cover:
            assert path in _PLAYWRIGHT_SCHOOL_PATHS, \
                f"missing school-finder path {path!r}"

    def test_loose_accept_rule_favors_subdomains(self):
        # A subdomain of the district (east.dpsk12.org for
        # dpsk12.org) is a very strong signal. It should be accepted
        # by the loose rule even without a school-word marker.
        from staffdir.schools import _looks_like_school_url
        assert _looks_like_school_url(
            "https://east.dpsk12.org", "https://www.dpsk12.org")
        # Same for a schools-subdomain child.
        assert _looks_like_school_url(
            "https://schools.dpsk12.org/east-hs",
            "https://www.dpsk12.org")

    def test_loose_accept_rule_rejects_off_domain(self):
        # A totally different domain shouldn't be picked up (chasing
        # charter networks like dsstpublicschools.org explodes
        # candidate count with no boundary).
        from staffdir.schools import _looks_like_school_url
        assert not _looks_like_school_url(
            "https://dsstpublicschools.org",
            "https://www.dpsk12.org")

    def test_loose_accept_rule_rejects_utility_links(self):
        # Same-root utility links (login, contact) should still be
        # rejected even in the loose rule.
        from staffdir.schools import _looks_like_school_url
        for junk in ("https://www.dpsk12.org/login",
                     "https://www.dpsk12.org/employment",
                     "https://www.dpsk12.org/board",
                     "https://www.dpsk12.org/calendar"):
            assert not _looks_like_school_url(
                junk, "https://www.dpsk12.org"), \
                f"loose rule should reject {junk}"

    def test_run_coro_sync_survives_threadpool_worker(self):
        # The helper must work from a background thread (Windows
        # PowerShell + ThreadPoolExecutor is where the previous
        # BrokenPipe cascade happened). Same reason as the earlier
        # test in playwright_fallback — school discovery calls into
        # Playwright from the district pipeline's worker threads.
        import asyncio
        import concurrent.futures
        from staffdir.schools import _run_coro_sync

        async def _tiny():
            await asyncio.sleep(0.01)
            return "ok"

        def _worker():
            # Verify no loop exists on this thread first.
            try:
                asyncio.get_event_loop()
                return "had_loop"
            except RuntimeError:
                pass
            return _run_coro_sync(_tiny())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            assert ex.submit(_worker).result(timeout=10) == "ok"


# --------------------------------------------------------------- fetch-failure escalation

class TestSchoolDiscoveryFetchFailureEscalation:
    """The previous version of discover_schools bailed with 0 schools
    if the district homepage fetch failed (Cloudflare 403, SSL error,
    transient network). That killed DPS-shaped sites in 5 seconds
    because Cloudflare blocks the requests UA but not real browsers.
    Now the fetch-failure path escalates directly to Playwright."""

    def test_source_wires_playwright_on_fetch_failure(self):
        # Guard rail against a refactor that reverts to the old
        # early-return.
        from pathlib import Path
        src = Path(__file__).parent.joinpath("schools.py").read_text()
        # The old code: `if not html: ... return [], report` — with
        # no playwright call. Now we must call _mine_with_playwright
        # inside the `if not html` branch.
        # Cheap check: search for the escalation marker phrase.
        assert "escalating directly to playwright" in src, \
            "fetch-failure escalation missing"

    def test_returns_playwright_schools_when_static_fetch_returns_none(self):
        # Use a stub fetcher that returns None to simulate blocked/
        # Cloudflare-protected homepage. discover_schools must not
        # return empty just because the static call failed — it
        # should try Playwright.
        #
        # We can't launch a real browser here (would need to hit the
        # network), so we monkeypatch _mine_with_playwright to return
        # a canned school list. That verifies the *wiring* — the
        # actual browser path is exercised by the DPS smoke run.
        import staffdir.schools as sm
        from staffdir.schools import (
            discover_schools, SchoolLink)

        fake_schools = [
            SchoolLink(name="East High School",
                       url="https://east.dpsk12.org", score=0.9,
                       is_subdomain=True),
            SchoolLink(name="North High School",
                       url="https://north.dpsk12.org", score=0.9,
                       is_subdomain=True),
            SchoolLink(name="South High School",
                       url="https://south.dpsk12.org", score=0.9,
                       is_subdomain=True),
        ]

        # requests-side stub: always None (Cloudflare-blocked)
        def dead_fetch(url):
            return None

        original = sm._mine_with_playwright
        sm._mine_with_playwright = lambda url, already, **kw: fake_schools
        try:
            result, report = discover_schools(
                "https://www.dpsk12.org", dead_fetch, max_schools=50)
        finally:
            sm._mine_with_playwright = original

        # Should have district office + 3 schools
        assert len(result) == 4, f"got {len(result)} not 4"
        names = [s.name for s in result]
        assert "(District Office)" in names
        assert "East High School" in names
        # Report should note the escalation
        assert any("escalating directly to playwright" in w
                   for w in report.warnings), \
            f"warnings missing escalation note: {report.warnings}"

    def test_returns_empty_only_when_both_static_and_playwright_fail(self):
        # If both requests AND playwright fail, then and only then do
        # we give up with 0 schools + a clear warning.
        import staffdir.schools as sm
        from staffdir.schools import discover_schools

        original = sm._mine_with_playwright
        sm._mine_with_playwright = lambda url, already, **kw: []
        try:
            result, report = discover_schools(
                "https://blocked.example",
                lambda url: None,
                max_schools=50)
        finally:
            sm._mine_with_playwright = original

        assert result == []
        assert any("playwright also could not find" in w
                   for w in report.warnings), \
            f"warnings missing double-failure note: {report.warnings}"
