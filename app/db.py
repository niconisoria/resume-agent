import uuid

from psycopg_pool import AsyncConnectionPool

from app.config import DATABASE_URL

MAX_TURNS = 20

# Neon's pooled endpoint is PgBouncer in transaction mode: no prepared statements.
# timeout: fail fast (503) when Neon is unreachable; still covers a cold-start wake (~1s).
pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    timeout=5,
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
