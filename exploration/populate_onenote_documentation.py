"""
Populate hive_metastore.embeddings_db.onenote_documentation from onenote_documentation.jsonl.

This script:
1. Creates the permanent table if it doesn't exist
2. Reads all 6,709 records from the .jsonl file
3. Inserts them in batches into Databricks

Usage:
    python -m exploration.populate_onenote_documentation
    python -m exploration.populate_onenote_documentation --dry-run
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from databricks import sql as databricks_sql

TABLE = "hive_metastore.embeddings_db.onenote_documentation"
JSONL_PATH = Path(__file__).resolve().parent.parent / "onenote_documentation.jsonl"
BATCH_SIZE = 50


def get_connection():
    return databricks_sql.connect(
        server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_API_KEY"),
    )


def create_table(cursor):
    """Create the onenote_documentation table if it doesn't exist."""
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            title STRING,
            content STRING,
            notebook STRING,
            section STRING,
            embeddings ARRAY<DOUBLE>
        )
    """)
    print(f"  Table {TABLE} ready.")


def load_records(jsonl_path: Path) -> list[dict]:
    """Load all records from the .jsonl file."""
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def escape_sql_string(s: str) -> str:
    """Escape a string for SQL insertion."""
    if s is None:
        return "NULL"
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_insert_sql(batch: list[dict]) -> str:
    """Build a multi-row INSERT statement for a batch of records."""
    rows = []
    for rec in batch:
        title = escape_sql_string(rec.get("title", ""))
        content = escape_sql_string(rec.get("content", ""))
        notebook = escape_sql_string(rec.get("notebook", ""))
        section = escape_sql_string(rec.get("section", ""))
        emb = rec.get("embeddings", [])
        emb_str = "ARRAY(" + ", ".join(f"CAST({v} AS DOUBLE)" for v in emb) + ")"
        rows.append(f"({title}, {content}, {notebook}, {section}, {emb_str})")

    values = ",\n".join(rows)
    return f"INSERT INTO {TABLE} (title, content, notebook, section, embeddings) VALUES\n{values}"


def main():
    parser = argparse.ArgumentParser(description="Populate onenote_documentation table")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch size")
    args = parser.parse_args()

    print("=" * 60)
    print("  OneNote Documentation Population Pipeline")
    print("=" * 60)
    print(f"  Source: {JSONL_PATH}")
    print(f"  Target: {TABLE}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Dry run: {args.dry_run}")
    print()

    # Load records
    print("  Loading records from .jsonl...")
    records = load_records(JSONL_PATH)
    print(f"  Loaded {len(records)} records.")
    print()

    if args.dry_run:
        print("  [DRY RUN] Would insert {len(records)} records.")
        print("  Sample SQL (first record):")
        sql = build_insert_sql(records[:1])
        print(f"  {sql[:500]}...")
        return

    # Connect and create table
    conn = get_connection()
    cursor = conn.cursor()

    print("  Creating table if not exists...")
    create_table(cursor)

    # Check current row count
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE}")
    existing = cursor.fetchone()[0]
    print(f"  Existing rows in table: {existing}")

    if existing > 0:
        print(f"  WARNING: Table already has {existing} rows.")
        print("  Dropping and recreating to avoid duplicates...")
        cursor.execute(f"DROP TABLE IF EXISTS {TABLE}")
        create_table(cursor)

    # Insert in batches
    total_batches = (len(records) + args.batch_size - 1) // args.batch_size
    print(f"\n  Inserting {len(records)} records in {total_batches} batches...")
    start_time = time.time()
    inserted = 0

    for i in range(0, len(records), args.batch_size):
        batch = records[i : i + args.batch_size]
        batch_num = (i // args.batch_size) + 1

        try:
            sql = build_insert_sql(batch)
            cursor.execute(sql)
            inserted += len(batch)
        except Exception as e:
            print(f"  ERROR in batch {batch_num}: {e}")
            # Try individual inserts for this batch
            for rec in batch:
                try:
                    sql = build_insert_sql([rec])
                    cursor.execute(sql)
                    inserted += 1
                except Exception as e2:
                    print(f"    SKIP record '{rec.get('title', '?')}': {e2}")

        elapsed = time.time() - start_time
        rate = inserted / elapsed if elapsed > 0 else 0
        remaining = (len(records) - inserted) / rate if rate > 0 else 0

        if batch_num % 10 == 0 or batch_num == total_batches:
            print(
                f"  Batch {batch_num}/{total_batches}: "
                f"{inserted}/{len(records)} inserted, "
                f"{rate:.1f} rec/sec, ~{remaining:.0f}s remaining"
            )

    # Verify
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE}")
    final_count = cursor.fetchone()[0]

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print("  COMPLETE")
    print("=" * 60)
    print(f"  Records inserted: {inserted}")
    print(f"  Final table row count: {final_count}")
    print(f"  Time elapsed: {elapsed:.1f}s")
    print(f"  Rate: {inserted / elapsed:.1f} rec/sec")
    print("=" * 60)

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()