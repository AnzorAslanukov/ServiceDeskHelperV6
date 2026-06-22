"""
Local vector store for fast in-memory similarity search.

Replaces Databricks SQL warehouse queries with local NumPy-based
cosine similarity. Data is loaded from .npz files at startup.

Performance: <5ms for documentation (6,709 rows), ~50ms for tickets (170K+ rows)
vs 2-120s for Databricks SQL over the network.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Default data directory (relative to project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data" / "vectors"


class LocalVectorStore:
    """In-memory vector store loaded from local .npz files."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self._loaded = False

        # Documentation data
        self._doc_embeddings: np.ndarray | None = None  # (N, 1024)
        self._doc_norms: np.ndarray | None = None  # (N,) precomputed
        self._doc_metadata: list[dict[str, Any]] = []  # [{content, notebook, section, title}, ...]

        # Ticket data
        self._ticket_embeddings: np.ndarray | None = None  # (M, 1024)
        self._ticket_norms: np.ndarray | None = None  # (M,) precomputed
        self._ticket_metadata: list[dict[str, Any]] = []  # [{Id, Title, Description, SupportGroup, Location}, ...]
        self._ticket_id_to_index: dict[str, int] = {}  # ticket_id -> row index

    def load(self) -> None:
        """Load vector data from disk into memory. Call once at startup."""
        if self._loaded:
            return

        self._load_documentation()
        self._load_tickets()
        self._loaded = True

    def _load_documentation(self) -> None:
        """Load OneNote documentation embeddings and metadata."""
        embeddings_path = self._data_dir / "onenote_embeddings.npy"
        metadata_path = self._data_dir / "onenote_metadata.json"

        if not embeddings_path.exists() or not metadata_path.exists():
            logger.warning(
                "Documentation vector files not found at %s. "
                "Run 'python -m exploration.export_local_vectors' to generate them.",
                self._data_dir,
            )
            return

        logger.info("Loading documentation vectors from %s", embeddings_path)
        self._doc_embeddings = np.load(str(embeddings_path))
        with open(metadata_path, "r", encoding="utf-8") as f:
            self._doc_metadata = json.load(f)

        # Precompute norms for fast cosine similarity
        self._doc_norms = np.linalg.norm(self._doc_embeddings, axis=1)
        # Replace zero norms to avoid division by zero
        self._doc_norms[self._doc_norms == 0] = 1.0

        logger.info(
            "Loaded %d documentation vectors (%d dims)",
            self._doc_embeddings.shape[0],
            self._doc_embeddings.shape[1],
        )

    def _load_tickets(self) -> None:
        """Load ticket embeddings and metadata."""
        embeddings_path = self._data_dir / "ticket_embeddings.npy"
        metadata_path = self._data_dir / "ticket_metadata.json"

        if not embeddings_path.exists() or not metadata_path.exists():
            logger.warning(
                "Ticket vector files not found at %s. "
                "Run 'python -m exploration.export_local_vectors' to generate them.",
                self._data_dir,
            )
            return

        logger.info("Loading ticket vectors from %s", embeddings_path)
        self._ticket_embeddings = np.load(str(embeddings_path))
        with open(metadata_path, "r", encoding="utf-8") as f:
            self._ticket_metadata = json.load(f)

        # Precompute norms for fast cosine similarity
        self._ticket_norms = np.linalg.norm(self._ticket_embeddings, axis=1)
        self._ticket_norms[self._ticket_norms == 0] = 1.0

        # Build ID lookup index
        self._ticket_id_to_index = {
            meta["Id"]: i for i, meta in enumerate(self._ticket_metadata) if meta.get("Id")
        }

        logger.info(
            "Loaded %d ticket vectors (%d dims)",
            self._ticket_embeddings.shape[0],
            self._ticket_embeddings.shape[1],
        )

    def _cosine_similarity(
        self,
        query_embedding: list[float] | np.ndarray,
        matrix: np.ndarray,
        norms: np.ndarray,
    ) -> np.ndarray:
        """
        Compute cosine similarity between a query vector and a matrix of vectors.

        Args:
            query_embedding: 1D query vector (1024 dims).
            matrix: 2D matrix of stored vectors (N x 1024).
            norms: Precomputed L2 norms of each row in matrix.

        Returns:
            1D array of similarity scores (N,).
        """
        query = np.asarray(query_embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return np.zeros(matrix.shape[0], dtype=np.float32)

        # Dot product divided by product of norms
        similarities = matrix.dot(query) / (norms * query_norm)
        return similarities

    def find_similar_documentation(
        self,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Find the most similar OneNote documentation entries by cosine similarity.

        Args:
            embedding: Query embedding vector (1024 dims).
            top_k: Number of top results to return.

        Returns:
            List of dicts with content, notebook, section, title, and similarity.
        """
        if self._doc_embeddings is None or len(self._doc_metadata) == 0:
            return []

        similarities = self._cosine_similarity(
            embedding, self._doc_embeddings, self._doc_norms
        )

        # Get top-k indices
        top_k = min(top_k, len(similarities))
        top_indices = np.argpartition(similarities, -top_k)[-top_k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            meta = self._doc_metadata[idx]
            results.append({
                "content": meta.get("content", ""),
                "notebook": meta.get("notebook", ""),
                "section": meta.get("section", ""),
                "title": meta.get("title", ""),
                "similarity": float(similarities[idx]),
            })

        return results

    def find_similar_by_embedding(
        self,
        embedding: list[float],
        table: str | None = None,
        embedding_column: str | None = None,
        id_column: str | None = None,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Find the most similar ticket records by cosine similarity.

        Args:
            embedding: Query embedding vector (1024 dims).
            table: Ignored (kept for interface compatibility).
            embedding_column: Ignored (kept for interface compatibility).
            id_column: Ignored (kept for interface compatibility).
            top_k: Number of top results to return.

        Returns:
            List of dicts with 'id' and 'similarity' keys, sorted by similarity desc.
        """
        if self._ticket_embeddings is None or len(self._ticket_metadata) == 0:
            return []

        similarities = self._cosine_similarity(
            embedding, self._ticket_embeddings, self._ticket_norms
        )

        # Get top-k indices
        top_k = min(top_k, len(similarities))
        top_indices = np.argpartition(similarities, -top_k)[-top_k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            meta = self._ticket_metadata[idx]
            results.append({
                "id": meta.get("Id", ""),
                "similarity": float(similarities[idx]),
            })

        return results

    def get_ticket_embedding(self, ticket_id: str) -> list[float] | None:
        """
        Retrieve the pre-computed embedding for a specific ticket ID.

        Args:
            ticket_id: Ticket identifier (e.g., 'IR1959493').

        Returns:
            Embedding vector or None if not found.
        """
        if self._ticket_embeddings is None:
            return None

        idx = self._ticket_id_to_index.get(ticket_id)
        if idx is None:
            return None

        return self._ticket_embeddings[idx].tolist()

    @property
    def is_loaded(self) -> bool:
        """Whether data has been loaded."""
        return self._loaded

    @property
    def documentation_count(self) -> int:
        """Number of documentation entries loaded."""
        return len(self._doc_metadata)

    @property
    def ticket_count(self) -> int:
        """Number of ticket entries loaded."""
        return len(self._ticket_metadata)