"""
Remote atomic-swap helper for the ticket embedding files.

Invoked over SSH by deploy.py AFTER the new ``.npy``/``.json`` have been SCP'd
to the server as ``*.tmp``. It validates the freshly-transferred temp files and,
only if they are internally consistent, atomically replaces the live files
(keeping a ``.bak`` of each for rollback).

Why a real script instead of a ``python -c "..."`` one-liner?  The one-liner had
to be wrapped in double quotes for ``python -c``, but deploy.py's ssh() ALSO
wraps the whole remote command in double quotes — the inner quotes closed the
outer ones, so the remote PowerShell tried to parse the Python source itself and
failed ("Missing argument in parameter list", "Unexpected token 'str'"). Shipping
this as a file that syncs via git and running ``python scripts/swap_embeddings.py``
removes all inner quoting, so nothing can be mangled by the shell.

Usage:
    python scripts/swap_embeddings.py [VECTORS_DIR]

VECTORS_DIR defaults to ``<repo>/data/vectors``. On success prints
``SWAP_OK rows=<N>`` and exits 0; on any failure prints ``SWAP_FAILED: ...``
and exits 1 (live files are left untouched).
"""

import json
import os
import sys

EMBEDDINGS_NAME = "ticket_embeddings.npy"
METADATA_NAME = "ticket_metadata.json"
EXPECTED_DIMS = 1024


def _default_vectors_dir() -> str:
    # <repo>/scripts/swap_embeddings.py -> <repo>/data/vectors
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    return os.path.join(repo_root, "data", "vectors")


def swap_embeddings(vectors_dir: str) -> int:
    """
    Validate the ``*.tmp`` files in ``vectors_dir`` and atomically swap them in.

    Returns the new row count on success. Raises on any validation failure
    (before any live file is modified), so a bad transfer never clobbers the
    currently-served files.
    """
    import numpy as np  # imported lazily so --help/arg errors don't need numpy

    live_emb = os.path.join(vectors_dir, EMBEDDINGS_NAME)
    live_meta = os.path.join(vectors_dir, METADATA_NAME)
    tmp_emb = live_emb + ".tmp"
    tmp_meta = live_meta + ".tmp"

    if not os.path.exists(tmp_emb):
        raise FileNotFoundError(f"missing temp embeddings file: {tmp_emb}")
    if not os.path.exists(tmp_meta):
        raise FileNotFoundError(f"missing temp metadata file: {tmp_meta}")

    # Validate the transferred temp files BEFORE touching anything live.
    arr = np.load(tmp_emb, mmap_mode="r")
    with open(tmp_meta, encoding="utf-8") as fh:
        meta = json.load(fh)

    if arr.shape[0] != len(meta):
        raise ValueError(
            f"row/meta mismatch: {arr.shape[0]} vectors vs {len(meta)} metadata rows"
        )
    if arr.shape[1] != EXPECTED_DIMS:
        raise ValueError(f"bad dims: expected {EXPECTED_DIMS}, got {arr.shape[1]}")

    rows = int(arr.shape[0])
    # Release the mmap handle before os.replace on Windows (can't replace a file
    # that still has an open mapping).
    del arr

    # Back up the current live files (if any), then move temp -> live.
    if os.path.exists(live_meta):
        os.replace(live_meta, live_meta + ".bak")
    if os.path.exists(live_emb):
        os.replace(live_emb, live_emb + ".bak")
    os.replace(tmp_meta, live_meta)
    os.replace(tmp_emb, live_emb)
    return rows


def main() -> int:
    vectors_dir = sys.argv[1] if len(sys.argv) > 1 else _default_vectors_dir()
    try:
        rows = swap_embeddings(vectors_dir)
    except Exception as e:  # noqa: BLE001 — report cleanly, never traceback-crash
        print(f"SWAP_FAILED: {e}")
        return 1
    print(f"SWAP_OK rows={rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
