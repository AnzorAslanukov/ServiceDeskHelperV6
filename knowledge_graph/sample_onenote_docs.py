"""
Sample OneNote Documentation Explorer
Fetches sample documents from hive_metastore.embeddings_db.onenote_documentation
to understand their structure and content for knowledge graph extraction.
"""

import os
import json
from dotenv import load_dotenv
from databricks import sql as databricks_sql

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

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

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return columns, rows


def main():
    # 1. Table overview
    print("\n" + "="*80)
    print("  ONENOTE DOCUMENTATION TABLE OVERVIEW")
    print("="*80)

    cols, rows = run_query(
        "SELECT COUNT(*) as total FROM hive_metastore.embeddings_db.onenote_documentation",
        "Total document count"
    )
    print(f"  Total documents: {rows[0][0]}")

    # 2. Notebook distribution
    cols, rows = run_query(
        """SELECT notebook, COUNT(*) as cnt 
           FROM hive_metastore.embeddings_db.onenote_documentation 
           GROUP BY notebook ORDER BY cnt DESC""",
        "Documents by notebook"
    )
    for row in rows:
        print(f"  {row[0]}: {row[1]} documents")

    # 3. Top sections
    cols, rows = run_query(
        """SELECT section, COUNT(*) as cnt 
           FROM hive_metastore.embeddings_db.onenote_documentation 
           GROUP BY section ORDER BY cnt DESC LIMIT 20""",
        "Top 20 sections by document count"
    )
    for row in rows:
        print(f"  {row[0]}: {row[1]} documents")

    # 4. Sample documents
    cols, rows = run_query(
        """SELECT title, section, notebook, LENGTH(content) as content_len
           FROM hive_metastore.embeddings_db.onenote_documentation
           ORDER BY LENGTH(content) DESC
           LIMIT 10""",
        "Top 10 longest documents"
    )
    for row in rows:
        print(f"  [{row[2]}] {row[1]} / {row[0]} ({row[3]} chars)")

    # 5. Sample content preview
    cols, rows = run_query(
        """SELECT title, section, notebook, SUBSTRING(content, 1, 500) as preview
           FROM hive_metastore.embeddings_db.onenote_documentation
           WHERE LENGTH(content) >= 200
           LIMIT 3""",
        "Sample content previews (first 500 chars)"
    )
    for row in rows:
        print(f"\n  --- {row[0]} ({row[1]}, {row[2]}) ---")
        print(f"  {row[3][:300]}...")

    # 6. Save samples to file
    cols, rows = run_query(
        """SELECT title, section, notebook, content
           FROM hive_metastore.embeddings_db.onenote_documentation
           WHERE LENGTH(content) >= 200
           LIMIT 10"""
    )
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    samples = [
        {"title": r[0], "section": r[1], "notebook": r[2], "content": r[3]}
        for r in rows
    ]
    output_path = os.path.join(output_dir, 'onenote_doc_samples.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(samples, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {len(samples)} sample documents to {output_path}")


if __name__ == '__main__':
    main()