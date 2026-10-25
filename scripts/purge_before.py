"""One-time purge of dev/test rows created before a cutoff (S18).

    uv run --env-file .env python -m scripts.purge_before 2026-10-25T00:00:00Z
    uv run --env-file .env python -m scripts.purge_before 2026-10-25T00:00:00Z --yes

Without --yes only prints counts. Messages go with their conversations (cascade).
"""

import argparse
import datetime as dt
import os

import psycopg

# Events first: deleting conversations first would only null their events' ids.
TABLES = ("events", "conversations")
MESSAGES_SQL = (
    "select count(*) from messages m join conversations c on c.id = m.conversation_id"
    " where c.created_at < %s"
)


def parse_cutoff(value: str) -> dt.datetime:
    cutoff = dt.datetime.fromisoformat(value)
    if cutoff.tzinfo is None:
        raise SystemExit("cutoff needs a timezone, e.g. 2026-10-25T00:00:00Z")
    return cutoff


def purge(conn, cutoff: dt.datetime, apply: bool) -> dict[str, int]:
    counts = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(
                f"select count(*) from {table} where created_at < %s", (cutoff,)
            )
            counts[table] = cur.fetchone()[0]
        cur.execute(MESSAGES_SQL, (cutoff,))
        counts["messages"] = cur.fetchone()[0]
        if apply:
            for table in TABLES:
                cur.execute(f"delete from {table} where created_at < %s", (cutoff,))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cutoff", type=parse_cutoff)
    parser.add_argument("--yes", action="store_true", help="delete (default: count)")
    args = parser.parse_args()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        counts = purge(conn, args.cutoff, args.yes)
    verb = "deleted" if args.yes else "would delete"
    print(f"{verb} before {args.cutoff.isoformat()}: {counts}")


if __name__ == "__main__":
    main()
