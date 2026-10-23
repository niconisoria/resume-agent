import hashlib
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from scripts import ingest


def words(n, tag="w"):
    return " ".join(f"{tag}{i}" for i in range(n))


class FakeConn:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.sql = []
        self.committed = False
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        text = sql.strip().lower()
        if text.startswith("select") and "from documents" in text:
            rows = list(self.existing.items())
        elif "returning id" in text:
            rows = [(len(self.sql),)]
        else:
            rows = []
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0])

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.rolled_back = True
            raise
        self.committed = True

    def writes(self, keyword):
        return [s for s, _ in self.sql if keyword in s.lower()]


def fake_embed(texts, input_type):
    assert input_type == "document"
    return [[0.0] * 1024 for _ in texts]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def kb(tmp_path):
    (tmp_path / "roles").mkdir()
    (tmp_path / "profile.md").write_text("# Profile\n\nNicolas builds backends.\n")
    (tmp_path / "roles" / "vention.md").write_text("# Vention\n\n## Stack\n\nRuby.\n")
    return tmp_path


def test_title_from_h1_else_filename():
    assert (
        ingest.title_of("# Vention Role\n\nBody", "roles/vention.md") == "Vention Role"
    )
    assert ingest.title_of("No heading here", "roles/vention.md") == "vention"


def test_short_file_is_one_chunk_with_title_header():
    chunks = ingest.chunk("# Profile\n\n## About\n\nShort body.\n", "Profile")
    assert len(chunks) == 1
    assert chunks[0].startswith("Profile")
    assert "Short body." in chunks[0]
    assert "# Profile" not in chunks[0]


def test_long_file_splits_per_section_with_header():
    text = f"# Role\n\n## Stack\n\n{words(250, 's')}\n\n## Wins\n\n{words(250, 'x')}\n"
    chunks = ingest.chunk(text, "Role")
    assert len(chunks) == 2
    assert chunks[0].startswith("Role > Stack")
    assert chunks[1].startswith("Role > Wins")


def test_long_section_splits_on_paragraphs_never_mid_paragraph():
    paragraphs = [words(200, f"p{n}_") for n in range(3)]
    text = "# Role\n\n## Wins\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = ingest.chunk(text, "Role")
    assert len(chunks) >= 2
    for p in paragraphs:
        assert sum(p in c for c in chunks) == 1
    assert all(c.startswith("Role > Wins") for c in chunks)


def test_plan_sorts_files_into_add_update_remove_skip():
    files = {"a.md": "1", "b.md": "2", "c.md": "3"}
    existing = {"b.md": "2", "c.md": "old", "d.md": "4"}
    assert ingest.plan(files, existing) == {
        "added": ["a.md"],
        "updated": ["c.md"],
        "removed": ["d.md"],
        "skipped": ["b.md"],
    }


def test_first_run_loads_every_file_in_one_transaction(kb):
    conn = FakeConn()
    summary = ingest.run(conn, fake_embed, kb)
    assert summary == {"added": 2, "updated": 0, "removed": 0, "skipped": 0}
    assert conn.committed
    assert conn.writes("create table if not exists documents")
    assert len(conn.writes("insert into documents")) == 2
    assert len(conn.writes("insert into chunks")) >= 2


def test_unchanged_rerun_writes_and_embeds_nothing(kb):
    existing = {
        "profile.md": sha((kb / "profile.md").read_text()),
        "roles/vention.md": sha((kb / "roles" / "vention.md").read_text()),
    }
    conn = FakeConn(existing)

    def no_embed(texts, input_type):
        raise AssertionError("re-embedded unchanged file")

    summary = ingest.run(conn, no_embed, kb)
    assert summary == {"added": 0, "updated": 0, "removed": 0, "skipped": 2}
    assert not conn.writes("insert into")


def test_edited_file_replaces_old_chunks(kb):
    existing = {"profile.md": "stale", "roles/vention.md": "stale"}
    conn = FakeConn(existing)
    summary = ingest.run(conn, fake_embed, kb)
    assert summary["updated"] == 2
    assert len(conn.writes("delete from chunks")) == 2


def test_deleted_file_removed(kb):
    existing = {"gone.md": "x"}
    conn = FakeConn(existing)
    summary = ingest.run(conn, fake_embed, kb)
    assert summary["removed"] == 1
    deletes = [p for s, p in conn.sql if "delete from documents" in s.lower()]
    assert deletes == [("gone.md",)]


def test_embedding_failure_writes_nothing(kb):
    conn = FakeConn()

    def broken(texts, input_type):
        raise RuntimeError("voyage down")

    with pytest.raises(RuntimeError):
        ingest.run(conn, broken, kb)
    assert conn.rolled_back
    assert not conn.committed


@pytest.mark.parametrize("missing", ["DATABASE_URL", "VOYAGE_API_KEY"])
def test_missing_env_exits_before_any_io(monkeypatch, missing):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    monkeypatch.delenv(missing)

    def no_connect(*args, **kwargs):
        raise AssertionError("connected despite missing env")

    monkeypatch.setattr(psycopg, "connect", no_connect)
    with pytest.raises(SystemExit) as exc:
        ingest.main()
    assert missing in str(exc.value)


def test_starter_knowledge_files_exist_without_email():
    root = ingest.KNOWLEDGE_DIR
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))
    assert files == ["profile.md", "roles/vention.md", "skills.md"]
    for p in root.rglob("*.md"):
        assert "@" not in p.read_text()


def test_chunk_rows_carry_1024_dim_vector_literal(kb):
    conn = FakeConn()
    ingest.run(conn, fake_embed, kb)
    vectors = [p[2] for s, p in conn.sql if "insert into chunks" in s.lower()]
    assert vectors
    for v in vectors:
        assert v.startswith("[") and v.endswith("]")
        assert len(v[1:-1].split(",")) == 1024


def test_embed_batches_and_keeps_input_order(monkeypatch):
    from app import embeddings

    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        batch = json["input"]
        data = [{"index": i, "embedding": [float(t)]} for i, t in enumerate(batch)]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": list(reversed(data))},
        )

    monkeypatch.setattr(embeddings.httpx2, "post", fake_post)
    texts = [str(n) for n in range(130)]
    vectors = embeddings.embed(texts, "query")
    assert [len(c["input"]) for c in calls] == [128, 2]
    assert all(c["input_type"] == "query" and c["model"] == "voyage-4" for c in calls)
    assert vectors == [[float(n)] for n in range(130)]


def test_all_changed_files_embedded_in_one_call(kb):
    calls = []

    def counting(texts, input_type):
        calls.append(len(texts))
        return fake_embed(texts, input_type)

    ingest.run(FakeConn(), counting, kb)
    assert len(calls) == 1
    assert calls[0] >= 2
