import asyncio
from dataclasses import dataclass

from app import db
from app.embeddings import embed, to_pgvector

TOP_K = 5
# Cosine similarity floor; below it a chunk counts as "not relevant".
# Tuned live on stub knowledge (relevant best hits 0.28–0.51, off-topic ≤ 0.20).
# Retune when the real knowledge base lands (M5).
MIN_SIMILARITY = 0.25


@dataclass(frozen=True)
class Hit:
    title: str
    path: str
    content: str
    similarity: float


async def search(query: str) -> list[Hit]:
    [vector] = await asyncio.to_thread(embed, [query], "query")
    q = to_pgvector(vector)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "select d.title, d.path, c.content, 1 - (c.embedding <=> %s::vector)"
            " from chunks c join documents d on d.id = c.document_id"
            " order by c.embedding <=> %s::vector limit %s",
            (q, q, TOP_K),
        )
        rows = await cur.fetchall()
    return [Hit(*row) for row in rows if row[3] >= MIN_SIMILARITY]
