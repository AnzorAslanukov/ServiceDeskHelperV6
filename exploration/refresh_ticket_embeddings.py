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
  2. Export   — by default, APPEND only the embedded tickets missing from the
                local file (read current file, diff IDs against the table,
                fetch + concatenate only the new rows), then validate and
                ATOMICALLY swap into place (keeping one .bak) and update the
                manifest. Use --full-export to rebuild the whole file instead.

Because the app mmaps the .npy at startup and the two files are POSITIONALLY
aligned (row i of the matrix == metadata[i]), this script never overwrites the
live files in place: it builds temp files, validates, then os.replace()s them.

Usage:
    # Report how many tickets need vectorizing + a measured time estimate
    python -m exploration.refresh_ticket_embeddings --status

    # Full refresh (incremental compute + incremental append + swap) — default
    python -m exploration.refresh_ticket_embeddings

    # Skip the Databricks compute step; only append missing rows locally
    python -m exploration.refresh_ticket_embeddings --skip-compute

    # Rebuild the WHOLE local file from the table (recovery / first build)
    python -m exploration.refresh_ticket_embeddings --full-export

    # Limit how many new tickets get embedded in the compute step
    python -m exploration.refresh_ticket_embeddings --limit 5000

    # Show what would happen without computing, exporting, or swapping
    python -m exploration.refresh_ticket_embeddings --dry-run
"""

import argparse
import hashlib
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Ensure stdout can render non-ASCII output (e.g. the "====" headers and any
# ticket text) on legacy Windows consoles (cp1252). Best-effort: never fatal.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

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


# ── In-place progress bar (carriage-return / single-line updater) ───────


def print_progress(done: int, total: int, prefix: str = "  Progress", width: int = 30) -> None:
    """
    Render a single-line progress bar that overwrites itself in place using a
    carriage return ('\\r') instead of printing a new line each update — e.g.

        Progress |#########---------------------|  50/150 (33%)

    ASCII-only glyphs are used so the bar renders on any console codepage
    (Windows cp1252 cannot encode block characters like U+2588).

    Call repeatedly with increasing ``done``; call ``finish_progress()`` once
    when complete to move the cursor to the next line.
    """
    total = max(total, 1)
    done = min(done, total)
    filled = int(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = int(100 * done / total)
    # '\r' returns to column 0; end='' keeps us on the same line; the trailing
    # spaces clear any leftover characters from a previous, longer render.
    sys.stdout.write(f"\r{prefix} |{bar}| {done}/{total} ({pct}%)   ")
    sys.stdout.flush()


def finish_progress() -> None:
    """Terminate an in-place progress bar by moving to the next line."""
    sys.stdout.write("\n")
    sys.stdout.flush()


class _SuppressStdout:
    """
    Context manager that swallows stdout writes. Used to silence the per-batch
    print() calls inside populate_ticket_embeddings.process_batch so they don't
    fragment the single-line progress bar. Anything written while active is
    captured and returned so callers can surface errors if needed.
    """

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._real = None

    def __enter__(self) -> "io.StringIO":
        self._real = sys.stdout
        sys.stdout = self._buffer
        return self._buffer

    def __exit__(self, *exc) -> None:
        sys.stdout = self._real


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
    processed = 0
    # Show an in-place progress bar counting tickets vectorized (e.g. 10/100).
    # process_batch() prints its own per-batch lines, which would fragment the
    # single-line bar, so we capture its stdout and only surface real errors.
    print_progress(0, total)
    for batch_num in range(1, total_batches + 1):
        start = (batch_num - 1) * batch_size
        batch = tickets[start:start + batch_size]

        with _SuppressStdout() as captured:
            inserted += pte.process_batch(batch, batch_num, total_batches, dry_run=dry_run)

        processed += len(batch)
        print_progress(processed, total)

        # If the batch reported an error, break the bar and surface it.
        batch_log = captured.getvalue()
        if "ERROR" in batch_log or "FAILED" in batch_log:
            finish_progress()
            for line in batch_log.splitlines():
                if "ERROR" in line or "FAILED" in line:
                    print(f"  {line.strip()}")
            print_progress(processed, total)

        if batch_num < total_batches:
            time.sleep(pte.DELAY_BETWEEN_BATCHES)

    finish_progress()
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


def _parse_embedding(raw) -> list | None:
    """Normalize a raw embedding cell into a 1024-float list, or None if invalid."""
    if isinstance(raw, np.ndarray):
        embedding = raw.tolist()
    elif isinstance(raw, str):
        try:
            embedding = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, list):
        embedding = raw
    else:
        return None
    if len(embedding) != EMBEDDING_DIM:
        return None
    return embedding


def _row_to_metadata(row_dict: dict) -> dict:
    """Build the runtime metadata record for one ticket row."""
    return {
        "Id": row_dict.get("Id", ""),
        "Title": row_dict.get("Title", ""),
        "Description": row_dict.get("Description", ""),
        "SupportGroup": row_dict.get("SupportGroup", ""),
        "Location": row_dict.get("Location", ""),
    }


# ── Incremental (append-only) local export helpers ──────────────────────


def fetch_table_ids(connection, ticket_table: str) -> set[str]:
    """Fetch the set of ticket Ids present in the Databricks table (IDs only)."""
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"SELECT Id FROM {ticket_table} WHERE embedding IS NOT NULL"
        )
        return {row[0] for row in cursor.fetchall() if row[0]}
    finally:
        cursor.close()


def load_local_matrix_and_metadata(
    embeddings_path: Path, metadata_path: Path
):
    """
    Load the current local (matrix, metadata, id_set). Returns
    (None, [], set()) if either file is missing (i.e. first-ever build).

    The matrix is loaded fully into memory (not mmap) because we are going to
    concatenate onto it and rewrite it.
    """
    if not embeddings_path.exists() or not metadata_path.exists():
        return None, [], set()
    matrix = np.load(str(embeddings_path))
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)
    local_ids = {m.get("Id") for m in metadata if m.get("Id")}
    return matrix, metadata, local_ids


def fetch_ticket_rows_by_ids(
    connection, ticket_table: str, ids: list[str], chunk_size: int = 500
):
    """
    Fetch (embeddings, metadata) for a specific set of ticket Ids using batched
    ``WHERE Id IN (...)`` queries. Falls back to a full scan + client-side
    filter if an IN-query fails (e.g. driver/param limits).

    Returns (embeddings_list, metadata_list) aligned by position.
    """
    id_set = set(ids)
    embeddings: list = []
    metadata: list[dict] = []
    cols = "Id, Title, Description, SupportGroup, Location, embedding"

    try:
        cursor = connection.cursor()
        try:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                in_list = ", ".join("'" + i.replace("'", "''") + "'" for i in chunk)
                cursor.execute(
                    f"SELECT {cols} FROM {ticket_table} "
                    f"WHERE embedding IS NOT NULL AND Id IN ({in_list})"
                )
                columns = [desc[0] for desc in cursor.description]
                for row in cursor.fetchall():
                    row_dict = dict(zip(columns, row))
                    emb = _parse_embedding(row_dict.get("embedding"))
                    if emb is None:
                        continue
                    embeddings.append(emb)
                    metadata.append(_row_to_metadata(row_dict))
        finally:
            cursor.close()
        return embeddings, metadata
    except Exception as e:  # noqa: BLE001 — robust fallback path
        print(f"  WHERE-IN fetch failed ({e}); falling back to full scan + filter.")
        embeddings, metadata = [], []
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT {cols} FROM {ticket_table} WHERE embedding IS NOT NULL"
            )
            columns = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get("Id") not in id_set:
                    continue
                emb = _parse_embedding(row_dict.get("embedding"))
                if emb is None:
                    continue
                embeddings.append(emb)
                metadata.append(_row_to_metadata(row_dict))
        finally:
            cursor.close()
        return embeddings, metadata


# ── Stage 2 (incremental): append only missing rows to the local file ───


def run_incremental_export_and_swap(min_row_ratio: float, dry_run: bool) -> dict | None:
    """
    Append-only local export: fetch ONLY the embedded tickets that are not yet
    in the local file, concatenate them onto the existing matrix/metadata, then
    validate and atomically swap. Falls back to a full rebuild if there is no
    existing local file yet.

    Returns the manifest dict on success, or None on dry-run / no-op.
    """
    print(f"\n{'=' * 60}")
    print("STAGE 2 — Incremental export (append missing rows to local file)")
    print(f"{'=' * 60}")

    from exploration.export_local_vectors import TICKET_TABLE, get_databricks_connection

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    live_embeddings = OUTPUT_DIR / EMBEDDINGS_NAME
    live_metadata = OUTPUT_DIR / METADATA_NAME
    tmp_embeddings = OUTPUT_DIR / (EMBEDDINGS_NAME + ".tmp")
    tmp_metadata = OUTPUT_DIR / (METADATA_NAME + ".tmp")

    matrix, metadata, local_ids = load_local_matrix_and_metadata(
        live_embeddings, live_metadata
    )
    if matrix is None:
        print("  No existing local file — falling back to a full export.")
        return run_export_and_swap(min_row_ratio=min_row_ratio, dry_run=dry_run)

    current_rows = len(metadata)
    print(f"  Current live file: {current_rows:,} rows")

    connection = get_databricks_connection()
    try:
        table_ids = fetch_table_ids(connection, TICKET_TABLE)
        missing = sorted(table_ids - local_ids)
        print(f"  Table has {len(table_ids):,} embedded ticket(s); "
              f"{len(missing):,} missing from local file.")

        if not missing:
            print("  Local file already up to date — nothing to append.")
            return None

        if dry_run:
            print(f"  [DRY RUN] Would append {len(missing):,} row(s) and swap.")
            return None

        new_embeddings, new_metadata = fetch_ticket_rows_by_ids(
            connection, TICKET_TABLE, missing
        )
        print(f"  Fetched {len(new_metadata):,} new row(s) from Databricks.")
    finally:
        connection.close()

    if not new_metadata:
        print("  No valid new rows fetched — nothing to append.")
        return None

    new_matrix = np.array(new_embeddings, dtype=np.float32)
    combined_matrix = np.concatenate([matrix.astype(np.float32), new_matrix], axis=0)
    combined_metadata = list(metadata) + new_metadata
    print(f"  New local size: {combined_matrix.shape[0]:,} rows "
          f"(was {current_rows:,}).")

    # Validate the combined result. current_rows is the floor basis; an append
    # can only grow, so the safety floor simply guards alignment/dims here.
    validate_export(combined_matrix, combined_metadata, current_rows, min_row_ratio)
    print("  Validation passed.")

    with open(tmp_embeddings, "wb") as fh:
        np.save(fh, combined_matrix)
    with open(tmp_metadata, "w", encoding="utf-8") as f:
        json.dump(combined_metadata, f, ensure_ascii=False)

    bak_meta = atomic_swap_with_backup(tmp_metadata, live_metadata)
    bak_emb = atomic_swap_with_backup(tmp_embeddings, live_embeddings)
    if bak_emb or bak_meta:
        print("  Previous files backed up (.bak) for rollback.")

    manifest = build_manifest(
        live_embeddings, live_metadata,
        rows=int(combined_matrix.shape[0]),
        dims=int(combined_matrix.shape[1]),
    )
    with open(OUTPUT_DIR / MANIFEST_NAME, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n  Appended {len(new_metadata):,} row(s); "
          f"local file now has {manifest['rows']:,} rows.")
    return manifest



# ── Status / time-estimate command ──────────────────────────────────────


def measure_embedding_throughput(sample_texts: list[str]) -> float:
    """
    Embed one small batch via the live endpoint and return measured throughput
    in tickets/second. Reuses populate_ticket_embeddings.generate_embeddings.
    """
    from exploration import populate_ticket_embeddings as pte

    if not sample_texts:
        return 0.0
    start = time.time()
    pte.generate_embeddings(sample_texts)
    elapsed = time.time() - start
    if elapsed <= 0:
        return 0.0
    return len(sample_texts) / elapsed


def _format_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} hr"


def estimate_stage1_seconds(count: int, rate: float, batch_size: int, delay: float) -> float | None:
    """
    Estimate Stage 1 embedding time for ``count`` tickets. Uses measured
    ``rate`` (tickets/sec) when > 0, else a constants-based fallback that
    assumes ~2s API latency per batch. Returns None when count == 0.
    """
    if count <= 0:
        return None
    n_batches = (count + batch_size - 1) // batch_size
    if rate > 0:
        return (count / rate) + (n_batches * delay)
    return n_batches * (2.0 + delay)


def _scalar(cursor, sql: str) -> int:
    """Run a COUNT(*)-style query and return the integer scalar."""
    cursor.execute(sql)
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def describe_pending(measure: bool = True) -> dict:
    """
    Report, WITHOUT changing anything:
      - source tickets not yet embedded in Databricks (Stage 1 backlog)
      - embedded tickets missing from the local file (Stage 2 append backlog)
      - a time estimate for the Stage 1 embedding work

    Counts are computed with cheap COUNT(*) queries (NOT by pulling 170K+ rows
    into Python), so this stays fast. Each step prints as it goes so the caller
    can see progress even while a cold Databricks warehouse is spinning up.

    When ``measure`` is True, embeds one small real sample batch to measure live
    throughput; otherwise uses the code's known constants.
    """
    from exploration import populate_ticket_embeddings as pte
    from exploration.export_local_vectors import TICKET_TABLE, get_databricks_connection

    print(f"\n{'=' * 60}")
    print("STATUS — how many tickets need vectorizing")
    print(f"{'=' * 60}")

    print("  Connecting to Databricks (a cold warehouse may take a few minutes)...")
    connection = get_databricks_connection()
    stage1 = 0
    stage2 = 0
    sample_texts: list[str] = []
    try:
        cursor = connection.cursor()
        try:
            # Stage 1 — source tickets with text that are NOT yet in the table.
            # Counted directly in SQL (anti-join) instead of transferring rows.
            print("  Counting source tickets not yet embedded (Stage 1)...")
            stage1 = _scalar(
                cursor,
                f"""
                SELECT COUNT(*) FROM {pte.SOURCE_TABLE} s
                WHERE (s.Title IS NOT NULL OR s.Description IS NOT NULL)
                  AND NOT EXISTS (
                      SELECT 1 FROM {TICKET_TABLE} t WHERE t.Id = s.Id
                  )
                """,
            )
            print(f"    -> {stage1:,} to embed.")

            # Stage 2 — embedded-in-table but missing from the local file.
            print("  Counting embedded tickets missing from local file (Stage 2)...")
            _m, _meta, local_ids = load_local_matrix_and_metadata(
                OUTPUT_DIR / EMBEDDINGS_NAME, OUTPUT_DIR / METADATA_NAME
            )
            table_ids = fetch_table_ids(connection, TICKET_TABLE)
            stage2 = len(table_ids - local_ids)
            print(f"    -> {stage2:,} to append locally.")

            # Small sample for measured throughput (LIMIT, not full scan).
            if stage1 > 0 and measure:
                print("  Fetching a small sample for a throughput measurement...")
                cursor.execute(
                    f"""
                    SELECT s.Title, s.Description FROM {pte.SOURCE_TABLE} s
                    WHERE (s.Title IS NOT NULL OR s.Description IS NOT NULL)
                      AND NOT EXISTS (
                          SELECT 1 FROM {TICKET_TABLE} t WHERE t.Id = s.Id
                      )
                    LIMIT {pte.EMBEDDING_BATCH_SIZE}
                    """
                )
                for row in cursor.fetchall():
                    text = pte.build_search_text(row[0], row[1])
                    if text.strip():
                        sample_texts.append(text)
        finally:
            cursor.close()
    finally:
        connection.close()

    # Throughput + estimate for Stage 1 (the expensive embedding work).
    rate = 0.0
    if sample_texts:
        print(f"  Measuring throughput on a sample of {len(sample_texts)} ticket(s)...")
        rate = measure_embedding_throughput(sample_texts)

    est_seconds = estimate_stage1_seconds(
        stage1, rate, pte.EMBEDDING_BATCH_SIZE, pte.DELAY_BETWEEN_BATCHES
    )

    print(f"\n  Stage 1 — source tickets NOT yet embedded (Databricks): {stage1:,}")
    print(f"  Stage 2 — embedded tickets MISSING from local file:     {stage2:,}")
    if rate > 0:
        print(f"  Measured throughput: {rate:.1f} tickets/sec")
    if est_seconds is not None:
        print(f"  Estimated Stage 1 embedding time: ~{_format_duration(est_seconds)}")
    if stage1 == 0 and stage2 == 0:
        print("\n  Everything is up to date.")

    return {
        "stage1_to_embed": stage1,
        "stage2_to_append": stage2,
        "tickets_per_sec": rate,
        "estimated_seconds": est_seconds,
    }




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
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Report how many tickets need vectorizing (Stage 1) and how many "
            "embedded tickets are missing from the local file (Stage 2), with a "
            "measured time estimate. Makes no changes."
        ),
    )
    parser.add_argument(
        "--no-measure",
        action="store_true",
        help="With --status, skip the live sample embedding; estimate from constants only.",
    )
    parser.add_argument(
        "--full-export",
        action="store_true",
        help=(
            "Rebuild the WHOLE local file from the table instead of appending "
            "only missing rows (recovery / first-time build)."
        ),
    )
    args = parser.parse_args()

    # Status is a read-only report; do it and exit.
    if args.status:
        describe_pending(measure=not args.no_measure)
        return

    print("=" * 70)
    print("  Ticket Embeddings Refresh Pipeline")
    print("=" * 70)
    print(f"  Output dir:     {OUTPUT_DIR}")
    print(f"  Skip compute:   {args.skip_compute}")
    print(f"  Limit:          {args.limit or 'None (all new tickets)'}")
    print(f"  Min row ratio:  {args.min_row_ratio}")
    print(f"  Export mode:    {'FULL rebuild' if args.full_export else 'incremental append'}")
    print(f"  Dry run:        {args.dry_run}")

    if not args.skip_compute:
        run_compute(limit=args.limit, dry_run=args.dry_run)
    else:
        print("\n  Skipping compute stage (--skip-compute).")

    if args.full_export:
        run_export_and_swap(min_row_ratio=args.min_row_ratio, dry_run=args.dry_run)
    else:
        run_incremental_export_and_swap(
            min_row_ratio=args.min_row_ratio, dry_run=args.dry_run
        )

    print(f"\n{'=' * 70}")
    print("  Refresh complete." if not args.dry_run else "  Dry run complete.")
    print("  NOTE: the running server mmaps this file at startup; it must be")
    print("        restarted (e.g. via deploy.py) to load the new embeddings.")
    print("=" * 70)


if __name__ == "__main__":
    main()

