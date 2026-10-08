"""
staffdir — cleaner extraction + classification layer for the
Universal Staff Directory Scraper.

This package is *additive*: it does not modify or replace any of the
existing modules (master_staff_directory_scraper, finalsite_scraper_async,
profile_district_directories, etc.). It provides a new, small set of
primitives you can call directly, and an adapter (`staffdir.adapt`)
that converts new-style Person records into the legacy Employee
dataclass so downstream export/dedupe stays unchanged.

Public API
----------
    from staffdir import (
        # Classification
        classify_page, PageType, PageClassification,

        # Extraction
        extract_staff_directory, extract_staff_profile,

        # Field-level (build custom pipelines)
        extract_emails, extract_phones, extract_name,
        extract_title, extract_department,

        # Data types
        Person, Field, Confidence, FieldSource, ExtractionReport,

        # Optional caching
        HttpCache, CachedFetcher,

        # Bridge to legacy pipeline
        persons_to_employees, employee_to_person,
    )
"""

from .classify   import classify_page
from .directory  import extract_staff_directory
from .discover   import (discover_staff_directories,
                          DirectoryCandidate, DiscoveryReport)
from .schools    import discover_schools, SchoolLink, SchoolDiscoveryReport
from .pipeline   import (run_pipeline, run_district_pipeline,
                          default_fetcher)
from .profile    import extract_staff_profile, enrich_with_profile
from .playwright_fallback import (
    extract_with_playwright, is_playwright_available,
)
from .fields     import (
    extract_emails, extract_phones, extract_name,
    extract_title, extract_department, extract_photo,
    extract_profile_url, normalize_name, normalize_phone,
)
from .schema     import (
    Person, Field, Confidence, FieldSource,
    PageType, PageClassification, ExtractionReport,
)
from .cache      import HttpCache, CachedFetcher
from .adapt      import persons_to_employees, employee_to_person
from .leads      import (
    classify_lead, deduplicate_leads, normalize_title,
    priority_for_score, score_lead,
)

__all__ = [
    "classify_page",
    "extract_staff_directory", "extract_staff_profile", "enrich_with_profile",
    "extract_with_playwright", "is_playwright_available",
    "discover_staff_directories", "DirectoryCandidate", "DiscoveryReport",
    "discover_schools", "SchoolLink", "SchoolDiscoveryReport",
    "run_pipeline", "run_district_pipeline", "default_fetcher",
    "extract_emails", "extract_phones", "extract_name",
    "extract_title", "extract_department", "extract_photo",
    "extract_profile_url", "normalize_name", "normalize_phone",
    "Person", "Field", "Confidence", "FieldSource",
    "PageType", "PageClassification", "ExtractionReport",
    "HttpCache", "CachedFetcher",
    "persons_to_employees", "employee_to_person",
    "classify_lead", "deduplicate_leads", "normalize_title",
    "priority_for_score", "score_lead",
]

__version__ = "0.1.0"
