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


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in text for kw in keywords)


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
