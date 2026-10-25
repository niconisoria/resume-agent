import asyncio
import datetime as dt
import logging
import time
from typing import Literal, get_args

from pydantic import BaseModel

from app import agent, db

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
    "topic",
    "no_hits",
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


Topic = Literal[
    "experience",
    "skills",
    "projects",
    "ai_work",
    "availability",
    "logistics",
    "out_of_scope",
    "other",
]
TOPICS = get_args(Topic)
LABEL_PROMPT = (
    "Label the visitor's message to Nicolas's resume assistant with one topic: "
    "experience (roles, employers), skills (tech, languages), projects, "
    "ai_work (LLMs, agents, this assistant), availability (hiring, rates, start "
    "date), logistics (call, CV, contact, location), out_of_scope (unrelated to "
    "Nicolas), other (greetings, small talk)."
)


class Label(BaseModel):
    topic: Topic


async def label(message: str) -> tuple[str, float] | None:
    """Topic + label cost for one visitor message; None on any failure."""
    try:
        response = await agent.client.messages.parse(
            model=agent.MODEL,
            max_tokens=64,
            thinking={"type": "disabled"},
            system=LABEL_PROMPT,
            messages=[{"role": "user", "content": message}],
            output_format=Label,
        )
        usage = response.usage
        return response.parsed_output.topic, cost_usd(
            usage.input_tokens, usage.output_tokens
        )
    except Exception:
        log.exception("topic label failed")
        return None


async def _write(rows: list[dict], message: str | None = None) -> None:
    answer = next((r for r in rows if r["type"] == "answer"), None)
    # Failed replies (no message_id) are never counted in /insights: skip the label.
    answered = message and answer and answer.get("message_id")
    if answered and (labelled := await label(message)):
        answer["topic"], cost = labelled
        answer["cost_usd"] += cost  # counts in the daily budget
    try:
        async with db.pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                INSERT, [{c: row.get(c) for c in COLUMNS} for row in rows]
            )
    except Exception:
        log.exception("event write failed (%d rows)", len(rows))


def record(rows: list[dict], message: str | None = None) -> None:
    """Write event rows in the background; never raises, never blocks the caller.

    With `message`, the answer row is first labelled with its topic.
    """
    if not rows:
        return
    task = asyncio.create_task(_write(rows, message))
    _tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for pending writes (shutdown, tests)."""
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)


# --- /insights ------------------------------------------------------------------

WINDOW_DAYS = 30
CACHE_SECONDS = 300
# Answered = a stored reply; failed replies (message_id null) only count as cost.
_WINDOW = f"created_at >= now() - interval '{WINDOW_DAYS} days'"
_ANSWERED = f"type = 'answer' and message_id is not null and {_WINDOW}"
DAYS_SQL = f"""
select (created_at at time zone 'utc')::date, count(distinct conversation_id), count(*)
from events where {_ANSWERED} group by 1 order by 1
"""
TOTALS_SQL = f"""
select
  count(distinct conversation_id) filter (
    where (type = 'answer' and message_id is not null) or type = 'escalation'),
  count(distinct conversation_id) filter (where type = 'escalation'),
  count(*) filter (where type = 'feedback' and rating = 1),
  count(*) filter (where type = 'feedback' and rating = -1),
  percentile_cont(0.95) within group (order by latency_ms) filter (
    where type = 'answer' and message_id is not null),
  sum(cost_usd),
  count(*) filter (where type = 'rate_limited'),
  count(*) filter (where type = 'budget_exceeded')
from events where {_WINDOW}
"""
TOPICS_SQL = f"""
select topic, count(*), count(*) filter (where no_hits)
from events where {_ANSWERED} and topic is not null group by 1
"""

_cache: tuple[float, dict] | None = None


async def _fetch() -> dict:
    async with db.pool.connection() as conn:
        days = await (await conn.execute(DAYS_SQL)).fetchall()
        totals = await (await conn.execute(TOTALS_SQL)).fetchone()
        topics = await (await conn.execute(TOPICS_SQL)).fetchall()
    return {"days": days, "totals": totals, "topics": topics}


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _summarize(raw: dict) -> dict:
    convs, escalated, up, down, p95, cost, rate_limited, budget = raw["totals"]
    counts = {t: (0, 0) for t in TOPICS}
    counts |= {topic: (n, no_hits) for topic, n, no_hits in raw["topics"]}
    return {
        "window_days": WINDOW_DAYS,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "days": [
            {"date": day.isoformat(), "conversations": c, "messages": m}
            for day, c, m in raw["days"]
        ],
        "resolution": {
            "conversations": convs,
            "escalated": escalated,
            "resolved_share": _share(convs - escalated, convs),
        },
        "feedback": {"up": up, "down": down, "up_share": _share(up, up + down)},
        "topics": [
            {"topic": t, "messages": n, "no_hits": miss}
            for t, (n, miss) in sorted(counts.items(), key=lambda kv: -kv[1][0])
        ],
        "p95_latency_ms": None if p95 is None else round(p95),
        "cost_per_conversation_usd": float(cost) / convs if cost and convs else None,
        "blocked": {"rate_limited": rate_limited, "budget_exceeded": budget},
    }


async def stats() -> dict:
    """Aggregates for the public /insights page; counts only, never text or ids."""
    global _cache
    now = time.monotonic()
    if _cache is None or now >= _cache[0]:
        _cache = (now + CACHE_SECONDS, _summarize(await _fetch()))
    return _cache[1]
