"""
Remote check: does the LIVE embeddings file actually match the manifest?

deploy.py runs this over SSH to decide whether the server's live embedding
files are truly the ones described by the manifest. This distinguishes:

  - "already applied"  -> live .npy row count == manifest 'rows'  -> LIVE_MATCH
  - "stale/stranded"   -> a prior swap failed, so the manifest was updated but
                          the live files were never replaced               -> LIVE_STALE

Prints exactly one token (LIVE_MATCH / LIVE_STALE / LIVE_UNKNOWN: ...) and
exits 0 for MATCH, 1 otherwise. Runs as a real file (not `python -c "..."`) so
no shell-quoting can mangle it — see scripts/swap_embeddings.py for the history.

Usage:
    python scripts/check_live_matches_manifest.py [VECTORS_DIR]
"""

import json
import os
import sys

EMBEDDINGS_NAME = "ticket_embeddings.npy"
MANIFEST_NAME = "ticket_vectors_manifest.json"


def _default_vectors_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data", "vectors")


def live_matches_manifest(vectors_dir: str) -> bool:
    import numpy as np  # lazy import

    live_emb = os.path.join(vectors_dir, EMBEDDINGS_NAME)
    manifest_path = os.path.join(vectors_dir, MANIFEST_NAME)
    if not os.path.exists(live_emb) or not os.path.exists(manifest_path):
        return False
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    expected_rows = int(manifest.get("rows", -1))
    arr = np.load(live_emb, mmap_mode="r")
    actual_rows = int(arr.shape[0])
    del arr
    return expected_rows >= 0 and actual_rows == expected_rows


def main() -> int:
    vectors_dir = sys.argv[1] if len(sys.argv) > 1 else _default_vectors_dir()
    try:
        matched = live_matches_manifest(vectors_dir)
    except Exception as e:  # noqa: BLE001
        print(f"LIVE_UNKNOWN: {e}")
        return 1
    if matched:
        print("LIVE_MATCH")
        return 0
    print("LIVE_STALE")
    return 1


if __name__ == "__main__":
    sys.exit(main())
