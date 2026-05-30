"""
Enrich IR Embeddings with Support Group Labels
================================================
Phase 2a of the Recommendation Accuracy Improvement Plan.

Fetches the resolved/assigned support group (tierQueue) for tickets in the
scratchpad.aslanuka.ir_embeddings Databricks table, and:
1. Saves a ticket_id → support_group mapping to JSON (for benchmark use)
2. Optionally adds a support_group column to the Databricks table

The mapping enables the benchmark to show "IR1234567 → assigned to: Service Desk"
in the similar ticket context, giving the LLM a powerful routing signal.

Strategy:
- Get all ticket IDs from ir_embeddings via Databricks SQL
- Fetch IR tickets from Athena view endpoint (all statuses) to get supportGroupValue
- For tickets not found via view, try individual GET requests (batched)
- Build mapping and save to JSON
- Optionally ALTER TABLE + UPDATE in Databricks

Usage:
    python -m exploration.enrich_embeddings
    python -m exploration.enrich_embeddings --update-table
    python -m exploration.enrich_embeddings --use-cached
    python -m exploration.enrich_embeddings --report-only

RULE: This script lives in exploration/ only. Core code in src/ is NOT edited.
"""

import argparse
import json
import logging
import os
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
from src.config import Settings  # noqa: E402
from src.clients.databricks_client import DatabricksClient  # noqa: E402

# ── Configuration ─────────────────────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env")

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
MAPPING_PATH = OUTPUT_DIR / "ticket_support_group_mapping.json"

INCIDENT_VIEW_URL = os.getenv("ATHENA_INCIDENT_VIEW_URL")
INCIDENT_URL = os.getenv("ATHENA_INCIDENT_URL")

# IR Status GUIDs for the view filter endpoint
IR_STATUSES = {
    "Active": "5e2d3932-ca6d-1515-7310-6f58584df73e",
    "Pending": "b6679968-e84e-96fa-1fec-8cd4ab39c3de",
    "Work in Progress": "9accddda-fbf5-10d4-b402-69bdd276a69b",
    "Resolved": "2b8830b6-59f0-f574-9c2a-f4b4682f1681",
    "Closed": "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf",
    "Updated by Affected User": "b7ba8903-e29b-0b2e-0b62-765c3f235c5f",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# STEP 1: Get ticket IDs from ir_embeddings
# ═══════════════════════════════════════════════════════════════════════

def get_embedding_ticket_ids(databricks_client: DatabricksClient) -> set[str]:
    """Get all ticket IDs from the ir_embeddings table."""
    logger.info("Fetching ticket IDs from scratchpad.aslanuka.ir_embeddings...")
    results = databricks_client.execute_query(
        "SELECT id FROM scratchpad.aslanuka.ir_embeddings"
    )
    ids = {r["id"] for r in results}
    logger.info("  Found %d ticket IDs in ir_embeddings", len(ids))

    # Breakdown
    ir_count = sum(1 for tid in ids if tid.startswith("IR"))
    sr_count = sum(1 for tid in ids if tid.startswith("SR"))
    other_count = len(ids) - ir_count - sr_count
    logger.info("  IR: %d, SR: %d, Other: %d", ir_count, sr_count, other_count)

    return ids


# ═══════════════════════════════════════════════════════════════════════
# STEP 2: Fetch support groups from Athena
# ═══════════════════════════════════════════════════════════════════════

def build_status_filter(status_guid: str) -> list[dict[str, Any]]:
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


def fetch_tickets_by_status(
    headers: dict[str, str],
    status_name: str,
    status_guid: str,
    target_ids: set[str] | None = None,
) -> dict[str, str]:
    """
    Fetch IR tickets for a given status via the view endpoint.
    Returns a mapping of ticket_id → support_group_name.

    The view endpoint returns flat fields including 'supportGroupValue'
    which is the human-readable support group name.
    """
    mapping: dict[str, str] = {}
    page_size = 50
    max_pages = 200  # Safety limit: 200 * 50 = 10,000 tickets per status

    filters = build_status_filter(status_guid)

    logger.info("  Fetching %s tickets...", status_name)

    for page_num in range(max_pages):
        skip = page_num * page_size
        separator = "&" if "?" in INCIDENT_VIEW_URL else "?"
        url = f"{INCIDENT_VIEW_URL}{separator}$skip={skip}&$top={page_size}"

        try:
            response = requests.post(url, headers=headers, json=filters, timeout=60)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("    Failed page %d: %s", page_num + 1, exc)
            break

        results = data.get("result", [])
        if not results:
            break

        for ticket in results:
            tid = ticket.get("id", "")
            # Skip if we have target IDs and this isn't one of them
            if target_ids and tid not in target_ids:
                continue

            # Extract support group name from view endpoint flat format
            sg_name = ticket.get("supportGroupValue")
            if not sg_name:
                # Try tierQueue field (sometimes present in view results)
                tq = ticket.get("tierQueue")
                if isinstance(tq, str) and len(tq) < 100:
                    sg_name = tq

            if sg_name and tid:
                mapping[tid] = sg_name

        has_more = data.get("hasMoreResults", False)
        if not has_more:
            break

        # Brief delay between pages
        time.sleep(0.2)

    logger.info("    Got %d tickets with support groups for %s", len(mapping), status_name)
    return mapping


def fetch_individual_tickets(
    headers: dict[str, str],
    ticket_ids: list[str],
    batch_delay: float = 0.1,
) -> dict[str, str]:
    """
    Fetch support groups for individual tickets via GET /v1/incident/{id}.
    Used as fallback for tickets not found in view endpoint results.
    """
    mapping: dict[str, str] = {}
    total = len(ticket_ids)

    logger.info("  Fetching %d individual tickets from Athena...", total)

    for i, tid in enumerate(ticket_ids):
        if i > 0 and i % 100 == 0:
            logger.info("    Progress: %d/%d (%d found so far)", i, total, len(mapping))

        try:
            url = f"{INCIDENT_URL}{tid}"
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
            raw = response.json()
            if not raw:
                continue

            # Extract support group from tierQueue (most reliable for resolved tickets)
            tier_queue = raw.get("tierQueue")
            sg_name = None
            if isinstance(tier_queue, dict):
                sg_name = tier_queue.get("name") or tier_queue.get("displayName")
            elif isinstance(tier_queue, str) and len(tier_queue) < 100:
                sg_name = tier_queue

            # Fallback to supportGroup
            if not sg_name:
                sg = raw.get("supportGroup")
                if isinstance(sg, dict):
                    sg_name = sg.get("name") or sg.get("displayName")

            if sg_name:
                mapping[tid] = sg_name

        except Exception:
            pass  # Skip failures silently

        time.sleep(batch_delay)

    logger.info("    Found support groups for %d/%d individual tickets", len(mapping), total)
    return mapping


def collect_support_group_mapping(
    target_ids: set[str],
    max_individual_lookups: int = 2000,
) -> dict[str, str]:
    """
    Collect support group mapping for all target ticket IDs.

    Strategy:
    1. Fetch IR tickets from view endpoint (all statuses) — bulk, efficient
    2. For remaining unmatched IR tickets, try individual GET requests
    3. Skip non-IR tickets (numeric IDs, SR tickets) — they're a small minority
    """
    headers = get_auth_headers()
    if not headers:
        logger.error("Failed to authenticate with Athena. Aborting.")
        sys.exit(1)

    # Filter to IR tickets only (view endpoint is for incidents)
    ir_ids = {tid for tid in target_ids if tid.startswith("IR")}
    logger.info("Target: %d IR tickets out of %d total", len(ir_ids), len(target_ids))

    full_mapping: dict[str, str] = {}

    # ── Fetch via view endpoint (bulk) ────────────────────────────────
    logger.info("\n=== Phase 1: Bulk fetch via view endpoint ===")
    for status_name, status_guid in IR_STATUSES.items():
        status_mapping = fetch_tickets_by_status(
            headers, status_name, status_guid, target_ids=ir_ids,
        )
        # Merge (don't overwrite — first match wins, which is the current status)
        for tid, sg in status_mapping.items():
            if tid not in full_mapping:
                full_mapping[tid] = sg

        # Re-authenticate periodically (tokens expire)
        if len(full_mapping) > 0 and status_name in ("Resolved", "Closed"):
            headers = get_auth_headers()
            if not headers:
                logger.error("Re-authentication failed. Continuing with existing data.")
                break

    matched_via_view = len(full_mapping)
    logger.info("\nView endpoint: matched %d/%d IR tickets (%.1f%%)",
                matched_via_view, len(ir_ids),
                matched_via_view / len(ir_ids) * 100 if ir_ids else 0)

    # ── Fetch remaining via individual GET (fallback) ─────────────────
    remaining = [tid for tid in ir_ids if tid not in full_mapping]
    if remaining and max_individual_lookups > 0:
        logger.info("\n=== Phase 2: Individual lookups for %d remaining tickets ===", len(remaining))

        # Limit individual lookups to avoid excessive API calls
        lookup_batch = remaining[:max_individual_lookups]
        if len(remaining) > max_individual_lookups:
            logger.info("  Limiting to %d lookups (of %d remaining)", max_individual_lookups, len(remaining))

        # Re-authenticate for individual lookups
        headers = get_auth_headers()
        if headers:
            individual_mapping = fetch_individual_tickets(headers, lookup_batch)
            full_mapping.update(individual_mapping)

    final_matched = len(full_mapping)
    logger.info("\n=== Final Results ===")
    logger.info("  Total IR tickets in embeddings: %d", len(ir_ids))
    logger.info("  Matched with support groups: %d (%.1f%%)",
                final_matched, final_matched / len(ir_ids) * 100 if ir_ids else 0)
    logger.info("  Unmatched: %d", len(ir_ids) - final_matched)

    return full_mapping


# ═══════════════════════════════════════════════════════════════════════
# STEP 3: Save mapping to JSON
# ═══════════════════════════════════════════════════════════════════════

def save_mapping(mapping: dict[str, str]) -> None:
    """Save the ticket → support group mapping to JSON."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Compute stats
    group_counts: dict[str, int] = {}
    for sg in mapping.values():
        group_counts[sg] = group_counts.get(sg, 0) + 1

    top_groups = sorted(group_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    data = {
        "generated_at": datetime.now().isoformat(),
        "total_tickets": len(mapping),
        "unique_groups": len(group_counts),
        "top_20_groups": [{"name": name, "count": count} for name, count in top_groups],
        "mapping": mapping,
    }

    with open(MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    logger.info("Mapping saved to %s (%d tickets, %d unique groups)",
                MAPPING_PATH, len(mapping), len(group_counts))


def load_mapping() -> dict[str, str]:
    """Load the cached mapping from JSON."""
    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    mapping = data.get("mapping", {})
    logger.info("Loaded cached mapping: %d tickets, generated %s",
                len(mapping), data.get("generated_at", "unknown"))
    return mapping


# ═══════════════════════════════════════════════════════════════════════
# STEP 4: Update Databricks table (optional)
# ═══════════════════════════════════════════════════════════════════════

def update_databricks_table(
    databricks_client: DatabricksClient,
    mapping: dict[str, str],
    batch_size: int = 500,
) -> None:
    """
    Add support_group column to ir_embeddings and populate it.

    Uses ALTER TABLE ADD COLUMNS + batched MERGE statements.
    """
    logger.info("\n=== Updating Databricks table ===")

    # Step 1: Check if column already exists
    logger.info("Checking if support_group column exists...")
    try:
        test_results = databricks_client.execute_query(
            "SELECT support_group FROM scratchpad.aslanuka.ir_embeddings LIMIT 1"
        )
        logger.info("  Column already exists.")
    except Exception:
        logger.info("  Column does not exist. Adding it...")
        databricks_client.execute_query(
            "ALTER TABLE scratchpad.aslanuka.ir_embeddings ADD COLUMNS (support_group STRING)"
        )
        logger.info("  Column added successfully.")

    # Step 2: Update in batches using temporary views + MERGE
    ticket_ids = list(mapping.keys())
    total_batches = (len(ticket_ids) + batch_size - 1) // batch_size

    logger.info("Updating %d tickets in %d batches of %d...",
                len(ticket_ids), total_batches, batch_size)

    updated_total = 0
    for batch_num in range(total_batches):
        start = batch_num * batch_size
        end = min(start + batch_size, len(ticket_ids))
        batch_ids = ticket_ids[start:end]

        # Build VALUES clause for the batch
        # Escape single quotes in support group names
        values_parts = []
        for tid in batch_ids:
            sg = mapping[tid].replace("'", "''")  # Escape single quotes
            values_parts.append(f"('{tid}', '{sg}')")

        values_str = ", ".join(values_parts)

        merge_sql = f"""
            MERGE INTO scratchpad.aslanuka.ir_embeddings AS target
            USING (SELECT * FROM VALUES {values_str} AS source(id, support_group))
            ON target.id = source.id
            WHEN MATCHED THEN UPDATE SET target.support_group = source.support_group
        """

        try:
            databricks_client.execute_query(merge_sql)
            updated_total += len(batch_ids)
            if (batch_num + 1) % 10 == 0 or batch_num == total_batches - 1:
                logger.info("  Batch %d/%d complete (%d tickets updated so far)",
                            batch_num + 1, total_batches, updated_total)
        except Exception as exc:
            logger.error("  Batch %d failed: %s", batch_num + 1, exc)

    logger.info("Table update complete: %d tickets updated", updated_total)


# ═══════════════════════════════════════════════════════════════════════
# STEP 5: Generate report
# ═══════════════════════════════════════════════════════════════════════

def generate_enrichment_report(mapping: dict[str, str], total_embeddings: int) -> str:
    """Generate a human-readable report of the enrichment results."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("EMBEDDING ENRICHMENT REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)

    lines.append(f"\nTotal tickets in ir_embeddings: {total_embeddings}")
    lines.append(f"Tickets with support group mapping: {len(mapping)}")
    coverage = len(mapping) / total_embeddings * 100 if total_embeddings else 0
    lines.append(f"Coverage: {coverage:.1f}%")

    # Group distribution
    group_counts: dict[str, int] = {}
    for sg in mapping.values():
        group_counts[sg] = group_counts.get(sg, 0) + 1

    lines.append(f"\nUnique support groups: {len(group_counts)}")

    lines.append(f"\n{'─' * 70}")
    lines.append("TOP 30 SUPPORT GROUPS BY TICKET COUNT")
    lines.append(f"{'─' * 70}")

    top_groups = sorted(group_counts.items(), key=lambda x: x[1], reverse=True)[:30]
    for i, (name, count) in enumerate(top_groups, 1):
        pct = count / len(mapping) * 100
        lines.append(f"  {i:>3}. {name:<55} {count:>5} ({pct:.1f}%)")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich ir_embeddings with support group labels",
    )
    parser.add_argument(
        "--use-cached",
        action="store_true",
        help="Use cached mapping from JSON instead of re-fetching from Athena",
    )
    parser.add_argument(
        "--update-table",
        action="store_true",
        help="Also update the Databricks ir_embeddings table with support_group column",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only generate report from existing mapping",
    )
    parser.add_argument(
        "--max-individual",
        type=int,
        default=2000,
        help="Max individual Athena lookups for unmatched tickets (default: 2000)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    databricks_client = DatabricksClient(settings)

    # ── Report Only Mode ──────────────────────────────────────────────
    if args.report_only:
        if not MAPPING_PATH.exists():
            logger.error("No mapping file found at %s. Run enrichment first.", MAPPING_PATH)
            sys.exit(1)
        mapping = load_mapping()
        embedding_ids = get_embedding_ticket_ids(databricks_client)
        report = generate_enrichment_report(mapping, len(embedding_ids))
        print(report)
        report_path = OUTPUT_DIR / "enrichment_report.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("Report saved to %s", report_path)
        return

    # ── Get embedding ticket IDs ──────────────────────────────────────
    embedding_ids = get_embedding_ticket_ids(databricks_client)

    # ── Load or collect mapping ───────────────────────────────────────
    if args.use_cached and MAPPING_PATH.exists():
        mapping = load_mapping()
    else:
        logger.info("\n" + "=" * 60)
        logger.info("Collecting support group mapping from Athena...")
        logger.info("=" * 60)
        mapping = collect_support_group_mapping(
            embedding_ids,
            max_individual_lookups=args.max_individual,
        )
        save_mapping(mapping)

    # ── Generate report ───────────────────────────────────────────────
    report = generate_enrichment_report(mapping, len(embedding_ids))
    print(report)
    report_path = OUTPUT_DIR / "enrichment_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("Report saved to %s", report_path)

    # ── Optionally update Databricks table ────────────────────────────
    if args.update_table:
        update_databricks_table(databricks_client, mapping)

    logger.info("\nDone!")


if __name__ == "__main__":
    main()