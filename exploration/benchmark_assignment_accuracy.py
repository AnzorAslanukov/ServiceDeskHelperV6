"""
Benchmark Assignment Accuracy
=============================
Blind accuracy benchmark for the assignment recommendation system (Features #2 & #3).
Measures how well the AI recommends support groups and priority levels by comparing
against ground truth from resolved/closed tickets.

Phase 1: Collect ground truth dataset from Athena (resolved/closed tickets, last 90 days)
Phase 2: Run blind recommendations (strip support_group & priority) and score

Usage:
    python -m exploration.benchmark_assignment_accuracy
    python -m exploration.benchmark_assignment_accuracy --sample-size 100
    python -m exploration.benchmark_assignment_accuracy --ticket-type IR --sample-size 50
    python -m exploration.benchmark_assignment_accuracy --use-cached --skip-fetch

RULE: This script lives in exploration/ only. Core code in src/ is NOT edited.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ── Path Setup ────────────────────────────────────────────────────────
# Add project root to sys.path so we can import from src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from exploration.athena_auth import get_auth_headers, ATHENA_BASE_URL  # noqa: E402
from src.config import Settings  # noqa: E402
from src.clients.athena_client import AthenaClient  # noqa: E402
from src.clients.databricks_client import DatabricksClient  # noqa: E402
from src.services.assignment import (  # noqa: E402
    AssignmentService,
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
)
from src.models.assignment import TicketInfo  # noqa: E402

# ── Configuration ─────────────────────────────────────────────────────

load_dotenv(PROJECT_ROOT / ".env")

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DATASET_PATH = OUTPUT_DIR / "benchmark_dataset.json"
RESULTS_PATH = OUTPUT_DIR / "benchmark_results.json"
REPORT_PATH = OUTPUT_DIR / "benchmark_report.txt"

INCIDENT_VIEW_URL = os.getenv("ATHENA_INCIDENT_VIEW_URL")
SERVICEREQUEST_VIEW_URL = os.getenv("ATHENA_SERVICEREQUEST_VIEW_URL")
INCIDENT_URL = os.getenv("ATHENA_INCIDENT_URL")
SERVICEREQUEST_URL = os.getenv("ATHENA_SERVICEREQUEST_URL")

# Rate limiting: seconds to wait between LLM calls
LLM_DELAY_SECONDS = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PHASE 1: Collect Ground Truth Dataset
# ═══════════════════════════════════════════════════════════════════════

# Status GUIDs for the view filter endpoint (requires GUIDs, not strings)
# IR Status GUIDs
IR_STATUS_RESOLVED = "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
IR_STATUS_CLOSED = "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf"


def build_single_status_filter(status_guid: str) -> list[dict[str, Any]]:
    """
    Build a view filter for a single status GUID.

    The Athena view endpoint requires GUIDs (not string names) for status
    and does not support OR as the sole child of an AND group.
    So we query one status at a time and combine results.
    """
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


def fetch_tickets_via_view_single_status(
    headers: dict[str, str],
    view_url: str,
    status_guid: str,
    max_results: int = 500,
) -> list[dict[str, Any]]:
    """
    Fetch tickets from the view endpoint for a single status GUID.
    Returns a list of basic ticket records.
    """
    filters = build_single_status_filter(status_guid)
    all_tickets: list[dict[str, Any]] = []
    page_size = 50
    max_pages = max_results // page_size + 1

    for page_num in range(max_pages):
        skip = page_num * page_size
        separator = "&" if "?" in view_url else "?"
        url = f"{view_url}{separator}$skip={skip}&$top={page_size}"

        try:
            response = requests.post(url, headers=headers, json=filters, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("  Failed to fetch page %d: %s", page_num + 1, exc)
            break

        results = data.get("result", [])
        if not results:
            break

        all_tickets.extend(results)

        if len(all_tickets) >= max_results:
            break

        has_more = data.get("hasMoreResults", False)
        if not has_more:
            break

        time.sleep(0.3)

    return all_tickets[:max_results]


def fetch_sr_tickets_via_object_query(
    headers: dict[str, str],
    status: str,
    max_results: int = 500,
) -> list[dict[str, Any]]:
    """
    Fetch SR tickets using the object query endpoint (supports string filters).
    This is used for SR tickets where we don't have status GUIDs.
    """
    url = f"{ATHENA_BASE_URL}v1/object/query"
    params = {
        "type": "servicerequest",
        "$filter": f"Status eq '{status}'",
        "$orderby": "CreatedDate Desc",
        "$top": min(max_results, 1000),
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        results = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(results, list):
            return results[:max_results]
        return results.get("result", [])[:max_results] if isinstance(results, dict) else []
    except Exception as exc:
        logger.warning("  Failed to fetch SR tickets (status=%s): %s", status, exc)
        return []


def fetch_ticket_ids_via_view(
    headers: dict[str, str],
    ticket_type: str,
    sample_size: int,
    days_back: int = 90,
) -> list[dict[str, Any]]:
    """
    Fetch ticket IDs for resolved/closed tickets.

    For IR: Uses the view endpoint with status GUIDs (proven to work).
    For SR: Uses the object query endpoint with string-based filters.

    Returns a list of basic ticket records (with id, title, status, etc.).
    """
    all_tickets: list[dict[str, Any]] = []
    target_pool = sample_size * 3  # Fetch 3x for random sampling

    if ticket_type == "IR":
        view_url = INCIDENT_VIEW_URL
        logger.info("Fetching IR tickets (Resolved) via view endpoint...")
        resolved = fetch_tickets_via_view_single_status(
            headers, view_url, IR_STATUS_RESOLVED, max_results=target_pool,
        )
        logger.info("  Got %d Resolved IR tickets", len(resolved))
        all_tickets.extend(resolved)

        # Also fetch Closed if we need more
        if len(all_tickets) < target_pool:
            logger.info("Fetching IR tickets (Closed) via view endpoint...")
            closed = fetch_tickets_via_view_single_status(
                headers, view_url, IR_STATUS_CLOSED,
                max_results=target_pool - len(all_tickets),
            )
            logger.info("  Got %d Closed IR tickets", len(closed))
            all_tickets.extend(closed)

    else:  # SR
        logger.info("Fetching SR tickets (Completed) via object query...")
        completed = fetch_sr_tickets_via_object_query(
            headers, "Completed", max_results=target_pool,
        )
        logger.info("  Got %d Completed SR tickets", len(completed))
        all_tickets.extend(completed)

        if len(all_tickets) < target_pool:
            logger.info("Fetching SR tickets (Closed) via object query...")
            closed = fetch_sr_tickets_via_object_query(
                headers, "Closed", max_results=target_pool - len(all_tickets),
            )
            logger.info("  Got %d Closed SR tickets", len(closed))
            all_tickets.extend(closed)

    logger.info("Total pool: %d %s tickets", len(all_tickets), ticket_type)
    return all_tickets


def fetch_ticket_detail_sync(
    ticket_id: str,
    ticket_type: str,
    headers: dict[str, str],
) -> dict[str, Any] | None:
    """Fetch full ticket detail via GET /v1/incident/{id} or /v1/servicerequest/{id}."""
    if ticket_type == "IR":
        url = f"{INCIDENT_URL}{ticket_id}"
    else:
        url = f"{SERVICEREQUEST_URL}{ticket_id}"

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Failed to fetch detail for %s: %s", ticket_id, exc)
        return None


def extract_ground_truth(raw: dict[str, Any], ticket_id: str, ticket_type: str) -> dict[str, Any]:
    """Extract ground truth fields from a raw Athena ticket response."""
    # Support group — for resolved/closed tickets, supportGroup is often None
    # but tierQueue retains the last assigned group
    support_group = raw.get("supportGroup")
    if isinstance(support_group, dict):
        support_group_name = support_group.get("name") or support_group.get("displayName")
    elif support_group and isinstance(support_group, str) and len(support_group) < 50:
        # Might be a GUID, not a name — skip it
        support_group_name = None
    else:
        support_group_name = support_group

    # Fallback to tierQueue if supportGroup is empty
    if not support_group_name:
        tier_queue = raw.get("tierQueue")
        if isinstance(tier_queue, dict):
            support_group_name = tier_queue.get("name") or tier_queue.get("displayName")
        elif tier_queue and isinstance(tier_queue, str):
            support_group_name = tier_queue

    # Priority
    priority = raw.get("priority")
    if isinstance(priority, dict):
        priority_value = priority.get("name") or priority.get("displayName")
    else:
        priority_value = priority

    # Status
    status = raw.get("status")
    if isinstance(status, dict):
        status_value = status.get("name") or status.get("displayName")
    else:
        status_value = status

    # Location
    location = raw.get("location")
    if isinstance(location, dict):
        location_value = location.get("name") or location.get("displayName")
    else:
        location_value = location

    # Affected user
    affected_user_raw = raw.get("affectedUser")
    affected_user = None
    affected_user_title = None
    if isinstance(affected_user_raw, dict):
        affected_user = affected_user_raw.get("displayName") or affected_user_raw.get("userName")
        affected_user_title = affected_user_raw.get("title")

    # Created date
    created_date = raw.get("createdDate")

    return {
        "ticket_id": ticket_id,
        "ticket_type": ticket_type,
        "title": raw.get("title"),
        "description": raw.get("description"),
        "status": status_value,
        "actual_support_group": support_group_name,
        "actual_priority": priority_value,
        "location": location_value,
        "affected_user": affected_user,
        "affected_user_title": affected_user_title,
        "created_date": created_date,
    }


def collect_ground_truth_dataset(
    sample_size: int = 500,
    ticket_types: list[str] | None = None,
    days_back: int = 90,
) -> list[dict[str, Any]]:
    """
    Phase 1: Collect ground truth dataset from Athena.

    Fetches resolved/closed tickets, randomly samples, and fetches full details.
    """
    if ticket_types is None:
        ticket_types = ["IR", "SR"]

    headers = get_auth_headers()
    if not headers:
        logger.error("Failed to authenticate with Athena. Aborting.")
        sys.exit(1)

    # Determine per-type sample sizes
    if len(ticket_types) == 2:
        ir_size = sample_size // 2
        sr_size = sample_size - ir_size
        type_sizes = {"IR": ir_size, "SR": sr_size}
    else:
        type_sizes = {ticket_types[0]: sample_size}

    dataset: list[dict[str, Any]] = []

    for ttype, target_count in type_sizes.items():
        logger.info("=" * 60)
        logger.info("Collecting %d %s tickets...", target_count, ttype)
        logger.info("=" * 60)

        # Step 1: Fetch a pool of ticket IDs
        pool = fetch_ticket_ids_via_view(headers, ttype, target_count, days_back)

        if not pool:
            logger.warning("No %s tickets found. Skipping.", ttype)
            continue

        # Step 2: Random sample from the pool
        if len(pool) > target_count:
            sampled = random.sample(pool, target_count)
        else:
            sampled = pool
            logger.warning(
                "Pool (%d) smaller than target (%d). Using all available.",
                len(pool), target_count,
            )

        # Step 3: Fetch full details for each sampled ticket
        logger.info("Fetching full details for %d %s tickets...", len(sampled), ttype)
        for i, ticket_summary in enumerate(sampled, 1):
            # View endpoint may return 'Id' (capital) or 'id' (lowercase)
            ticket_id = (
                ticket_summary.get("id")
                or ticket_summary.get("Id")
                or ticket_summary.get("ID")
                or ""
            )
            if not ticket_id:
                logger.debug("Skipping ticket with no ID. Keys: %s", list(ticket_summary.keys())[:10])
                continue

            raw = fetch_ticket_detail_sync(ticket_id, ttype, headers)
            if raw is None:
                continue

            ground_truth = extract_ground_truth(raw, ticket_id, ttype)

            # Skip tickets without a support group (can't score them)
            if not ground_truth["actual_support_group"]:
                logger.debug("Skipping %s — no support group assigned.", ticket_id)
                continue

            dataset.append(ground_truth)

            if i % 25 == 0 or i == len(sampled):
                logger.info("  Progress: %d/%d %s tickets fetched", i, len(sampled), ttype)

            # Small delay between API calls
            time.sleep(0.3)

    logger.info("Ground truth dataset: %d tickets collected", len(dataset))
    return dataset


def save_dataset(dataset: list[dict[str, Any]], path: Path) -> None:
    """Save the ground truth dataset to JSON."""
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
    logger.info("Dataset saved to %s (%d tickets)", path, len(dataset))


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Load a cached ground truth dataset from JSON."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickets = data.get("tickets", [])
    logger.info("Loaded cached dataset from %s (%d tickets)", path, len(tickets))
    return tickets


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2: Run Blind Recommendations & Score
# ═══════════════════════════════════════════════════════════════════════


async def run_single_recommendation(
    ticket: dict[str, Any],
    assignment_service: AssignmentService,
    databricks_client: DatabricksClient,
) -> dict[str, Any]:
    """
    Run the assignment recommendation pipeline for a single ticket,
    with support_group and priority stripped (blind test).

    Returns a result dict with ground truth, prediction, and match info.
    """
    ticket_id = ticket["ticket_id"]
    ticket_type_str = ticket["ticket_type"]
    ticket_type = "incident" if ticket_type_str == "IR" else "servicerequest"

    # Build a TicketInfo with support_group and priority STRIPPED
    ticket_info = TicketInfo(
        id=ticket_id,
        ticket_type=ticket_type,
        title=ticket.get("title"),
        description=ticket.get("description"),
        status=ticket.get("status"),
        priority=None,  # STRIPPED for blind test
        support_group=None,  # STRIPPED for blind test
        affected_user=ticket.get("affected_user"),
        affected_user_title=ticket.get("affected_user_title"),
        location=ticket.get("location"),
        created_date=ticket.get("created_date"),
    )

    # Build search text and generate embedding
    search_text = AssignmentService._build_search_text(ticket_info)
    query_embedding = await databricks_client.generate_embedding(search_text)

    # Run semantic search (sync methods, run in executor)
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

    # Select support groups based on ticket type
    support_groups = IR_SUPPORT_GROUPS if ticket_type_str == "IR" else SR_SUPPORT_GROUPS

    # Build context and LLM messages
    context = AssignmentService._build_context(doc_results, ticket_results)
    messages = AssignmentService._build_llm_messages(
        ticket_info=ticket_info,
        support_groups=support_groups,
        context=context,
    )

    # Call LLM
    llm_response = await databricks_client.call_llm(messages, max_tokens=2048)

    # Parse recommendation
    recommendation = AssignmentService._parse_recommendation(llm_response, support_groups)

    # Build result
    return {
        "ticket_id": ticket_id,
        "ticket_type": ticket_type_str,
        "title": ticket.get("title", ""),
        "location": ticket.get("location", ""),
        # Ground truth
        "actual_support_group": ticket["actual_support_group"],
        "actual_priority": ticket["actual_priority"],
        # Predictions
        "predicted_support_group": recommendation.support_group_name,
        "predicted_priority": str(recommendation.priority),
        "rationale": recommendation.rationale,
    }


def score_support_group(actual: str, predicted: str) -> dict[str, bool]:
    """
    Score a support group prediction with multiple match levels.

    Returns dict with:
        exact_match: Exact string match (case-insensitive)
        leaf_match: The leaf (last segment) of predicted path matches actual
        contains_match: Actual name appears anywhere in the predicted path
        hierarchical_match: Predicted is a parent/child of actual
        top_level_match: Same top-level category (before first backslash)
    """
    if not actual or not predicted:
        return {
            "exact_match": False, "leaf_match": False, "contains_match": False,
            "hierarchical_match": False, "top_level_match": False,
        }

    actual_lower = actual.strip().lower()
    predicted_lower = predicted.strip().lower()

    # Exact match
    exact = actual_lower == predicted_lower

    # Leaf match: last segment of predicted path matches actual
    # e.g., actual="PMUC", predicted="EUS\PMUC" → leaf="pmuc" → match
    predicted_leaf = predicted_lower.rsplit("\\", 1)[-1]
    actual_leaf = actual_lower.rsplit("\\", 1)[-1]
    leaf_match = exact or actual_lower == predicted_leaf or actual_leaf == predicted_leaf

    # Contains match: actual name appears as a segment in the predicted path
    predicted_segments = [s.strip() for s in predicted_lower.split("\\")]
    contains_match = leaf_match or actual_lower in predicted_segments

    # Hierarchical match: one is a prefix of the other
    hierarchical = (
        exact
        or actual_lower.startswith(predicted_lower + "\\")
        or predicted_lower.startswith(actual_lower + "\\")
    )

    # Top-level match: same root category
    actual_top = actual_lower.split("\\")[0]
    predicted_top = predicted_lower.split("\\")[0]
    top_level = actual_top == predicted_top

    return {
        "exact_match": exact,
        "leaf_match": leaf_match,
        "contains_match": contains_match,
        "hierarchical_match": hierarchical,
        "top_level_match": top_level,
    }


def score_priority(actual: Any, predicted: str, ticket_type: str) -> dict[str, bool]:
    """
    Score a priority prediction.

    Returns dict with:
        exact_match: Exact match
        within_one: Within 1 level (for numeric IR priorities)
    """
    if actual is None or predicted is None:
        return {"exact_match": False, "within_one": False}

    actual_str = str(actual).strip().lower()
    predicted_str = predicted.strip().lower()

    exact = actual_str == predicted_str

    # For IR tickets, priorities are numeric (1-4)
    within_one = exact
    if ticket_type == "IR":
        try:
            actual_num = int(actual_str)
            predicted_num = int(predicted_str)
            within_one = abs(actual_num - predicted_num) <= 1
        except (ValueError, TypeError):
            within_one = exact
    else:
        # For SR, priorities are strings: Immediate, High, Medium, Low
        sr_order = {"immediate": 0, "high": 1, "medium": 2, "low": 3}
        actual_rank = sr_order.get(actual_str)
        predicted_rank = sr_order.get(predicted_str)
        if actual_rank is not None and predicted_rank is not None:
            within_one = abs(actual_rank - predicted_rank) <= 1
        else:
            within_one = exact

    return {"exact_match": exact, "within_one": within_one}


async def run_benchmark(
    dataset: list[dict[str, Any]],
    resume_from: int = 0,
) -> list[dict[str, Any]]:
    """
    Phase 2: Run blind recommendations for all tickets in the dataset.

    Supports resuming from a specific index.
    """
    settings = Settings()
    athena_client = AthenaClient(settings)
    databricks_client = DatabricksClient(settings)
    assignment_service = AssignmentService(athena_client, databricks_client)

    results: list[dict[str, Any]] = []

    # Load existing results if resuming
    if resume_from > 0 and RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        results = existing.get("results", [])
        logger.info("Resuming from index %d (loaded %d existing results)", resume_from, len(results))

    total = len(dataset)
    logger.info("=" * 60)
    logger.info("Running blind recommendations for %d tickets (starting at %d)...", total - resume_from, resume_from)
    logger.info("=" * 60)

    for i in range(resume_from, total):
        ticket = dataset[i]
        ticket_id = ticket["ticket_id"]

        try:
            logger.info(
                "[%d/%d] Processing %s (%s)...",
                i + 1, total, ticket_id, ticket["ticket_type"],
            )

            result = await run_single_recommendation(
                ticket, assignment_service, databricks_client,
            )

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
            sg_emoji = "✅" if sg_scores["exact_match"] else ("🟡" if sg_scores["hierarchical_match"] else "❌")
            pri_emoji = "✅" if pri_scores["exact_match"] else ("🟡" if pri_scores["within_one"] else "❌")
            logger.info(
                "  SG: %s actual='%s' predicted='%s'",
                sg_emoji, result["actual_support_group"], result["predicted_support_group"],
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

        # Save progress after each ticket (resume support)
        save_results(results)

        # Rate limiting delay between LLM calls
        if i < total - 1:
            time.sleep(LLM_DELAY_SECONDS)

    # Cleanup
    await athena_client.close()
    await databricks_client.close()

    return results


def save_results(results: list[dict[str, Any]]) -> None:
    """Save benchmark results to JSON (called after each ticket for resume support)."""
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now().isoformat(),
                "count": len(results),
                "results": results,
            },
            f,
            indent=2,
            default=str,
        )


# ═══════════════════════════════════════════════════════════════════════
# PHASE 3: Generate Report
# ═══════════════════════════════════════════════════════════════════════


def generate_report(results: list[dict[str, Any]]) -> str:
    """Generate a human-readable accuracy report from benchmark results."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("ASSIGNMENT RECOMMENDATION ACCURACY BENCHMARK REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)

    # Filter out errored results
    valid = [r for r in results if "error" not in r]
    errored = [r for r in results if "error" in r]

    lines.append(f"\nTotal tickets processed: {len(results)}")
    lines.append(f"Successful: {len(valid)}")
    lines.append(f"Errors: {len(errored)}")

    if not valid:
        lines.append("\nNo valid results to report.")
        return "\n".join(lines)

    # ── Re-score with latest scoring logic ────────────────────────────
    for r in valid:
        r["support_group_scores"] = score_support_group(
            r.get("actual_support_group", ""),
            r.get("predicted_support_group", ""),
        )
        r["priority_scores"] = score_priority(
            r.get("actual_priority"),
            r.get("predicted_priority", ""),
            r.get("ticket_type", "IR"),
        )

    # ── Overall Support Group Accuracy ────────────────────────────────
    sg_exact = sum(1 for r in valid if r["support_group_scores"]["exact_match"])
    sg_leaf = sum(1 for r in valid if r["support_group_scores"]["leaf_match"])
    sg_contains = sum(1 for r in valid if r["support_group_scores"]["contains_match"])
    sg_hier = sum(1 for r in valid if r["support_group_scores"]["hierarchical_match"])
    sg_top = sum(1 for r in valid if r["support_group_scores"]["top_level_match"])
    n = len(valid)

    lines.append("\n" + "-" * 70)
    lines.append("SUPPORT GROUP ACCURACY")
    lines.append("-" * 70)
    lines.append(f"  Exact match:        {sg_exact:>4}/{n}  ({sg_exact/n*100:.1f}%)")
    lines.append(f"  Leaf match:         {sg_leaf:>4}/{n}  ({sg_leaf/n*100:.1f}%)  [last path segment matches]")
    lines.append(f"  Contains match:     {sg_contains:>4}/{n}  ({sg_contains/n*100:.1f}%)  [actual appears in predicted path]")
    lines.append(f"  Hierarchical match: {sg_hier:>4}/{n}  ({sg_hier/n*100:.1f}%)")
    lines.append(f"  Top-level match:    {sg_top:>4}/{n}  ({sg_top/n*100:.1f}%)")

    # ── Overall Priority Accuracy ─────────────────────────────────────
    pri_exact = sum(1 for r in valid if r.get("priority_scores", {}).get("exact_match"))
    pri_within = sum(1 for r in valid if r.get("priority_scores", {}).get("within_one"))

    lines.append("\n" + "-" * 70)
    lines.append("PRIORITY ACCURACY")
    lines.append("-" * 70)
    lines.append(f"  Exact match:   {pri_exact:>4}/{n}  ({pri_exact/n*100:.1f}%)")
    lines.append(f"  Within-1:      {pri_within:>4}/{n}  ({pri_within/n*100:.1f}%)")

    # ── IR vs SR Breakdown ────────────────────────────────────────────
    for ttype in ["IR", "SR"]:
        subset = [r for r in valid if r.get("ticket_type") == ttype]
        if not subset:
            continue

        sn = len(subset)
        t_sg_exact = sum(1 for r in subset if r.get("support_group_scores", {}).get("exact_match"))
        t_sg_hier = sum(1 for r in subset if r.get("support_group_scores", {}).get("hierarchical_match"))
        t_sg_top = sum(1 for r in subset if r.get("support_group_scores", {}).get("top_level_match"))
        t_pri_exact = sum(1 for r in subset if r.get("priority_scores", {}).get("exact_match"))
        t_pri_within = sum(1 for r in subset if r.get("priority_scores", {}).get("within_one"))

        lines.append(f"\n{'─' * 70}")
        lines.append(f"{ttype} TICKETS ({sn} tickets)")
        lines.append(f"{'─' * 70}")
        lines.append(f"  Support Group:")
        lines.append(f"    Exact match:        {t_sg_exact:>4}/{sn}  ({t_sg_exact/sn*100:.1f}%)")
        lines.append(f"    Hierarchical match: {t_sg_hier:>4}/{sn}  ({t_sg_hier/sn*100:.1f}%)")
        lines.append(f"    Top-level match:    {t_sg_top:>4}/{sn}  ({t_sg_top/sn*100:.1f}%)")
        lines.append(f"  Priority:")
        lines.append(f"    Exact match:   {t_pri_exact:>4}/{sn}  ({t_pri_exact/sn*100:.1f}%)")
        lines.append(f"    Within-1:      {t_pri_within:>4}/{sn}  ({t_pri_within/sn*100:.1f}%)")

    # ── Confusion Patterns (Most-Confused Support Groups) ─────────────
    lines.append(f"\n{'=' * 70}")
    lines.append("CONFUSION PATTERNS — MOST-CONFUSED SUPPORT GROUPS")
    lines.append(f"{'=' * 70}")

    # Count mismatches by (actual, predicted) pair
    confusion: dict[tuple[str, str], int] = {}
    for r in valid:
        if not r.get("support_group_scores", {}).get("exact_match"):
            actual = r.get("actual_support_group", "?")
            predicted = r.get("predicted_support_group", "?")
            pair = (actual, predicted)
            confusion[pair] = confusion.get(pair, 0) + 1

    # Sort by frequency
    sorted_confusion = sorted(confusion.items(), key=lambda x: x[1], reverse=True)

    if sorted_confusion:
        lines.append(f"\n  {'Count':>5}  {'Actual':<40} → {'Predicted':<40}")
        lines.append(f"  {'─'*5}  {'─'*40}   {'─'*40}")
        for (actual, predicted), count in sorted_confusion[:25]:
            actual_short = actual[:38] + ".." if len(actual) > 40 else actual
            predicted_short = predicted[:38] + ".." if len(predicted) > 40 else predicted
            lines.append(f"  {count:>5}  {actual_short:<40} → {predicted_short:<40}")
    else:
        lines.append("  No mismatches found!")

    # ── Per-Ticket Breakdown ──────────────────────────────────────────
    lines.append(f"\n{'=' * 70}")
    lines.append("PER-TICKET BREAKDOWN")
    lines.append(f"{'=' * 70}")

    for r in valid:
        sg_scores = r.get("support_group_scores", {})
        pri_scores = r.get("priority_scores", {})
        sg_mark = "✅" if sg_scores.get("exact_match") else ("🟡" if sg_scores.get("hierarchical_match") else "❌")
        pri_mark = "✅" if pri_scores.get("exact_match") else ("🟡" if pri_scores.get("within_one") else "❌")

        lines.append(f"\n  {r['ticket_id']} ({r['ticket_type']})")
        lines.append(f"    Title: {(r.get('title') or 'N/A')[:80]}")
        lines.append(f"    SG  {sg_mark}  actual='{r.get('actual_support_group', 'N/A')}'")
        lines.append(f"              predicted='{r.get('predicted_support_group', 'N/A')}'")
        lines.append(f"    Pri {pri_mark}  actual='{r.get('actual_priority', 'N/A')}'  predicted='{r.get('predicted_priority', 'N/A')}'")

    # ── Errors ────────────────────────────────────────────────────────
    if errored:
        lines.append(f"\n{'=' * 70}")
        lines.append(f"ERRORS ({len(errored)} tickets)")
        lines.append(f"{'=' * 70}")
        for r in errored:
            lines.append(f"  {r['ticket_id']}: {r.get('error', 'Unknown error')}")

    lines.append(f"\n{'=' * 70}")
    lines.append("END OF REPORT")
    lines.append(f"{'=' * 70}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Benchmark AI assignment recommendation accuracy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=500,
        help="Number of tickets to benchmark (default: 500)",
    )
    parser.add_argument(
        "--ticket-type",
        choices=["IR", "SR", "both"],
        default="both",
        help="Ticket type to benchmark (default: both)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="How many days back to look for tickets (default: 90)",
    )
    parser.add_argument(
        "--use-cached",
        action="store_true",
        help="Use cached dataset if available (skip Phase 1 fetch)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Alias for --use-cached",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from where a previous run left off",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only generate report from existing results (skip Phase 1 & 2)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between LLM calls (default: 2.0)",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    global LLM_DELAY_SECONDS
    LLM_DELAY_SECONDS = args.delay

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Determine ticket types
    if args.ticket_type == "both":
        ticket_types = ["IR", "SR"]
    else:
        ticket_types = [args.ticket_type]

    # ── Report Only Mode ──────────────────────────────────────────────
    if args.report_only:
        if not RESULTS_PATH.exists():
            logger.error("No results file found at %s. Run the benchmark first.", RESULTS_PATH)
            sys.exit(1)
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        results = data.get("results", [])
        report = generate_report(results)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
        try:
            print(report)
        except UnicodeEncodeError:
            print(report.encode("ascii", errors="replace").decode("ascii"))
        logger.info("Report saved to %s", REPORT_PATH)
        return

    # ── Phase 1: Collect Ground Truth ─────────────────────────────────
    use_cached = args.use_cached or args.skip_fetch

    if use_cached and DATASET_PATH.exists():
        dataset = load_dataset(DATASET_PATH)
    else:
        if use_cached:
            logger.warning("--use-cached specified but no cached dataset found. Fetching fresh.")
        dataset = collect_ground_truth_dataset(
            sample_size=args.sample_size,
            ticket_types=ticket_types,
            days_back=args.days_back,
        )
        save_dataset(dataset, DATASET_PATH)

    if not dataset:
        logger.error("No tickets in dataset. Aborting.")
        sys.exit(1)

    # ── Phase 2: Run Blind Recommendations ────────────────────────────
    resume_from = 0
    if args.resume and RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        resume_from = len(existing.get("results", []))
        logger.info("Resuming from ticket %d", resume_from)

    results = asyncio.run(run_benchmark(dataset, resume_from=resume_from))

    # ── Phase 3: Generate Report ──────────────────────────────────────
    report = generate_report(results)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    # Print report (handle Windows encoding issues with emojis)
    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("ascii", errors="replace").decode("ascii"))
    logger.info("Report saved to %s", REPORT_PATH)
    logger.info("Results saved to %s", RESULTS_PATH)
    logger.info("Dataset saved to %s", DATASET_PATH)


if __name__ == "__main__":
    main()