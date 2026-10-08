"""
staffdir.cms_paths
==================

Canonical staff-directory URL paths for known CMS platforms. When we
detect a CMS on the homepage, we try these paths first — a direct
probe of `/apps/pages/staff` beats crawling the nav.

Populated from patterns observed across the states in the project
README (Texas, Ohio, Michigan, Arizona, Maryland). Add to the lists
as new patterns show up in real crawls.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# CMS name (lowercase) -> list of relative paths to probe, in priority order.
# Priority = confidence this path IS the directory (not just "leads to" it).
CMS_CANONICAL_PATHS: Dict[str, List[str]] = {
    "finalsite": [
        "/apps/pages/staff",
        "/apps/pages/StaffDirectory",
        "/apps/pages/faculty",
        "/staff",
        "/our-staff",
        "/our-team",
        "/directory",
        "/staff-directory",
        "/faculty-staff",
        "/about-us/staff",
    ],
    "blackboard": [
        "/site/Default.aspx?PageType=1",  # generic staff listing
        "/domain/staff",
        "/Page/staff-directory",
        "/Page/StaffDirectory",
        "/staff",
        "/staff-directory",
    ],
    "edlio": [
        "/apps/pages/staff",
        "/staff",
        "/faculty-staff",
        "/our-staff",
        "/staff-directory",
    ],
    "apptegy": [
        "/o/district/browse/staff",
        "/staff",
        "/staff-directory",
        "/our-staff",
    ],
    "schoolblocks": [
        "/o/staff",
        "/staff",
        "/staff-directory",
    ],
    "gabbart": [
        "/vnews/display.v/SEC/Staff",
        "/staff",
        "/staff-directory",
    ],
    "wordpress": [
        "/staff/",
        "/staff-directory/",
        "/faculty/",
        "/faculty-staff/",
        "/our-team/",
        "/about/staff/",
    ],
    "squarespace": [
        "/staff",
        "/faculty",
        "/team",
        "/our-team",
    ],
    "parentsquare": [],   # known dead-end for email — don't probe
    "unknown": [
        "/staff",
        "/staff-directory",
        "/staff-directory/",
        "/our-staff",
        "/faculty",
        "/faculty-staff",
        "/our-team",
        "/directory",
        "/our-district/staff",
        "/about/staff",
        "/about-us/staff",
        "/personnel",
    ],
}


# Common link-text hints (lowercased) that a homepage anchor points to
# the staff directory. Priority order matters — more specific first.
STAFF_LINK_KEYWORDS: List[Tuple[str, float]] = [
    ("staff directory",   1.0),
    ("faculty directory", 1.0),
    ("staff & faculty",   0.95),
    ("faculty & staff",   0.95),
    ("faculty and staff", 0.95),
    ("staff and faculty", 0.95),
    ("our staff",         0.9),
    ("our faculty",       0.9),
    ("our team",          0.85),
    ("meet the staff",    0.9),
    ("meet our staff",    0.9),
    ("meet the team",     0.85),
    ("meet our team",     0.85),
    ("staff",             0.7),
    ("faculty",           0.7),
    ("personnel",         0.75),
    ("directory",         0.6),
    ("employees",         0.65),
    ("administration",    0.55),
    ("leadership",        0.55),
    ("who we are",        0.5),
    ("our people",        0.75),
]

# URL-path tokens that boost a candidate's confidence.
STAFF_URL_TOKENS: List[Tuple[str, float]] = [
    ("staff-directory",   1.0),
    ("staff_directory",   1.0),
    ("faculty-directory", 1.0),
    ("faculty-staff",     0.9),
    ("our-staff",         0.9),
    ("our-team",          0.85),
    ("staffdirectory",    1.0),
    ("staff",             0.7),
    ("faculty",           0.7),
    ("personnel",         0.75),
    ("employees",         0.65),
    ("directory",         0.55),
    ("administration",    0.5),
    ("leadership",        0.5),
    ("meet-the-team",     0.9),
    ("meet-our-team",     0.9),
]
