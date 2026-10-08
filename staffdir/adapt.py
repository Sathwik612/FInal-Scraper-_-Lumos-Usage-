"""
staffdir.adapt
==============

Bridge between the new `Person` dataclass and the legacy `Employee`
dataclass in master_staff_directory_scraper.

Why: your existing exporters, dedupe, `classify_lead()`, and CSV
schemas all speak `Employee`. Rather than rewriting those, this
module converts a list of `Person` objects into a list of legacy
`Employee` objects, so:

    from staffdir.directory import extract_staff_directory
    from staffdir.adapt     import persons_to_employees

    persons, report = extract_staff_directory(html, url)
    employees = persons_to_employees(persons, district="Foo ISD",
                                     school="Lincoln HS",
                                     directory_url=url,
                                     school_url="https://lincolnhs.example")

    # Feed straight into the existing pipeline:
    from master_staff_directory_scraper import export_results, deduplicate
    export_results(deduplicate(employees), schools=[...], out_dir="out")

Nothing is imported eagerly — legacy imports are attempted lazily so
this module is safe to import in environments where the legacy scraper
is not present.
"""

from __future__ import annotations

from typing import List, Optional

from .schema import Person


def persons_to_employees(persons: List[Person], *,
                         district: str = "",
                         school: str = "",
                         school_url: str = "",
                         directory_url: str = "",
                         min_confidence: float = 0.0):
    """Convert Persons -> legacy Employee[]. Skips records with
    overall_confidence below `min_confidence` (0 disables the filter).
    Uses `infer_role` and `classify_lead` from the legacy scraper if
    available, so lead classification is unchanged."""
    try:
        from master_staff_directory_scraper import (       # type: ignore
            Employee, EmployeeType, classify_lead, infer_role,
        )
    except Exception as exc:
        raise ImportError(
            "master_staff_directory_scraper is required for persons_to_employees; "
            f"import failed: {exc}")

    employees = []
    for p in persons:
        if p.overall_confidence < min_confidence: continue
        name = p.name.value if p.name else ""
        title = p.title.value if p.title else ""
        email = p.email.value if p.email else None
        phone = p.phone.value if p.phone else None
        dept  = p.department.value if p.department else None
        role, dept_inferred, emp_type = infer_role(
            name, title, email, directory_url, school)
        emp = Employee(
            district=district,
            school=school,
            name=name,
            title=title or role,
            first_name=p.first_name.value if p.first_name else None,
            last_name=p.last_name.value if p.last_name else None,
            email=email,
            phone=phone,
            extension=p.extension.value if p.extension else None,
            department=dept or dept_inferred,
            room=p.room.value if p.room else None,
            photo_url=p.photo_url.value if p.photo_url else None,
            profile_url=p.profile_url.value if p.profile_url else None,
            school_url=school_url,
            directory_url=directory_url,
            employee_type=emp_type,
            lead_type=classify_lead(title or role, role, emp_type,
                                     district, school),
        )
        employees.append(emp)
    return employees


def employee_to_person(emp) -> Person:
    """Reverse conversion, useful if you're feeding legacy rows through
    the new merge / dedupe stages."""
    from .schema import Confidence, Field, FieldSource
    def f(val: Optional[str], src=FieldSource.UNKNOWN,
          conf=Confidence.HIGH.value):
        return Field(val, conf, src) if val else None
    p = Person(
        name=f(emp.name),
        first_name=f(emp.first_name),
        last_name=f(emp.last_name),
        email=f(emp.email, FieldSource.MAILTO, Confidence.CERTAIN.value),
        phone=f(emp.phone, FieldSource.TEL_LINK, Confidence.HIGH.value),
        extension=f(emp.extension),
        title=f(emp.title),
        department=f(emp.department),
        room=f(emp.room),
        photo_url=f(emp.photo_url),
        profile_url=f(emp.profile_url),
        biography=f(emp.biography),
        school=emp.school,
        district=emp.district,
        source_url=emp.directory_url,
    )
    p.compute_overall_confidence()
    return p
