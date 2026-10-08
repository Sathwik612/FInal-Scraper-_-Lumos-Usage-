"""Offline tests for build_nj_seed.py (no network).

Run:  python tools/tests_build_nj_seed.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import build_nj_seed as b


def test_normalize_key_matches_across_naming_styles():
    a = b.normalize_district_key("Newark Public Schools")
    c = b.normalize_district_key("Newark School District")
    assert a == c == "newark", (a, c)
    assert b.normalize_district_key("Cherry Hill Township Public Schools") == \
        b.normalize_district_key("Cherry Hill School District")
    print("ok  normalize_district_key collapses naming styles")


def test_clean_website():
    assert b.clean_website("www.foo.k12.nj.us") == "https://www.foo.k12.nj.us"
    assert b.clean_website("https://x.org/") == "https://x.org/"
    assert b.clean_website("  N/A ") is None
    assert b.clean_website("") is None
    assert b.clean_website(None) is None
    print("ok  clean_website normalizes + rejects junk")


def test_ccd_type_and_operating_filters():
    reg = {"lea_name": "Regular USD", "agency_type": 1, "sy_status": 1}
    charter = {"lea_name": "Charter LEA", "agency_type": 7, "sy_status": 1}
    state = {"lea_name": "State Facility", "agency_type": 4, "sy_status": 1}
    closed = {"lea_name": "Gone USD", "agency_type": 1, "sy_status": 2}
    assert b.ccd_type(reg)[2] is True and b.ccd_type(reg)[1] is False
    assert b.ccd_type(charter)[1] is True and b.ccd_type(charter)[2] is True
    assert b.ccd_type(state)[2] is False
    assert b.ccd_is_operating(reg) is True
    assert b.ccd_is_operating(closed) is False
    print("ok  agency-type keep + closed filter")


def test_website_precedence_merge_over_ccd_over_wikidata():
    ccd = [
        {"lea_name": "Alpha Township Public Schools", "leaid": "3400001",
         "agency_type": 1, "sy_status": 1, "county_name": "Bergen",
         "enrollment": 1200, "website": "https://alpha-ccd.k12.nj.us"},
        {"lea_name": "Beta School District", "leaid": "3400002",
         "agency_type": 1, "sy_status": 1, "county_name": "Essex",
         "enrollment": 800},  # no CCD website
        {"lea_name": "Gamma Charter School", "leaid": "3400003",
         "agency_type": 7, "sy_status": 1, "county_name": "Hudson",
         "enrollment": 300},  # website only from wikidata
        {"lea_name": "Closed USD", "leaid": "3400004",
         "agency_type": 1, "sy_status": 2, "county_name": "Ocean"},
        {"lea_name": "State Agency", "leaid": "3400005",
         "agency_type": 4, "sy_status": 1, "county_name": "Mercer"},
    ]
    wikidata = b.SiteIndex()
    wikidata.add("Alpha Township Public Schools", "Bergen",
                 "https://alpha-wiki.k12.nj.us")
    wikidata.add("Gamma Charter School", "Hudson", "https://gamma-wiki.org")
    merge = b.SiteIndex()
    merge.add("Alpha Township Public Schools", "Bergen",
              "https://alpha-merge.k12.nj.us")

    rows, stats = b.assemble_rows(ccd, wikidata=wikidata, merge=merge)
    by_name = {r["name"]: r for r in rows}

    # Closed + state agency dropped.
    assert "Closed USD" not in by_name and "State Agency" not in by_name
    assert stats["dropped_closed"] == 1 and stats["dropped_type"] == 1
    # Alpha: merge wins over CCD and Wikidata.
    assert by_name["Alpha Township Public Schools"]["website"] == \
        "https://alpha-merge.k12.nj.us"
    assert by_name["Alpha Township Public Schools"]["website_source"] == "merge"
    # Beta: no website anywhere -> blank, counted.
    assert by_name["Beta School District"]["website"] == ""
    # Gamma: charter flag + website from wikidata.
    assert by_name["Gamma Charter School"]["charter"] == "Y"
    assert by_name["Gamma Charter School"]["website"] == "https://gamma-wiki.org"
    assert by_name["Gamma Charter School"]["website_source"] == "wikidata"
    assert stats["no_website"] == 1
    print("ok  website precedence merge>ccd>wikidata + type/closed drops")


def test_pagination_follow_and_parse_wikidata():
    # Simulate a 2-page CCD response via a stub fetcher.
    pages = {
        "URL1": {"results": [{"lea_name": "P1", "leaid": "1",
                              "agency_type": 1, "sy_status": 1}],
                 "next": "URL2"},
        "URL2": {"results": [{"lea_name": "P2", "leaid": "2",
                              "agency_type": 1, "sy_status": 1}],
                 "next": None},
    }
    seq = ["URL1"]

    def stub(url):
        # first call uses the formatted URL; we ignore it and drive by seq
        key = seq.pop(0)
        payload = pages[key]
        if payload.get("next"):
            seq.append(payload["next"])
        return payload

    recs = b.fetch_ccd_directory(2023, fetch=stub)
    assert [r["lea_name"] for r in recs] == ["P1", "P2"]

    wd = b.parse_wikidata_websites({"results": {"bindings": [
        {"itemLabel": {"value": "Newark Public Schools"},
         "countyLabel": {"value": "Essex County"},
         "website": {"value": "https://www.nps.k12.nj.us/"}},
        {"itemLabel": {"value": "No Site District"}},  # no website -> skip
    ]}})
    assert wd.get("Newark School District", "Essex") == \
        "https://www.nps.k12.nj.us/"
    assert len(wd) == 1
    print("ok  CCD pagination follow + Wikidata parse")


def test_county_disambiguates_same_named_districts():
    idx = b.SiteIndex()
    idx.add("Franklin Township School District", "Somerset",
            "https://somerset-franklin.k12.nj.us")
    idx.add("Franklin Township School District", "Gloucester",
            "https://gloucester-franklin.k12.nj.us")
    # Same name, different county -> each resolves correctly.
    assert idx.get("Franklin Township Public Schools", "Somerset") == \
        "https://somerset-franklin.k12.nj.us"
    assert idx.get("Franklin Township Public Schools", "Gloucester") == \
        "https://gloucester-franklin.k12.nj.us"
    # Ambiguous (unknown county) -> refuse to guess.
    assert idx.get("Franklin Township Public Schools", "") is None
    print("ok  county disambiguates same-named NJ districts")


def test_write_and_merge_roundtrip():
    ccd = [{"lea_name": "Delta Public Schools", "leaid": "3400010",
            "agency_type": 1, "sy_status": 1, "county_name": "Camden",
            "enrollment": 500}]
    with tempfile.TemporaryDirectory() as d:
        merge_csv = os.path.join(d, "override.csv")
        with open(merge_csv, "w", encoding="utf-8") as fh:
            fh.write("District Name,County,Website\n"
                     "Delta School District,Camden,delta.k12.nj.us\n")
        merge = b.load_merge_websites(merge_csv, "District Name", "Website",
                                      "County")
        rows, _ = b.assemble_rows(ccd, merge=merge)
        out = os.path.join(d, "seed.csv")
        b.write_seed(rows, out)
        text = open(out, encoding="utf-8").read()
    assert "Delta Public Schools" in text
    assert "https://delta.k12.nj.us" in text  # cleaned + merged
    print("ok  merge CSV load + write_seed roundtrip")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nall {len(tests)} seed-builder tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
