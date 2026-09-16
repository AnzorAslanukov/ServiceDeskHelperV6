"""
Site / Organization routing helpers (UPHS vs LGH).

Penn Medicine's service desk covers two distinct organizations whose support
groups are NOT interchangeable:

- **UPHS** — University of Pennsylvania Health System (HUP, PAH, PCAM, PPMC,
  CCH, Presbyterian, Penn Medicine at Home, PennChart, MyPennMedicine, etc.)
- **LGH** — Lancaster General Health (LGH, LGHP, Lancaster, MyLGHealth,
  Women & Babies, DOP/SOP, lha.org, etc.)

A common, costly error is recommending an LGH support group for a UPHS ticket
(or vice-versa). These helpers detect the organization ("site") of a ticket,
a free-text query, or a support-group name so callers can filter documentation,
re-rank classifier predictions, and warn the LLM against cross-site routing.

This module is intentionally dependency-free (pure string heuristics) so it can
be shared by Feature #2 (Q&A Chatbot) and Feature #3 (Assignment Recommendation)
without importing either service.
"""

from __future__ import annotations

# ── Site constants ────────────────────────────────────────────────────

SITE_UPHS = "UPHS"
SITE_LGH = "LGH"

# Notebook names as stored in scratchpad.aslanuka.onenote_documentation /
# data/vectors/onenote_metadata.json
NOTEBOOK_UPHS = "uphs_notebook"
NOTEBOOK_LGH = "lgh_notebook"


# ── Keyword signals ───────────────────────────────────────────────────
# Lower-cased substrings. Order/spacing chosen to avoid false positives:
# each entry is matched as a plain substring against lower-cased text.

# Strong LGH indicators. "lgh" as a standalone token is handled separately to
# avoid matching unrelated substrings.
_LGH_KEYWORDS: tuple[str, ...] = (
    "lgh",
    "lghp",
    "lha.org",
    "lgh.org",
    "lancaster general",
    "lancaster",
    "mylghealth",
    "women & babies",
    "women and babies",
    "penn medicine lancaster",
    "suburban outpatient",
    "downtown outpatient",
)

# Strong UPHS indicators.
_UPHS_KEYWORDS: tuple[str, ...] = (
    "uphs",
    "hup",
    "pah",
    "pcam",
    "ppmc",
    "cch",
    "chester county",
    "pennsylvania hospital",
    "penn presbyterian",
    "presbyterian",
    "perelman",
    "pennchart",
    "mypennmedicine",
    "penn medicine at home",
    "pmah",
    "pmuc",
    "rittenhouse",
    "abramson",
    "founders",
    "silverstein",
    "ravdin",
    "maloney",
    "rhoads",
    "gibson",
    "dulles",
    "spruce",
    "cathcart",
    "pmdh",
    "doylestown",
)


# ── Top-level campus → site map ───────────────────────────────────────
# Locations in Athena resolve to a hierarchical path whose FIRST segment is
# the top-level campus (e.g. 'PPMC\\MUTCH' -> 'PPMC'). Mapping that campus to
# a site is far more reliable than substring keyword matching, because it does
# not depend on a building/leaf name (e.g. 'MUTCH') happening to be a keyword.
#
# Derived from exploration/output/locations.json (19 top-level campuses).
# Ambiguous top segments that contain BOTH UPHS and LGH children (e.g.
# 'Community Connect', 'CAMPUS', 'Data Center', 'Remote sites (RSI)',
# 'Remote User') are intentionally OMITTED so they resolve to None (neutral)
# and fall back to keyword detection instead of forcing a wrong site.
_CAMPUS_SITE: dict[str, str] = {
    # UPHS campuses
    "cch": SITE_UPHS,
    "doylestown (pmdh)": SITE_UPHS,
    "hup": SITE_UPHS,
    "hup cedar": SITE_UPHS,
    "hup pavilion": SITE_UPHS,
    "pah": SITE_UPHS,
    "pcam": SITE_UPHS,
    "pmuc": SITE_UPHS,
    "pmah": SITE_UPHS,
    "ppmc": SITE_UPHS,
    "princeton (mcp)": SITE_UPHS,
    "ritt": SITE_UPHS,
    # LGH campuses
    "lgh": SITE_LGH,
    "lghp": SITE_LGH,
}


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in text for kw in keywords)


def site_for_location_path(location_path: str | None) -> str | None:
    """
    Determine the site (UPHS or LGH) from a resolved location path.

    Uses the TOP-LEVEL campus segment of a ``parent\\child`` path (e.g.
    'PPMC\\MUTCH' -> 'PPMC' -> UPHS), which is deterministic and does not
    depend on the leaf/building name being a keyword. Falls back to keyword
    detection over the whole path when the top segment is unknown/ambiguous.

    Returns SITE_UPHS, SITE_LGH, or None when the campus is site-neutral or no
    signal is present.
    """
    if not location_path:
        return None

    top_segment = location_path.split("\\")[0].strip().lower()
    site = _CAMPUS_SITE.get(top_segment)
    if site is not None:
        return site

    # Unknown/ambiguous top segment (e.g. 'CAMPUS', 'Community Connect', or a
    # bare street address) — fall back to keyword detection over the full path.
    return detect_site(location_path)


def detect_site(*text_fields: str | None) -> str | None:
    """
    Detect the organization/site (UPHS or LGH) from one or more text fields.

    Pass any combination of location, title, description, or a raw user query.
    Returns SITE_UPHS, SITE_LGH, or None when the text is ambiguous or contains
    no site signal (e.g., "Remote User").

    Detection rules:
    - Combine all provided fields into one lower-cased string.
    - Count LGH vs UPHS signals. The side with a signal wins.
    - If both sides have signals, the side with MORE distinct matches wins;
      a tie resolves to None (ambiguous — caller should not force a site).
    """
    combined = " ".join(f for f in text_fields if f).lower()
    if not combined.strip():
        return None

    lgh_hits = sum(1 for kw in _LGH_KEYWORDS if kw in combined)
    uphs_hits = sum(1 for kw in _UPHS_KEYWORDS if kw in combined)

    if lgh_hits == 0 and uphs_hits == 0:
        return None
    if lgh_hits > uphs_hits:
        return SITE_LGH
    if uphs_hits > lgh_hits:
        return SITE_UPHS
    # Tie with signals on both sides — genuinely ambiguous.
    return None


def notebook_for_site(site: str | None) -> str | None:
    """Map a detected site to its OneNote notebook name, or None if unknown."""
    if site == SITE_LGH:
        return NOTEBOOK_LGH
    if site == SITE_UPHS:
        return NOTEBOOK_UPHS
    return None


def group_site(group_name: str | None) -> str | None:
    """
    Infer the site of a support-group name/path.

    LGH support groups live under the top-level 'LGH' group (e.g.,
    'LGH\\Epic', 'LGH\\Technical Services') per the Athena support-group
    hierarchy. Any group whose path starts with 'LGH' (or contains explicit
    LGH markers) is LGH; otherwise, if it contains a UPHS marker it is UPHS.

    Returns SITE_LGH, SITE_UPHS, or None when the group is site-neutral
    (e.g., 'Service Desk', 'Applications', 'PennChart' shared queues) and no
    marker is present.
    """
    if not group_name:
        return None

    name = group_name.strip().lower()

    # Top-level 'LGH' or any 'LGH\...' path segment → LGH.
    if name == "lgh" or name.startswith("lgh\\") or "\\lgh\\" in name or name.endswith("\\lgh"):
        return SITE_LGH

    if _has_keyword(name, _LGH_KEYWORDS):
        return SITE_LGH
    if _has_keyword(name, _UPHS_KEYWORDS):
        return SITE_UPHS

    return None


def sites_conflict(site_a: str | None, site_b: str | None) -> bool:
    """
    Return True only when BOTH sites are known and they differ.

    Used to detect a cross-site routing error (e.g., an LGH ticket being
    recommended a UPHS support group). Unknown sites never conflict.
    """
    if site_a is None or site_b is None:
        return False
    return site_a != site_b
