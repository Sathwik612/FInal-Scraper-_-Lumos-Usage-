"""Tests for staffdir.decision_makers (run: python -m staffdir.tests_decision_makers)."""

from __future__ import annotations

import sys

from .schema import Confidence, Field, FieldSource, Person
from .directory import extract_staff_directory
from .decision_makers import (
    match_role, extract_decision_makers, harvest_decision_makers,
)


def _person(name, title, email=None, email_source=FieldSource.MAILTO,
            email_conf=Confidence.CERTAIN.value):
    p = Person(name=Field(name, Confidence.HIGH.value, FieldSource.HEADING),
               title=Field(title, Confidence.HIGH.value, FieldSource.CARD_TEXT))
    if email is not None:
        p.email = Field(email, email_conf, email_source)
    return p


def test_match_role_tiers():
    assert match_role("Superintendent of Schools").tier == 1
    assert match_role("Assistant Superintendent").canonical.startswith(
        "Assistant/Associate/Deputy")
    assert match_role("School Business Administrator / Board Secretary"
                      ).canonical == "Business Administrator"
    assert match_role("Director of Curriculum & Instruction").tier == 1
    assert match_role("Director of Technology").tier == 1
    assert match_role("Principal").canonical == "Principal"
    # Non-buyers get no role.
    assert match_role("3rd Grade Teacher") is None
    assert match_role("Instructional Aide") is None
    assert match_role("Administrative Assistant to the Principal") is None \
        or match_role("Administrative Assistant to the Principal").tier == 2
    # Negative-term veto on generic leadership.
    assert match_role("Athletic Director") is None
    assert match_role("Band Director") is None
    # ...but a real cabinet role is never vetoed.
    assert match_role("Superintendent") is not None
    print("ok  match_role tiers + vetoes")


def test_email_gate_drops_unpublished():
    people = [
        _person("Jane Super", "Superintendent",
                "jsuper@d.k12.nj.us"),                       # keep
        _person("John Biz", "Business Administrator",
                "jbiz@d.k12.nj.us"),                         # keep
        _person("Sam Tech", "Director of Technology"),       # no email -> drop
        _person("Mary Teach", "3rd Grade Teacher",
                "mteach@d.k12.nj.us"),                       # not a buyer -> drop
    ]
    rows = extract_decision_makers(
        people, district_name="D", district_url="https://d.k12.nj.us",
        directory_url="https://d.k12.nj.us/staff", require_email=True)
    got = {r["staff_name"] for r in rows}
    assert got == {"Jane Super", "John Biz"}, got
    # Every kept row actually has an email + a published source.
    assert all(r["email"] and r["email_source"] for r in rows)
    # Superintendent scores into priority A (title+scope+email+class).
    sup = next(r for r in rows if r["staff_name"] == "Jane Super")
    assert sup["lead_priority"] == "A", sup["lead_score"]
    assert sup["organization_level"] == "DISTRICT"
    print("ok  email gate keeps only published-email buyers")


def test_allow_missing_email_opt_in():
    people = [_person("Sam Tech", "Director of Technology")]
    keep = extract_decision_makers(
        people, district_name="D", require_email=False)
    assert len(keep) == 1 and keep[0]["email"] == ""
    drop = extract_decision_makers(
        people, district_name="D", require_email=True)
    assert drop == []
    print("ok  require_email=False opt-in returns emailless buyers")


def test_inferred_source_is_not_published():
    # Simulate a hypothetical guessed address: high confidence but a
    # non-published source. It must NOT count as a contactable lead.
    p = _person("Guess Person", "Superintendent",
                "g.person@d.k12.nj.us", email_source=FieldSource.URL_TOKEN,
                email_conf=0.9)
    rows = extract_decision_makers(
        p and [p], district_name="D", require_email=True)
    assert rows == [], "URL-token/guessed email must not qualify as a lead"
    print("ok  guessed (non-published) email is rejected by the gate")


def test_end_to_end_through_real_extractor():
    html = """
    <html><body>
    <div class="staff-member"><h3>Dr. Jane Superintendent</h3>
      <p>Superintendent of Schools</p>
      <a href="mailto:jsuper@example.k12.nj.us">Email</a></div>
    <div class="staff-member"><h3>John Business</h3>
      <p>School Business Administrator / Board Secretary</p>
      <a href="mailto:jbusiness@example.k12.nj.us">Email</a></div>
    <div class="staff-member"><h3>Mary Teacher</h3>
      <p>3rd Grade Teacher</p>
      <a href="mailto:mteacher@example.k12.nj.us">Email</a></div>
    <div class="staff-member"><h3>Alex Athletics</h3>
      <p>Athletic Director</p>
      <a href="mailto:athletics@example.k12.nj.us">Email</a></div>
    <div class="staff-member"><h3>Sam Tech</h3>
      <p>Director of Technology</p></div>
    </body></html>"""
    persons, _ = extract_staff_directory(html, "https://example.k12.nj.us/staff")
    rows = extract_decision_makers(
        persons, district_name="Example Township Public Schools",
        district_url="https://example.k12.nj.us/",
        directory_url="https://example.k12.nj.us/staff", require_email=True)
    names = sorted(r["staff_name"] for r in rows)
    # Superintendent + Business Admin only: teacher (not a buyer),
    # athletic director (vetoed), tech director (no email) all excluded.
    assert names == ["Dr. Jane Superintendent", "John Business"], names
    for r in rows:
        assert r["email"].endswith("@example.k12.nj.us")
        assert r["first_name"] and r["last_name"]
    print("ok  end-to-end via extract_staff_directory ->", names)


def test_harvest_with_stub_fetcher():
    # A realistic roster: 4 buyers among a crowd of teachers. Discovery
    # only accepts a page as a directory if it looks like one, so the
    # fixture needs real size.
    cards = [
        ("Pat Leader", "Superintendent", "pleader"),
        ("Chris Money", "Business Administrator", "cmoney"),
        ("Dana Curriculum", "Director of Curriculum & Instruction", "dcurr"),
        ("Robin Tech", "Director of Technology", "rtech"),
    ]
    cards += [(f"Teacher {i}", f"{i + 1}th Grade Teacher", f"teach{i}")
              for i in range(6)]
    staff_html = "<html><body>" + "".join(
        f'<div class="staff-member"><h3>{nm}</h3><p>{ti}</p>'
        f'<a href="mailto:{u}@d.k12.nj.us">Email</a></div>'
        for nm, ti, u in cards) + "</body></html>"
    home_html = ('<html><body><nav>'
                 '<a href="/staff-directory">Staff Directory</a></nav></body></html>')

    def stub_fetch(url):
        if url.rstrip("/").endswith("staff-directory"):
            return staff_html
        return home_html

    rows, report = harvest_decision_makers(
        "https://d.k12.nj.us/", stub_fetch,
        district_name="Demo District", require_email=True)
    assert report["leads"] == len(rows)
    # All 4 buyers kept; none of the 6 teachers.
    assert {r["staff_name"] for r in rows} == {
        "Pat Leader", "Chris Money", "Dana Curriculum", "Robin Tech"}, rows
    assert all(r["email"] and r["dm_tier"] == 1 for r in rows)
    print("ok  harvest end-to-end w/ stub fetcher ->", report["leads"],
          "buyer leads from", report["persons_seen"], "people")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nall {len(tests)} decision-maker tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
