"""
Async client for Databricks serving endpoints and SQL warehouse.
Provides embedding generation and vector similarity search via SQL.
"""

from typing import Any

import httpx
from databricks import sql as databricks_sql
from databricks.sql.client import Connection as DatabricksConnection

from src.config import Settings


class DatabricksClient:
    """Client for Databricks embedding endpoint and SQL warehouse."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http_client: httpx.AsyncClient | None = None
        self._sql_connection: DatabricksConnection | None = None

    # ── HTTP Client ───────────────────────────────────────────────────

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the async HTTP client for serving endpoints."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers for Databricks API."""
        return {
            "Authorization": f"Bearer {self._settings.databricks_api_key}",
            "Content-Type": "application/json",
        }

    # ── Embedding Generation ──────────────────────────────────────────

    async def generate_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        Generate 1024-dimensional embeddings for one or more texts
        using the GTE-Large-EN serving endpoint.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (each 1024 floats).
        """
        client = await self._get_http_client()
        response = await client.post(
            self._settings.databricks_embedding_url,
            headers=self._auth_headers(),
            json={"input": texts},
        )
        response.raise_for_status()
        data = response.json()
        # Sort by index to ensure order matches input
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_data]

    async def generate_embedding(self, text: str) -> list[float]:
        """
        Generate a single embedding vector for one text string.

        Args:
            text: Text to embed.

        Returns:
            1024-dimensional embedding vector.
        """
        embeddings = await self.generate_embeddings([text])
        return embeddings[0]

    # ── SQL Warehouse ─────────────────────────────────────────────────

    def _get_sql_connection(self) -> DatabricksConnection:
        """Get or create a SQL warehouse connection."""
        if self._sql_connection is None:
            self._sql_connection = databricks_sql.connect(
                server_hostname=self._settings.databricks_server_hostname,
                http_path=self._settings.databricks_http_path,
                access_token=self._settings.databricks_api_key,
            )
        return self._sql_connection

    def execute_query(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Execute a SQL query against the Databricks warehouse.

        Note: databricks-sql-connector is synchronous. For async contexts,
        run this in a thread executor.

        Args:
            query: SQL query string.
            parameters: Optional query parameters.

        Returns:
            List of result rows as dictionaries.
        """
        connection = self._get_sql_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(query, parameters)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            cursor.close()

    def find_similar_by_embedding(
        self,
        embedding: list[float],
        table: str = "scratchpad.aslanuka.ir_embeddings",
        embedding_column: str = "ticket_embedding",
        id_column: str = "id",
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Find the most similar records by cosine similarity against a vector column.

        Args:
            embedding: Query embedding vector (1024 dims).
            table: Fully qualified table name.
            embedding_column: Column containing stored embeddings.
            id_column: Column containing record identifiers.
            top_k: Number of top results to return.

        Returns:
            List of dicts with 'id' and 'similarity' keys, sorted by similarity desc.
        """
        # Build the embedding literal as a SQL array
        # Use CAST(x AS DOUBLE) to avoid scientific notation parsing issues
        embedding_str = ", ".join(f"CAST({v:.20f} AS DOUBLE)" for v in embedding)
        query = f"""
            SELECT
                {id_column} AS id,
                aggregate(
                    zip_with(
                        {embedding_column},
                        array({embedding_str}),
                        (a, b) -> a * b
                    ),
                    DOUBLE(0),
                    (acc, x) -> acc + x
                ) / (
                    sqrt(aggregate({embedding_column}, DOUBLE(0), (acc, x) -> acc + x * x))
                    * sqrt(aggregate(array({embedding_str}), DOUBLE(0), (acc, x) -> acc + x * x))
                ) AS similarity
            FROM {table}
            ORDER BY similarity DESC
            LIMIT {top_k}
        """
        return self.execute_query(query)

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
        embedding_str = ", ".join(f"CAST({v:.20f} AS DOUBLE)" for v in embedding)
        query = f"""
            SELECT
                content,
                notebook,
                section,
                title,
                aggregate(
                    zip_with(
                        embeddings,
                        array({embedding_str}),
                        (a, b) -> a * b
                    ),
                    DOUBLE(0),
                    (acc, x) -> acc + x
                ) / (
                    sqrt(aggregate(embeddings, DOUBLE(0), (acc, x) -> acc + x * x))
                    * sqrt(aggregate(array({embedding_str}), DOUBLE(0), (acc, x) -> acc + x * x))
                ) AS similarity
            FROM scratchpad.aslanuka.onenote_documentation
            ORDER BY similarity DESC
            LIMIT {top_k}
        """
        return self.execute_query(query)

    def get_ticket_embedding(self, ticket_id: str) -> list[float] | None:
        """
        Retrieve the pre-computed embedding for a specific ticket ID.

        Args:
            ticket_id: Ticket identifier (e.g., 'IR1959493').

        Returns:
            Embedding vector or None if not found.
        """
        query = """
            SELECT ticket_embedding
            FROM scratchpad.aslanuka.ir_embeddings
            WHERE id = :ticket_id
            LIMIT 1
        """
        results = self.execute_query(query, {"ticket_id": ticket_id})
        if results:
            raw = results[0]["ticket_embedding"]
            return self._parse_embedding(raw)
        return None

    @staticmethod
    def _parse_embedding(raw: Any) -> list[float]:
        """Parse an embedding that may be a list or a JSON string."""
        if isinstance(raw, list):
            return [float(v) for v in raw]
        if isinstance(raw, str):
            import json
            return [float(v) for v in json.loads(raw)]
        raise ValueError(f"Unexpected embedding type: {type(raw)}")

    # ── LLM Inference ─────────────────────────────────────────────────

    async def call_llm(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 2048,
    ) -> str:
        """
        Call Claude Sonnet 4.5 via the Databricks serving endpoint.

        Uses the OpenAI-compatible chat completions format.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            max_tokens: Maximum tokens in the response.

        Returns:
            The assistant's response text.
        """
        client = await self._get_http_client()
        response = await client.post(
            self._settings.databricks_sonnet_url,
            headers=self._auth_headers(),
            json={
                "messages": messages,
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close all connections."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
        if self._sql_connection:
            self._sql_connection.close()
            self._sql_connection = None