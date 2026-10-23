import os

import httpx2

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
MODEL = "voyage-4"
BATCH = 128


def embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Voyage embeddings; input_type is "document" at ingest, "query" at search."""
    headers = {"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"}
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        res = httpx2.post(
            VOYAGE_URL,
            headers=headers,
            json={
                "input": texts[i : i + BATCH],
                "model": MODEL,
                "input_type": input_type,
            },
            timeout=30,
        )
        res.raise_for_status()
        vectors += [
            d["embedding"] for d in sorted(res.json()["data"], key=lambda d: d["index"])
        ]
    return vectors
