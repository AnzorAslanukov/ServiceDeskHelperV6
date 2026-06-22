"""
Async client for Databricks serving endpoints.
Provides embedding generation and LLM inference via REST API.

Note: Vector similarity search has been moved to LocalVectorStore
(src/services/local_vector_store.py) for fast in-memory queries.
"""

import httpx

from src.config import Settings


class DatabricksClient:
    """Client for Databricks serving endpoints (embeddings + LLM)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http_client: httpx.AsyncClient | None = None

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

    async def call_llm_stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 2048,
    ):
        """
        Stream Claude Sonnet 4.5 response tokens via the Databricks serving endpoint.

        Uses the OpenAI-compatible chat completions format with stream=true.
        Yields text chunks as they arrive.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            max_tokens: Maximum tokens in the response.

        Yields:
            str: Text chunks (delta content) as they arrive from the LLM.
        """
        import json as json_module

        client = await self._get_http_client()
        async with client.stream(
            "POST",
            self._settings.databricks_sonnet_url,
            headers=self._auth_headers(),
            json={
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                # SSE format: "data: {...}" or "data: [DONE]"
                if not line.startswith("data: "):
                    continue
                payload = line[6:]  # Strip "data: " prefix
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json_module.loads(payload)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json_module.JSONDecodeError, IndexError, KeyError):
                    continue

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close all connections."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None