"""
Exploration script: Investigate the onenote_documentation table.

Goals:
1. Check if scratchpad.aslanuka.onenote_documentation still exists
2. If yes, get its schema and row count
3. Check if we can create a permanent copy in hive_metastore.embeddings_db
4. If the old table is gone, determine what's needed to recreate it
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from databricks import sql as databricks_sql


def get_connection():
    return databricks_sql.connect(
        server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_API_KEY"),
    )


def run_query(cursor, query, label=""):
    """Run a query and print results, handling errors gracefully."""
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    print(f"  SQL: {query[:200]}...")
    try:
        cursor.execute(query)
        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            print(f"  Result: {len(rows)} rows")
            if rows:
                for i, row in enumerate(rows[:10]):
                    print(f"    [{i}] {dict(zip(columns, row))}")
                if len(rows) > 10:
                    print(f"    ... ({len(rows) - 10} more rows)")
            return [dict(zip(columns, row)) for row in rows]
        else:
            print("  (no result set)")
            return []
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def main():
    print("=" * 60)
    print("  OneNote Documentation Table Exploration")
    print("=" * 60)

    conn = get_connection()
    cursor = conn.cursor()

    # 1. Check if the old table still exists
    print("\n\n--- Step 1: Check if scratchpad.aslanuka.onenote_documentation exists ---")
    result = run_query(
        cursor,
        "SELECT COUNT(*) AS cnt FROM scratchpad.aslanuka.onenote_documentation",
        "Check old table existence & row count",
    )

    if result is not None:
        print("\n  ✓ Old table EXISTS!")
        count = result[0]["cnt"] if result else 0
        print(f"  Row count: {count}")

        # 2. Get schema
        print("\n\n--- Step 2: Get table schema ---")
        run_query(
            cursor,
            "DESCRIBE scratchpad.aslanuka.onenote_documentation",
            "Table schema",
        )

        # 3. Sample data
        print("\n\n--- Step 3: Sample data (first 3 rows, no embeddings) ---")
        run_query(
            cursor,
            """SELECT content, notebook, section, title, 
                      SIZE(embeddings) as embedding_dims
               FROM scratchpad.aslanuka.onenote_documentation 
               LIMIT 3""",
            "Sample rows",
        )

        # 4. Check if we can create a permanent copy
        print("\n\n--- Step 4: Check if hive_metastore.embeddings_db.onenote_documentation exists ---")
        result2 = run_query(
            cursor,
            "SELECT COUNT(*) AS cnt FROM hive_metastore.embeddings_db.onenote_documentation",
            "Check permanent table",
        )

        if result2 is None:
            print("\n  Table does NOT exist yet. We can create it.")
            print("\n\n--- Step 5: Try CREATE TABLE AS SELECT (CTAS) ---")
            print("  (This would copy all data from scratchpad to permanent storage)")
            print("  SQL would be:")
            print("    CREATE TABLE hive_metastore.embeddings_db.onenote_documentation AS")
            print("    SELECT * FROM scratchpad.aslanuka.onenote_documentation")
            
            # Actually try it
            print("\n  Attempting CTAS...")
            ctas_result = run_query(
                cursor,
                """CREATE TABLE IF NOT EXISTS hive_metastore.embeddings_db.onenote_documentation AS
                   SELECT * FROM scratchpad.aslanuka.onenote_documentation""",
                "CTAS execution",
            )

            if ctas_result is not None:
                # Verify
                print("\n\n--- Step 6: Verify new permanent table ---")
                run_query(
                    cursor,
                    "SELECT COUNT(*) AS cnt FROM hive_metastore.embeddings_db.onenote_documentation",
                    "Verify permanent table row count",
                )
                run_query(
                    cursor,
                    "DESCRIBE hive_metastore.embeddings_db.onenote_documentation",
                    "Verify permanent table schema",
                )
        else:
            print(f"\n  ✓ Permanent table already exists with {result2[0]['cnt']} rows!")

    else:
        print("\n  [X] Old table does NOT exist (scratchpad expired).")
        print("  The onenote_documentation data would need to be re-ingested from scratch.")
        print("  This would require re-running the OneNote -> embeddings pipeline.")

        # Check if permanent version exists anyway
        print("\n\n--- Checking if permanent version exists ---")
        run_query(
            cursor,
            "SELECT COUNT(*) AS cnt FROM hive_metastore.embeddings_db.onenote_documentation",
            "Check permanent table",
        )

        # Also check what tables exist in hive_metastore.embeddings_db
        print("\n\n--- List all tables in hive_metastore.embeddings_db ---")
        run_query(
            cursor,
            "SHOW TABLES IN hive_metastore.embeddings_db",
            "Tables in embeddings_db",
        )

        # Check if there's any onenote-related table anywhere
        print("\n\n--- Search for onenote tables across catalogs ---")
        run_query(
            cursor,
            "SHOW TABLES IN hive_metastore.embeddings_db LIKE '*onenote*'",
            "Search for onenote tables in embeddings_db",
        )

    cursor.close()
    conn.close()
    print("\n\nDone.")


if __name__ == "__main__":
    main()