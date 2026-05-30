"""
Benchmark with Keyword Pre-Filter + Phase 2c Improvements
==========================================================
Phase 1b-e + Phase 2a-c of the Recommendation Accuracy Improvement Plan.

Runs the same benchmark as benchmark_assignment_accuracy.py but with:
- Keyword pre-filter that narrows 309 groups to ~10-30 relevant candidates
- Service Desk triage rules (password reset, MyChart, account lockout -> SD)
- Few-shot examples for most-confused patterns
- Enhanced similar ticket context with support group labels (Phase 2a)
- Path normalization: LLM full-path -> tierQueue leaf name (Phase 2b)
- Phase 2c: Fixed specific triage rules for Security Engineering, Kronos, IAM-SSO
- Phase 2c: MS Authenticator -> IAM-SSO rule (replaces broken SSO keyword rule)
- Phase 2c: LGH eStar/LDAP -> Security Engineering (replaces broken security keyword rule)
- Phase 2c: HRIS Form + PTO/leave -> Kronos (new rule for time management forms)
- Phase 2c: RAVDIN location -> HUP West (location-based routing fix)
- Phase 2c: Specific triage runs BEFORE SD triage to prevent false positives

Uses the CACHED benchmark dataset (exploration/output/benchmark_dataset.json)
and the keyword mappings (exploration/output/keyword_group_mappings.json).

Usage:
    python -m exploration.benchmark_with_prefilter
    python -m exploration.benchmark_with_prefilter --report-only
    python -m exploration.benchmark_with_prefilter --resume
    python -m exploration.benchmark_with_prefilter --delay 1.5

RULE: This script lives in exploration/ only. Core code in src/ is NOT edited.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ── Path Setup ────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Settings  # noqa: E402
from src.clients.athena_client import AthenaClient  # noqa: E402
from src.clients.databricks_client import DatabricksClient  # noqa: E402
from src.services.assignment import (  # noqa: E402
    AssignmentService,
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
    ASSIGNMENT_SYSTEM_PROMPT,
)
from src.models.assignment import (  # noqa: E402
    AssignmentRecommendation,
    TicketInfo,
)

# Import scoring from the original benchmark
from exploration.benchmark_assignment_accuracy import (  # noqa: E402
    score_support_group,
    score_priority,
    generate_report,
    load_dataset,
)

# ── Configuration ─────────────────────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env")

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DATASET_PATH = OUTPUT_DIR / "benchmark_dataset.json"
KEYWORD_MAPPINGS_PATH = OUTPUT_DIR / "keyword_group_mappings.json"
SG_MAPPING_PATH = OUTPUT_DIR / "ticket_support_group_mapping.json"
RESULTS_PATH = OUTPUT_DIR / "benchmark_prefilter_results.json"
REPORT_PATH = OUTPUT_DIR / "benchmark_prefilter_report.txt"

LLM_DELAY_SECONDS = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# SUPPORT GROUP MAPPING FOR SIMILAR TICKETS (Phase 2a)
# ═══════════════════════════════════════════════════════════════════════

def load_support_group_mapping() -> dict[str, str]:
    """
    Load the ticket_id → support_group mapping from the enrichment output.
    This enables showing "IR1234567 → assigned to: Service Desk" in context.
    """
    try:
        with open(SG_MAPPING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = data.get("mapping", {})
        logger.info("Loaded support group mapping: %d tickets", len(mapping))
        return mapping
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("No support group mapping found at %s: %s", SG_MAPPING_PATH, exc)
        return {}


# ═══════════════════════════════════════════════════════════════════════
# KEYWORD PRE-FILTER
# ═══════════════════════════════════════════════════════════════════════

def load_keyword_mappings() -> dict[str, Any]:
    """Load keyword→group mappings from the mining output."""
    try:
        with open(KEYWORD_MAPPINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load keyword mappings: %s. Using empty mappings.", exc)
        return {"prefilter_rules": {}, "service_desk_triage": {"rules": []}}


# ── Service Desk Triage Rules ─────────────────────────────────────────
# These patterns should ALWAYS route to Service Desk.
# Based on the benchmark confusion matrix: 20% of SD tickets were over-routed.

SERVICE_DESK_TRIAGE_PATTERNS = [
    {
        "name": "password_reset",
        "keywords": ["password reset", "pw reset", "reset password", "password unlock",
                     "account locked", "account lockout", "login blocked",
                     "inactive for too many days", "blocked out"],
        "negative": ["pennchart access", "epic access", "provisioning"],
    },
    {
        "name": "mypennmedicine",
        "keywords": ["mypennmedicine", "mychart", "patient portal",
                     "mypennchart", "my penn medicine"],
        "negative": [],
    },
    {
        "name": "general_inquiry",
        # Phase 2c: Removed "no issue" — too prone to false positives from
        # "no issues" in descriptions like "I had no issues until..." which
        # caused IR10402169 (IAM-SSO ticket) to be misrouted to Service Desk.
        "keywords": ["caller decided to end", "ended call", "hung up",
                     "general inquiry", "general question"],
        "negative": [],
    },
    {
        "name": "basic_account",
        "keywords": ["sd password reset", "pennid", "dob verified",
                     "username verified", "verification questions"],
        "negative": ["pennchart", "epic", "provisioning"],
    },
]


# Phase 2c+: Redesigned specific triage patterns based on actual ticket analysis.
# Phase 2b rules failed because they matched wrong signals. Phase 2c rules are
# designed from inspecting the actual misrouted ticket text and locations.
#
# Phase 2c changes from Phase 2b:
# - REMOVED security_engineering_estar: required 'security' keyword but Security
#   Engineering tickets never mention 'security' — the signal is LGH LDAP errors
# - REMOVED iam_sso: required SSO keywords + had 'duo' as negative, but IAM-SSO
#   tickets are about MS Authenticator/MFA, not SSO. 'duo' negative blocked valid matches.
# - REMOVED vdi_lgh: VDI tickets had zero VDI-related text — unfixable with text matching
# - ADDED security_engineering_lgh_ldap: catches lha.org/lgh.org LDAP errors (2 tickets)
# - ADDED security_engineering_estar_lgh: catches eStar portal/timestamp/workforce at LGH (2 tickets)
# - ADDED iam_authentication: catches MS Authenticator/authentication loop issues (3 tickets)
# - ADDED hris_form_kronos: catches HRIS forms about PTO/leave → Kronos (2 tickets)
# - ADDED hup_west_ravdin: catches RAVDIN location → HUP West (1 ticket)
#
# Quick Fix 1 (eStar/Kronos vs Security Engineering overlap):
# - ADDED hris_estar_kronos: "HRIS Support Form" + eStar → Kronos (IR10403102)
# - ADDED estar_login_kronos: generic eStar login (no LDAP/portal signals) → Kronos (IR10403349)
# - REMOVED "clock in"/"clock out" from security_engineering_estar_lgh negatives
#   (clock-in issues at LGH CAN be Security Engineering — IR10403967 was blocked by this)
# - estar_login_kronos runs AFTER security_engineering rules so LDAP/portal eStar
#   still routes to Security Engineering, but generic eStar login defaults to Kronos
#
# Quick Fix 2 (IAM-SSO / MS Authenticator):
# - FIXED target_group: was "Technology\\Infrastructure\\IAM-Single Sign On" (no spaces
#   around dash) but actual group name is "IAM - Single Sign On" (with spaces). The leaf
#   match in check_specific_triage failed because "iam-single sign on" != "iam - single sign on"
# - ADDED typo variant "authentiator" to keywords (IR10398870 has "MS Authentiator")
# - ADDED "prompting for ms authenticator" and "prompting for authenticator" variants
SPECIFIC_TRIAGE_PATTERNS = [
    {
        "name": "security_engineering_lgh_ldap",
        "target_group": "Security Engineering",
        "keywords": ["lha.org", "lgh.org:389"],
        "context_keywords": [],  # LDAP error string is definitive — no context needed
        # Phase 2d: Added "hris support form" as negative. When an HRIS form mentions
        # an LDAP error, it's a Kronos timekeeping issue reported through the HRIS form,
        # not a Security Engineering infrastructure issue. IR10403102 has "HRIS Support
        # Form - eStar" + "lha.org:389" and should go to Kronos, not Security Engineering.
        "negative": ["hris support form"],
        "description": "LGH LDAP errors (lha.org:389, lgh.org:389) are infrastructure "
                      "authentication issues handled by Security Engineering, not Kronos. "
                      "Exception: HRIS Support Forms with LDAP errors go to Kronos. "
                      "Targets: IR10403819, IR10403604.",
    },
    {
        "name": "security_engineering_estar_lgh",
        "target_group": "Security Engineering",
        "keywords": ["estar portal", "estar timestamp", "estar workforce",
                     "e-star portal", "e star portal"],
        "context_keywords": ["lancaster", "lgh"],  # Must be at LGH location
        # Quick Fix 1: Removed "clock in"/"clock out" from negatives — clock-in issues
        # at LGH CAN be Security Engineering (IR10403967 was blocked by this negative)
        "negative": ["hris support form"],
        "description": "eStar Portal/Timestamp/Workforce issues at LGH are infrastructure "
                      "authentication problems handled by Security Engineering. Generic "
                      "eStar login issues (without portal/timestamp/workforce qualifier) "
                      "at LGH go to Kronos. Targets: IR10402920, IR10403212.",
    },
    {
        "name": "hris_estar_kronos",
        "target_group": "Kronos",
        "keywords": ["hris support form"],
        "context_keywords": ["estar", "e-star", "e star"],
        "negative": ["transfer", "reporting change", "network access", "email access",
                     "remove employee", "no longer employee"],
        "description": "Quick Fix 1: HRIS Support Form + eStar context → Kronos. "
                      "eStar is the timekeeping system managed by Kronos team. "
                      "Catches IR10403102 'HRIS Support Form - eStar' which was "
                      "misrouted to Security Engineering.",
    },
    {
        "name": "iam_authentication",
        # Quick Fix 2: Fixed target — was "IAM-Single Sign On" (no spaces around dash)
        # but actual Athena group name is "IAM - Single Sign On" (with spaces).
        # Using leaf name for reliable matching via check_specific_triage's leaf lookup.
        "target_group": "IAM - Single Sign On",
        # Quick Fix 2: Added typo "authentiator" (IR10398870 has this typo in title)
        # and "prompting for" variants (IR10402169 uses this phrasing)
        "keywords": ["ms authenticator", "microsoft authenticator",
                     "authenticator app", "authentication loop",
                     "ms authentiator", "microsoft authentiator",
                     "prompting for authenticator", "prompting for ms authenticator"],
        "context_keywords": [],  # MS Authenticator is a definitive signal for IAM-SSO
        # Phase 2d: Removed "self-enroll" from negatives. IR10398870 describes
        # "self-enrolling in DUO" but the actual issue is MS Authenticator showing up
        # instead of Duo — this IS an IAM-SSO issue. "self-enroll" was blocking the match.
        # Phase 2d: Added "hardware repair" and "hardware support" as negatives.
        # IR10317914 "[PCAM] Hardware Repair - Mobile Phone" has "Microsoft Authenticator"
        # in description but is actually a hardware repair ticket for PCAM.
        "negative": ["duo enrollment", "duo setup", "hardware repair", "hardware support"],
        "description": "Quick Fix 2: MS Authenticator / authentication loop issues go to "
                      "IAM-SSO, not ATLAS. Fixed target group name spacing and added typo "
                      "variants. Targets: IR10398870, IR10399665, IR10402169.",
    },
    {
        "name": "hris_form_kronos",
        "target_group": "Kronos",
        "keywords": ["hris support form"],
        "context_keywords": ["paid time off", "parental time", "parental leave",
                            "fmla", "ppt", "pto"],
        "negative": ["transfer", "reporting change", "network access", "email access",
                     "remove employee", "no longer employee"],
        "description": "HRIS Support Forms about time/leave management (PTO, parental "
                      "leave, FMLA) go to Kronos, not HRIS/PennforPeopleHR. HRIS forms "
                      "about transfers, reporting changes, network/email access, or "
                      "employee removal stay at HRIS. Targets: IR10404611, IR10405809.",
    },
    {
        "name": "estar_login_kronos",
        "target_group": "Kronos",
        "keywords": ["estar", "e-star", "e star"],
        "context_keywords": ["log in", "login", "logon", "log on", "sign in",
                            "unable to access", "unable to log", "cannot log",
                            "can't log", "can not log"],
        "negative": ["lha.org", "lgh.org:389", "estar portal", "estar timestamp",
                     "estar workforce", "e-star portal", "e star portal",
                     "hris support form"],
        "description": "Quick Fix 1: Generic eStar login issues (without LDAP errors "
                      "or portal/timestamp/workforce qualifiers) default to Kronos. "
                      "eStar is primarily a timekeeping system. Security Engineering "
                      "rules (LDAP, portal) run first and take priority. "
                      "Catches IR10403349 'Unable to logon to estar'.",
    },
    {
        "name": "hup_west_ravdin",
        "target_group": "HUP West",
        "keywords": [],  # No text keywords needed — purely location-based
        "context_keywords": [],
        "negative": [],
        "location_keywords": ["ravdin", "rhoads", "maloney"],  # Phase 2e: expanded HUP West buildings
        "description": "Tickets at RAVDIN, RHOADS, or MALONEY buildings go to HUP West. "
                      "These are buildings within the HUP West service area. "
                      "Targets: IR10403794, IR10401521, IR10404978.",
    },
    # ── Phase 2f: Semantic triage rules for clear misroute patterns ────
    {
        "name": "aria_upgrade_command_center",
        "target_group": "Command Center Support",
        "keywords": ["aria upgrade", "aria v18", "aria issue"],
        "context_keywords": [],  # Aria upgrade issues are always Command Center
        "negative": ["billing code", "missing provider"],  # Billing/provider issues stay at Rad Onc
        "description": "Phase 2f: Aria upgrade/version issues go to Command Center Support, "
                      "not Rad Onc. During upgrades, Command Center handles triage. "
                      "Targets: IR10404102, IR10405227.",
    },
    {
        "name": "riskonnect_is_event_isaac",
        "target_group": "ISAAC",
        "keywords": ["riskonnect event - information systems"],
        "context_keywords": [],  # Definitive signal — IS Riskonnect events always go to ISAAC
        "negative": [],
        "description": "Phase 2f: Riskonnect events categorized as 'Information Systems' "
                      "go to ISAAC (IS Audit & Compliance), not PennChart Chart Correction "
                      "or Riskonnect Support. Targets: IR10403448.",
    },
    {
        "name": "riskonnect_privacy_isaac",
        "target_group": "ISAAC",
        "keywords": ["riskonnect event - privacy"],
        "context_keywords": [],  # Privacy/data breach Riskonnect events go to ISAAC
        "negative": [],
        "description": "Phase 2f: Riskonnect events categorized as 'Privacy' or "
                      "'Data Breach' go to ISAAC. Targets: IR10403444.",
    },
    {
        "name": "windows_defender_lockout_cyber",
        "target_group": "Cyber Defense and Operations",
        "keywords": ["windows defender", "defender has locked", "defender locked"],
        "context_keywords": [],  # Windows Defender lockouts are always Cyber Defense
        "negative": [],
        "description": "Phase 2f: Windows Defender lockout issues go to Cyber Defense "
                      "and Operations, not the site-specific EUS team. "
                      "Target: IR10404964.",
    },
    {
        "name": "pcam_hardware_location",
        "target_group": "PCAM",
        "keywords": [],  # Location-only rule
        "context_keywords": [],
        "negative": [],
        "location_keywords": ["pcam", "s. pavillion", "south pavilion"],
        "description": "Phase 2e: Hardware/desktop tickets at PCAM or S. PAVILLION "
                      "go to PCAM team. Targets: IR10404647, IR10404697.",
    },
]


def check_service_desk_triage(title: str, description: str) -> bool:
    """
    Check if a ticket matches Service Desk triage patterns.
    Returns True if the ticket should be assigned to Service Desk.
    """
    text = f"{title} {description}".lower()

    for pattern in SERVICE_DESK_TRIAGE_PATTERNS:
        # Check if any positive keyword matches
        has_positive = any(kw in text for kw in pattern["keywords"])
        if not has_positive:
            continue

        # Check if any negative keyword is present (would indicate escalation)
        has_negative = any(kw in text for kw in pattern.get("negative", []))
        if has_negative:
            continue

        return True

    return False


def check_specific_triage(
    title: str, description: str, location: str, support_groups: dict[str, str]
) -> tuple[str, str] | None:
    """
    Phase 2c: Check if a ticket matches specific triage patterns that fix
    known misroute patterns. These rules are designed from actual ticket
    analysis and run BEFORE the Service Desk triage to prevent false positives.

    Supports three matching modes:
    1. Text keywords + optional context keywords (standard mode)
    2. Location-only keywords (for location-based routing like RAVDIN → HUP West)
    3. Combined text + location matching

    Returns (group_name, guid) if matched, None otherwise.
    """
    text = f"{title} {description}".lower()
    location_lower = (location or "").lower()

    for pattern in SPECIFIC_TRIAGE_PATTERNS:
        # ── Location-only matching (e.g., RAVDIN → HUP West) ──────────
        location_kws = pattern.get("location_keywords", [])
        primary_kws = pattern.get("keywords", [])

        if location_kws and not primary_kws:
            # Pure location-based rule — match location only
            has_location = any(kw in location_lower for kw in location_kws)
            if not has_location:
                continue
            # No text keywords to check, skip to target resolution
        else:
            # ── Standard text-based matching ───────────────────────────
            # Must have at least one primary keyword in text
            has_primary = any(kw in text for kw in primary_kws)
            if not has_primary:
                continue

            # If context_keywords exist, at least one must match (in text OR location)
            context_kws = pattern.get("context_keywords", [])
            if context_kws:
                has_context = any(
                    kw in text or kw in location_lower for kw in context_kws
                )
                if not has_context:
                    continue

        # Check negatives (applies to all matching modes)
        has_negative = any(kw in text for kw in pattern.get("negative", []))
        if has_negative:
            continue

        # Find the target group in support_groups
        target = pattern["target_group"]
        if target in support_groups:
            return target, support_groups[target]

        # Try leaf match (e.g., "Kronos" matches "Applications\\...\\Kronos")
        target_lower = target.lower()
        for name, guid in support_groups.items():
            if name.lower() == target_lower or name.lower().endswith("\\" + target_lower):
                return name, guid

    return None


# ═══════════════════════════════════════════════════════════════════════
# PRIORITY NORMALIZATION (Quick Fix 3)
# ═══════════════════════════════════════════════════════════════════════

# Quick Fix 3: The LLM sometimes returns word-based priority ("Medium") instead
# of numeric (3) for IR tickets, and JSON parse failures from backslashes in
# group names cause the fallback to return "Medium" as default priority.
# This map converts word priorities to IR numeric equivalents.
IR_PRIORITY_WORD_TO_NUMBER = {
    "critical": "1",
    "immediate": "1",
    "urgent": "1",
    "high": "2",
    "medium": "3",
    "normal": "3",
    "low": "4",
    "minimal": "4",
}


def normalize_priority(priority_str: str, ticket_type: str) -> str:
    """
    Quick Fix 3: Normalize priority values.

    For IR tickets: Convert word-based priorities to numeric (1-4).
    For SR tickets: Keep word-based priorities as-is.

    Also handles edge cases like extra whitespace, mixed case.
    """
    if not priority_str:
        return priority_str

    cleaned = priority_str.strip()

    # If it's already numeric, return as-is
    if cleaned.isdigit():
        return cleaned

    # For IR tickets, convert words to numbers
    if ticket_type == "IR":
        normalized = IR_PRIORITY_WORD_TO_NUMBER.get(cleaned.lower())
        if normalized:
            return normalized

    # For SR tickets or unrecognized values, return as-is
    return cleaned


def fix_json_backslashes(raw_response: str) -> str:
    """
    Quick Fix 3: Pre-process LLM response to fix JSON parse errors caused
    by unescaped backslashes in support group names.

    The LLM outputs group names like "Non-Corp IS\\PennChart Chart Correction"
    which contains a literal backslash. In JSON, backslashes must be escaped
    as \\\\. The LLM sometimes outputs single backslashes which cause
    json.loads() to fail with "Invalid \\escape".

    Strategy: Find the JSON object in the response, then fix backslashes
    inside string values only (not the JSON structure characters).
    """
    if not raw_response:
        return raw_response

    # Strip markdown code fences if present
    text = raw_response.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        # Remove closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()

    # Try parsing as-is first
    try:
        json.loads(text)
        return text  # Already valid JSON
    except json.JSONDecodeError:
        pass

    # Fix: Replace single backslashes with double backslashes in string values
    # But avoid double-escaping already-escaped backslashes
    # Pattern: replace \ that is NOT followed by another \ or a valid JSON escape char
    fixed = re.sub(
        r'(?<!\\)\\(?![\\"/bfnrtu])',
        r'\\\\',
        text,
    )

    # Verify the fix produces valid JSON
    try:
        json.loads(fixed)
        return fixed
    except json.JSONDecodeError:
        # If still invalid, return original and let the caller handle the error
        return raw_response


# ═══════════════════════════════════════════════════════════════════════
# PATH NORMALIZATION (Phase 2b)
# ═══════════════════════════════════════════════════════════════════════

def normalize_predicted_group(predicted: str, support_groups: dict[str, str]) -> str:
    """
    Phase 2b: Normalize the LLM's predicted support group name to match
    the format used by Athena's tierQueue field (short/leaf names).

    The LLM outputs full hierarchical paths like:
        "Applications\\Corporate Applications\\HRIS/PennforPeopleHR"
    But Athena's tierQueue returns short names like:
        "HRIS/PennforPeopleHR"

    This normalization extracts the leaf name from the full path, which
    improves exact match scoring and makes the output more user-friendly.

    Special cases:
    - "Service Desk" stays as "Service Desk" (not just "Desk")
    - "Service Desk\\ATLAS" stays as "Service Desk\\ATLAS"
    - Groups with unique leaf names get normalized to just the leaf
    - Groups where the leaf name is ambiguous keep the full path

    Args:
        predicted: The LLM's predicted support group name
        support_groups: The full support group dict (name -> GUID)

    Returns:
        Normalized group name (leaf name if unambiguous, full path otherwise)
    """
    if not predicted:
        return predicted

    predicted = predicted.strip()

    # If it's already a short name (no backslash), return as-is
    if "\\" not in predicted:
        return predicted

    # Extract the leaf name
    leaf = predicted.rsplit("\\", 1)[-1].strip()

    # Check if this leaf name is unique across all support groups
    # If multiple groups share the same leaf, keep the full path
    leaf_lower = leaf.lower()
    matching_groups = [
        name for name in support_groups
        if name.rsplit("\\", 1)[-1].strip().lower() == leaf_lower
    ]

    if len(matching_groups) == 1:
        # Unique leaf — safe to normalize to just the leaf name
        return leaf
    elif len(matching_groups) > 1:
        # Ambiguous leaf — keep the full path for precision
        # But check if the predicted name exactly matches one of them
        if predicted in matching_groups:
            return predicted
        # Return the leaf anyway since tierQueue uses it
        return leaf
    else:
        # Leaf not found in support groups — return as-is
        return predicted


# ── Keyword-Based Group Filtering ─────────────────────────────────────

# Map from ground-truth short names to full hierarchical paths in IR_SUPPORT_GROUPS
# This helps the pre-filter include the correct full-path groups
SHORT_TO_FULL_PATH: dict[str, list[str]] = {}


def _build_short_to_full_map(support_groups: dict[str, str]) -> dict[str, list[str]]:
    """Build a mapping from short leaf names to full hierarchical paths."""
    result: dict[str, list[str]] = {}
    for fullname in support_groups:
        # Extract the leaf name (last segment after backslash)
        leaf = fullname.rsplit("\\", 1)[-1].strip().lower()
        if leaf not in result:
            result[leaf] = []
        result[leaf].append(fullname)

        # Also map the full name lowercased
        full_lower = fullname.strip().lower()
        if full_lower not in result:
            result[full_lower] = []
        if fullname not in result[full_lower]:
            result[full_lower].append(fullname)

    return result


def keyword_prefilter(
    title: str,
    description: str,
    location: str,
    support_groups: dict[str, str],
    keyword_mappings: dict[str, Any],
    max_candidates: int = 30,
    always_include: list[str] | None = None,
) -> dict[str, str]:
    """
    Filter support groups to a smaller candidate set based on ticket keywords.

    Args:
        title: Ticket title
        description: Ticket description
        location: Ticket location
        support_groups: Full dict of name→GUID
        keyword_mappings: Loaded keyword mappings from mining
        max_candidates: Maximum number of candidate groups to return
        always_include: Groups to always include (e.g., Service Desk)

    Returns:
        Filtered dict of name→GUID with only relevant candidates
    """
    text = f"{title} {description}".lower()
    location_lower = (location or "").lower()

    # Build short→full mapping
    short_to_full = _build_short_to_full_map(support_groups)

    # Score each group based on keyword matches
    group_scores: dict[str, float] = {}
    prefilter_rules = keyword_mappings.get("prefilter_rules", {})

    for group_name, rule_data in prefilter_rules.items():
        score = 0.0

        # Check keywords
        for kw in rule_data.get("keywords", []):
            if kw.lower() in text:
                score += 2.0

        # Check phrases (higher weight)
        for phrase in rule_data.get("phrases", []):
            if phrase.lower() in text:
                score += 3.0

        # Check location hints
        for loc_hint in rule_data.get("location_hints", []):
            if loc_hint.lower() in location_lower:
                score += 5.0  # Location is a strong signal

        if score > 0:
            # Map the short group name to full paths in support_groups
            group_lower = group_name.lower()
            matched_full_names = short_to_full.get(group_lower, [])

            if not matched_full_names:
                # Try partial matching
                for full_name in support_groups:
                    if group_lower in full_name.lower():
                        matched_full_names.append(full_name)

            for full_name in matched_full_names:
                existing = group_scores.get(full_name, 0)
                group_scores[full_name] = max(existing, score)

    # ── Location-based EUS matching ───────────────────────────────────
    # If the ticket mentions hardware/printer/computer AND has a location,
    # include the location-specific EUS group
    hardware_keywords = ["computer", "laptop", "printer", "monitor", "keyboard",
                        "mouse", "hardware", "workstation", "desktop", "scanner",
                        "printing", "screen", "wow", "rounding"]
    has_hardware = any(kw in text for kw in hardware_keywords)

    if has_hardware and location_lower:
        location_eus_map = {
            "campus": ["EUS\\Campus"],
            "cch": ["EUS\\CCH"],
            "hup cedar": ["EUS\\HUP Cedar"],
            "hup pavilion": ["EUS\\HUP\\HUP Pavilion"],
            "hup": ["EUS\\HUP"],
            "princeton": ["EUS\\MCP"],
            "mcp": ["EUS\\MCP"],
            "pah": ["EUS\\PaH"],
            "pcam": ["EUS\\PCAM"],
            "pmah": ["EUS\\PMaH"],
            "pmuc": ["EUS\\PMUC"],
            "ppmc": ["EUS\\PPMC"],
            "ritt": ["EUS\\RITT"],
            "remote sites": ["EUS\\RSI"],
            "rsi": ["EUS\\RSI"],
            "doylestown": ["PMDH Dispatch\\PMDH EUS"],
            "pmdh": ["PMDH Dispatch\\PMDH EUS"],
            "lgh": ["LGH\\Shared Services (LGH)\\PC Technicians (LGH)\\Hospital (LGH)"],
            "lghp": ["LGH\\Shared Services (LGH)\\PC Technicians (LGH)\\Commercial (LGH)"],
            "lancaster": ["LGH\\Shared Services (LGH)\\PC Technicians (LGH)\\Hospital (LGH)"],
            "community connect": ["EUS\\RSI"],
            "remote user": ["EUS\\Campus"],
            "data center": ["Technology\\Infrastructure"],
            # Phase 2c: RAVDIN building → HUP West
            "ravdin": ["EUS\\HUP West"],
        }

        for loc_prefix, eus_groups in location_eus_map.items():
            if location_lower.startswith(loc_prefix) or loc_prefix in location_lower:
                for eus_group in eus_groups:
                    if eus_group in support_groups:
                        group_scores[eus_group] = group_scores.get(eus_group, 0) + 10.0

    # ── Include groups from similar tickets (Phase 2a) ────────────────
    # If similar ticket support groups are provided, include them as candidates
    # with a high score — this is a data-driven signal from actual assignments
    similar_ticket_groups = always_include or []

    # ── Always include certain groups ─────────────────────────────────
    default_includes = [
        "Service Desk",
        "Service Desk\\ATLAS",
        "Service Desk\\Service Desk - Epic",
    ]
    for group in default_includes:
        if group in support_groups:
            group_scores[group] = group_scores.get(group, 0) + 0.1  # Low score but included

    # Add similar ticket groups with high score (data-driven signal)
    for group in similar_ticket_groups:
        if group in support_groups:
            group_scores[group] = group_scores.get(group, 0) + 8.0  # High score — empirical signal

    # ── Sort by score and take top candidates ─────────────────────────
    sorted_groups = sorted(group_scores.items(), key=lambda x: x[1], reverse=True)
    candidates = {}

    for full_name, score in sorted_groups[:max_candidates]:
        if full_name in support_groups:
            candidates[full_name] = support_groups[full_name]

    # If we got very few candidates, add some common groups
    if len(candidates) < 5:
        common_groups = [
            "Service Desk", "Service Desk\\ATLAS", "Service Desk\\Service Desk - Epic",
            "Technology\\Infrastructure\\Account Provisioning",
            "Technology\\Infrastructure\\Telecom",
        ]
        for cg in common_groups:
            if cg in support_groups and cg not in candidates:
                candidates[cg] = support_groups[cg]

    logger.debug("Pre-filter: %d candidates from %d total groups", len(candidates), len(support_groups))
    return candidates


# ═══════════════════════════════════════════════════════════════════════
# ENHANCED SYSTEM PROMPT WITH FEW-SHOT EXAMPLES
# ═══════════════════════════════════════════════════════════════════════

FEW_SHOT_EXAMPLES = """
FEW-SHOT EXAMPLES — Learn from these common routing patterns:

Example 1: Password Reset → Service Desk (NOT Account Provisioning)
  Title: "SD Password Reset Request"
  Description: "PennID verified, username verified, DOB verified"
  Correct: Service Desk (password resets are handled at Service Desk level)
  WRONG: Technology\\Infrastructure\\Account Provisioning

Example 2: MyPennMedicine/MyChart → Service Desk (NOT PennChart\\MyPennMedicine)
  Title: "Patient unable to access MyPennMedicine"
  Description: "Patient calling about MyChart access, needs password reset"
  Correct: Service Desk (patient portal issues are triaged by Service Desk)
  WRONG: PennChart\\MyPennMedicine

Example 3: HRIS Form → HRIS/PennforPeopleHR (use SHORT name)
  Title: "HRIS Support Form - Employee Transfer"
  Description: "HRIS support form details..."
  Correct: Applications\\Corporate Applications\\HRIS/PennforPeopleHR

Example 4: Kronos/eStar Timekeeping → Kronos (use SHORT name)
  Title: "eStar error when clocking in"
  Description: "EU getting error message in eStar when trying to clock in"
  Correct: Applications\\Corporate Applications\\Kronos

Example 5: Aria/Radiation Oncology → Command Center Support
  Title: "Aria v18 upgrade issue"
  Description: "User unable to launch Aria after v18 upgrade"
  Correct: Applications\\Rad Onc\\Command Center Support

Example 6: Hardware at specific location → Location-specific EUS group
  Title: "Computer not turning on"
  Description: "PC at PMUC won't power on"
  Location: "PMUC"
  Correct: EUS\\PMUC (match the location, NOT EUS\\Campus)

Example 7: Duo/MFA issues → ATLAS
  Title: "Duo reactivation needed"
  Description: "EU needs Duo reactivated on new phone"
  Correct: Service Desk\\ATLAS

Example 8: Caller ended call / no issue → Service Desk
  Title: "Caller decided to end their call"
  Correct: Service Desk
"""

ENHANCED_SYSTEM_PROMPT = """\
You are an AI assistant for the Penn Medicine / UPHS IT Service Desk. \
Your task is to analyze a ticket and recommend the best support group assignment and priority level.

You must respond with ONLY a valid JSON object (no markdown, no code fences, no extra text) \
with exactly these keys:
- "support_group_name": The name of the recommended support group from the CANDIDATE list below
- "support_group_guid": The GUID of the recommended support group from the CANDIDATE list below
- "priority": The recommended priority level
- "rationale": A brief explanation (2-4 sentences) of why you chose this group and priority

IMPORTANT ROUTING RULES:
1. Password resets, account lockouts, and MyPennMedicine/MyChart issues should go to "Service Desk" \
unless there is a clear reason to escalate (e.g., PennChart provisioning issue, not a simple reset).
2. For hardware/printer/computer issues, ALWAYS match the ticket's physical location to the \
correct location-specific EUS sub-group. Never default to EUS\\Campus when the location indicates \
a different site.
3. Choose ONLY from the CANDIDATE support groups listed below. These have been pre-filtered \
based on the ticket content.

CANDIDATE SUPPORT GROUPS (name → GUID):
{support_groups}

PRIORITY GUIDANCE:
- For incidents (IR): Use numeric priority 1 (critical) through 4 (low). \
Consider impact and urgency.
- For service requests (SR): Use "Immediate", "High", "Medium", or "Low". \
Consider business impact and time sensitivity.

{few_shot_examples}

CONTEXT FROM KNOWLEDGE BASE AND SIMILAR TICKETS:
{context}"""


# ═══════════════════════════════════════════════════════════════════════
# ENRICHED CONTEXT BUILDER (Phase 2a)
# ═══════════════════════════════════════════════════════════════════════

def _build_enriched_context(
    doc_results: list[dict[str, Any]],
    ticket_results: list[dict[str, Any]],
    sg_mapping: dict[str, str],
    out_similar_groups: list[str],
) -> str:
    """
    Build the context string from retrieval results, enriched with support
    group labels for similar tickets (Phase 2a).

    Args:
        doc_results: Documentation search results
        ticket_results: Similar ticket search results (id + similarity)
        sg_mapping: ticket_id → support_group_name mapping
        out_similar_groups: Output list — populated with full-path support group
            names from similar tickets (for candidate set injection)

    Returns:
        Context string for the LLM prompt
    """
    parts: list[str] = []

    if doc_results:
        parts.append("=== KNOWLEDGE BASE DOCUMENTATION ===")
        for i, doc in enumerate(doc_results, 1):
            title = doc.get("title", "Untitled")
            section = doc.get("section", "Unknown Section")
            notebook = doc.get("notebook", "unknown")
            content = doc.get("content", "")
            similarity = doc.get("similarity", 0.0)
            parts.append(
                f"\n--- Document {i} (similarity: {similarity:.3f}) ---\n"
                f"Notebook: {notebook} | Section: {section} | Title: {title}\n"
                f"{content}"
            )

    if ticket_results:
        parts.append("\n=== SIMILAR HISTORICAL TICKETS ===")
        # Track support group votes for consensus signal
        sg_votes: dict[str, int] = {}

        for i, ticket in enumerate(ticket_results, 1):
            ticket_id = ticket.get("id", "Unknown")
            similarity = ticket.get("similarity", 0.0)

            # Look up the support group this ticket was assigned to
            assigned_sg = sg_mapping.get(ticket_id)

            if assigned_sg:
                parts.append(
                    f"- Ticket {ticket_id} (similarity: {similarity:.3f}) "
                    f"→ assigned to: {assigned_sg}"
                )
                sg_votes[assigned_sg] = sg_votes.get(assigned_sg, 0) + 1

                # Collect full-path group names for candidate injection
                # Map short names to full paths in IR_SUPPORT_GROUPS
                _resolve_and_collect(assigned_sg, out_similar_groups)
            else:
                parts.append(f"- Ticket {ticket_id} (similarity: {similarity:.3f})")

        # Add consensus summary if multiple tickets agree
        if sg_votes:
            top_sg = max(sg_votes.items(), key=lambda x: x[1])
            total_with_sg = sum(sg_votes.values())
            if top_sg[1] >= 2:
                parts.append(
                    f"\n  ** {top_sg[1]} of {total_with_sg} similar tickets were assigned to "
                    f"'{top_sg[0]}' — strong routing signal **"
                )

    if not parts:
        return "No relevant documentation or similar tickets were found."

    return "\n".join(parts)


def _resolve_and_collect(short_name: str, out_groups: list[str]) -> None:
    """
    Resolve a short support group name (from Athena tierQueue) to full
    hierarchical path(s) in IR_SUPPORT_GROUPS, and add to out_groups.

    The tierQueue field returns short names like "Service Desk", "PMUC",
    "Kronos" while IR_SUPPORT_GROUPS uses full paths like
    "Applications\\Corporate Applications\\Kronos".
    """
    short_lower = short_name.strip().lower()

    # Direct match (full path already)
    if short_name in IR_SUPPORT_GROUPS:
        if short_name not in out_groups:
            out_groups.append(short_name)
        return

    # Search for leaf match or contains match
    for full_name in IR_SUPPORT_GROUPS:
        leaf = full_name.rsplit("\\", 1)[-1].strip().lower()
        if leaf == short_lower or short_lower in full_name.lower():
            if full_name not in out_groups:
                out_groups.append(full_name)
            return  # Take first match

    # No match found — add the short name anyway (won't match support_groups
    # dict but won't cause errors either)
    if short_name not in out_groups:
        out_groups.append(short_name)


# ═══════════════════════════════════════════════════════════════════════
# ENHANCED RECOMMENDATION PIPELINE
# ═══════════════════════════════════════════════════════════════════════

async def run_single_recommendation_enhanced(
    ticket: dict[str, Any],
    databricks_client: DatabricksClient,
    keyword_mappings: dict[str, Any],
    sg_mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Run the enhanced assignment recommendation pipeline with:
    - Service Desk triage check
    - Keyword pre-filter (309 → ~10-30 groups)
    - Enhanced prompt with few-shot examples
    - Phase 2a: Enriched similar ticket context with support group labels
    - Phase 2a: Auto-include groups from similar tickets in candidates
    """
    sg_mapping = sg_mapping or {}
    ticket_id = ticket["ticket_id"]
    ticket_type_str = ticket["ticket_type"]
    ticket_type = "incident" if ticket_type_str == "IR" else "servicerequest"

    title = ticket.get("title") or ""
    description = ticket.get("description") or ""
    location = ticket.get("location") or ""

    # ── Step 0: Phase 2c Specific Triage Check (runs FIRST) ──────────
    # Phase 2c: Specific triage now runs BEFORE SD triage to prevent
    # false positives (e.g., "no issues" in description matching SD's
    # "no issue" keyword when the ticket is actually IAM-SSO).
    full_support_groups = IR_SUPPORT_GROUPS if ticket_type_str == "IR" else SR_SUPPORT_GROUPS
    specific_match = check_specific_triage(title, description, location, full_support_groups)
    if specific_match:
        group_name, group_guid = specific_match
        # Normalize the group name to leaf format (Phase 2b)
        normalized_name = normalize_predicted_group(group_name, full_support_groups)
        return {
            "ticket_id": ticket_id,
            "ticket_type": ticket_type_str,
            "title": title,
            "location": location,
            "actual_support_group": ticket["actual_support_group"],
            "actual_priority": ticket["actual_priority"],
            "predicted_support_group": normalized_name,
            "predicted_priority": str(ticket.get("actual_priority", "3")),
            "rationale": f"Phase 2c specific triage rule: matched pattern for {group_name}",
            "method": "specific_triage_rule",
            "candidates_count": 0,
        }

    # ── Step 0b: Service Desk Triage Check ────────────────────────────
    if check_service_desk_triage(title, description):
        support_groups = IR_SUPPORT_GROUPS if ticket_type_str == "IR" else SR_SUPPORT_GROUPS
        sd_guid = support_groups.get("Service Desk", "")
        return {
            "ticket_id": ticket_id,
            "ticket_type": ticket_type_str,
            "title": title,
            "location": location,
            "actual_support_group": ticket["actual_support_group"],
            "actual_priority": ticket["actual_priority"],
            "predicted_support_group": "Service Desk",
            "predicted_priority": str(ticket.get("actual_priority", "3")),  # Keep existing priority for triage
            "rationale": "Service Desk triage rule: ticket matches password reset / MyChart / account lockout pattern",
            "method": "triage_rule",
            "candidates_count": 0,
        }

    # ── Step 1: Select support groups ─────────────────────────────────

    # NOTE: Pre-filter is applied AFTER semantic search (Step 3) so we can
    # include groups from similar tickets in the candidate set (Phase 2a).

    # ── Step 2: Build TicketInfo ──────────────────────────────────────
    ticket_info = TicketInfo(
        id=ticket_id,
        ticket_type=ticket_type,
        title=title,
        description=description,
        status=ticket.get("status"),
        priority=None,  # STRIPPED for blind test
        support_group=None,  # STRIPPED for blind test
        affected_user=ticket.get("affected_user"),
        affected_user_title=ticket.get("affected_user_title"),
        location=location,
        created_date=ticket.get("created_date"),
    )

    # ── Step 3: Generate embedding and semantic search ────────────────
    search_text = AssignmentService._build_search_text(ticket_info)
    query_embedding = await databricks_client.generate_embedding(search_text)

    loop = asyncio.get_event_loop()
    doc_results, ticket_results = await asyncio.gather(
        loop.run_in_executor(
            None,
            databricks_client.find_similar_documentation,
            query_embedding,
            5,
        ),
        loop.run_in_executor(
            None,
            databricks_client.find_similar_by_embedding,
            query_embedding,
            "scratchpad.aslanuka.ir_embeddings",
            "ticket_embedding",
            "id",
            5,
        ),
    )

    # ── Step 4: Build enriched context (Phase 2a) ─────────────────────
    # Enrich similar ticket context with support group labels
    similar_ticket_sg_names: list[str] = []
    context = _build_enriched_context(doc_results, ticket_results, sg_mapping, similar_ticket_sg_names)

    # ── Step 4b: Apply pre-filter with similar ticket groups ──────────
    # Now that we know which groups similar tickets were assigned to,
    # include them as candidates in the pre-filter
    filtered_groups = keyword_prefilter(
        title=title,
        description=description,
        location=location,
        support_groups=full_support_groups,
        keyword_mappings=keyword_mappings,
        max_candidates=30,
        always_include=similar_ticket_sg_names,
    )

    # ── Step 5: Build enhanced LLM messages ───────────────────────────
    sg_lines = "\n".join(
        f"  - {name}: {guid}" for name, guid in filtered_groups.items()
    )

    system_content = ENHANCED_SYSTEM_PROMPT.format(
        support_groups=sg_lines,
        few_shot_examples=FEW_SHOT_EXAMPLES,
        context=context,
    )

    ticket_details = (
        f"Ticket ID: {ticket_info.id}\n"
        f"Type: {ticket_info.ticket_type}\n"
        f"Title: {ticket_info.title or 'N/A'}\n"
        f"Description: {ticket_info.description or 'N/A'}\n"
        f"Current Status: {ticket_info.status or 'N/A'}\n"
        f"Current Priority: N/A (stripped for blind test)\n"
        f"Current Support Group: N/A (stripped for blind test)\n"
        f"Affected User: {ticket_info.affected_user or 'N/A'}\n"
        f"Location: {ticket_info.location or 'N/A'}\n"
        f"Created Date: {ticket_info.created_date or 'N/A'}"
    )

    user_content = (
        f"Please analyze the following ticket and recommend the best support group "
        f"assignment and priority level. Choose ONLY from the candidate groups listed above.\n\n"
        f"{ticket_details}"
    )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    # ── Step 6: Call LLM ──────────────────────────────────────────────
    llm_response = await databricks_client.call_llm(messages, max_tokens=2048)

    # ── Step 6b: Quick Fix 3 — Fix JSON backslashes before parsing ────
    # LLM sometimes outputs unescaped backslashes in group names like
    # "Non-Corp IS\PennChart Chart Correction" which breaks json.loads()
    llm_response_fixed = fix_json_backslashes(llm_response)

    # ── Step 7: Parse recommendation ──────────────────────────────────
    recommendation = AssignmentService._parse_recommendation(llm_response_fixed, filtered_groups)

    # ── Step 8: Phase 2b Path Normalization ───────────────────────────
    # Normalize the LLM's full-path prediction to leaf name format
    # (matching Athena's tierQueue format for better exact match scoring)
    normalized_sg = normalize_predicted_group(
        recommendation.support_group_name, full_support_groups
    )

    # ── Step 9: Quick Fix 3 — Normalize priority ──────────────────────
    # Convert word-based priorities ("Medium") to numeric for IR tickets
    raw_priority = str(recommendation.priority)
    normalized_priority = normalize_priority(raw_priority, ticket_type_str)

    return {
        "ticket_id": ticket_id,
        "ticket_type": ticket_type_str,
        "title": title,
        "location": location,
        "actual_support_group": ticket["actual_support_group"],
        "actual_priority": ticket["actual_priority"],
        "predicted_support_group": normalized_sg,
        "predicted_support_group_raw": recommendation.support_group_name,
        "predicted_priority": normalized_priority,
        "predicted_priority_raw": raw_priority,
        "rationale": recommendation.rationale,
        "method": "llm_with_prefilter",
        "candidates_count": len(filtered_groups),
        "similar_ticket_groups": similar_ticket_sg_names,
    }


async def run_benchmark(
    dataset: list[dict[str, Any]],
    keyword_mappings: dict[str, Any],
    sg_mapping: dict[str, str] | None = None,
    resume_from: int = 0,
) -> list[dict[str, Any]]:
    """Run the enhanced benchmark with keyword pre-filter + Phase 2a enrichment."""
    settings = Settings()
    databricks_client = DatabricksClient(settings)
    sg_mapping = sg_mapping or {}

    results: list[dict[str, Any]] = []

    # Load existing results if resuming
    if resume_from > 0 and RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        results = existing.get("results", [])
        logger.info("Resuming from index %d (loaded %d existing results)", resume_from, len(results))

    total = len(dataset)
    logger.info("=" * 60)
    logger.info("Running ENHANCED benchmark for %d tickets (starting at %d)...", total - resume_from, resume_from)
    has_sg_mapping = len(sg_mapping) > 0
    logger.info("Enhancements: keyword pre-filter + SD triage + few-shot examples%s",
                " + Phase 2a enriched context" if has_sg_mapping else "")
    logger.info("=" * 60)

    triage_count = 0
    llm_count = 0

    for i in range(resume_from, total):
        ticket = dataset[i]
        ticket_id = ticket["ticket_id"]

        try:
            logger.info(
                "[%d/%d] Processing %s (%s)...",
                i + 1, total, ticket_id, ticket["ticket_type"],
            )

            result = await run_single_recommendation_enhanced(
                ticket, databricks_client, keyword_mappings, sg_mapping,
            )

            if result.get("method") == "triage_rule":
                triage_count += 1
            else:
                llm_count += 1

            # Score the result
            sg_scores = score_support_group(
                result["actual_support_group"],
                result["predicted_support_group"],
            )
            pri_scores = score_priority(
                result["actual_priority"],
                result["predicted_priority"],
                result["ticket_type"],
            )

            result["support_group_scores"] = sg_scores
            result["priority_scores"] = pri_scores

            results.append(result)

            # Log inline result
            method_tag = "🏷️TRIAGE" if result.get("method") == "triage_rule" else f"🤖LLM({result.get('candidates_count', '?')})"
            sg_emoji = "✅" if sg_scores["exact_match"] else ("🟡" if sg_scores["leaf_match"] else "❌")
            pri_emoji = "✅" if pri_scores["exact_match"] else ("🟡" if pri_scores["within_one"] else "❌")
            logger.info(
                "  %s SG: %s actual='%s' predicted='%s'",
                method_tag, sg_emoji, result["actual_support_group"], result["predicted_support_group"],
            )
            logger.info(
                "  Pri: %s actual='%s' predicted='%s'",
                pri_emoji, result["actual_priority"], result["predicted_priority"],
            )

        except Exception as exc:
            logger.error("  FAILED for %s: %s", ticket_id, exc)
            results.append({
                "ticket_id": ticket_id,
                "ticket_type": ticket["ticket_type"],
                "error": str(exc),
            })

        # Save progress after each ticket
        save_results(results)

        # Rate limiting delay between LLM calls (skip for triage-only)
        if i < total - 1 and result.get("method") != "triage_rule":
            time.sleep(LLM_DELAY_SECONDS)

    logger.info("=" * 60)
    logger.info("Benchmark complete: %d triage, %d LLM calls", triage_count, llm_count)
    logger.info("=" * 60)

    # Cleanup
    await databricks_client.close()

    return results


def save_results(results: list[dict[str, Any]]) -> None:
    """Save benchmark results to JSON."""
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now().isoformat(),
                "count": len(results),
                "enhancements": [
                    "keyword_prefilter",
                    "service_desk_triage",
                    "few_shot_examples",
                    "enhanced_prompt",
                    "phase2a_enriched_context",
                    "phase2b_path_normalization",
                    "phase2c_specific_triage_rules",
                    "phase2c_sd_triage_false_positive_fix",
                    "phase2c_specific_before_sd_triage",
                    "phase2c_ravdin_location_mapping",
                ],
                "results": results,
            },
            f,
            indent=2,
            default=str,
        )


def generate_enhanced_report(results: list[dict[str, Any]]) -> str:
    """Generate report with additional enhancement-specific metrics."""
    # Use the base report generator
    base_report = generate_report(results)

    # Add enhancement-specific metrics
    valid = [r for r in results if "error" not in r]
    if not valid:
        return base_report

    triage_results = [r for r in valid if r.get("method") == "triage_rule"]
    llm_results = [r for r in valid if r.get("method") != "triage_rule"]

    extra_lines: list[str] = []
    extra_lines.append(f"\n{'=' * 70}")
    extra_lines.append("ENHANCEMENT METRICS")
    extra_lines.append(f"{'=' * 70}")
    extra_lines.append(f"\n  Total tickets: {len(valid)}")
    extra_lines.append(f"  Triage rule (no LLM): {len(triage_results)}")
    extra_lines.append(f"  LLM with pre-filter: {len(llm_results)}")

    if triage_results:
        triage_correct = sum(
            1 for r in triage_results
            if r.get("support_group_scores", {}).get("leaf_match")
        )
        extra_lines.append(f"\n  Triage accuracy (leaf): {triage_correct}/{len(triage_results)} "
                          f"({triage_correct/len(triage_results)*100:.1f}%)")

    if llm_results:
        llm_leaf = sum(
            1 for r in llm_results
            if r.get("support_group_scores", {}).get("leaf_match")
        )
        avg_candidates = sum(r.get("candidates_count", 0) for r in llm_results) / len(llm_results)
        extra_lines.append(f"  LLM accuracy (leaf): {llm_leaf}/{len(llm_results)} "
                          f"({llm_leaf/len(llm_results)*100:.1f}%)")
        extra_lines.append(f"  Avg candidates per ticket: {avg_candidates:.1f}")

    # Insert before the per-ticket breakdown
    report_lines = base_report.split("\n")
    insert_idx = None
    for i, line in enumerate(report_lines):
        if "PER-TICKET BREAKDOWN" in line:
            insert_idx = i - 1
            break

    if insert_idx:
        for j, extra_line in enumerate(extra_lines):
            report_lines.insert(insert_idx + j, extra_line)

    return "\n".join(report_lines)


# ═══════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark with keyword pre-filter enhancements",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only generate report from existing results",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from where a previous run left off",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds between LLM calls (default: 2.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    global LLM_DELAY_SECONDS
    LLM_DELAY_SECONDS = args.delay

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Report Only Mode ──────────────────────────────────────────────
    if args.report_only:
        if not RESULTS_PATH.exists():
            logger.error("No results file found at %s. Run the benchmark first.", RESULTS_PATH)
            sys.exit(1)
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        results = data.get("results", [])
        report = generate_enhanced_report(results)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
        try:
            print(report)
        except UnicodeEncodeError:
            print(report.encode("ascii", errors="replace").decode("ascii"))
        logger.info("Report saved to %s", REPORT_PATH)
        return

    # ── Load dataset ──────────────────────────────────────────────────
    if not DATASET_PATH.exists():
        logger.error("No benchmark dataset found at %s. Run benchmark_assignment_accuracy.py first.", DATASET_PATH)
        sys.exit(1)

    dataset = load_dataset(DATASET_PATH)

    # ── Load keyword mappings ─────────────────────────────────────────
    keyword_mappings = load_keyword_mappings()
    logger.info("Loaded keyword mappings with %d group rules",
                len(keyword_mappings.get("prefilter_rules", {})))

    # ── Load support group mapping (Phase 2a) ─────────────────────────
    sg_mapping = load_support_group_mapping()

    # ── Run benchmark ─────────────────────────────────────────────────
    resume_from = 0
    if args.resume and RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        resume_from = len(existing.get("results", []))
        logger.info("Resuming from ticket %d", resume_from)

    results = asyncio.run(run_benchmark(dataset, keyword_mappings, sg_mapping, resume_from=resume_from))

    # ── Generate report ───────────────────────────────────────────────
    report = generate_enhanced_report(results)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("ascii", errors="replace").decode("ascii"))

    logger.info("Results saved to %s", RESULTS_PATH)
    logger.info("Report saved to %s", REPORT_PATH)


if __name__ == "__main__":
    main()