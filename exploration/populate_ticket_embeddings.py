"""
Ticket Embeddings Population Pipeline
=======================================
Reads tickets from prepared.ticketing.athena_tickets, generates embeddings
via Databricks GTE-Large-EN endpoint, and inserts into the permanent table
hive_metastore.embeddings_db.ticket_embeddings.

Usage:
    # Initial subset load (10K most recent tickets)
    python -m exploration.populate_ticket_embeddings --limit 10000

    # Full load (all tickets)
    python -m exploration.populate_ticket_embeddings

    # Incremental mode (only new tickets not yet embedded)
    python -m exploration.populate_ticket_embeddings --mode incremental

    # Resume after interruption (same as incremental)
    python -m exploration.populate_ticket_embeddings --mode incremental --limit 5000

    # Dry run (show what would be processed without inserting)
    python -m exploration.populate_ticket_embeddings --dry-run --limit 100
"""

import argparse
import os
import sys
import io
import time
import json
from datetime import datetime

import httpx
from dotenv import load_dotenv
from databricks import sql as databricks_sql

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABRICKS_SERVER_HOSTNAME = os.getenv('DATABRICKS_SERVER_HOSTNAME')
DATABRICKS_HTTP_PATH = os.getenv('DATABRICKS_HTTP_PATH')
DATABRICKS_API_KEY = os.getenv('DATABRICKS_API_KEY')
DATABRICKS_EMBEDDING_URL = os.getenv('DATABRICKS_EMBEDDING_URL')

# Table names
SOURCE_TABLE = "prepared.ticketing.athena_tickets"
TARGET_TABLE = "hive_metastore.embeddings_db.ticket_embeddings"

# Embedding config
EMBEDDING_BATCH_SIZE = 50  # Max texts per API call
INSERT_BATCH_SIZE = 50     # Rows per INSERT statement
MAX_TEXT_LENGTH = 2000     # Truncate text to avoid token limits
DELAY_BETWEEN_BATCHES = 0.5  # Seconds between embedding API calls


def get_connection():
    """Create a Databricks SQL connection."""
    return databricks_sql.connect(
        server_hostname=DATABRICKS_SERVER_HOSTNAME,
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_API_KEY
    )


def generate_embeddings(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings via Databricks GTE-Large-EN serving endpoint.
    
    Args:
        texts: List of text strings (max 50 per call).
    
    Returns:
        List of 1024-dim embedding vectors.
    """
    headers = {
        "Authorization": f"Bearer {DATABRICKS_API_KEY}",
        "Content-Type": "application/json",
    }
    
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            DATABRICKS_EMBEDDING_URL,
            headers=headers,
            json={"input": texts},
        )
        response.raise_for_status()
        data = response.json()
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_data]


def build_search_text(title: str | None, description: str | None) -> str:
    """Build the text to embed from title + description."""
    parts = []
    if title:
        parts.append(title.strip())
    if description:
        parts.append(description.strip())
    text = " ".join(parts)
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    return text


def get_already_embedded_ids() -> set[str]:
    """Get the set of ticket IDs already in the target table."""
    print("  Fetching already-embedded ticket IDs...")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT Id FROM {TARGET_TABLE}")
            rows = cursor.fetchall()
            ids = {row[0] for row in rows}
            print(f"  Found {len(ids)} tickets already embedded.")
            return ids


def fetch_source_tickets(limit: int | None = None, exclude_ids: set[str] | None = None) -> list[dict]:
    """
    Fetch tickets from the source table.
    
    Args:
        limit: Max number of tickets to fetch.
        exclude_ids: Set of IDs to skip (already embedded).
    
    Returns:
        List of ticket dicts.
    """
    # Build query — order by CreatedDate DESC to get most recent first
    # NOTE: Priority is excluded because the VIEW has a type conflict
    # (Incidents=BIGINT, ServiceRequests=STRING) that causes UNION ALL to fail.
    # We query each ticket type separately and UNION them ourselves.
    query = f"""
        SELECT 
            TicketType, Location, Floor, Room, CreatedDate, ResolvedDate,
            Id, Title, Description, SupportGroup, Source, Status,
            Impact, Urgency, AssignedToUserName, AssignedToBaseManagedEntityId,
            AffectedUserName, AffectedBaseManagedEntityId, LastModifiedDate,
            Escalated, First_Call_Resolution, `Classification/Area`,
            ResolutionCategory, ResolutionNotes, CommandCenter,
            ConfirmedResolution, Increments, FeedbackValue, Feedback_Notes,
            Tags, Specialty, Next_Steps, User_Assign_Change, Support_Group_Change
        FROM {SOURCE_TABLE}
        WHERE Title IS NOT NULL OR Description IS NOT NULL
        ORDER BY CreatedDate DESC
    """
    
    if limit:
        query += f"\n        LIMIT {limit}"
    
    print(f"  Fetching tickets from {SOURCE_TABLE}...")
    if limit:
        print(f"  (limited to {limit} most recent tickets)")
    
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            
            tickets = []
            for row in rows:
                ticket = dict(zip(columns, row))
                # Skip if already embedded
                if exclude_ids and ticket['Id'] in exclude_ids:
                    continue
                tickets.append(ticket)
            
            print(f"  Fetched {len(rows)} tickets from source, {len(tickets)} need embedding.")
            return tickets


def escape_sql_string(value) -> str:
    """Escape a value for SQL insertion."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"
    # String — escape single quotes
    s = str(value).replace("'", "''").replace("\\", "\\\\")
    return f"'{s}'"


def build_insert_sql(tickets: list[dict], embeddings: list[list[float]]) -> str:
    """Build an INSERT statement for a batch of tickets with embeddings."""
    columns = [
        "TicketType", "Location", "Floor", "Room", "CreatedDate", "ResolvedDate",
        "Id", "Title", "Description", "SupportGroup", "Source", "Status",
        "Impact", "Urgency", "AssignedToUserName", "AssignedToBaseManagedEntityId",
        "AffectedUserName", "AffectedBaseManagedEntityId", "LastModifiedDate",
        "Escalated", "First_Call_Resolution", "`Classification/Area`",
        "ResolutionCategory", "ResolutionNotes", "CommandCenter",
        "ConfirmedResolution", "Increments", "FeedbackValue", "Feedback_Notes",
        "Tags", "Specialty", "Next_Steps", "User_Assign_Change", "Support_Group_Change",
        "embedding", "embedded_at"
    ]
    
    # Map source column names (some differ due to backtick quoting)
    source_keys = [
        "TicketType", "Location", "Floor", "Room", "CreatedDate", "ResolvedDate",
        "Id", "Title", "Description", "SupportGroup", "Source", "Status",
        "Impact", "Urgency", "AssignedToUserName", "AssignedToBaseManagedEntityId",
        "AffectedUserName", "AffectedBaseManagedEntityId", "LastModifiedDate",
        "Escalated", "First_Call_Resolution", "Classification/Area",
        "ResolutionCategory", "ResolutionNotes", "CommandCenter",
        "ConfirmedResolution", "Increments", "FeedbackValue", "Feedback_Notes",
        "Tags", "Specialty", "Next_Steps", "User_Assign_Change", "Support_Group_Change"
    ]
    
    value_rows = []
    for ticket, embedding in zip(tickets, embeddings):
        values = []
        for key in source_keys:
            values.append(escape_sql_string(ticket.get(key)))
        
        # Add embedding as array literal
        embedding_str = "array(" + ", ".join(f"CAST({v:.20f} AS DOUBLE)" for v in embedding) + ")"
        values.append(embedding_str)
        
        # Add embedded_at timestamp
        values.append("current_timestamp()")
        
        value_rows.append(f"({', '.join(values)})")
    
    col_list = ", ".join(columns)
    values_list = ",\n".join(value_rows)
    
    return f"INSERT INTO {TARGET_TABLE} ({col_list}) VALUES\n{values_list}"


def process_batch(tickets: list[dict], batch_num: int, total_batches: int, dry_run: bool = False) -> int:
    """
    Process a batch of tickets: generate embeddings and insert.
    
    Returns:
        Number of tickets successfully inserted.
    """
    # Build texts for embedding
    texts = []
    valid_tickets = []
    for ticket in tickets:
        text = build_search_text(ticket.get('Title'), ticket.get('Description'))
        if text.strip():
            texts.append(text)
            valid_tickets.append(ticket)
    
    if not texts:
        print(f"  Batch {batch_num}/{total_batches}: No valid text to embed, skipping.")
        return 0
    
    # Generate embeddings
    try:
        embeddings = generate_embeddings(texts)
    except Exception as e:
        print(f"  Batch {batch_num}/{total_batches}: EMBEDDING ERROR: {e}")
        return 0
    
    if dry_run:
        print(f"  Batch {batch_num}/{total_batches}: [DRY RUN] Would insert {len(valid_tickets)} tickets")
        return len(valid_tickets)
    
    # Insert into target table
    try:
        sql = build_insert_sql(valid_tickets, embeddings)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
        print(f"  Batch {batch_num}/{total_batches}: Inserted {len(valid_tickets)} tickets")
        return len(valid_tickets)
    except Exception as e:
        print(f"  Batch {batch_num}/{total_batches}: INSERT ERROR: {e}")
        # Try inserting one at a time to identify the problematic row
        inserted = 0
        for i, (ticket, embedding) in enumerate(zip(valid_tickets, embeddings)):
            try:
                sql = build_insert_sql([ticket], [embedding])
                with get_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(sql)
                inserted += 1
            except Exception as e2:
                print(f"    Row {i} (Id={ticket.get('Id')}): FAILED: {str(e2)[:100]}")
        print(f"  Batch {batch_num}/{total_batches}: Recovered {inserted}/{len(valid_tickets)} via individual inserts")
        return inserted


def main():
    parser = argparse.ArgumentParser(description="Populate ticket embeddings table")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental",
                        help="full=fetch all tickets; incremental=skip already-embedded (default: incremental)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of tickets to process (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without inserting")
    parser.add_argument("--batch-size", type=int, default=EMBEDDING_BATCH_SIZE,
                        help=f"Texts per embedding API call (default: {EMBEDDING_BATCH_SIZE})")
    parser.add_argument("--delay", type=float, default=DELAY_BETWEEN_BATCHES,
                        help=f"Seconds between API calls (default: {DELAY_BETWEEN_BATCHES})")
    args = parser.parse_args()

    print("=" * 70)
    print("  Ticket Embeddings Population Pipeline")
    print("=" * 70)
    print(f"  Source: {SOURCE_TABLE}")
    print(f"  Target: {TARGET_TABLE}")
    print(f"  Mode: {args.mode}")
    print(f"  Limit: {args.limit or 'None (all tickets)'}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Delay: {args.delay}s between batches")
    print(f"  Dry run: {args.dry_run}")
    print()

    # Step 1: Get already-embedded IDs (for incremental mode)
    exclude_ids = None
    if args.mode == "incremental":
        exclude_ids = get_already_embedded_ids()

    # Step 2: Fetch source tickets
    # For incremental mode with a limit, fetch more than the limit
    # since some will be filtered out as already-embedded
    fetch_limit = args.limit
    if args.mode == "incremental" and args.limit and exclude_ids:
        # Fetch extra to account for already-embedded tickets being filtered
        fetch_limit = args.limit + len(exclude_ids)
    
    tickets = fetch_source_tickets(limit=fetch_limit, exclude_ids=exclude_ids)
    
    if not tickets:
        print("\n  No tickets to process. Table is up to date!")
        return
    
    # Apply limit after filtering
    if args.limit and len(tickets) > args.limit:
        tickets = tickets[:args.limit]
        print(f"  Limited to {args.limit} tickets after filtering.")
    
    # Step 3: Process in batches
    total_tickets = len(tickets)
    batch_size = args.batch_size
    total_batches = (total_tickets + batch_size - 1) // batch_size
    
    print(f"\n  Processing {total_tickets} tickets in {total_batches} batches...")
    print(f"  Estimated time: ~{total_batches * (args.delay + 2):.0f} seconds")
    print()
    
    total_inserted = 0
    start_time = time.time()
    
    for batch_num in range(1, total_batches + 1):
        batch_start = (batch_num - 1) * batch_size
        batch_end = min(batch_start + batch_size, total_tickets)
        batch = tickets[batch_start:batch_end]
        
        inserted = process_batch(batch, batch_num, total_batches, dry_run=args.dry_run)
        total_inserted += inserted
        
        # Progress update every 10 batches
        if batch_num % 10 == 0 or batch_num == total_batches:
            elapsed = time.time() - start_time
            rate = total_inserted / elapsed if elapsed > 0 else 0
            remaining = (total_tickets - batch_end) / rate if rate > 0 else 0
            print(f"\n  --- Progress: {batch_end}/{total_tickets} processed, "
                  f"{total_inserted} inserted, "
                  f"{rate:.1f} tickets/sec, "
                  f"~{remaining:.0f}s remaining ---\n")
        
        # Delay between batches (except last)
        if batch_num < total_batches:
            time.sleep(args.delay)
    
    # Final summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print("  COMPLETE")
    print("=" * 70)
    print(f"  Total tickets processed: {total_tickets}")
    print(f"  Total inserted: {total_inserted}")
    print(f"  Failed: {total_tickets - total_inserted}")
    print(f"  Time elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Rate: {total_inserted/elapsed:.1f} tickets/sec" if elapsed > 0 else "")
    print(f"  Target table: {TARGET_TABLE}")
    
    if not args.dry_run:
        # Verify final count
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) as cnt FROM {TARGET_TABLE}")
                count = cursor.fetchone()[0]
                print(f"  Total rows in target table: {count}")
    
    print("=" * 70)


if __name__ == '__main__':
    main()