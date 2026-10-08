"""
staffdir.blocks
===============

DOM-agnostic **repeating-block detector**.

Motivation
----------
The legacy `parse_card` in master_staff_directory_scraper works by
matching a fixed list of CSS class substrings (`staff|employee|
faculty|person|member|profile`). That catches many CMS templates but
misses:

  * Sites that use generic class names (`col-md-3`, `card`, `grid-item`).
  * Custom in-house templates with unique classes.
  * Rendered React output where the meaningful classes are hashed.

A person directory is fundamentally *many sibling elements with
similar DOM shape*. This module finds those clusters purely from
structure — no class-name assumptions. It returns candidate regions
that the field extractors can then run on.

Algorithm
---------
1. Walk every parent element with >= 3 direct children.
2. For each parent, compute a "shape signature" for every child:
   tag path (last 3 levels) + presence of `<a>`, `<img>`, `<h*>`, `<p>`,
   plus text-length bucket.
3. Group siblings by signature. If a group has >= `min_repeats` items
   AND each item contains at least one of {mailto, tel, heading, image},
   it's a candidate.
4. Score each candidate cluster by size × per-item person-likeness
   (has_name_shape + has_email/phone). Return top-N clusters.

The person-likeness threshold is deliberately loose because we let the
field extractors decide later; here we only want to find *structure*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:
    from bs4 import Tag                    # type: ignore
except Exception:                           # pragma: no cover
    Tag = None


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def _tag_path(elem, depth: int = 2) -> str:
    """Rough shape signature — the last `depth` tag names ancestors +
    self, joined by '/'. Two elements with the same tag path are the
    same *kind* of node in the DOM."""
    parts = []
    cur = elem
    for _ in range(depth + 1):
        if cur is None or not hasattr(cur, "name"): break
        parts.append(cur.name or "?")
        cur = cur.parent
    return "/".join(reversed(parts))


# Coarse text-length bucketing — deliberately wide so that person cards
# that differ only in a few characters (say, 37 vs 52) still land in the
# same bucket. Two thresholds is enough: nav-like elements are tiny,
# person cards are anywhere from a few dozen to a few hundred chars,
# article-like elements are longer.
_TEXT_LEN_BUCKETS = [(20, "tiny"), (600, "card"), (10**9, "long")]

def _text_len_bucket(txt: str) -> str:
    n = len(txt)
    for lim, name in _TEXT_LEN_BUCKETS:
        if n < lim: return name
    return "long"


def _shape_signature(elem) -> str:
    """Signature = tag path + which "person-card-ish" descendants it
    has + text-length bucket. Two elements with the same signature are
    structurally almost identical."""
    if not hasattr(elem, "get_text"): return ""
    has_a       = elem.find("a") is not None
    has_mailto  = elem.find("a", href=re.compile(r"^mailto:", re.I)) is not None
    has_tel     = elem.find("a", href=re.compile(r"^tel:", re.I))    is not None
    has_img     = elem.find("img") is not None
    has_heading = any(elem.find(t) is not None for t in ("h1","h2","h3","h4","h5","h6"))
    has_para    = elem.find("p") is not None
    bucket      = _text_len_bucket(elem.get_text(" ") or "")
    tp          = _tag_path(elem, depth=2)
    flags = "".join(("A" if has_a else ".",
                     "M" if has_mailto else ".",
                     "T" if has_tel else ".",
                     "I" if has_img else ".",
                     "H" if has_heading else ".",
                     "P" if has_para else "."))
    return f"{tp}|{flags}|{bucket}"


# ---------------------------------------------------------------------------
# Person-likeness (loose)
# ---------------------------------------------------------------------------

_NAME_SHAPE_RE = re.compile(
    r"\b[A-Z][a-z'\-]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z'\-]+\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")


def _person_likeness(elem) -> float:
    """0..1 score for one candidate element. Rough — used only to
    filter obvious non-people (footer link groups, etc.)."""
    if not hasattr(elem, "get_text"): return 0.0
    txt = elem.get_text(" ", strip=True)
    if not txt or len(txt) > 4000: return 0.0
    score = 0.0
    if _NAME_SHAPE_RE.search(txt):                score += 0.4
    if _EMAIL_RE.search(txt):                     score += 0.3
    if _PHONE_RE.search(txt):                     score += 0.2
    if elem.find("a", href=re.compile(r"^mailto:", re.I)): score += 0.2
    if elem.find("a", href=re.compile(r"^tel:", re.I)):    score += 0.1
    if elem.find("img") is not None:              score += 0.1
    if any(elem.find(t) for t in ("h1","h2","h3","h4","h5","h6")): score += 0.15
    # Punish elements that look like nav (many external links, tiny text).
    a_count = len(elem.find_all("a"))
    if a_count > 8 and len(txt) < 300:            score -= 0.4
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class BlockCluster:
    signature: str
    parent_path: str
    elements: List[object]
    score: float           # sum of per-item person-likeness
    avg_person_likeness: float

    def __len__(self) -> int: return len(self.elements)


def find_repeating_blocks(soup, *, min_repeats: int = 3,
                          min_person_likeness: float = 0.35,
                          top_n: int = 3) -> List[BlockCluster]:
    """Return up to `top_n` clusters of sibling elements that look like
    repeated person cards.

    Parameters
    ----------
    min_repeats: minimum number of sibling elements needed to form a
        cluster (a 2-person page can be handled by other strategies).
    min_person_likeness: mean per-item likeness required to accept the
        cluster — filters out obvious non-people (link grids, calendar
        cells).
    top_n: return at most this many clusters, largest first.
    """
    if soup is None or Tag is None: return []

    candidates: List[BlockCluster] = []
    seen_parents = set()

    for parent in soup.find_all(True):
        if id(parent) in seen_parents: continue
        seen_parents.add(id(parent))
        children = [c for c in parent.find_all(recursive=False)
                    if getattr(c, "name", None)]
        if len(children) < min_repeats:
            continue

        # Group direct children by shape signature.
        groups: dict = {}
        for c in children:
            sig = _shape_signature(c)
            groups.setdefault(sig, []).append(c)

        for sig, items in groups.items():
            if len(items) < min_repeats: continue
            # Cheap filter: must have *something* person-y.
            flags = sig.split("|", 2)[1] if "|" in sig else ""
            if not any(f in flags for f in ("M", "T", "H", "I")):
                continue
            likenesses = [_person_likeness(el) for el in items]
            avg = sum(likenesses) / len(likenesses)
            if avg < min_person_likeness:
                continue
            candidates.append(BlockCluster(
                signature=sig,
                parent_path=_tag_path(parent, depth=3),
                elements=items,
                score=sum(likenesses) * (len(items) ** 0.5),  # size boost
                avg_person_likeness=avg,
            ))

    candidates.sort(key=lambda c: -c.score)

    # Drop nested clusters — if cluster B's elements are all descendants
    # of cluster A's parent, keep A (usually the outer one is the real
    # directory; the inner is repeated sub-elements per card).
    kept: List[BlockCluster] = []
    for c in candidates:
        keep = True
        for k in kept:
            if _is_nested_within(c, k):
                keep = False; break
        if keep:
            kept.append(c)
        if len(kept) >= top_n:
            break
    return kept


def _is_nested_within(inner: BlockCluster, outer: BlockCluster) -> bool:
    """Every element of `inner` is a descendant of some element of `outer`?"""
    for iel in inner.elements[:3]:      # check a sample
        found = False
        for oel in outer.elements:
            try:
                if iel in oel.descendants:
                    found = True; break
            except Exception:
                pass
        if not found: return False
    return True
