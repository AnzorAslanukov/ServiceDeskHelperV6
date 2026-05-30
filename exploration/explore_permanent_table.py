"""
Databricks Permanent Table Creation Explorer
=============================================
Tests whether we can create a permanent (non-scratchpad) table in Databricks
for storing ticket embeddings.

The scratchpad catalog has a 7-day TTL, so we need to find a catalog/schema
where tables persist permanently.

Steps:
1. List all available catalogs
2. Check current user identity
3. Explore permissions on each catalog
4. Attempt to create a schema in a non-scratchpad catalog
5. Attempt to create a test table
6. Verify the table exists
7. Clean up (drop test table)
"""

import os
import sys
from dotenv import load_dotenv
from databricks import sql as databricks_sql

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

                return columns, rows
    except Exception as e:
        if suppress_error:
            print(f"  FAILED: {e}")
        else:
            print(f"  ERROR: {e}")
        return [], []


def step1_list_catalogs():
    """Step 1: List all available catalogs."""
    print("\n" + "#"*70)
    print("  STEP 1: List All Available Catalogs")
    print("#"*70)
    return run_query("SHOW CATALOGS", "Available catalogs in this Databricks workspace")


def step2_current_user():
    """Step 2: Check current user identity."""
    print("\n" + "#"*70)
    print("  STEP 2: Current User Identity")
    print("#"*70)
    run_query("SELECT current_user() AS current_user", "Who am I?")
    run_query("SELECT current_catalog() AS current_catalog", "Current default catalog")
    run_query("SELECT current_schema() AS current_schema", "Current default schema")


def step3_explore_catalog_schemas(catalogs):
    """Step 3: List schemas in each catalog to understand structure."""
    print("\n" + "#"*70)
    print("  STEP 3: Explore Schemas in Each Catalog")
    print("#"*70)

    for catalog in catalogs:
        run_query(
            f"SHOW SCHEMAS IN `{catalog}`",
            f"Schemas in catalog '{catalog}'",
            suppress_error=True
        )


def step4_check_permissions(catalogs):
    """Step 4: Check what permissions we have on each catalog."""
    print("\n" + "#"*70)
    print("  STEP 4: Check Permissions on Catalogs")
    print("#"*70)

    for catalog in catalogs:
        # Try SHOW GRANTS — may not work depending on permissions
        run_query(
            f"SHOW GRANTS ON CATALOG `{catalog}`",
            f"Grants on catalog '{catalog}'",
            suppress_error=True
        )

    # Also check what we can do with our current user
    run_query(
        "SHOW GRANTS ON SCHEMA scratchpad.aslanuka",
        "Grants on scratchpad.aslanuka (our known writable schema)",
        suppress_error=True
    )


def step5_try_create_schema(target_catalog):
    """Step 5: Try to create a schema in a non-scratchpad catalog."""
    print("\n" + "#"*70)
    print(f"  STEP 5: Try Creating Schema in '{target_catalog}' Catalog")
    print("#"*70)

    schema_name = f"{target_catalog}.aslanuka_service_desk"

    # Try creating the schema
    cols, rows = run_query(
        f"CREATE SCHEMA IF NOT EXISTS {schema_name} COMMENT 'Service Desk Helper embeddings and data'",
        f"Attempting to create schema: {schema_name}",
        suppress_error=True
    )

    # Verify it exists
    run_query(
        f"SHOW SCHEMAS IN `{target_catalog}` LIKE 'aslanuka*'",
        f"Check if our schema exists in {target_catalog}",
        suppress_error=True
    )

    return schema_name


def step6_try_create_test_table(schema_name):
    """Step 6: Try to create a test table to verify write access."""
    print("\n" + "#"*70)
    print(f"  STEP 6: Try Creating Test Table in '{schema_name}'")
    print("#"*70)

    table_name = f"{schema_name}._test_permanent_table"

    # Create a simple Delta table
    run_query(
        f"""CREATE TABLE IF NOT EXISTS {table_name} (
            id STRING NOT NULL,
            test_value STRING,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
        ) USING DELTA
        COMMENT 'Test table to verify permanent storage - safe to delete'""",
        f"Creating test table: {table_name}",
        suppress_error=True
    )

    # Insert a test row
    run_query(
        f"INSERT INTO {table_name} (id, test_value) VALUES ('test_001', 'permanent_table_test')",
        f"Inserting test row into {table_name}",
        suppress_error=True
    )

    # Verify the data
    run_query(
        f"SELECT * FROM {table_name}",
        f"Reading back from {table_name}",
        suppress_error=True
    )

    # Get table details to confirm it's Delta/managed
    run_query(
        f"DESCRIBE TABLE EXTENDED {table_name}",
        f"Table details for {table_name}",
        suppress_error=True
    )

    return table_name


def step7_cleanup(table_name):
    """Step 7: Clean up the test table."""
    print("\n" + "#"*70)
    print(f"  STEP 7: Cleanup - Dropping Test Table")
    print("#"*70)

    run_query(
        f"DROP TABLE IF EXISTS {table_name}",
        f"Dropping test table: {table_name}",
        suppress_error=True
    )

    print("\n  Test table dropped. If schema creation succeeded, the schema")
    print("  is left in place for future use (it's empty now).")


def step8_test_embeddings_table_schema(schema_name):
    """Step 8: Show what the final embeddings table would look like."""
    print("\n" + "#"*70)
    print("  STEP 8: Proposed Embeddings Table Schema (NOT CREATING)")
    print("#"*70)

    print(f"""
  If permanent table creation succeeds, the embeddings table would be:

  Table: {schema_name}.ticket_embeddings

  CREATE TABLE {schema_name}.ticket_embeddings (
      ticket_id STRING NOT NULL COMMENT 'Ticket ID (e.g., IR1234567, SR1234567)',
      ticket_type STRING COMMENT 'IR or SR',
      title STRING COMMENT 'Ticket title',
      description STRING COMMENT 'Ticket description (truncated)',
      support_group STRING COMMENT 'Assigned support group name',
      location STRING COMMENT 'Ticket location',
      embedding ARRAY<DOUBLE> COMMENT '1024-dim GTE-Large-EN embedding vector',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP() COMMENT 'When embedding was generated',
      source_date TIMESTAMP COMMENT 'Original ticket creation date'
  ) USING DELTA
  COMMENT 'Pre-computed ticket embeddings for similarity search (915K+ tickets)'
  TBLPROPERTIES (
      'delta.autoOptimize.optimizeWrite' = 'true',
      'delta.autoOptimize.autoCompact' = 'true'
  );

  This table would:
  - Store embeddings for ALL 915K tickets from prepared.ticketing.athena_tickets
  - Include metadata (support_group, location) for enriched search results
  - Use Delta format for ACID transactions and efficient updates
  - Support incremental updates as new tickets arrive
  - NOT expire (unlike scratchpad tables)
""")


def main():
    """Main exploration flow."""
    print("=" * 70)
    print("  Databricks Permanent Table Creation Explorer")
    print("=" * 70)
    print(f"  Server: {DATABRICKS_SERVER_HOSTNAME}")
    print(f"  HTTP Path: {DATABRICKS_HTTP_PATH}")
    print(f"  API Key: {'*' * 10}...{DATABRICKS_API_KEY[-4:] if DATABRICKS_API_KEY else 'NOT SET'}")
    print()

    # Step 1: List catalogs
    cols, rows = step1_list_catalogs()
    if not rows:
        print("\n  FATAL: Could not list catalogs. Check connection.")
        sys.exit(1)

    catalog_names = [row[0] for row in rows]
    print(f"\n  Found catalogs: {catalog_names}")

    # Step 2: Current user
    step2_current_user()

    # Step 3: Explore schemas in each catalog
    step3_explore_catalog_schemas(catalog_names)

    # Step 4: Check permissions
    step4_check_permissions(catalog_names)

    # Step 5: Try to create a schema in a non-scratchpad catalog
    # Priority order for permanent storage:
    # 1. A catalog we own or have CREATE SCHEMA on (not scratchpad)
    # 2. hive_metastore (legacy, usually writable)
    # 3. Any other catalog with write access

    # Determine target catalog — prefer non-scratchpad catalogs
    non_scratchpad = [c for c in catalog_names if c != 'scratchpad']
    target_catalogs = []

    # Try hive_metastore first (usually writable), then others
    if 'hive_metastore' in non_scratchpad:
        target_catalogs.append('hive_metastore')
    # Try any catalog that isn't system/information_schema/prepared
    skip_catalogs = {'scratchpad', 'system', 'information_schema', 'prepared', '__databricks_internal'}
    for c in non_scratchpad:
        if c not in skip_catalogs and c not in target_catalogs:
            target_catalogs.append(c)

    print(f"\n  Target catalogs to try (in order): {target_catalogs}")

    schema_name = None
    for target_catalog in target_catalogs:
        schema_name = step5_try_create_schema(target_catalog)
        # If we get here without error, try creating a table
        break  # Try the first one; if it fails, the error is printed

    if not target_catalogs:
        print("\n  WARNING: No non-scratchpad catalogs found to try.")
        print("  Falling back to testing in scratchpad (will still expire in 7 days)")
        schema_name = "scratchpad.aslanuka"

    # Step 6: Try creating a test table
    if schema_name:
        table_name = step6_try_create_test_table(schema_name)

        # Step 7: Cleanup
        step7_cleanup(table_name)

    # Step 8: Show proposed schema
    if schema_name:
        step8_test_embeddings_table_schema(schema_name)

    # Summary
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Catalogs found: {catalog_names}")
    print(f"  Target catalog attempted: {target_catalogs[0] if target_catalogs else 'None'}")
    print(f"  Schema attempted: {schema_name}")
    print(f"\n  Review the output above to determine:")
    print(f"  1. Which catalog allows permanent table creation")
    print(f"  2. Whether CREATE SCHEMA succeeded")
    print(f"  3. Whether CREATE TABLE + INSERT succeeded")
    print(f"  4. Whether the table is Delta-managed (permanent)")
    print("=" * 70)


if __name__ == '__main__':
    main()