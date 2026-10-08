"""
staffdir.tests_smoke
====================

Fast synthetic-HTML tests. Not a substitute for real-site tests but
they lock in the field extractors, the classifier, and the strategy
dispatch against representative page shapes.

Run:  python -m staffdir.tests_smoke
"""

from __future__ import annotations

import sys
import traceback
from typing import Callable, List, Tuple

from bs4 import BeautifulSoup

from .classify   import classify_page
from .directory  import extract_staff_directory
from .fields     import (
    extract_emails, extract_phones, extract_name, extract_title,
    normalize_name, normalize_phone,
)
from .schema     import Confidence, FieldSource, PageType


# ---------------------------------------------------------------------------
# Fixtures — small, deliberately varied HTML snippets
# ---------------------------------------------------------------------------

FIX_MAILTO_LIST = """
<html><head><title>Staff Directory - Example ISD</title></head><body>
<h1>Our Staff</h1>
<ul>
  <li><h3>Jane Q Doe</h3>
      <p>Principal</p>
      <a href="mailto:jdoe@example.org">jdoe@example.org</a></li>
  <li><h3>John Smith</h3>
      <p>5th Grade Teacher</p>
      <a href="mailto:jsmith@example.org">jsmith@example.org</a></li>
  <li><h3>Alicia Ramirez, RN</h3>
      <p>School Nurse</p>
      <a href="mailto:aramirez@example.org">aramirez@example.org</a></li>
  <li><h3>Marcus Chen</h3>
      <p>Assistant Principal</p>
      <a href="mailto:mchen@example.org">mchen@example.org</a></li>
</ul>
</body></html>
"""

FIX_AUSTIN_TABLE = """
<html><head><title>Staff | Lincoln HS</title></head><body>
<h1>Staff Directory</h1>
<table><tr><th>Name</th><th>Position</th><th>Department</th><th>Email</th></tr>
  <tr><td>Doe, Jane</td><td>Principal</td><td>Administration</td>
      <td><a href="mailto:jdoe@lincoln.edu">Email</a></td></tr>
  <tr><td>Smith, John</td><td>Math Teacher</td><td>Mathematics</td>
      <td><a href="mailto:jsmith@lincoln.edu">Email</a></td></tr>
  <tr><td>Kim, Sooyoung</td><td>Counselor</td><td>Student Services</td>
      <td><a href="mailto:skim@lincoln.edu">Email</a></td></tr>
  <tr><td>Patel, Amit</td><td>English Teacher</td><td>English</td>
      <td><a href="mailto:apatel@lincoln.edu">Email</a></td></tr>
</table>
</body></html>
"""

FIX_JSON_LD = """
<html><head><title>Meet the Team - Foo Academy</title>
<script type="application/ld+json">
{ "@context": "https://schema.org",
  "@graph": [
    {"@type":"Person","name":"Dr. Robin Alvarez",
     "jobTitle":"Superintendent","email":"ralvarez@foo.edu",
     "telephone":"512-555-0100","image":"/img/ra.jpg",
     "url":"/about/ralvarez"},
    {"@type":"Person","name":"Casey Nguyen",
     "jobTitle":"Assistant Superintendent",
     "email":"cnguyen@foo.edu","telephone":"(512) 555-0101"},
    {"@type":"Person","name":"Priya Rao",
     "jobTitle":"Director of Curriculum",
     "email":"prao@foo.edu"}
  ]}
</script></head><body><h1>Leadership</h1><div id="app"></div></body></html>
"""

FIX_GENERIC_CARDS = """
<html><head><title>Faculty - Bar School</title></head><body>
<h1>Meet Our Faculty</h1>
<div class="col-md-12"><div class="row">
  <div class="col-md-4 mb-3">
     <h4>Emily Watson</h4>
     <p>3rd Grade Teacher</p>
     <p>ewatson at bar dot edu</p>
     <p>(512) 555-2001</p>
  </div>
  <div class="col-md-4 mb-3">
     <h4>Rakesh Verma</h4>
     <p>Music Teacher</p>
     <p>rverma at bar dot edu</p>
     <p>(512) 555-2002 ext 4</p>
  </div>
  <div class="col-md-4 mb-3">
     <h4>Sunny Park</h4>
     <p>Librarian</p>
     <p>spark at bar dot edu</p>
     <p>(512) 555-2003</p>
  </div>
  <div class="col-md-4 mb-3">
     <h4>Diego Ortiz</h4>
     <p>Assistant Principal</p>
     <p>dortiz at bar dot edu</p>
     <p>(512) 555-2004</p>
  </div>
</div></div>
</body></html>
"""

def _cf_hex(email: str, key: int = 0x1e) -> str:
    """Build a valid Cloudflare data-cfemail hex from a plain email +
    XOR key. Format: [key_byte][xored_bytes...] as hex."""
    payload = bytes([key]) + bytes(b ^ key for b in email.encode())
    return payload.hex()

FIX_CLOUDFLARE = f"""
<html><body><h1>Staff</h1>
<div class="staff-card"><h3>Kenji Ito</h3><p>Principal</p>
  <a href="/cdn-cgi/l/email-protection#0000"
     class="__cf_email__" data-cfemail="{_cf_hex('kito@school.edu', 0x2a)}">
     [email&nbsp;protected]</a>
</div>
<div class="staff-card"><h3>Nadia Farouk</h3><p>Vice Principal</p>
  <a class="__cf_email__" data-cfemail="{_cf_hex('nfarouk@school.edu', 0x11)}">
     [email&nbsp;protected]</a>
</div>
<div class="staff-card"><h3>Ben Sato</h3><p>Assistant Principal</p>
  <a class="__cf_email__" data-cfemail="{_cf_hex('bsato@school.edu', 0x77)}">
     [email&nbsp;protected]</a>
</div>
</body></html>
"""

FIX_STAFF_PROFILE = """
<html><head>
<script type="application/ld+json">
{"@type":"Person","name":"Dr. Elena Vasquez",
 "jobTitle":"Principal","email":"evasquez@school.edu",
 "telephone":"555-123-4567"}
</script></head><body>
<h1>Dr. Elena Vasquez</h1><h2>Principal</h2>
<p>Dr. Vasquez has been principal since 2019...</p>
<a href="mailto:evasquez@school.edu">evasquez@school.edu</a>
</body></html>
"""

FIX_LOGIN = """
<html><body><h1>Sign In</h1>
<form><input type="email" name="e"><input type="password" name="p">
<button>Log in</button></form></body></html>
"""

FIX_NEWS = """
<html><head><meta property="og:type" content="article"></head>
<body><article><h1>School Board Adopts New Policy</h1>
<p>By staff writer</p><p>...</p></article></body></html>
"""

FIX_EMPLOYEE_NEWS = """
<html><head><title>Employee News</title>
<meta property="og:type" content="article"></head><body>
<article><h1>Mercy Arancon Recognized for Service</h1>
<p>Mercy Arancon was recognized at this year's employee event.</p>
</article></body></html>
"""

FIX_LABEL_CARDS = """
<html><head><title>Staff Awards</title></head><body>
<div class="staff-card"><h3>About Us</h3><p>Our District</p></div>
<div class="staff-card"><h3>Students</h3><p>Check Grades</p></div>
<div class="staff-card"><h3>Community</h3><p>2026 Election Announcements</p></div>
<div class="staff-card"><h3>Resources</h3><p>Information</p></div>
<div class="staff-card"><h3>Legal Notices</h3><p>Information</p></div>
<div class="staff-card"><h3>Enroll Now</h3><p>Registration</p></div>
</body></html>
"""

FIX_GENERIC_STAFF_NO_PEOPLE = """
<html><head><title>Staff</title></head><body>
<h1>Staff</h1><p>Learn about employment opportunities and staff news.</p>
</body></html>
"""

FIX_FACULTY_STAFF = """
<html><head><title>Faculty &amp; Staff</title></head><body>
<h1>Faculty and Staff</h1>
<div class="staff-card"><h3>Mercy Arancon</h3><p>Principal</p></div>
<div class="staff-card"><h3>Jordan Lee</h3><p>Teacher</p></div>
<div class="staff-card"><h3>Priya Shah</h3><p>Counselor</p></div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Framework
# ---------------------------------------------------------------------------

FAILS: List[str] = []

def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)

def run(name: str, fn: Callable[[], None]) -> None:
    print(f"  {name}...", end=" ")
    try:
        fn()
        print("ok")
    except Exception:
        FAILS.append(f"[{name}] exception:\n{traceback.format_exc()}")
        print("EXCEPTION")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_normalize_name():
    assert normalize_name("Dr. Jane Q. Doe, PhD") == ("Jane", "Doe"), \
        normalize_name("Dr. Jane Q. Doe, PhD")
    assert normalize_name("Smith, John") == ("John", "Smith"), \
        normalize_name("Smith, John")
    assert normalize_name("Prof Robin Alvarez, Ed.D") == ("Robin", "Alvarez")
    assert normalize_name("Cher") == ("Cher", None)
    assert normalize_name("") == (None, None)

def test_normalize_phone():
    assert normalize_phone("(512) 555-1234") == ("(512) 555-1234", None)
    assert normalize_phone("5125551234") == ("(512) 555-1234", None)
    assert normalize_phone("+1 512.555.1234 ext 87") == ("(512) 555-1234", "87")
    assert normalize_phone("bogus") == (None, None)

def test_extract_emails_mailto():
    html = '<div><a href="mailto:jdoe@a.org">jdoe@a.org</a></div>'
    soup = BeautifulSoup(html, "lxml")
    fs = extract_emails(soup.div)
    assert len(fs) == 1
    assert fs[0].value == "jdoe@a.org"
    assert fs[0].source == FieldSource.MAILTO
    assert fs[0].confidence >= Confidence.CERTAIN.value - 0.01

def test_extract_emails_obfuscated():
    html = '<p>Contact: jdoe at example dot org</p>'
    fs = extract_emails(BeautifulSoup(html, "lxml"))
    values = {f.value for f in fs}
    assert "jdoe@example.org" in values, values

def test_extract_emails_cloudflare():
    # data-cfemail hex for "email@example.com" using key 0x1e:
    # Precompute: build a valid hex for a known email.
    email = "user@example.com"
    key = 0x1e
    payload = bytes([key]) + bytes(b ^ key for b in email.encode())
    hex_str = payload.hex()
    html = f'<div><a data-cfemail="{hex_str}">[protected]</a></div>'
    fs = extract_emails(BeautifulSoup(html, "lxml").div)
    assert any(f.value == email and f.source == FieldSource.CLOUDFLARE
               for f in fs), [f.to_dict() for f in fs]

def test_extract_phones():
    html = '<div>Call (512) 555-8899 ext 12 or <a href="tel:5125550100">office</a></div>'
    fs = extract_phones(BeautifulSoup(html, "lxml").div)
    values = {f.value for f in fs}
    assert "(512) 555-0100" in values, values
    assert any("555-8899" in f.value for f in fs), values

def test_extract_name_from_heading():
    html = '<div><h3>Jane Q Doe</h3><p>Principal</p></div>'
    f = extract_name(BeautifulSoup(html, "lxml").div)
    assert f is not None and f.value == "Jane Q Doe"

def test_extract_title_ignores_name():
    html = '<div><h3>Jane Doe</h3><p>Elementary Teacher</p></div>'
    root = BeautifulSoup(html, "lxml").div
    name = extract_name(root)
    title = extract_title(root, exclude_text=name.value)
    assert title is not None and "Teacher" in title.value

def test_classify_directory_mailto_list():
    pc = classify_page(BeautifulSoup(FIX_MAILTO_LIST, "lxml"),
                       "https://example.org/staff-directory")
    assert pc.page_type == PageType.STAFF_DIRECTORY, \
        (pc.page_type, pc.reasons)

def test_classify_directory_austin_table():
    pc = classify_page(BeautifulSoup(FIX_AUSTIN_TABLE, "lxml"),
                       "https://lincoln.edu/staff")
    assert pc.page_type == PageType.STAFF_DIRECTORY

def test_classify_staff_profile():
    pc = classify_page(BeautifulSoup(FIX_STAFF_PROFILE, "lxml"),
                       "https://school.edu/about/vasquez")
    assert pc.page_type == PageType.STAFF_PROFILE, \
        (pc.page_type, pc.confidence, pc.reasons)

def test_classify_login():
    pc = classify_page(BeautifulSoup(FIX_LOGIN, "lxml"),
                       "https://portal.example.org/login")
    assert pc.page_type == PageType.LOGIN

def test_classify_news():
    pc = classify_page(BeautifulSoup(FIX_NEWS, "lxml"),
                       "https://example.org/news/policy")
    assert pc.page_type == PageType.NEWS, (pc.page_type, pc.reasons)


def test_negative_staff_awards_url_not_directory():
    pc = classify_page(FIX_LABEL_CARDS,
                       "https://example.edu/staff-awards-recognition")
    assert pc.page_type != PageType.STAFF_DIRECTORY, \
        (pc.page_type, pc.reasons)


def test_negative_staff_news_url_not_directory():
    pc = classify_page(FIX_EMPLOYEE_NEWS, "https://example.edu/staff-news")
    assert pc.page_type != PageType.STAFF_DIRECTORY, \
        (pc.page_type, pc.reasons)


def test_employee_news_person_not_extracted_as_directory():
    people, report = extract_staff_directory(
        FIX_EMPLOYEE_NEWS, "https://example.edu/employee-news/mercy")
    assert people == [], [p.to_dict() for p in people]
    assert report.page_classification.page_type == PageType.NEWS
    assert report.strategy_used == "rejected_page_type"


def test_navigation_labels_not_extracted_as_people():
    people, report = extract_staff_directory(
        FIX_LABEL_CARDS, "https://example.edu/staff-directory")
    assert people == [], [p.to_dict() for p in people]


def test_generic_staff_url_needs_person_evidence():
    pc = classify_page(FIX_GENERIC_STAFF_NO_PEOPLE,
                       "https://example.edu/staff")
    assert pc.page_type != PageType.STAFF_DIRECTORY, \
        (pc.page_type, pc.reasons)
    people, report = extract_staff_directory(
        FIX_GENERIC_STAFF_NO_PEOPLE, "https://example.edu/staff")
    assert people == []


def test_faculty_staff_cards_still_classify_as_directory():
    pc = classify_page(FIX_FACULTY_STAFF,
                       "https://example.edu/faculty-staff")
    assert pc.page_type == PageType.STAFF_DIRECTORY, \
        (pc.page_type, pc.reasons)


def test_legitimate_person_with_staff_context_still_extracts():
    people, report = extract_staff_directory(
        FIX_FACULTY_STAFF, "https://example.edu/faculty-staff")
    names = {p.name.value for p in people if p.name}
    assert "Mercy Arancon" in names, [p.to_dict() for p in people]
    mercy = next(p for p in people if p.name.value == "Mercy Arancon")
    assert mercy.title and mercy.title.value == "Principal"


def test_normal_extractor_rejects_news_page():
    people, report = extract_staff_directory(
        FIX_NEWS, "https://example.edu/news/policy")
    assert people == []
    assert report.strategy_used == "rejected_page_type"


def test_extract_directory_mailto_list():
    people, report = extract_staff_directory(FIX_MAILTO_LIST,
                                              "https://example.org/staff")
    assert len(people) >= 4, [p.to_dict() for p in people]
    names = {p.name.value for p in people if p.name}
    assert "Jane Q Doe" in names
    assert any(p.email and p.email.value == "aramirez@example.org"
               for p in people)


def test_extract_directory_austin_table():
    people, report = extract_staff_directory(FIX_AUSTIN_TABLE,
                                              "https://lincoln.edu/staff")
    assert report.strategy_used in ("tables", "named_cards",
                                     "repeating_blocks", "microdata")
    assert len(people) >= 4, [p.to_dict() for p in people]
    # Table extractor should preserve the "Position" column as title.
    doe = next((p for p in people if p.last_name and p.last_name.value == "Doe"), None)
    assert doe is not None and doe.title and "Principal" in doe.title.value


def test_extract_directory_json_ld():
    people, report = extract_staff_directory(FIX_JSON_LD,
                                              "https://foo.edu/leadership")
    assert report.strategy_used == "json_ld"
    assert len(people) == 3
    ralvarez = next(p for p in people if p.last_name and
                    p.last_name.value == "Alvarez")
    assert ralvarez.email and ralvarez.email.value == "ralvarez@foo.edu"
    assert ralvarez.title and "Superintendent" in ralvarez.title.value


def test_extract_directory_generic_cards():
    """The key test: no `.staff-member` classes anywhere, just Bootstrap
    grid divs. The repeating-blocks detector should still find them."""
    people, report = extract_staff_directory(FIX_GENERIC_CARDS,
                                              "https://bar.edu/faculty")
    assert len(people) >= 3, (report.to_dict(),
                              [p.to_dict() for p in people])
    # Names should be present.
    names = {p.name.value for p in people if p.name}
    assert "Emily Watson" in names and "Rakesh Verma" in names
    # Obfuscated emails should be decoded.
    ewat = next(p for p in people if p.name.value == "Emily Watson")
    assert ewat.email and ewat.email.value == "ewatson@bar.edu", \
        (ewat.email.to_dict() if ewat.email else None)


def test_extract_directory_cloudflare():
    people, report = extract_staff_directory(FIX_CLOUDFLARE,
                                              "https://cf.example/staff")
    assert len(people) >= 3
    for p in people:
        assert p.email is not None, p.to_dict()
        assert "@" in p.email.value


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_normalize_name, test_normalize_phone,
        test_extract_emails_mailto, test_extract_emails_obfuscated,
        test_extract_emails_cloudflare, test_extract_phones,
        test_extract_name_from_heading, test_extract_title_ignores_name,
        test_classify_directory_mailto_list,
        test_classify_directory_austin_table,
        test_classify_staff_profile, test_classify_login, test_classify_news,
        test_negative_staff_awards_url_not_directory,
        test_negative_staff_news_url_not_directory,
        test_employee_news_person_not_extracted_as_directory,
        test_navigation_labels_not_extracted_as_people,
        test_generic_staff_url_needs_person_evidence,
        test_faculty_staff_cards_still_classify_as_directory,
        test_legitimate_person_with_staff_context_still_extracts,
        test_normal_extractor_rejects_news_page,
        test_extract_directory_mailto_list,
        test_extract_directory_austin_table,
        test_extract_directory_json_ld,
        test_extract_directory_generic_cards,
        test_extract_directory_cloudflare,
    ]
    print(f"Running {len(tests)} smoke tests")
    for t in tests: run(t.__name__, t)
    if FAILS:
        print("\nFAILURES:")
        for msg in FAILS: print("  -", msg)
        return 1
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
