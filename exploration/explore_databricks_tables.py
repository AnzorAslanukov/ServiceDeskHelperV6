"""
Databricks SQL Warehouse Explorer
Explores catalogs, schemas, tables, and sample data available via Databricks SQL.
"""

import os
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


def run_query(query, description=""):
    """Execute a SQL query and return results."""
    if description:
        print(f"\n{'='*60}")
        print(f"  {description}")
        print(f"{'='*60}")
    print(f"  Query: {query}")
    print(f"{'-'*60}")

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
                    if i >= 49:  # Limit output to 50 rows
                        print(f"  ... (showing first 50 of {len(rows)} rows)")
                        break

                return columns, rows
    except Exception as e:
        print(f"  ERROR: {e}")
        return [], []


def explore_catalogs():
    """List all available catalogs."""
    run_query("SHOW CATALOGS", "Available Catalogs")


def explore_schemas(catalog="scratchpad"):
    """List schemas in a catalog."""
    run_query(f"SHOW SCHEMAS IN {catalog}", f"Schemas in '{catalog}' catalog")


def explore_tables(catalog="scratchpad", schema="aslanuka"):
    """List tables in a schema."""
    run_query(f"SHOW TABLES IN {catalog}.{schema}", f"Tables in '{catalog}.{schema}'")


def explore_table_schema(full_table_name):
    """Describe a table's schema."""
    run_query(f"DESCRIBE TABLE {full_table_name}", f"Schema of '{full_table_name}'")


def explore_table_detail(full_table_name):
    """Get detailed table info."""
    run_query(f"DESCRIBE TABLE EXTENDED {full_table_name}", f"Extended details of '{full_table_name}'")


def explore_sample_data(full_table_name, limit=5):
    """Get sample rows from a table (excluding large array columns for readability)."""
    run_query(f"SELECT * FROM {full_table_name} LIMIT {limit}", f"Sample data from '{full_table_name}' (limit {limit})")


def explore_row_count(full_table_name):
    """Get row count for a table."""
    run_query(f"SELECT COUNT(*) as row_count FROM {full_table_name}", f"Row count of '{full_table_name}'")


def explore_onenote_documentation():
    """Deep dive into the onenote_documentation table."""
    table = "scratchpad.aslanuka.onenote_documentation"

    print("\n" + "#"*60)
    print("  DEEP DIVE: onenote_documentation")
    print("#"*60)

    explore_table_schema(table)
    explore_row_count(table)

    # Explore distinct notebooks
    run_query(
        f"SELECT DISTINCT notebook FROM {table} ORDER BY notebook",
        "Distinct notebooks in onenote_documentation"
    )

    # Explore distinct sections per notebook
    run_query(
        f"SELECT notebook, section, COUNT(*) as entry_count FROM {table} GROUP BY notebook, section ORDER BY notebook, section",
        "Sections per notebook with entry counts"
    )

    # Sample content (without embeddings for readability)
    run_query(
        f"SELECT notebook, section, title, LEFT(content, 200) as content_preview FROM {table} LIMIT 10",
        "Sample content (first 200 chars) from onenote_documentation"
    )

    # Embedding dimensions
    run_query(
        f"SELECT SIZE(embeddings) as embedding_dimensions FROM {table} LIMIT 1",
        "Embedding dimensions in onenote_documentation"
    )


def explore_ir_embeddings():
    """Deep dive into the ir_embeddings table."""
    table = "scratchpad.aslanuka.ir_embeddings"

    print("\n" + "#"*60)
    print("  DEEP DIVE: ir_embeddings")
    print("#"*60)

    explore_table_schema(table)
    explore_row_count(table)

    # Sample IDs
    run_query(
        f"SELECT id FROM {table} LIMIT 20",
        "Sample ticket IDs in ir_embeddings"
    )

    # ID pattern analysis
    run_query(
        f"SELECT LEFT(id, 2) as prefix, COUNT(*) as count FROM {table} GROUP BY LEFT(id, 2) ORDER BY count DESC",
        "ID prefix distribution in ir_embeddings"
    )

    # Embedding dimensions
    run_query(
        f"SELECT SIZE(ticket_embedding) as embedding_dimensions FROM {table} LIMIT 1",
        "Embedding dimensions in ir_embeddings"
    )


if __name__ == '__main__':
    print("Databricks SQL Warehouse Explorer")
    print(f"Server: {DATABRICKS_SERVER_HOSTNAME}")
    print(f"HTTP Path: {DATABRICKS_HTTP_PATH}")

    # 1. Explore overall structure
    explore_catalogs()
    explore_schemas("scratchpad")
    explore_tables("scratchpad", "aslanuka")

    # 2. Deep dive into known tables
    explore_onenote_documentation()
    explore_ir_embeddings()

    print("\n" + "="*60)
    print("  Exploration complete!")
    print("="*60)