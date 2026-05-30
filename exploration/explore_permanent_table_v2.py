"""
Databricks Permanent Table Creation Explorer - V2
==================================================
Follow-up exploration after V1 found:
- hive_metastore has an existing 'embeddings_db' schema
- isoperations_analytics has a 'dev' schema
- We cannot CREATE SCHEMA in hive_metastore
- We have ALL PRIVILEGES on scratchpad.aslanuka (but 7-day TTL)

This script tests:
1. Can we write to hive_metastore.embeddings_db?
2. Can we write to isoperations_analytics.dev?
3. What's already in hive_metastore.embeddings_db?
4. Can we create a table in the existing embeddings_db schema?
"""

import os
import sys
import io
from dotenv import load_dotenv
from databricks import sql as databricks_sql

# Fix Windows console encoding for Unicode output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABRICKS_SERVER_HOSTNAME = os.getenv('DATABRICKS_SERVER_HOSTNAME')
DATABRICKS_HTTP_PATH = os.getenv('DATABRICKS_HTTP_PATH')
DATABRICKS_API_KEY = os.getenv('DATABRICKS_API_KEY')


def get_connection():
    """Create a Databricks SQL connection."""
    return databricks_sql.connect(
        server_hostname=DATABRICKS_SERVER_HOSTNAME,
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_API_KEY
    )


def run_query(query, description="", suppress_error=False):
    """Execute a SQL query and return results."""
    if description:
        print(f"\n{'='*70}")
        print(f"  {description}")
        print(f"{'='*70}")
    print(f"  SQL: {query}")
    print(f"{'-'*70}")

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()

                if columns:
                    print(f"  Columns: {columns}")
                print(f"  Rows returned: {len(rows)}")
                print()

                for i, row in enumerate(rows):
                    print(f"  [{i}] {row}")
                    if i >= 29:  # Limit output
                        print(f"  ... (showing first 30 of {len(rows)} rows)")
                        break

                return columns, rows, True
    except Exception as e:
        if suppress_error:
            print(f"  FAILED: {e}")
        else:
            print(f"  ERROR: {e}")
        return [], [], False


def test_embeddings_db():
    """Test the existing hive_metastore.embeddings_db schema."""
    print("\n" + "#"*70)
    print("  TEST 1: Explore hive_metastore.embeddings_db")
    print("#"*70)

    # What tables exist?
    run_query(
        "SHOW TABLES IN hive_metastore.embeddings_db",
        "Tables in hive_metastore.embeddings_db"
    )

    # Check grants on this schema
    run_query(
        "SHOW GRANTS ON SCHEMA hive_metastore.embeddings_db",
        "Grants on hive_metastore.embeddings_db",
        suppress_error=True
    )

    # Try to create a test table
    print("\n  Attempting to create a test table in hive_metastore.embeddings_db...")
    _, _, success = run_query(
        """CREATE TABLE IF NOT EXISTS hive_metastore.embeddings_db._test_write_access (
            id STRING,
            value STRING
        ) USING DELTA""",
        "CREATE TABLE in hive_metastore.embeddings_db",
        suppress_error=True
    )

    if success:
        print("\n  [OK] SUCCESS: Can create tables in hive_metastore.embeddings_db!")

        # Insert test data
        run_query(
            "INSERT INTO hive_metastore.embeddings_db._test_write_access VALUES ('test1', 'write_test')",
            "INSERT test row",
            suppress_error=True
        )

        # Read back
        run_query(
            "SELECT * FROM hive_metastore.embeddings_db._test_write_access",
            "Read back test data",
            suppress_error=True
        )

        # Check table details (is it permanent?)
        run_query(
            "DESCRIBE TABLE EXTENDED hive_metastore.embeddings_db._test_write_access",
            "Table details (check if permanent/Delta)",
            suppress_error=True
        )

        # Cleanup
        run_query(
            "DROP TABLE IF EXISTS hive_metastore.embeddings_db._test_write_access",
            "Cleanup: drop test table",
            suppress_error=True
        )
    else:
        print("\n  [FAIL] Cannot create tables in hive_metastore.embeddings_db")


def test_isoperations_dev():
    """Test the isoperations_analytics.dev schema."""
    print("\n" + "#"*70)
    print("  TEST 2: Explore isoperations_analytics.dev")
    print("#"*70)

    # What tables exist?
    run_query(
        "SHOW TABLES IN isoperations_analytics.dev",
        "Tables in isoperations_analytics.dev",
        suppress_error=True
    )

    # Check grants
    run_query(
        "SHOW GRANTS ON SCHEMA isoperations_analytics.dev",
        "Grants on isoperations_analytics.dev",
        suppress_error=True
    )

    # Try to create a test table
    print("\n  Attempting to create a test table in isoperations_analytics.dev...")
    _, _, success = run_query(
        """CREATE TABLE IF NOT EXISTS isoperations_analytics.dev._test_write_access (
            id STRING,
            value STRING
        ) USING DELTA""",
        "CREATE TABLE in isoperations_analytics.dev",
        suppress_error=True
    )

    if success:
        print("\n  [OK] SUCCESS: Can create tables in isoperations_analytics.dev!")

        # Insert test data
        run_query(
            "INSERT INTO isoperations_analytics.dev._test_write_access VALUES ('test1', 'write_test')",
            "INSERT test row",
            suppress_error=True
        )

        # Read back
        run_query(
            "SELECT * FROM isoperations_analytics.dev._test_write_access",
            "Read back test data",
            suppress_error=True
        )

        # Check table details
        run_query(
            "DESCRIBE TABLE EXTENDED isoperations_analytics.dev._test_write_access",
            "Table details (check if permanent/Delta)",
            suppress_error=True
        )

        # Cleanup
        run_query(
            "DROP TABLE IF EXISTS isoperations_analytics.dev._test_write_access",
            "Cleanup: drop test table",
            suppress_error=True
        )
    else:
        print("\n  [FAIL] Cannot create tables in isoperations_analytics.dev")


def test_isoperations_create_schema():
    """Try creating our own schema in isoperations_analytics."""
    print("\n" + "#"*70)
    print("  TEST 3: Try creating schema in isoperations_analytics")
    print("#"*70)

    # Check grants on the catalog
    run_query(
        "SHOW GRANTS ON CATALOG isoperations_analytics",
        "Grants on isoperations_analytics catalog",
        suppress_error=True
    )

    # Try creating a schema
    _, _, success = run_query(
        "CREATE SCHEMA IF NOT EXISTS isoperations_analytics.service_desk COMMENT 'Service Desk Helper - ticket embeddings and ML data'",
        "CREATE SCHEMA isoperations_analytics.service_desk",
        suppress_error=True
    )

    if success:
        print("\n  [OK] SUCCESS: Created schema isoperations_analytics.service_desk!")
        run_query(
            "SHOW SCHEMAS IN isoperations_analytics LIKE 'service*'",
            "Verify schema exists",
            suppress_error=True
        )
    else:
        print("\n  [FAIL] Cannot create schema in isoperations_analytics")


def test_scratchpad_table_properties():
    """Check if scratchpad tables have any TTL property we can override."""
    print("\n" + "#"*70)
    print("  TEST 4: Check scratchpad table properties (TTL mechanism)")
    print("#"*70)

    # Check existing table properties
    run_query(
        "DESCRIBE TABLE EXTENDED scratchpad.aslanuka.ir_embeddings",
        "Properties of scratchpad.aslanuka.ir_embeddings (existing table)",
        suppress_error=True
    )

    # Check if there's a TBLPROPERTIES that controls TTL
    run_query(
        "SHOW TBLPROPERTIES scratchpad.aslanuka.ir_embeddings",
        "TBLPROPERTIES of ir_embeddings",
        suppress_error=True
    )


def test_create_in_existing_embeddings_db():
    """Try creating the actual embeddings table structure (empty) in hive_metastore.embeddings_db."""
    print("\n" + "#"*70)
    print("  TEST 5: Test full embeddings table schema in hive_metastore.embeddings_db")
    print("#"*70)

    table = "hive_metastore.embeddings_db._test_ticket_embeddings_schema"

    _, _, success = run_query(
        f"""CREATE TABLE IF NOT EXISTS {table} (
            ticket_id STRING NOT NULL COMMENT 'Ticket ID (e.g., IR1234567, SR1234567)',
            ticket_type STRING COMMENT 'IR or SR',
            title STRING COMMENT 'Ticket title',
            description STRING COMMENT 'Ticket description (first 500 chars)',
            support_group STRING COMMENT 'Assigned support group name',
            location STRING COMMENT 'Ticket location',
            embedding ARRAY<DOUBLE> COMMENT '1024-dim GTE-Large-EN embedding vector',
            created_at TIMESTAMP COMMENT 'When embedding was generated',
            source_date TIMESTAMP COMMENT 'Original ticket creation date'
        ) USING DELTA
        COMMENT 'Test: ticket embeddings schema validation'""",
        f"CREATE full embeddings table schema: {table}",
        suppress_error=True
    )

    if success:
        print(f"\n  [OK] SUCCESS: Full embeddings table schema created at {table}!")

        # Verify schema
        run_query(
            f"DESCRIBE TABLE {table}",
            "Verify table schema",
            suppress_error=True
        )

        # Test inserting a row with a small fake embedding (just 3 dims for test)
        run_query(
            f"""INSERT INTO {table} (ticket_id, ticket_type, title, embedding, created_at)
            VALUES ('IR0000001', 'IR', 'Test ticket', array(0.1, 0.2, 0.3), current_timestamp())""",
            "Insert test row with embedding array",
            suppress_error=True
        )

        # Read back
        run_query(
            f"SELECT ticket_id, ticket_type, title, SIZE(embedding) as embed_dims FROM {table}",
            "Read back (verify embedding stored)",
            suppress_error=True
        )

        # Cleanup
        run_query(
            f"DROP TABLE IF EXISTS {table}",
            "Cleanup: drop test table",
            suppress_error=True
        )
    else:
        print(f"\n  [FAIL] Cannot create embeddings table in hive_metastore.embeddings_db")


def main():
    """Main exploration flow."""
    print("=" * 70)
    print("  Databricks Permanent Table Explorer - V2")
    print("  Testing write access to existing schemas")
    print("=" * 70)
    print(f"  Server: {DATABRICKS_SERVER_HOSTNAME}")
    print(f"  User: aslanuka@pennmedicine.upenn.edu")
    print()

    # Test 1: hive_metastore.embeddings_db (already exists!)
    test_embeddings_db()

    # Test 2: isoperations_analytics.dev
    test_isoperations_dev()

    # Test 3: Try creating our own schema in isoperations_analytics
    test_isoperations_create_schema()

    # Test 4: Check scratchpad TTL mechanism
    test_scratchpad_table_properties()

    # Test 5: Full embeddings table schema test
    test_create_in_existing_embeddings_db()

    # Final summary
    print("\n" + "=" * 70)
    print("  FINAL SUMMARY")
    print("=" * 70)
    print("""
  Candidate locations for permanent embeddings table:

  1. hive_metastore.embeddings_db.ticket_embeddings
     - Schema already exists (may have been created for this purpose)
     - hive_metastore is permanent (no TTL)
     - Need to verify write access (Test 1 & 5 above)

  2. isoperations_analytics.dev.ticket_embeddings
     - IS Operations is our department's catalog
     - 'dev' schema exists for development work
     - Need to verify write access (Test 2 above)

  3. isoperations_analytics.service_desk.ticket_embeddings
     - Our own schema if we can create it (Test 3 above)
     - Most organized option

  4. scratchpad.aslanuka.ticket_embeddings (FALLBACK)
     - We definitely have write access (ALL PRIVILEGES)
     - BUT: 7-day TTL means data is lost weekly
     - Only viable with automated weekly refresh pipeline
""")
    print("=" * 70)


if __name__ == '__main__':
    main()