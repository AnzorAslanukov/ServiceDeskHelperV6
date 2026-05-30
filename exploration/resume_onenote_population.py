"""
Resume populating onenote_documentation from where it left off.

Uses individual record inserts with better error handling to skip
problematic records that break SQL escaping.
"""

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
BATCH_SIZE = 25  # Smaller batches to isolate failures


def get_connection():
    return databricks_sql.connect(
        server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_API_KEY"),
    )


def load_records(jsonl_path: Path) -> list[dict]:
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def escape_sql_string(s: str) -> str:
    """Escape a string for SQL insertion - more robust version."""
    if s is None:
        return "NULL"
    # Replace backslash first, then single quotes
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    # Also handle null bytes and other problematic chars
    s = s.replace("\x00", "")
    return "'" + s + "'"


def build_insert_sql(batch: list[dict]) -> str:
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
    print("=" * 60)
    print("  Resume OneNote Documentation Population")
    print("=" * 60)

    # Get current count
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE}")
    current_count = cursor.fetchone()[0]
    print(f"  Current rows in table: {current_count}")

    # Load all records
    print("  Loading records from .jsonl...")
    records = load_records(JSONL_PATH)
    print(f"  Total records in file: {len(records)}")

    # Skip already-inserted records
    start_idx = current_count
    remaining = records[start_idx:]
    print(f"  Resuming from record {start_idx}, {len(remaining)} remaining")
    print()

    if not remaining:
        print("  Nothing to do - all records already inserted!")
        cursor.close()
        conn.close()
        return

    # Insert remaining in batches
    start_time = time.time()
    inserted = 0
    skipped = 0
    total_batches = (len(remaining) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[i: i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1

        try:
            sql = build_insert_sql(batch)
            cursor.execute(sql)
            inserted += len(batch)
        except Exception as e:
            # Batch failed - try individual records
            err_msg = str(e)[:100]
            print(f"  Batch {batch_num} failed ({err_msg}), trying individually...")
            for j, rec in enumerate(batch):
                try:
                    sql = build_insert_sql([rec])
                    cursor.execute(sql)
                    inserted += 1
                except Exception as e2:
                    skipped += 1
                    title = rec.get("title", "?")[:50]
                    print(f"    SKIP [{start_idx + i + j}] '{title}': {str(e2)[:80]}")

        elapsed = time.time() - start_time
        rate = inserted / elapsed if elapsed > 0 else 0

        if batch_num % 10 == 0 or batch_num == total_batches:
            print(
                f"  Batch {batch_num}/{total_batches}: "
                f"+{inserted} inserted, {skipped} skipped, "
                f"{rate:.1f} rec/sec"
            )

    # Final count
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {TABLE}")
    final_count = cursor.fetchone()[0]

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print("  COMPLETE")
    print("=" * 60)
    print(f"  New records inserted: {inserted}")
    print(f"  Records skipped: {skipped}")
    print(f"  Final table row count: {final_count}/{len(records)}")
    print(f"  Time elapsed: {elapsed:.1f}s")
    print("=" * 60)

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()