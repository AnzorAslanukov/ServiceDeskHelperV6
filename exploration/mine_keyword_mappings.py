"""
Mine Keyword → Support Group Mappings
======================================
Phase 1a of the Recommendation Accuracy Improvement Plan.

Fetches a large sample of resolved/closed IR tickets from Athena,
analyzes which keywords in ticket titles/descriptions correlate with
which support groups, and outputs a keyword→group mapping file.

Usage:
    python -m exploration.mine_keyword_mappings
    python -m exploration.mine_keyword_mappings --max-tickets 2000
    python -m exploration.mine_keyword_mappings --use-cached
    python -m exploration.mine_keyword_mappings --analyze-only

RULE: This script lives in exploration/ only. Core code in src/ is NOT edited.
"""

import argparse
import collections
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ── Path Setup ────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from exploration.athena_auth import get_auth_headers, ATHENA_BASE_URL  # noqa: E402

# ── Configuration ─────────────────────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env")

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
MINED_DATASET_PATH = OUTPUT_DIR / "mined_tickets_dataset.json"
KEYWORD_MAPPINGS_PATH = OUTPUT_DIR / "keyword_group_mappings.json"
ANALYSIS_REPORT_PATH = OUTPUT_DIR / "keyword_mining_report.txt"

INCIDENT_VIEW_URL = os.getenv("ATHENA_INCIDENT_VIEW_URL")
INCIDENT_URL = os.getenv("ATHENA_INCIDENT_URL")

# Status GUIDs
IR_STATUS_RESOLVED = "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
IR_STATUS_CLOSED = "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PHASE 1: Fetch Large Ticket Dataset via View Endpoint
# ═══════════════════════════════════════════════════════════════════════

def build_single_status_filter(status_guid: str) -> list[dict[str, Any]]:
    """Build a view filter for a single status GUID."""
    return [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "Status",
                    "operator": "eq",
                    "value": status_guid,
                },
            ],
        }
    ]


def fetch_tickets_via_view(
    headers: dict[str, str],
    status_guid: str,
    max_results: int = 2000,
) -> list[dict[str, Any]]:
    """
    Fetch tickets from the view endpoint for a single status GUID.
    The view endpoint returns flattened records with supportGroupValue,
    title, locationValue, etc. — no need to fetch individual details.
    """
    filters = build_single_status_filter(status_guid)
    all_tickets: list[dict[str, Any]] = []
    page_size = 100
    max_pages = max_results // page_size + 1

    for page_num in range(max_pages):
        skip = page_num * page_size
        separator = "&" if "?" in INCIDENT_VIEW_URL else "?"
        url = f"{INCIDENT_VIEW_URL}{separator}$skip={skip}&$top={page_size}"

        try:
            response = requests.post(url, headers=headers, json=filters, timeout=60)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("  Failed to fetch page %d: %s", page_num + 1, exc)
            break

        results = data.get("result", [])
        if not results:
            break

        all_tickets.extend(results)
        logger.info("  Fetched page %d: %d tickets (total: %d)", page_num + 1, len(results), len(all_tickets))

        if len(all_tickets) >= max_results:
            break

        has_more = data.get("hasMoreResults", False)
        if not has_more:
            break

        time.sleep(0.3)

    return all_tickets[:max_results]


def extract_view_ticket(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract relevant fields from a view endpoint ticket record.
    View endpoint returns flattened fields like supportGroupValue, locationValue, etc.
    """
    ticket_id = raw.get("id") or raw.get("Id") or raw.get("ID")
    if not ticket_id:
        return None

    # Support group — view endpoint uses supportGroupValue or TierQueue
    support_group = (
        raw.get("supportGroupValue")
        or raw.get("tierQueueValue")
        or raw.get("TierQueueValue")
    )

    # If support group is still None, try the tierQueue field directly
    if not support_group:
        tq = raw.get("tierQueue") or raw.get("TierQueue")
        if isinstance(tq, dict):
            support_group = tq.get("name") or tq.get("displayName")
        elif isinstance(tq, str) and len(tq) > 40:
            # Likely a GUID, skip
            support_group = None
        elif isinstance(tq, str):
            support_group = tq

    if not support_group:
        return None

    title = raw.get("title") or raw.get("Title") or ""
    description = raw.get("description") or raw.get("Description") or ""
    location = (
        raw.get("locationValue")
        or raw.get("LocationValue")
        or ""
    )

    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": description[:500],  # Truncate for storage
        "support_group": support_group,
        "location": location,
    }


def fetch_mining_dataset(max_tickets: int = 2000) -> list[dict[str, Any]]:
    """
    Fetch a large dataset of resolved/closed IR tickets for keyword mining.
    Uses the view endpoint for efficiency (no individual detail fetches needed).
    """
    headers = get_auth_headers()
    if not headers:
        logger.error("Failed to authenticate with Athena. Aborting.")
        sys.exit(1)

    dataset: list[dict[str, Any]] = []
    per_status = max_tickets // 2

    # Fetch Resolved tickets
    logger.info("Fetching Resolved IR tickets (up to %d)...", per_status)
    resolved_raw = fetch_tickets_via_view(headers, IR_STATUS_RESOLVED, max_results=per_status)
    logger.info("  Got %d raw Resolved tickets", len(resolved_raw))

    for raw in resolved_raw:
        extracted = extract_view_ticket(raw)
        if extracted:
            dataset.append(extracted)

    # Fetch Closed tickets
    logger.info("Fetching Closed IR tickets (up to %d)...", per_status)
    closed_raw = fetch_tickets_via_view(headers, IR_STATUS_CLOSED, max_results=per_status)
    logger.info("  Got %d raw Closed tickets", len(closed_raw))

    for raw in closed_raw:
        extracted = extract_view_ticket(raw)
        if extracted:
            dataset.append(extracted)

    # Deduplicate by ticket_id
    seen_ids: set[str] = set()
    unique_dataset: list[dict[str, Any]] = []
    for ticket in dataset:
        tid = ticket["ticket_id"]
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_dataset.append(ticket)

    logger.info("Mining dataset: %d unique tickets with support groups", len(unique_dataset))
    return unique_dataset


def save_mining_dataset(dataset: list[dict[str, Any]], path: Path) -> None:
    """Save the mining dataset to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "collected_at": datetime.now().isoformat(),
                "count": len(dataset),
                "tickets": dataset,
            },
            f,
            indent=2,
            default=str,
        )
    logger.info("Mining dataset saved to %s (%d tickets)", path, len(dataset))


def load_mining_dataset(path: Path) -> list[dict[str, Any]]:
    """Load a cached mining dataset."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickets = data.get("tickets", [])
    logger.info("Loaded cached mining dataset from %s (%d tickets)", path, len(tickets))
    return tickets


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2: Analyze Keywords per Support Group
# ═══════════════════════════════════════════════════════════════════════

# Common English stop words to exclude from keyword analysis
STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must", "ought",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those", "what",
    "which", "who", "whom", "how", "when", "where", "why",
    "and", "or", "but", "if", "then", "else", "so", "not", "no", "nor",
    "for", "to", "from", "of", "in", "on", "at", "by", "with", "about",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further",
    "up", "down", "here", "there", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "only", "own",
    "same", "than", "too", "very", "just", "also", "now", "new",
    # Service desk common words (not distinctive)
    "user", "please", "ticket", "request", "issue", "problem", "help",
    "call", "caller", "called", "calling", "phone", "email", "sent",
    "reported", "reporting", "reports", "report", "says", "said",
    "needs", "needed", "need", "wants", "want", "wanted",
    "unable", "cannot", "can't", "doesn't", "don't", "won't", "isn't",
    "not", "working", "work", "works", "worked",
    "get", "getting", "got", "try", "trying", "tried",
    "see", "seeing", "seen", "look", "looking",
    "still", "already", "yet", "back", "going", "went", "come",
    "time", "today", "yesterday", "morning", "afternoon",
    "per", "via", "etc", "n/a", "na", "yes", "no",
    "template", "questions", "verified",
}

# Bigrams and trigrams that are more meaningful than individual words
IMPORTANT_PHRASES = [
    "password reset", "account lockout", "account locked",
    "epic access", "pennchart access", "mypennmedicine",
    "vpn", "citrix", "remote access", "remote desktop",
    "printer", "printing", "print queue",
    "monitor", "dual monitor", "second monitor",
    "computer", "laptop", "desktop", "workstation",
    "phone", "voicemail", "softphone", "cisco phone",
    "network", "wifi", "wireless", "ethernet", "network jack",
    "email", "outlook", "teams", "microsoft teams",
    "kronos", "estar", "clock in", "time clock",
    "hris", "pennforpeople", "pennforpeoplehr",
    "rad onc", "aria", "radiation oncology",
    "epic", "pennchart", "pennaccess",
    "cerner", "powerchart",
    "shared drive", "network drive", "file share",
    "software install", "application install",
    "new hire", "onboarding", "termination",
    "badge", "id badge", "access badge",
    "pager", "paging",
    "fax", "fax machine",
    "scanner", "scanning",
    "vdi", "virtual desktop",
    "single sign on", "sso",
    "mfa", "multi factor", "duo",
    "certificate", "ssl", "encryption",
    "firewall", "security",
    "database", "sql", "server",
    "interface", "hl7", "integration",
    "downtime", "outage", "maintenance",
]


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words, removing punctuation and numbers-only tokens."""
    if not text:
        return []
    # Lowercase and split on non-alphanumeric (keep hyphens within words)
    tokens = re.findall(r"[a-z][a-z0-9\-]*[a-z0-9]|[a-z]", text.lower())
    # Filter stop words and very short tokens
    return [t for t in tokens if t not in STOP_WORDS and len(t) >= 2]


def extract_phrases(text: str) -> list[str]:
    """Extract known important phrases from text."""
    if not text:
        return []
    text_lower = text.lower()
    found: list[str] = []
    for phrase in IMPORTANT_PHRASES:
        if phrase in text_lower:
            found.append(phrase)
    return found


def compute_tfidf(
    group_documents: dict[str, list[str]],
) -> dict[str, list[tuple[str, float]]]:
    """
    Compute TF-IDF scores for keywords per support group.

    Args:
        group_documents: dict mapping group_name → list of document texts
                        (each document is a ticket's title + description)

    Returns:
        dict mapping group_name → list of (keyword, tfidf_score) sorted by score desc
    """
    # Step 1: Build vocabulary and document frequency
    num_groups = len(group_documents)
    doc_freq: dict[str, int] = collections.Counter()  # How many groups contain this term
    group_term_freq: dict[str, collections.Counter] = {}

    for group, docs in group_documents.items():
        # Combine all docs for this group into one "mega-document"
        all_tokens: list[str] = []
        for doc in docs:
            all_tokens.extend(tokenize(doc))
            all_tokens.extend(extract_phrases(doc))

        tf = collections.Counter(all_tokens)
        group_term_freq[group] = tf

        # Document frequency: count each term once per group
        for term in tf:
            doc_freq[term] += 1

    # Step 2: Compute TF-IDF per group
    result: dict[str, list[tuple[str, float]]] = {}

    for group, tf in group_term_freq.items():
        total_terms = sum(tf.values())
        if total_terms == 0:
            result[group] = []
            continue

        scores: list[tuple[str, float]] = []
        for term, count in tf.items():
            # TF: normalized by total terms in group
            term_freq = count / total_terms
            # IDF: log(N / df) — higher for terms unique to fewer groups
            idf = math.log(num_groups / max(doc_freq[term], 1))
            tfidf = term_freq * idf
            scores.append((term, tfidf))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        result[group] = scores

    return result


def analyze_dataset(dataset: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Analyze the mined dataset to discover keyword→support_group patterns.

    Returns a comprehensive analysis dict.
    """
    # Group tickets by support group
    group_tickets: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for ticket in dataset:
        sg = ticket["support_group"]
        group_tickets[sg].append(ticket)

    # Count tickets per group
    group_counts = {sg: len(tickets) for sg, tickets in group_tickets.items()}
    total_tickets = len(dataset)

    logger.info("Found %d unique support groups across %d tickets", len(group_counts), total_tickets)

    # Build document collections per group (title + description)
    group_documents: dict[str, list[str]] = {}
    for sg, tickets in group_tickets.items():
        docs = []
        for t in tickets:
            text = f"{t.get('title', '')} {t.get('description', '')}"
            docs.append(text)
        group_documents[sg] = docs

    # Compute TF-IDF
    logger.info("Computing TF-IDF scores...")
    tfidf_scores = compute_tfidf(group_documents)

    # Build keyword mappings
    logger.info("Building keyword mappings...")
    keyword_mappings: dict[str, dict[str, Any]] = {}

    for sg, scores in tfidf_scores.items():
        count = group_counts[sg]
        pct = count / total_tickets * 100

        # Take top keywords (those with meaningful TF-IDF scores)
        top_keywords = [
            {"keyword": kw, "score": round(score, 6)}
            for kw, score in scores[:30]
            if score > 0.001  # Minimum significance threshold
        ]

        # Also collect the most common raw words (by frequency, not TF-IDF)
        all_tokens: list[str] = []
        for doc in group_documents[sg]:
            all_tokens.extend(tokenize(doc))
        freq = collections.Counter(all_tokens)
        top_frequent = [
            {"keyword": kw, "count": cnt}
            for kw, cnt in freq.most_common(20)
        ]

        # Extract common phrases
        all_phrases: list[str] = []
        for doc in group_documents[sg]:
            all_phrases.extend(extract_phrases(doc))
        phrase_freq = collections.Counter(all_phrases)
        top_phrases = [
            {"phrase": ph, "count": cnt}
            for ph, cnt in phrase_freq.most_common(10)
            if cnt >= 2  # At least 2 occurrences
        ]

        # Location distribution for this group
        location_dist: dict[str, int] = collections.Counter()
        for t in group_tickets[sg]:
            loc = t.get("location", "")
            if loc:
                # Normalize to top-level location
                top_loc = loc.split("\\")[0].strip() if "\\" in loc else loc.strip()
                if top_loc:
                    location_dist[top_loc] += 1

        keyword_mappings[sg] = {
            "ticket_count": count,
            "percentage": round(pct, 2),
            "top_tfidf_keywords": top_keywords,
            "top_frequent_keywords": top_frequent,
            "top_phrases": top_phrases,
            "location_distribution": dict(location_dist.most_common(10)),
        }

    return {
        "total_tickets": total_tickets,
        "unique_groups": len(group_counts),
        "group_distribution": dict(
            sorted(group_counts.items(), key=lambda x: x[1], reverse=True)
        ),
        "keyword_mappings": keyword_mappings,
    }


# ═══════════════════════════════════════════════════════════════════════
# PHASE 3: Build Actionable Keyword Pre-Filter Rules
# ═══════════════════════════════════════════════════════════════════════

def build_prefilter_rules(analysis: dict[str, Any]) -> dict[str, Any]:
    """
    Build actionable keyword pre-filter rules from the analysis.

    For each support group, produces:
    - keywords: list of keywords that strongly indicate this group
    - negative_keywords: keywords that indicate this is NOT the right group
    - location_hints: locations that commonly map to this group
    - min_confidence: minimum number of keyword matches to include this group

    Also produces Service Desk triage rules (tickets that should stay at SD).
    """
    mappings = analysis.get("keyword_mappings", {})
    group_dist = analysis.get("group_distribution", {})
    total = analysis.get("total_tickets", 1)

    rules: dict[str, dict[str, Any]] = {}

    for sg, data in mappings.items():
        count = data["ticket_count"]
        pct = data["percentage"]

        # Extract distinctive keywords (high TF-IDF score)
        distinctive_kws = [
            item["keyword"]
            for item in data.get("top_tfidf_keywords", [])[:15]
            if item["score"] > 0.002
        ]

        # Extract distinctive phrases
        distinctive_phrases = [
            item["phrase"]
            for item in data.get("top_phrases", [])
        ]

        # Location hints
        loc_dist = data.get("location_distribution", {})
        location_hints = list(loc_dist.keys())[:5]

        if distinctive_kws or distinctive_phrases:
            rules[sg] = {
                "keywords": distinctive_kws,
                "phrases": distinctive_phrases,
                "location_hints": location_hints,
                "ticket_count": count,
                "percentage": pct,
            }

    # ── Service Desk Triage Rules ─────────────────────────────────────
    # These are patterns where tickets should STAY at Service Desk
    # and not be escalated to specialized groups
    service_desk_triage = {
        "description": (
            "Tickets matching these patterns should be assigned to Service Desk. "
            "The LLM tends to over-route these to specialized groups."
        ),
        "rules": [
            {
                "pattern": "password reset",
                "keywords": ["password", "reset", "unlock", "locked", "lockout", "blocked"],
                "assign_to": "Service Desk",
                "rationale": "Standard password resets are handled by Service Desk, not Account Provisioning",
            },
            {
                "pattern": "mypennmedicine patient portal",
                "keywords": ["mypennmedicine", "mychart", "patient portal", "patient access"],
                "assign_to": "Service Desk",
                "rationale": "MyPennMedicine/MyChart issues are triaged by Service Desk first",
            },
            {
                "pattern": "account lockout",
                "keywords": ["account", "lockout", "locked out", "login failed", "blocked"],
                "assign_to": "Service Desk",
                "rationale": "Account lockouts are resolved by Service Desk, not Account Provisioning",
            },
            {
                "pattern": "general inquiry / caller ended call",
                "keywords": ["caller decided", "ended call", "hung up", "general inquiry", "general question"],
                "assign_to": "Service Desk",
                "rationale": "General inquiries and abandoned calls stay at Service Desk",
            },
            {
                "pattern": "basic troubleshooting",
                "keywords": ["restart", "reboot", "clear cache", "clear cookies", "try again"],
                "assign_to": "Service Desk",
                "rationale": "Basic troubleshooting steps are handled at Service Desk level",
            },
        ],
    }

    return {
        "generated_at": datetime.now().isoformat(),
        "total_tickets_analyzed": total,
        "groups_with_rules": len(rules),
        "prefilter_rules": rules,
        "service_desk_triage": service_desk_triage,
    }


# ═══════════════════════════════════════════════════════════════════════
# PHASE 4: Generate Report
# ═══════════════════════════════════════════════════════════════════════

def generate_report(analysis: dict[str, Any], rules: dict[str, Any]) -> str:
    """Generate a human-readable report of the keyword mining results."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("KEYWORD MINING REPORT — Support Group Keyword Analysis")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)

    total = analysis["total_tickets"]
    unique_groups = analysis["unique_groups"]
    lines.append(f"\nTotal tickets analyzed: {total}")
    lines.append(f"Unique support groups found: {unique_groups}")

    # ── Group Distribution ────────────────────────────────────────────
    lines.append(f"\n{'─' * 70}")
    lines.append("SUPPORT GROUP DISTRIBUTION (top 30)")
    lines.append(f"{'─' * 70}")

    group_dist = analysis["group_distribution"]
    for i, (sg, count) in enumerate(list(group_dist.items())[:30], 1):
        pct = count / total * 100
        bar = "█" * int(pct / 2)
        lines.append(f"  {i:>3}. {sg:<45} {count:>5} ({pct:>5.1f}%) {bar}")

    # ── Top Keywords per Group ────────────────────────────────────────
    lines.append(f"\n{'=' * 70}")
    lines.append("TOP DISTINCTIVE KEYWORDS PER SUPPORT GROUP")
    lines.append(f"{'=' * 70}")

    mappings = analysis["keyword_mappings"]
    # Sort by ticket count descending
    sorted_groups = sorted(mappings.items(), key=lambda x: x[1]["ticket_count"], reverse=True)

    for sg, data in sorted_groups[:40]:
        count = data["ticket_count"]
        pct = data["percentage"]
        lines.append(f"\n  {sg} ({count} tickets, {pct:.1f}%)")
        lines.append(f"  {'─' * 60}")

        # TF-IDF keywords
        tfidf_kws = data.get("top_tfidf_keywords", [])[:10]
        if tfidf_kws:
            kw_str = ", ".join(f"{item['keyword']}({item['score']:.4f})" for item in tfidf_kws)
            lines.append(f"    TF-IDF: {kw_str}")

        # Phrases
        phrases = data.get("top_phrases", [])
        if phrases:
            ph_str = ", ".join(f"'{item['phrase']}'({item['count']})" for item in phrases)
            lines.append(f"    Phrases: {ph_str}")

        # Frequent words
        freq_kws = data.get("top_frequent_keywords", [])[:10]
        if freq_kws:
            freq_str = ", ".join(f"{item['keyword']}({item['count']})" for item in freq_kws)
            lines.append(f"    Frequent: {freq_str}")

        # Locations
        loc_dist = data.get("location_distribution", {})
        if loc_dist:
            loc_str = ", ".join(f"{loc}({cnt})" for loc, cnt in list(loc_dist.items())[:5])
            lines.append(f"    Locations: {loc_str}")

    # ── Pre-filter Rules Summary ──────────────────────────────────────
    lines.append(f"\n{'=' * 70}")
    lines.append("PRE-FILTER RULES SUMMARY")
    lines.append(f"{'=' * 70}")

    prefilter = rules.get("prefilter_rules", {})
    lines.append(f"\nGroups with keyword rules: {len(prefilter)}")

    for sg, rule_data in sorted(prefilter.items(), key=lambda x: x[1]["ticket_count"], reverse=True)[:30]:
        kws = rule_data.get("keywords", [])[:8]
        phrases = rule_data.get("phrases", [])[:5]
        lines.append(f"\n  {sg} ({rule_data['ticket_count']} tickets)")
        if kws:
            lines.append(f"    Keywords: {', '.join(kws)}")
        if phrases:
            lines.append(f"    Phrases: {', '.join(phrases)}")

    # ── Service Desk Triage Rules ─────────────────────────────────────
    lines.append(f"\n{'=' * 70}")
    lines.append("SERVICE DESK TRIAGE RULES")
    lines.append(f"{'=' * 70}")

    triage = rules.get("service_desk_triage", {})
    for rule in triage.get("rules", []):
        lines.append(f"\n  Pattern: {rule['pattern']}")
        lines.append(f"    Keywords: {', '.join(rule['keywords'])}")
        lines.append(f"    Assign to: {rule['assign_to']}")
        lines.append(f"    Rationale: {rule['rationale']}")

    lines.append(f"\n{'=' * 70}")
    lines.append("END OF REPORT")
    lines.append(f"{'=' * 70}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mine keyword→support group mappings from historical tickets",
    )
    parser.add_argument(
        "--max-tickets",
        type=int,
        default=2000,
        help="Maximum tickets to fetch (default: 2000)",
    )
    parser.add_argument(
        "--use-cached",
        action="store_true",
        help="Use cached mining dataset if available",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Only run analysis on cached dataset (skip fetch)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Fetch or load dataset ─────────────────────────────────
    if args.analyze_only or (args.use_cached and MINED_DATASET_PATH.exists()):
        if not MINED_DATASET_PATH.exists():
            logger.error("No cached dataset found at %s. Run without --analyze-only first.", MINED_DATASET_PATH)
            sys.exit(1)
        dataset = load_mining_dataset(MINED_DATASET_PATH)
    else:
        dataset = fetch_mining_dataset(max_tickets=args.max_tickets)
        save_mining_dataset(dataset, MINED_DATASET_PATH)

    if not dataset:
        logger.error("No tickets in dataset. Aborting.")
        sys.exit(1)

    # ── Step 2: Analyze keywords ──────────────────────────────────────
    logger.info("Analyzing keyword patterns...")
    analysis = analyze_dataset(dataset)

    # ── Step 3: Build pre-filter rules ────────────────────────────────
    logger.info("Building pre-filter rules...")
    rules = build_prefilter_rules(analysis)

    # Save keyword mappings
    with open(KEYWORD_MAPPINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, default=str)
    logger.info("Keyword mappings saved to %s", KEYWORD_MAPPINGS_PATH)

    # ── Step 4: Generate report ───────────────────────────────────────
    report = generate_report(analysis, rules)
    with open(ANALYSIS_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("Report saved to %s", ANALYSIS_REPORT_PATH)

    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    main()