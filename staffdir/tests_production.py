"""Focused regression tests for the production lead workflow."""

from __future__ import annotations

import csv
from pathlib import Path

from .directory import extract_staff_directory
from .classify import classify_page
from .leads import classify_lead, deduplicate_leads, priority_for_score, score_lead
from .pipeline import run_district_pipeline


CONTACT_LIST_PRO = """<!doctype html><html><head><title>Staff Directory</title></head>
<body><h1>Staff Directory</h1><ul class="contact-list">
<li id="cl-101" class="contact-list-contact-container">
  <div class="contact-list-contact-name">Jordan Lee</div>
  <div class="contact-list-job-title">Principal</div>
  <a data-name="Jordan Lee" data-id="101" href="/contact">Send message</a>
</li>
<li id="cl-102" class="contact-list-contact-container">
  <div class="contact-list-contact-name">Casey Morgan</div>
  <div class="contact-list-job-title">Teacher</div>
  <a data-name="Casey Morgan" data-id="102" href="/contact">Send message</a>
</li>
</ul></body></html>"""


FINALSITE_CONSTITUENTS = """<!doctype html><html><head><title>Teachers</title></head>
<body><main class="fsConstituent fsDirectory">
<div class="fsConstituentItem" data-constituent-id="1001">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  href="/staff/jason">Jason Reed</a></h3>
  <div class="fsTitles">English Teacher</div>
</div>
<div class="fsConstituentItem" data-constituent-id="1002">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  href="#">Morgan Bell</a></h3>
  <div class="fsTitles">Counselor</div>
</div>
<div class="fsConstituentItem" data-constituent-id="1003">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  href="/staff/lee">Lee Carter</a></h3>
  <div class="fsTitles">Principal</div>
</div>
<div class="fsEmail" data-constituent-id="1001">
  <script>FS.util.insertEmail('x', 'nosaj', 'moc.elpmaxe', false);</script>
</div>
<div class="fsEmail" data-constituent-id="1002">
  <a href="mailto:morgan@example.org">Email</a>
</div>
<div class="fsEmail" data-constituent-id="1003">
  <a href="mailto:lee@example.org">Email</a>
</div>
</main></body></html>"""


FINALSITE_JS_ONLY = """<!doctype html><html><body>
<div class="fsElement fsConstituent fsDirectory" id="fsEl_43122">
<div class="fsConstituentItem" data-constituent-id="4709">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  data-constituent-id="4709" href="#">Marissa Ames</a></h3>
  <div class="fsTitles">English Teacher</div>
</div>
<div class="fsConstituentItem" data-constituent-id="4485">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  data-constituent-id="4485" href="#">Kate Bast</a></h3>
  <div class="fsTitles">Resource Teacher</div>
</div>
<div class="fsConstituentItem" data-constituent-id="35909">
  <h3 class="fsFullName"><a class="fsConstituentProfileLink"
  data-constituent-id="35909" href="#">Olivia Bauman</a></h3>
  <div class="fsTitles">Science Teacher</div>
</div>
</div></body></html>"""


FINALSITE_PROFILE_4709 = """<div class="fsElement fsConstituent fsProfile"
id="fsEl_43122"><div class="fsElementContent">
<div class="fsConstituentCustom fsConstituentProfile"
data-constituent-id="4709"><h3 class="fsFullName">Marissa Ames</h3>
<div class="fsTitles">English Teacher</div><div class="fsEmail">
<div id="fsEmail-43122-1947-profile"><script>
FS.util.insertEmail("fsEmail-43122-1947-profile", "gro.dsuh",
"sema.assiram", false);</script></div></div></div></div></div>"""


FINALSITE_PROFILE_4485 = """<div class="fsElement fsConstituent fsProfile"
id="fsEl_43122"><div class="fsElementContent">
<div class="fsConstituentCustom fsConstituentProfile"
data-constituent-id="4485"><h3 class="fsFullName">Kate Bast</h3>
<div class="fsTitles">Resource Teacher</div><div class="fsEmail">
<div id="fsEmail-43122-4485-profile"><a
href="mailto:kate.bast@husd.org">kate.bast@husd.org</a></div>
</div></div></div></div>"""


def test_contact_list_pro_requires_visible_name_and_supporting_evidence():
    people, report = extract_staff_directory(CONTACT_LIST_PRO, "https://x.test/staff")
    assert [p.name.value for p in people] == ["Jordan Lee", "Casey Morgan"]
    assert all(p.contact_id and p.contact_id.value in {"101", "102"}
               for p in people)
    assert all(p.name.source.value == "contact_list_pro" for p in people)

    only_data_name = """<html><body><a data-name="Taylor Smith"
    href="/contact">Send message</a></body></html>"""
    people, _ = extract_staff_directory(only_data_name, "https://x.test/")
    assert people == []

    one_contact = """<html><body>
    <div class="contact-list-contact-container">
      <div class="contact-list-contact-name">Avery Jones</div>
      <div class="contact-list-job-title">Superintendent</div>
      <a data-name="Avery Jones" data-id="1" href="/contact">Send message</a>
    </div></body></html>"""
    people, report = extract_staff_directory(one_contact,
                                             "https://x.test/staff")
    assert len(people) == 1
    assert report.strategy_used == "contact_list_pro"


def test_finalsite_constituents_preserve_ids_profiles_and_safe_emails():
    people, report = extract_staff_directory(
        FINALSITE_CONSTITUENTS, "https://x.test/staff-directory/teachers")
    assert report.strategy_used == "finalsite_constituent"
    assert [p.name.value for p in people] == [
        "Jason Reed", "Morgan Bell", "Lee Carter"]
    assert [p.contact_id.value for p in people] == ["1001", "1002", "1003"]
    assert people[0].email.value == "jason@example.com"
    assert people[1].email.value == "morgan@example.org"
    assert people[2].profile_url.value == "https://x.test/staff/lee"
    assert people[1].profile_url is None
    assert report.emails_found == 3


def test_finalsite_does_not_assign_unpaired_email_to_next_person():
    html = FINALSITE_CONSTITUENTS.replace(
        '<div class="fsEmail" data-constituent-id="1002">\n'
        '  <a href="mailto:morgan@example.org">Email</a>\n</div>\n', "")
    people, _ = extract_staff_directory(html, "https://x.test/staff-directory")
    by_name = {p.name.value: p for p in people}
    assert by_name["Jason Reed"].email.value == "jason@example.com"
    assert by_name["Morgan Bell"].email is None
    assert by_name["Lee Carter"].email.value == "lee@example.org"


def test_embedded_json_recovers_dynamic_directory_records():
    html = """<!doctype html><html><head><title>Staff</title></head><body>
    <h1>Staff Directory</h1>
    <script type="application/json">{"props":{"staff":[
      {"name":"Riley Stone","jobTitle":"Principal",
       "email":"riley@example.org","profileUrl":"/staff/riley"},
      {"firstName":"Taylor","lastName":"Ng","title":"Counselor",
       "phone":"602-555-0101"}
    ]}}</script></body></html>"""
    people, report = extract_staff_directory(
        html, "https://x.test/staff", classify_first=True)
    assert report.page_classification.page_type.value == "staff_directory"
    assert [person.name.value for person in people] == [
        "Riley Stone", "Taylor Ng"]
    assert people[0].email.value == "riley@example.org"
    assert people[0].profile_url.value == "https://x.test/staff/riley"
    assert people[1].phone.value == "(602) 555-0101"


def test_generic_numbered_pagination_merges_bounded_pages():
    page_one = """<!doctype html><html><head><title>Staff</title></head><body>
    <h1>Staff Directory</h1><ul class="staff-list">
    <li><h3>Alex Kim</h3><p>Principal</p>
      <a href="mailto:alex@example.org">Email</a></li>
    <li><h3>Blair Fox</h3><p>Teacher</p>
      <a href="mailto:blair@example.org">Email</a></li>
    <li><h3>Chris Ray</h3><p>Counselor</p>
      <a href="mailto:chris@example.org">Email</a></li>
    </ul><nav class="pagination">
      <a href="/staff?page=1">1</a>
      <a href="/staff?page=2">2</a>
      <a href="/staff?page=3">3</a>
    </nav></body></html>"""
    page_two = """<!doctype html><html><body><h1>Staff Directory</h1>
    <ul class="staff-list">
    <li><h3>Drew Cole</h3><p>Teacher</p>
      <a href="mailto:drew@example.org">Email</a></li>
    <li><h3>Evan Hall</h3><p>Director</p>
      <a href="mailto:evan@example.org">Email</a></li>
    <li><h3>Frank Wu</h3><p>Aide</p>
      <a href="mailto:frank@example.org">Email</a></li>
    </ul></body></html>"""
    page_three = """<!doctype html><html><body><h1>Staff Directory</h1>
    <ul class="staff-list">
    <li><h3>Gale Park</h3><p>Teacher</p>
      <a href="mailto:gale@example.org">Email</a></li>
    <li><h3>Harper Joy</h3><p>Nurse</p>
      <a href="mailto:harper@example.org">Email</a></li>
    <li><h3>Indigo Lee</h3><p>Teacher</p>
      <a href="mailto:indigo@example.org">Email</a></li>
    </ul></body></html>"""
    pages = {
        "https://x.test/staff": page_one,
        "https://x.test/staff?page=2": page_two,
        "https://x.test/staff?page=3": page_three,
    }
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.get(url)

    people, report = extract_staff_directory(
        page_one, "https://x.test/staff", fetch=fetch,
        max_directory_pages=3)
    assert len(people) == 9
    assert report.pagination_pages == 3
    assert report.pagination_urls == [
        "https://x.test/staff?page=2",
        "https://x.test/staff?page=3",
    ]
    assert report.recovered_by_strategy["named_cards"] >= 9
    assert calls == [
        "https://x.test/staff?page=2",
        "https://x.test/staff?page=3",
    ]


def test_generic_next_pagination_continues_from_each_page():
    def page(name, next_url=""):
        next_link = (
            f'<a rel="next" href="{next_url}">Next</a>'
            if next_url else "")
        return f"""<html><body><h1>Staff Directory</h1>
        <ul class="staff-list">
        <li><h3>{name} One</h3><p>Teacher</p>
          <a href="mailto:{name.lower()}@example.org">Email</a></li>
        <li><h3>{name} Two</h3><p>Teacher</p>
          <a href="mailto:{name.lower()}2@example.org">Email</a></li>
        <li><h3>{name} Three</h3><p>Teacher</p>
          <a href="mailto:{name.lower()}3@example.org">Email</a></li>
        </ul><nav class="pager">{next_link}</nav></body></html>"""

    pages = {
        "https://x.test/faculty": page("A", "/faculty?page=2"),
        "https://x.test/faculty?page=2": page("B", "/faculty?page=3"),
        "https://x.test/faculty?page=3": page("C"),
    }

    people, report = extract_staff_directory(
        pages["https://x.test/faculty"], "https://x.test/faculty",
        fetch=pages.get, max_directory_pages=3)
    assert len(people) == 9
    assert report.pagination_pages == 3
    assert report.pagination_urls == [
        "https://x.test/faculty?page=2",
        "https://x.test/faculty?page=3",
    ]


def test_mailto_context_recovers_name_in_nearby_heading_without_page_scan():
    html = """<html><body><main><section class="directory">
    <div class="contact-row"><div class="identity"><h3>Jordan Vale</h3>
      <span>Assistant Principal</span></div>
      <div class="actions"><a href="mailto:jordan@example.org">
      Email Jordan</a></div></div>
    </section></main></body></html>"""
    people, _ = extract_staff_directory(
        html, "https://x.test/staff", classify_first=False)
    assert len(people) == 1
    assert people[0].name.value == "Jordan Vale"
    assert people[0].email.value == "jordan@example.org"


def test_finalsite_js_only_profiles_hydrate_emails_by_constituent_id():
    calls = []
    responses = {
        "https://x.test/fs/elements/43122?const_id=4709&show_profile=true":
            FINALSITE_PROFILE_4709,
        "https://x.test/fs/elements/43122?const_id=4485&show_profile=true":
            FINALSITE_PROFILE_4485,
        "https://x.test/fs/elements/43122?const_id=35909&show_profile=true":
            "",
    }

    def fetch(url):
        calls.append(url)
        return responses.get(url)

    people, report = extract_staff_directory(
        FINALSITE_JS_ONLY, "https://x.test/staff", fetch=fetch)
    by_id = {p.contact_id.value: p for p in people}
    assert by_id["4709"].email.value == "marissa.ames@husd.org"
    assert by_id["4485"].email.value == "kate.bast@husd.org"
    assert by_id["35909"].email is None
    assert calls == list(responses)
    assert report.js_hydration_attempted is True
    assert report.js_hydration_requests == 3
    assert report.js_hydration_succeeded == 2
    assert report.js_hydration_failed == 1
    assert "finalsite_hydration" in report.strategy_results
    assert any("35909: empty response" in w for w in report.warnings)


def test_finalsite_js_only_unavailable_profile_is_explicit_and_unassigned():
    calls = []

    def fetch(url):
        calls.append(url)
        return """<div class="fsConstituentCustom fsConstituentProfile"
        data-constituent-id="9999">
        <div class="fsEmail"><a href="mailto:wrong@example.org">Email</a>
        </div></div>"""

    people, report = extract_staff_directory(
        FINALSITE_JS_ONLY, "https://x.test/staff", fetch=fetch,
        max_profile_hydrations=1)
    assert all(p.email is None for p in people)
    assert len(calls) == 1
    assert report.js_hydration_succeeded == 0
    assert report.js_hydration_failed == 3
    assert any("no safely associated email" in w for w in report.warnings)
    assert any("hydration capped at 1" in w for w in report.warnings)


def test_finalsite_uses_order_only_for_one_to_one_email_wrappers():
    html = """<html><body><main class="fsConstituent fsDirectory">
    <div class="fsConstituentItem"><h3 class="fsFullName">Ava Stone</h3>
      <div class="fsTitles">Teacher</div></div>
    <div class="fsConstituentItem"><h3 class="fsFullName">Noah West</h3>
      <div class="fsTitles">Teacher</div></div>
    <div class="fsEmail"><a href="mailto:ava@example.org">Email</a></div>
    <div class="fsEmail"><a href="mailto:noah@example.org">Email</a></div>
    </main></body></html>"""
    people, _ = extract_staff_directory(html, "https://x.test/staff")
    assert [p.email.value for p in people] == [
        "ava@example.org", "noah@example.org"]


def test_finalsite_false_positive_navigation_is_rejected():
    html = """<html><body><nav>
      <a class="fsConstituentProfileLink" data-constituent-id="1"
      href="#">A navigation link</a>
    </nav></body></html>"""
    classification = classify_page(html, "https://x.test/staff-directory")
    assert classification.page_type.value != "staff_directory"
    people, report = extract_staff_directory(html, "https://x.test/staff-directory")
    assert people == []
    assert report.strategy_used == "rejected_page_type"


def test_extraction_merges_evidence_across_strategies():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Person",
     "name":"Jamie Rivera","email":"jamie@example.org"}
    </script></head><body>
    <div class="staff-card"><h2>Jamie Rivera</h2>
      <div class="title">Superintendent</div>
      <a href="tel:+1-555-010-0100">Call</a></div>
    <div class="staff-card"><h2>Robin Patel</h2>
      <div class="title">Teacher</div></div>
    <div class="staff-card"><h2>Alex Morgan</h2>
      <div class="title">Counselor</div></div>
    </body></html>"""
    people, report = extract_staff_directory(html, "https://x.test/staff")
    jamie = next(p for p in people if p.name.value == "Jamie Rivera")
    assert jamie.email.value == "jamie@example.org"
    assert jamie.phone.value == "(555) 010-0100"
    assert report.merged_records == 3
    assert report.strategy_used == "multi_pass"


def test_finalsite_pagination_is_bounded_and_deduplicated():
    page_one = FINALSITE_CONSTITUENTS.replace(
        '<div class="fsConstituentItem" data-constituent-id="1003">',
        '<a class="fsNextPageLink" data-page="2" href="?const_page=2&">Next</a>'
        '<div class="fsConstituentItem" data-constituent-id="1003">')
    page_two = """<html><body><main class="fsConstituent fsDirectory">
    <div class="fsConstituentItem" data-constituent-id="1004">
      <h3 class="fsFullName">Taylor Jones</h3>
      <div class="fsTitles">Teacher</div>
    </div></main></body></html>"""
    calls = []
    pages = {
        "https://x.test/staff-directory/teachers": page_one,
        "https://x.test/staff-directory/teachers?const_page=2&": page_two,
    }

    def fetch(url):
        calls.append(url)
        return pages.get(url)

    people, report = extract_staff_directory(
        page_one, "https://x.test/staff-directory/teachers",
        fetch=fetch, max_directory_pages=2)
    assert {p.contact_id.value for p in people} == {"1001", "1002", "1003", "1004"}
    assert report.pagination_pages == 2
    assert len(calls) == 1


def test_lead_classification_and_deterministic_scoring():
    assert classify_lead("Superintendent") == "DISTRICT_ADMIN"
    assert classify_lead("Principal") == "HIGH_VALUE"
    assert classify_lead("Chief Financial Officer") == "DECISION_MAKER"
    assert classify_lead("5th Grade Teacher") == "TEACHING_STAFF"
    assert classify_lead("School Nurse") == "SUPPORT_STAFF"
    assert classify_lead("") == "UNKNOWN"
    row = {
        "title": "Principal", "lead_class": "HIGH_VALUE",
        "organization_level": "SCHOOL", "school_name": "A School",
        "district_name": "A District", "email": "j@example.org",
        "phone": "555-555-5555", "profile_url": "https://x.test/j",
    }
    assert score_lead(row) == 100
    assert priority_for_score(80) == "A"
    assert priority_for_score(60) == "B"
    assert priority_for_score(59) == "C"


def test_dedupe_respects_district_and_school_scope():
    base = {
        "staff_name": "Alex Smith", "email": "alex@example.org",
        "organization_level": "SCHOOL", "school_name": "North School",
        "lead_score": 75,
    }
    rows = deduplicate_leads([
        {**base, "phone": ""},
        {**base, "phone": "555-555-1212", "lead_score": 80},
        {**base, "organization_level": "DISTRICT", "school_name": "",
         "title": "Superintendent"},
    ])
    assert len(rows) == 2
    north = next(r for r in rows if r["school_name"] == "North School")
    assert north["phone"] == "555-555-1212"


def test_production_runner_writes_scope_and_status_outputs(tmp_path: Path):
    district_home = """<html><head><title>Example District</title></head><body>
    <a href="/district-staff">District Staff</a>
    <a href="/schools">Schools</a></body></html>"""
    school_home = """<html><head><title>North School</title></head><body>
    <a href="/staff">Staff Directory</a></body></html>"""
    district_staff = """<html><body><h1>District Staff</h1>
    <div class="staff-card"><h2>Jamie Rivera</h2><p>Superintendent</p>
    <a href="mailto:jamie@example.org">Email</a></div>
    <div class="staff-card"><h2>Robin Patel</h2><p>Business Office</p>
    <a href="mailto:robin@example.org">Email</a></div>
    <div class="staff-card"><h2>Sam Chen</h2><p>Director of Operations</p>
    <a href="mailto:sam@example.org">Email</a></div></body></html>"""
    pages = {
        "https://district.test/": district_home,
        "https://district.test/district-staff": district_staff,
        "https://district.test/schools": (
            '<html><body><a href="/north-elementary">North Elementary School</a>'
            '</body></html>'),
        "https://district.test/north-elementary": school_home,
        "https://district.test/north-elementary/staff": CONTACT_LIST_PRO,
    }

    def fetch(url: str):
        return pages.get(url)

    prefix = str(tmp_path / "az_final")
    schools_path, leads_path = run_district_pipeline(
        [("Example District", "https://district.test/")],
        out_prefix=prefix, fetch=fetch, workers=1, inner_workers=1)
    status_path = prefix + "_district_status.csv"
    assert Path(schools_path).exists()
    assert Path(leads_path).exists()
    assert Path(status_path).exists()

    with open(leads_path, newline="", encoding="utf-8") as fh:
        leads = list(csv.DictReader(fh))
    assert any(r["organization_level"] == "DISTRICT" and
               r["school_name"] == "" for r in leads)
    assert any(r["organization_level"] == "SCHOOL" and
               r["school_name"] == "North Elementary School" for r in leads)
    assert any(r["lead_class"] == "DISTRICT_ADMIN" for r in leads)
    assert any(r["lead_class"] == "HIGH_VALUE" for r in leads)

    with open(status_path, newline="", encoding="utf-8-sig") as fh:
        status = list(csv.DictReader(fh))
    assert len(status) == 1
    assert status[0]["district_name"] == "Example District"
    assert status[0]["district_status"] in {"SUCCESS", "PARTIAL"}