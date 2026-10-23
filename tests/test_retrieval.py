import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app import db
from app import retrieval as retrieval_module


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    async def execute(self, sql, params):
        self.sql.append((sql, params))
        return FakeCursor(self.rows)


def test_search_embeds_as_query_and_drops_hits_below_floor(monkeypatch, fake_search):
    real = retrieval_module
    embedded = []

    def fake_embed(texts, input_type):
        embedded.append((texts, input_type))
        return [[0.5] * 1024]

    rows = [
        ("Vention", "roles/vention.md", "AnyCable", 0.82),
        ("Skills", "skills.md", "Ruby", real.MIN_SIMILARITY),
        ("Profile", "profile.md", "Summary", real.MIN_SIMILARITY - 0.01),
    ]
    conn = FakeConn(rows)

    @asynccontextmanager
    async def connection():
        yield conn

    monkeypatch.setattr(real, "embed", fake_embed)
    monkeypatch.setattr(db, "pool", SimpleNamespace(connection=connection))

    hits = asyncio.run(fake_search.real("WebSockets work"))

    assert embedded == [(["WebSockets work"], "query")]
    assert [h.path for h in hits] == ["roles/vention.md", "skills.md"]
    sql, params = conn.sql[0]
    assert "<=>" in sql and "limit" in sql.lower()
    assert params[0].startswith("[") and params[-1] == real.TOP_K
