"""
Export vector data from Databricks SQL warehouse to local .npy/.json files
for fast in-memory similarity search.

Usage:
    python -m exploration.export_local_vectors              # Full export
    python -m exploration.export_local_vectors --docs-only  # Only documentation
    python -m exploration.export_local_vectors --tickets-only  # Only tickets

Output files (in data/vectors/):
    - onenote_embeddings.npy   (N x 1024 float32 matrix)
    - onenote_metadata.json    (list of {content, notebook, section, title})
    - ticket_embeddings.npy    (M x 1024 float32 matrix)
    - ticket_metadata.json     (list of {Id, Title, Description, SupportGroup, Location})
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from exploration.athena_auth import get_databricks_connection  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "data" / "vectors"

# Databricks table names
ONENOTE_TABLE = "hive_metastore.embeddings_db.onenote_documentation"
TICKET_TABLE = "hive_metastore.embeddings_db.ticket_embeddings"


def export_documentation(connection) -> None:
    """Export OneNote documentation embeddings and metadata."""
    print(f"\n{'='*60}")
    print("Exporting OneNote documentation vectors...")
    print(f"Table: {ONENOTE_TABLE}")
    print(f"{'='*60}")

    cursor = connection.cursor()
    try:
        # Count rows
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM {ONENOTE_TABLE}")
        count = cursor.fetchone()[0]
        print(f"Total rows: {count:,}")

        # Fetch all data
        print("Fetching data (this may take a minute)...")
        start = time.time()
        cursor.execute(f"""
            SELECT title, content, notebook, section, embeddings
            FROM {ONENOTE_TABLE}
        """)

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        elapsed = time.time() - start
        print(f"Fetched {len(rows):,} rows in {elapsed:.1f}s")

        # Parse into embeddings matrix and metadata list
        embeddings = []
        metadata = []

        print("Parsing embeddings...")
        for row in rows:
            row_dict = dict(zip(columns, row))

            # Parse embedding
            raw_embedding = row_dict["embeddings"]
            if isinstance(raw_embedding, str):
                embedding = json.loads(raw_embedding)
            elif isinstance(raw_embedding, list):
                embedding = raw_embedding
            else:
                print(f"  WARNING: Unexpected embedding type: {type(raw_embedding)}, skipping")
                continue

            embeddings.append(embedding)
            metadata.append({
                "title": row_dict.get("title", ""),
                "content": row_dict.get("content", ""),
                "notebook": row_dict.get("notebook", ""),
                "section": row_dict.get("section", ""),
            })

        # Save as numpy array
        embeddings_array = np.array(embeddings, dtype=np.float32)
        print(f"Embeddings shape: {embeddings_array.shape}")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        embeddings_path = OUTPUT_DIR / "onenote_embeddings.npy"
        metadata_path = OUTPUT_DIR / "onenote_metadata.json"

        np.save(str(embeddings_path), embeddings_array)
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False)

        emb_size_mb = embeddings_path.stat().st_size / (1024 * 1024)
        meta_size_mb = metadata_path.stat().st_size / (1024 * 1024)
        print(f"\nSaved:")
        print(f"  {embeddings_path} ({emb_size_mb:.1f} MB)")
        print(f"  {metadata_path} ({meta_size_mb:.1f} MB)")
        print(f"  Total: {emb_size_mb + meta_size_mb:.1f} MB")

    finally:
        cursor.close()


def export_tickets(connection) -> None:
    """Export ticket embeddings and metadata."""
    print(f"\n{'='*60}")
    print("Exporting ticket embeddings...")
    print(f"Table: {TICKET_TABLE}")
    print(f"{'='*60}")

    cursor = connection.cursor()
    try:
        # Count rows
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM {TICKET_TABLE}")
        count = cursor.fetchone()[0]
        print(f"Total rows: {count:,}")

        # Fetch all data — only the columns we need
        print("Fetching data (this may take several minutes for 170K+ rows)...")
        start = time.time()
        cursor.execute(f"""
            SELECT Id, Title, Description, SupportGroup, Location, embedding
            FROM {TICKET_TABLE}
            WHERE embedding IS NOT NULL
        """)

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        elapsed = time.time() - start
        print(f"Fetched {len(rows):,} rows in {elapsed:.1f}s")

        # Parse into embeddings matrix and metadata list
        embeddings = []
        metadata = []
        skipped = 0

        print("Parsing embeddings...")
        for i, row in enumerate(rows):
            if i > 0 and i % 10000 == 0:
                print(f"  Processed {i:,}/{len(rows):,} rows...")

            row_dict = dict(zip(columns, row))

            # Parse embedding
            raw_embedding = row_dict["embedding"]
            if isinstance(raw_embedding, str):
                try:
                    embedding = json.loads(raw_embedding)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
            elif isinstance(raw_embedding, list):
                embedding = raw_embedding
            else:
                skipped += 1
                continue

            if len(embedding) != 1024:
                skipped += 1
                continue

            embeddings.append(embedding)
            metadata.append({
                "Id": row_dict.get("Id", ""),
                "Title": row_dict.get("Title", ""),
                "Description": row_dict.get("Description", ""),
                "SupportGroup": row_dict.get("SupportGroup", ""),
                "Location": row_dict.get("Location", ""),
            })

        if skipped > 0:
            print(f"  Skipped {skipped} rows with invalid embeddings")

        # Save as numpy array
        embeddings_array = np.array(embeddings, dtype=np.float32)
        print(f"Embeddings shape: {embeddings_array.shape}")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        embeddings_path = OUTPUT_DIR / "ticket_embeddings.npy"
        metadata_path = OUTPUT_DIR / "ticket_metadata.json"

        np.save(str(embeddings_path), embeddings_array)
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False)

        emb_size_mb = embeddings_path.stat().st_size / (1024 * 1024)
        meta_size_mb = metadata_path.stat().st_size / (1024 * 1024)
        print(f"\nSaved:")
        print(f"  {embeddings_path} ({emb_size_mb:.1f} MB)")
        print(f"  {metadata_path} ({meta_size_mb:.1f} MB)")
        print(f"  Total: {emb_size_mb + meta_size_mb:.1f} MB")

    finally:
        cursor.close()


def get_databricks_connection():
    """Get a Databricks SQL connection using environment variables."""
    import os
    from dotenv import load_dotenv
    from databricks import sql as databricks_sql

    load_dotenv(PROJECT_ROOT / ".env")

    server_hostname = os.getenv("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.getenv("DATABRICKS_HTTP_PATH")
    access_token = os.getenv("DATABRICKS_API_KEY")

    if not all([server_hostname, http_path, access_token]):
        print("ERROR: Missing Databricks credentials in .env file.")
        print("Required: DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH, DATABRICKS_API_KEY")
        sys.exit(1)

    print(f"Connecting to Databricks SQL warehouse...")
    print(f"  Server: {server_hostname}")
    print(f"  HTTP Path: {http_path}")

    connection = databricks_sql.connect(
        server_hostname=server_hostname,
        http_path=http_path,
        access_token=access_token,
    )
    print("  Connected successfully!")
    return connection


def main():
    parser = argparse.ArgumentParser(
        description="Export Databricks vector data to local files for fast similarity search."
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Only export OneNote documentation vectors",
    )
    parser.add_argument(
        "--tickets-only",
        action="store_true",
        help="Only export ticket embedding vectors",
    )
    args = parser.parse_args()

    connection = get_databricks_connection()

    try:
        if not args.tickets_only:
            export_documentation(connection)

        if not args.docs_only:
            export_tickets(connection)

        print(f"\n{'='*60}")
        print("Export complete!")
        print(f"Output directory: {OUTPUT_DIR}")
        print(f"{'='*60}")

    finally:
        connection.close()


if __name__ == "__main__":
    main()