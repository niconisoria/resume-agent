import asyncio
import logging

from app import db

log = logging.getLogger(__name__)

# claude-haiku-5-5 list price, USD per million tokens (prompts up to 100K).
HAIKU_INPUT_PER_MTOK = 0.10
HAIKU_OUTPUT_PER_MTOK = 0.50

COLUMNS = (
    "type",
    "conversation_id",
    "message_id",
    "tool_name",
    "latency_ms",
    "first_token_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
)
INSERT = (
    f"insert into events ({', '.join(COLUMNS)})"
    f" values ({', '.join(f'%({c})s' for c in COLUMNS)})"
)

_tasks: set[asyncio.Task] = set()


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * HAIKU_INPUT_PER_MTOK + output_tokens * HAIKU_OUTPUT_PER_MTOK
    ) / 1_000_000


async def _write(rows: list[dict]) -> None:
    try:
        async with db.pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                INSERT, [{c: row.get(c) for c in COLUMNS} for row in rows]
            )
    except Exception:
        log.exception("event write failed (%d rows)", len(rows))


def record(rows: list[dict]) -> None:
    """Write event rows in the background; never raises, never blocks the caller."""
    if not rows:
        return
    task = asyncio.create_task(_write(rows))
    _tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for pending writes (shutdown, tests)."""
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
