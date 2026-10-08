"""Deterministic lead classification, scoring, and deduplication."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Iterable, List, Mapping


_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> str:
    return " ".join(_WORD_RE.findall((title or "").lower()))


def classify_lead(title: str | None) -> str:
    """Return the stable lead class used by the production CSV."""
    normalized = normalize_title(title or "")
    if not normalized:
        return "UNKNOWN"
    if any(term in normalized for term in (
            "superintendent", "district administrator",
            "assistant superintendent", "deputy superintendent",
            "district director", "district coordinator", "central office")):
        return "DISTRICT_ADMIN"
    if any(term in normalized for term in (
            "principal", "assistant principal", "associate principal",
            "director", "executive director", "head of school")):
        return "HIGH_VALUE"
    if any(term in normalized for term in (
            "chief", "administrator", "dean", "president",
            "decision maker", "headmaster")):
        return "DECISION_MAKER"
    if any(term in normalized for term in (
            "teacher", "educator", "instructor", "professor",
            "classroom", "special education", "interventionist",
            "librarian", "counselor")):
        return "TEACHING_STAFF"
    if any(term in normalized for term in (
            "aide", "assistant", "secretary", "administrative assistant",
            "nurse", "custodian", "paraprofessional", "bus driver",
            "technician")):
        return "SUPPORT_STAFF"
    return "OTHER_STAFF"


def score_lead(row: Mapping[str, object], *,
               confirmed_directory: bool = True) -> int:
    """Score an already validated row without using score as a gate."""
    score = 30  # every row entering this function has a validated person
    if row.get("title"):
        score += 15
    if row.get("organization_level") == "SCHOOL" and row.get("school_name"):
        score += 15
    elif row.get("organization_level") == "DISTRICT" and row.get("district_name"):
        score += 15
    if row.get("email"):
        score += 20
    if row.get("phone"):
        score += 5
    if row.get("profile_url"):
        score += 5
    if row.get("lead_class") in ("HIGH_VALUE", "DECISION_MAKER",
                                 "DISTRICT_ADMIN"):
        score += 10
    if confirmed_directory:
        score += 5
    elif row.get("source_type") == "staff_profile":
        score += 3
    return min(100, score)


def priority_for_score(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    return "C"


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def deduplicate_leads(rows: Iterable[dict]) -> List[dict]:
    """Merge duplicate observations while respecting organizational scope.

    A person listed as a district administrator and the same-named person
    listed as a school principal are retained as separate organizational
    records, even if an email or profile URL happens to match.
    """
    grouped: dict[tuple[str, str, str], dict] = {}
    for incoming in rows:
        row = dict(incoming)
        scope = (
            _norm(row.get("organization_level") or "UNKNOWN"),
            _norm(row.get("school_name")),
        )
        name_key = _norm(row.get("staff_name") or
                         f"{row.get('first_name', '')} {row.get('last_name', '')}")
        context_key = (name_key, scope[0], scope[1])
        existing = grouped.get(context_key)
        if existing is None:
            grouped[context_key] = row
            continue
        for key, value in row.items():
            if value and not existing.get(key):
                existing[key] = value
        if float(row.get("confidence") or 0) > float(
                existing.get("confidence") or 0):
            existing["confidence"] = row["confidence"]
        if int(row.get("lead_score") or 0) > int(
                existing.get("lead_score") or 0):
            existing["lead_score"] = row["lead_score"]
    output = list(grouped.values())
    for row in output:
        row["lead_priority"] = priority_for_score(
            int(row.get("lead_score") or 0))
    return output