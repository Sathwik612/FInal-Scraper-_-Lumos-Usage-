"""
staffdir.schema
===============

Value objects for the new extraction pipeline. The design goal is
*debuggability*: every field the scraper produces carries its
confidence and the source it came from, so bad data can be traced
to the specific rule that produced it.

Nothing in this module touches HTTP or HTML — it's pure data types.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Confidence + source enums
# ---------------------------------------------------------------------------

class Confidence(float, Enum):
    """Coarse-grained confidence bands. Use as floats — comparison
    against thresholds just works. Kept coarse on purpose: fine-grained
    calibration on 50 disparate CMS templates is a fool's errand."""
    CERTAIN = 0.99          # schema.org itemprop=email, mailto: href
    HIGH    = 0.85          # matched a strong pattern (regex + context)
    MEDIUM  = 0.60          # heuristic (heading near a mailto, etc.)
    LOW     = 0.30          # guessed from URL or generic fallback
    NONE    = 0.0


class FieldSource(str, Enum):
    """Where a field's value came from. Useful when the same person
    is seen on both a list page and a profile page — the merge layer
    prefers profile-page values because they're usually canonical."""
    MICRODATA     = "microdata"        # itemprop=email etc.
    JSON_LD       = "json_ld"          # @type: Person block
    JSON_EMBEDDED = "json_embedded"    # __NEXT_DATA__, inline arrays
    MAILTO        = "mailto"           # <a href="mailto:...">
    CLOUDFLARE    = "cloudflare"       # data-cfemail decoded
    DATA_ATTR     = "data_attr"        # data-email / data-mail
    OBFUSCATED    = "obfuscated"       # "name at domain dot com"
    SR_ONLY       = "sr_only"          # Finalsite fsStyleSROnly span
    HEADING       = "heading"          # h1..h6 inside a card
    TABLE_CELL    = "table_cell"       # <td>
    CARD_TEXT     = "card_text"        # generic .text-of-card
    TEL_LINK      = "tel_link"         # <a href="tel:...">
    URL_TOKEN     = "url_token"        # inferred from URL words
    HCARD         = "hcard"            # .vcard .fn / .email
    CONTACT_LIST_PRO = "contact_list_pro"  # WordPress Contact List Pro markup
    FINALSITE_CONSTITUENT = "finalsite_constituent"
    UNKNOWN       = "unknown"


# ---------------------------------------------------------------------------
# Field wrapper
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """A single extracted value plus provenance.

    Two Fields for the same attribute (say, two candidate emails from
    different strategies) can be reconciled by `Field.better_of(a, b)`
    which picks the higher-confidence one, tie-broken by source
    priority.
    """
    value: str
    confidence: float = Confidence.MEDIUM.value
    source: FieldSource = FieldSource.UNKNOWN

    def __bool__(self) -> bool:
        return bool(self.value and self.confidence > 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value,
                "confidence": round(float(self.confidence), 3),
                "source": self.source.value}

    @staticmethod
    def better_of(a: Optional["Field"], b: Optional["Field"]) -> Optional["Field"]:
        if not a: return b
        if not b: return a
        if b.confidence > a.confidence: return b
        if a.confidence > b.confidence: return a
        # Tie: prefer canonical sources.
        priority = {
            FieldSource.MICRODATA: 5, FieldSource.JSON_LD: 5,
            FieldSource.MAILTO: 4, FieldSource.CLOUDFLARE: 4,
            FieldSource.HCARD: 4, FieldSource.TEL_LINK: 4,
            FieldSource.JSON_EMBEDDED: 3, FieldSource.DATA_ATTR: 3,
            FieldSource.CONTACT_LIST_PRO: 3,
            FieldSource.FINALSITE_CONSTITUENT: 3,
            FieldSource.OBFUSCATED: 2, FieldSource.SR_ONLY: 2,
            FieldSource.HEADING: 2, FieldSource.TABLE_CELL: 2,
            FieldSource.CARD_TEXT: 1, FieldSource.URL_TOKEN: 1,
            FieldSource.UNKNOWN: 0,
        }
        return b if priority.get(b.source, 0) > priority.get(a.source, 0) else a


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

class PageType(str, Enum):
    """What *kind* of page are we looking at? Directly consumable
    downstream to decide which extractor to call."""
    HOMEPAGE         = "homepage"          # district / school landing
    STAFF_DIRECTORY  = "staff_directory"   # list of many people
    STAFF_PROFILE    = "staff_profile"     # single person page
    DEPARTMENT_INDEX = "department_index"  # links to per-dept directories
    CONTACT          = "contact"           # generic contact form / info
    NEWS             = "news"              # article / press release
    CALENDAR         = "calendar"          # calendar / events
    LOGIN            = "login"             # portal login (dead end)
    ERROR            = "error"             # 404 / soft-404 / blocked
    OTHER            = "other"


@dataclass
class PageClassification:
    """Verdict of the page classifier, with debug info attached."""
    page_type: PageType = PageType.OTHER
    confidence: float = 0.0
    # Human-readable "why". Every rule that fired appends a line here.
    reasons: List[str] = field(default_factory=list)
    # Structural counts the classifier consulted — kept so callers can
    # sanity-check the decision without re-parsing.
    signals: Dict[str, Any] = field(default_factory=dict)

    def add_reason(self, weight: float, why: str) -> None:
        self.confidence = min(1.0, self.confidence + weight)
        self.reasons.append(f"[+{weight:.2f}] {why}")

    def to_dict(self) -> Dict[str, Any]:
        return {"page_type": self.page_type.value,
                "confidence": round(self.confidence, 3),
                "reasons": self.reasons,
                "signals": self.signals}


# ---------------------------------------------------------------------------
# Person + extraction report
# ---------------------------------------------------------------------------

@dataclass
class Person:
    """A single staff member, assembled by the directory/profile
    extractors. Every scalar attribute is a Field so provenance is
    preserved. Convert to the legacy `Employee` via `staffdir.adapt`."""
    # Core identity
    name:        Optional[Field] = None      # full name as printed
    first_name:  Optional[Field] = None
    last_name:   Optional[Field] = None
    # Contact
    email:       Optional[Field] = None
    phone:       Optional[Field] = None
    extension:   Optional[Field] = None
    # Role
    title:       Optional[Field] = None      # e.g. "5th Grade Teacher"
    department:  Optional[Field] = None
    subjects:    List[str] = field(default_factory=list)
    grade:       Optional[Field] = None
    room:        Optional[Field] = None
    # Media / links
    photo_url:   Optional[Field] = None
    profile_url: Optional[Field] = None      # link to their own page
    biography:   Optional[Field] = None
    contact_id:  Optional[Field] = None
    contact_name: Optional[Field] = None
    # Context (set by caller / extractor)
    school:      Optional[str] = None
    district:    Optional[str] = None
    source_url:  Optional[str] = None        # page this record came from
    # Overall quality score derived from the fields present.
    overall_confidence: float = 0.0

    # ---- helpers --------------------------------------------------
    def is_usable(self, min_confidence: float = Confidence.MEDIUM.value
                  ) -> bool:
        """True iff this record has a name + at least one contact
        field at or above the confidence threshold."""
        if not self.name or self.name.confidence < min_confidence:
            return False
        return any(f and f.confidence >= min_confidence
                   for f in (self.email, self.phone, self.profile_url))

    def compute_overall_confidence(self) -> float:
        """Weighted mean of the fields actually present. Weights favor
        contact fields because they are what the pipeline is for."""
        weights = {
            "name": 2.0, "email": 3.0, "phone": 2.0,
            "title": 1.0, "department": 0.5, "profile_url": 1.0,
        }
        num = den = 0.0
        for attr, w in weights.items():
            f = getattr(self, attr)
            if f:
                num += f.confidence * w
                den += w
        self.overall_confidence = round(num / den, 3) if den else 0.0
        return self.overall_confidence

    def to_dict(self, verbose: bool = False) -> Dict[str, Any]:
        """`verbose=True` includes per-field confidence + source dicts.
        `verbose=False` (default) returns flat scalar values suitable
        for CSV export."""
        scalar_fields = ("name", "first_name", "last_name", "email",
                         "phone", "extension", "title", "department",
                         "grade", "room", "photo_url", "profile_url",
                         "biography", "contact_id", "contact_name")
        out: Dict[str, Any] = {}
        for attr in scalar_fields:
            f = getattr(self, attr)
            if verbose:
                out[attr] = f.to_dict() if f else None
            else:
                out[attr] = f.value if f else None
        out["subjects"] = "; ".join(self.subjects) if self.subjects else None
        out["school"] = self.school
        out["district"] = self.district
        out["source_url"] = self.source_url
        out["overall_confidence"] = self.compute_overall_confidence()
        return out


@dataclass
class ExtractionReport:
    """Diagnostic bundle returned alongside every extraction so a run
    can be audited: what strategy fired, how many candidates each
    stage produced, why some were dropped."""
    url: str = ""
    page_classification: Optional[PageClassification] = None
    strategy_used: str = ""
    strategies_tried: List[str] = field(default_factory=list)
    candidates_seen: int = 0
    persons_returned: int = 0
    dropped: Dict[str, int] = field(default_factory=dict)     # reason -> count
    warnings: List[str] = field(default_factory=list)
    strategy_results: Dict[str, str] = field(default_factory=dict)
    candidates_by_strategy: Dict[str, int] = field(default_factory=dict)
    validated_by_strategy: Dict[str, int] = field(default_factory=dict)
    recovered_by_strategy: Dict[str, int] = field(default_factory=dict)
    merged_records: int = 0
    emails_found: int = 0
    phones_found: int = 0
    pagination_pages: int = 1
    pagination_urls: List[str] = field(default_factory=list)
    js_rendered: bool = False
    js_hydration_attempted: bool = False
    js_hydration_requests: int = 0
    js_hydration_succeeded: int = 0
    js_hydration_failed: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.page_classification is not None:
            d["page_classification"] = self.page_classification.to_dict()
        return d
