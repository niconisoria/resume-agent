import uuid

from psycopg_pool import AsyncConnectionPool

from app.config import DATABASE_URL

MAX_TURNS = 20

# Neon's pooled endpoint is PgBouncer in transaction mode: no prepared statements.
# timeout: fail fast (503) when Neon is unreachable; still covers a cold-start wake (~1s).
# Neon suspends after 5 idle minutes and closes every connection: `check` replaces dead
# ones on checkout, `max_idle` recycles idle ones before the suspend.
pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    timeout=5,
    check=AsyncConnectionPool.check_connection,
    max_idle=240,
    open=False,
    kwargs={"prepare_threshold": None},
)


class TurnLimit(Exception):
    pass


async def start_turn(
    conversation_id: uuid.UUID | None, ip_hash: str, text: str
) -> tuple[uuid.UUID, list[dict], str]:
    """Load or create the conversation, count the turn, save the user message.

    Unknown or absent ids start a fresh conversation. Returns (id, prior messages
    oldest first excluding the one just saved, status).
    """
    async with pool.connection() as conn, conn.transaction():
        row = None
        if conversation_id:
            cur = await conn.execute(
                "select turns, status from conversations where id = %s for update",
                (conversation_id,),
            )
            row = await cur.fetchone()
        if row is None:
            conversation_id = uuid.uuid4()
            await conn.execute(
                "insert into conversations (id, ip_hash) values (%s, %s)",
                (conversation_id, ip_hash),
            )
            turns, status = 0, "open"
        else:
            turns, status = row
        if turns >= MAX_TURNS:
            raise TurnLimit

        cur = await conn.execute(
            "select role, content from messages where conversation_id = %s"
            " order by created_at, id",
            (conversation_id,),
        )
        history = [{"role": r, "content": c} for r, c in await cur.fetchall()]
        await conn.execute(
            "update conversations set turns = turns + 1 where id = %s",
            (conversation_id,),
        )
        await conn.execute(
            "insert into messages (conversation_id, role, content)"
            " values (%s, 'user', %s)",
            (conversation_id, text),
        )
    return conversation_id, history, status


async def mark_escalated(conversation_id: uuid.UUID) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update conversations set status = 'escalated' where id = %s",
            (conversation_id,),
        )


async def save_reply(conversation_id: uuid.UUID, text: str) -> uuid.UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into messages (conversation_id, role, content)"
            " values (%s, 'assistant', %s) returning id",
            (conversation_id, text),
        )
        return (await cur.fetchone())[0]


async def save_feedback(message_id: uuid.UUID, rating: int) -> bool:
    """Store the latest vote for an assistant message; False if no such message.

    Upsert on the partial unique index (migration 003): one row per message,
    a re-vote replaces the rating atomically.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into events (type, conversation_id, message_id, rating)"
            " select 'feedback', conversation_id, id, %s from messages"
            " where id = %s and role = 'assistant'"
            " on conflict (message_id) where type = 'feedback'"
            " do update set rating = excluded.rating, created_at = now()",
            (rating, message_id),
        )
        return cur.rowcount == 1


async def spent_today() -> float:
    """Model spend (USD) since the start of the current UTC day."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select coalesce(sum(cost_usd), 0) from events"
            " where created_at >= date_trunc('day', now() at time zone 'utc')"
            " at time zone 'utc'"
        )
        return float((await cur.fetchone())[0])
