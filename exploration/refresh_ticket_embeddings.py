"""
Ticket Embeddings Refresh Pipeline
===================================
Rebuilds the LOCAL ticket vector files that the running application mmaps at
startup (data/vectors/ticket_embeddings.npy + ticket_metadata.json), keeping
them in sync with newly-created tickets.

Scope: TICKET embeddings ONLY. OneNote documentation vectors are never touched.

Two stages (both reuse the existing, proven scripts):

  1. Compute  — incrementally embed NEW tickets into the Databricks table
                hive_metastore.embeddings_db.ticket_embeddings
                (delegates to exploration.populate_ticket_embeddings).
  2. Export   — re-export that table to local .npy/.json TEMP files, validate
                them, then ATOMICALLY swap them into place (keeping one .bak)
                and write a manifest for observability + transfer gating.

Because the app mmaps the .npy at startup and the two files are POSITIONALLY
aligned (row i of the matrix == metadata[i]), this script never overwrites the
live files in place: it builds temp files, validates, then os.replace()s them.

Usage:
    # Full refresh (incremental compute + export + swap) — default
    python -m exploration.refresh_ticket_embeddings

    # Skip the Databricks compute step; only re-export the existing table
    python -m exploration.refresh_ticket_embeddings --skip-compute

    # Limit how many new tickets get embedded in the compute step
    python -m exploration.refresh_ticket_embeddings --limit 5000

    # Show what would happen without computing, exporting, or swapping
    python -m exploration.refresh_ticket_embeddings --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Add project root to path (mirrors export_local_vectors.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "data" / "vectors"

# Live files consumed by src/services/local_vector_store.py
EMBEDDINGS_NAME = "ticket_embeddings.npy"
METADATA_NAME = "ticket_metadata.json"
MANIFEST_NAME = "ticket_vectors_manifest.json"

EMBEDDING_DIM = 1024

# Safety floor: refuse to swap in a freshly-exported file that has fewer rows
# than this fraction of the current live file. Guards against a truncated or
# partially-failed export clobbering good data. Overridable via --min-row-ratio.
DEFAULT_MIN_ROW_RATIO = 0.98


# ── Pure, unit-testable helpers ────────────────────────────────────────


def sha256_of_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Compute the SHA-256 hex digest of a file, streamed in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_export(
    embeddings: np.ndarray,
    metadata: list[dict],
    current_row_count: int,
    min_row_ratio: float = DEFAULT_MIN_ROW_RATIO,
) -> None:
    """
    Validate a freshly-built (matrix, metadata) pair before it is allowed to
    replace the live files.

    Raises ValueError with an explanatory message if any invariant is violated:
      - matrix must be 2-D
      - embedding dimension must be EMBEDDING_DIM (1024)
      - row count of matrix must equal len(metadata)  [positional alignment]
      - every metadata record must carry a non-empty "Id"
      - row count must not fall below current_row_count * min_row_ratio
        (skipped when current_row_count == 0, i.e. first-ever build)
    """
    if embeddings.ndim != 2:
        raise ValueError(
            f"Embeddings matrix must be 2-D, got shape {embeddings.shape!r}"
        )

    n_rows, n_dims = embeddings.shape
    if n_dims != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding dimension must be {EMBEDDING_DIM}, got {n_dims}"
        )

    if n_rows != len(metadata):
        raise ValueError(
            "Row count mismatch between embeddings and metadata: "
            f"{n_rows} rows vs {len(metadata)} metadata records "
            "(.npy and .json must be positionally aligned)"
        )

    if n_rows == 0:
        raise ValueError("Refusing to write an empty export (0 rows)")

    missing_ids = sum(1 for m in metadata if not m.get("Id"))
    if missing_ids:
        raise ValueError(
            f"{missing_ids} metadata record(s) are missing an 'Id' "
            "(would break the id->index lookup at runtime)"
        )

    if current_row_count > 0:
        floor = int(current_row_count * min_row_ratio)
        if n_rows < floor:
            raise ValueError(
                f"New export has {n_rows} rows, below the safety floor of "
                f"{floor} ({min_row_ratio:.0%} of the current {current_row_count}). "
                "Refusing to swap. Re-run the export or pass a lower "
                "--min-row-ratio if this shrinkage is intentional."
            )



def build_manifest(
    embeddings_path: Path,
    metadata_path: Path,
    rows: int,
    dims: int,
    source_max_created_date: str | None = None,
) -> dict:
    """Build the manifest dict describing a built vector fileset."""
    return {
        "rows": rows,
        "dims": dims,
        "sha256_npy": sha256_of_file(embeddings_path),
        "sha256_meta": sha256_of_file(metadata_path),
        "source_max_created_date": source_max_created_date,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def read_current_row_count(metadata_path: Path) -> int:
    """Return the number of rows in the current live metadata file (0 if none)."""
    if not metadata_path.exists():
        return 0
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            return len(json.load(f))
    except (json.JSONDecodeError, OSError):
        return 0


def atomic_swap_with_backup(temp_path: Path, live_path: Path) -> Path | None:
    """
    Atomically move ``temp_path`` onto ``live_path``.

    If a live file already exists it is first moved aside to
    ``<live_path>.bak`` (a previous .bak is overwritten). Returns the backup
    path, or None if there was no prior live file.

    os.replace() is atomic on the same filesystem/volume, so a reader either
    sees the old file or the new one, never a partial write.
    """
    backup_path: Path | None = None
    if live_path.exists():
        backup_path = live_path.with_suffix(live_path.suffix + ".bak")
        os.replace(str(live_path), str(backup_path))
    os.replace(str(temp_path), str(live_path))
    return backup_path


# ── Stage 1: incremental compute (delegates to populate_ticket_embeddings) ──


def run_compute(limit: int | None, dry_run: bool) -> None:
    """
    Incrementally embed NEW tickets into the Databricks target table by
    reusing exploration.populate_ticket_embeddings. Only tickets not already
    present in the target table are embedded and inserted.
    """
    print(f"\n{'=' * 60}")
    print("STAGE 1 — Incremental compute (new tickets -> Databricks table)")
    print(f"{'=' * 60}")

    from exploration import populate_ticket_embeddings as pte

    exclude_ids = pte.get_already_embedded_ids()

    fetch_limit = limit
    if limit and exclude_ids:
        fetch_limit = limit + len(exclude_ids)

    tickets = pte.fetch_source_tickets(limit=fetch_limit, exclude_ids=exclude_ids)
    if not tickets:
        print("  No new tickets to embed. Databricks table is up to date.")
        return

    if limit and len(tickets) > limit:
        tickets = tickets[:limit]
        print(f"  Limited to {limit} tickets after filtering.")

    batch_size = pte.EMBEDDING_BATCH_SIZE
    total = len(tickets)
    total_batches = (total + batch_size - 1) // batch_size
    print(f"  Embedding {total} new ticket(s) in {total_batches} batch(es)...")

    inserted = 0
    for batch_num in range(1, total_batches + 1):
        start = (batch_num - 1) * batch_size
        batch = tickets[start:start + batch_size]
        inserted += pte.process_batch(batch, batch_num, total_batches, dry_run=dry_run)
        if batch_num < total_batches:
            time.sleep(pte.DELAY_BETWEEN_BATCHES)

    print(f"  Compute complete: {inserted}/{total} new ticket(s) embedded.")


# ── Stage 2: export to temp files, validate, swap ──────────────────────


def run_export_and_swap(min_row_ratio: float, dry_run: bool) -> dict | None:
    """
    Export the Databricks ticket-embeddings table to temp local files,
    validate them, atomically swap into place, and write the manifest.

    Returns the manifest dict on success, or None on dry-run.
    """
    print(f"\n{'=' * 60}")
    print("STAGE 2 — Export Databricks table -> local files (temp + swap)")
    print(f"{'=' * 60}")

    from exploration.export_local_vectors import TICKET_TABLE

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    live_embeddings = OUTPUT_DIR / EMBEDDINGS_NAME
    live_metadata = OUTPUT_DIR / METADATA_NAME
    tmp_embeddings = OUTPUT_DIR / (EMBEDDINGS_NAME + ".tmp")
    tmp_metadata = OUTPUT_DIR / (METADATA_NAME + ".tmp")

    current_rows = read_current_row_count(live_metadata)
    print(f"  Current live file: {current_rows:,} rows")

    if dry_run:
        print("  [DRY RUN] Would query Databricks, export temp files, "
              "validate, and swap. No changes made.")
        return None

    from exploration.export_local_vectors import get_databricks_connection

    connection = get_databricks_connection()
    try:
        embeddings, metadata, source_max_created = _fetch_ticket_rows(
            connection, TICKET_TABLE
        )
    finally:
        connection.close()

    embeddings_array = np.array(embeddings, dtype=np.float32)
    print(f"  Exported matrix shape: {embeddings_array.shape}")

    # Validate BEFORE touching any live file.
    validate_export(embeddings_array, metadata, current_rows, min_row_ratio)
    print("  Validation passed.")

    # Write temp files. Use a file handle for np.save so the exact
    # "*.npy.tmp" path is used (np.save would otherwise append ".npy"
    # because the path does not already end in it).
    with open(tmp_embeddings, "wb") as fh:
        np.save(fh, embeddings_array)
    with open(tmp_metadata, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)

    # Atomic swap (metadata first, then embeddings — both aligned).
    bak_meta = atomic_swap_with_backup(tmp_metadata, live_metadata)
    bak_emb = atomic_swap_with_backup(tmp_embeddings, live_embeddings)
    if bak_emb or bak_meta:
        print("  Previous files backed up (.bak) for rollback.")

    manifest = build_manifest(
        live_embeddings,
        live_metadata,
        rows=int(embeddings_array.shape[0]),
        dims=int(embeddings_array.shape[1]),
        source_max_created_date=source_max_created,
    )
    with open(OUTPUT_DIR / MANIFEST_NAME, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    emb_mb = live_embeddings.stat().st_size / (1024 * 1024)
    meta_mb = live_metadata.stat().st_size / (1024 * 1024)
    print(f"\n  Swapped into place:")
    print(f"    {live_embeddings} ({emb_mb:.1f} MB)")
    print(f"    {live_metadata} ({meta_mb:.1f} MB)")
    print(f"    {OUTPUT_DIR / MANIFEST_NAME}")
    print(f"    rows={manifest['rows']:,}  dims={manifest['dims']}")
    return manifest


def _fetch_ticket_rows(connection, ticket_table: str):
    """
    Fetch (Id, Title, Description, SupportGroup, Location, embedding) rows and
    parse them into an aligned (embeddings, metadata) pair. Mirrors the parsing
    contract of exploration.export_local_vectors.export_tickets so the file
    format stays byte-for-byte compatible with the runtime loader.

    Returns (embeddings_list, metadata_list, source_max_created_date_or_None).
    """
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM {ticket_table}")
        count = cursor.fetchone()[0]
        print(f"  Table rows: {count:,}")

        print("  Fetching data (this may take several minutes for 170K+ rows)...")
        start = time.time()
        cursor.execute(f"""
            SELECT Id, Title, Description, SupportGroup, Location, embedding
            FROM {ticket_table}
            WHERE embedding IS NOT NULL
        """)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        print(f"  Fetched {len(rows):,} rows in {time.time() - start:.1f}s")

        embeddings: list = []
        metadata: list[dict] = []
        skipped = 0
        for row in rows:
            row_dict = dict(zip(columns, row))
            raw = row_dict["embedding"]
            if isinstance(raw, np.ndarray):
                embedding = raw.tolist()
            elif isinstance(raw, str):
                try:
                    embedding = json.loads(raw)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
            elif isinstance(raw, list):
                embedding = raw
            else:
                skipped += 1
                continue

            if len(embedding) != EMBEDDING_DIM:
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

        if skipped:
            print(f"  Skipped {skipped} row(s) with invalid embeddings")

        return embeddings, metadata, None
    finally:
        cursor.close()



# ── CLI ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Refresh the LOCAL ticket embedding files (compute + export + atomic swap).",
    )
    parser.add_argument(
        "--skip-compute",
        action="store_true",
        help="Skip the Databricks compute step; only re-export the existing table.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of NEW tickets to embed in the compute step (default: all).",
    )
    parser.add_argument(
        "--min-row-ratio",
        type=float,
        default=DEFAULT_MIN_ROW_RATIO,
        help=(
            "Safety floor: new export must have at least this fraction of the "
            f"current row count to be swapped in (default: {DEFAULT_MIN_ROW_RATIO})."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without computing, exporting, or swapping.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  Ticket Embeddings Refresh Pipeline")
    print("=" * 70)
    print(f"  Output dir:     {OUTPUT_DIR}")
    print(f"  Skip compute:   {args.skip_compute}")
    print(f"  Limit:          {args.limit or 'None (all new tickets)'}")
    print(f"  Min row ratio:  {args.min_row_ratio}")
    print(f"  Dry run:        {args.dry_run}")

    if not args.skip_compute:
        run_compute(limit=args.limit, dry_run=args.dry_run)
    else:
        print("\n  Skipping compute stage (--skip-compute).")

    run_export_and_swap(min_row_ratio=args.min_row_ratio, dry_run=args.dry_run)

    print(f"\n{'=' * 70}")
    print("  Refresh complete." if not args.dry_run else "  Dry run complete.")
    print("  NOTE: the running server mmaps this file at startup; it must be")
    print("        restarted (e.g. via deploy.py) to load the new embeddings.")
    print("=" * 70)


if __name__ == "__main__":
    main()

