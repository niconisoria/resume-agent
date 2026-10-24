from app import db
from app.config import DAILY_BUDGET_USD


async def over_budget() -> bool:
    """True once today's (UTC) model spend reaches the daily cap. Raises on DB error."""
    return await db.spent_today() >= DAILY_BUDGET_USD
