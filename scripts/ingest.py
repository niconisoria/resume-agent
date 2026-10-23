"""Load knowledge/*.md into Postgres + pgvector.

Usage: uv run --env-file .env python -m scripts.ingest
"""

import hashlib
import os
import re
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.embeddings import embed, to_pgvector

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
MIGRATION = ROOT / "migrations" / "001_init.sql"
REQUIRED_ENV = ("DATABASE_URL", "VOYAGE_API_KEY")
# ponytail: word count as token estimate (~0.75 words/token); real tokenizer if chunks misbehave.
MAX_WORDS = 375


def title_of(text: str, path: str) -> str:
    match = re.search(r"^# (.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else Path(path).stem


def _sections(text: str) -> list[tuple[str | None, str]]:
    body = re.sub(r"^# .+$\n?", "", text, count=1, flags=re.MULTILINE)
    parts = re.split(r"^## (.+)$", body, flags=re.MULTILINE)
    sections = [(None, parts[0].strip())] if parts[0].strip() else []
    sections += [
        (parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)
    ]
    return sections


def _paragraph_groups(body: str) -> list[str]:
    groups, current = [], []
    for para in [p.strip() for p in body.split("\n\n") if p.strip()]:
        if current and len(" ".join(current + [para]).split()) > MAX_WORDS:
            groups.append("\n\n".join(current))
            current = []
        current.append(para)
    if current:
        groups.append("\n\n".join(current))
    return groups


def chunk(text: str, title: str) -> list[str]:
    sections = _sections(text)
    whole = "\n\n".join(f"## {h}\n\n{b}" if h else b for h, b in sections)
    if len(whole.split()) <= MAX_WORDS:
        return [f"{title}\n\n{whole}"]
    chunks = []
    for heading, body in sections:
        header = f"{title} > {heading}" if heading else title
        chunks += [f"{header}\n\n{group}" for group in _paragraph_groups(body)]
    return chunks


def plan(files: dict[str, str], existing: dict[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(p for p in files if p not in existing),
        "updated": sorted(
            p for p in files if p in existing and existing[p] != files[p]
        ),
        "removed": sorted(p for p in existing if p not in files),
        "skipped": sorted(p for p in files if existing.get(p) == files[p]),
    }


def _write_document(conn, path, title, sha, pieces, vectors) -> None:
    doc_id = conn.execute(
        "insert into documents (path, title, metadata, updated_at)"
        " values (%s, %s, %s, now())"
        " on conflict (path) do update set title = excluded.title,"
        " metadata = excluded.metadata, updated_at = now()"
        " returning id",
        (path, title, Jsonb({"sha256": sha})),
    ).fetchone()[0]
    conn.execute("delete from chunks where document_id = %s", (doc_id,))
    for position, (content, vector) in enumerate(zip(pieces, vectors, strict=True)):
        conn.execute(
            "insert into chunks (document_id, content, embedding, position)"
            " values (%s, %s, %s::vector, %s)",
            (doc_id, content, to_pgvector(vector), position),
        )


def run(conn, embedder, knowledge_dir: Path = KNOWLEDGE_DIR) -> dict[str, int]:
    texts = {
        p.relative_to(knowledge_dir).as_posix(): p.read_text()
        for p in sorted(knowledge_dir.rglob("*.md"))
    }
    hashes = {path: hashlib.sha256(t.encode()).hexdigest() for path, t in texts.items()}

    # One transaction: any failure (embedding included) rolls everything back.
    with conn.transaction():
        conn.execute(MIGRATION.read_text())
        existing = dict(
            conn.execute("select path, metadata->>'sha256' from documents").fetchall()
        )
        changes = plan(hashes, existing)
        for path in changes["removed"]:
            conn.execute("delete from documents where path = %s", (path,))
        changed = changes["added"] + changes["updated"]
        titles = {path: title_of(texts[path], path) for path in changed}
        pieces = {path: chunk(texts[path], titles[path]) for path in changed}
        # One batched embed call for every changed chunk: Voyage free tier is ~3 req/min.
        flat = [piece for path in changed for piece in pieces[path]]
        vectors = iter(embedder(flat, "document") if flat else [])
        for path in changed:
            own = [next(vectors) for _ in pieces[path]]
            _write_document(conn, path, titles[path], hashes[path], pieces[path], own)

    return {key: len(paths) for key, paths in changes.items()}


def main() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        summary = run(conn, embed)
    print(", ".join(f"{key} {count}" for key, count in summary.items()))


if __name__ == "__main__":
    main()
