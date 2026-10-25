import math
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app import analytics

CHAT_LIMITS = "10/minute;60/day"
FEEDBACK_LIMITS = "30/minute"
INSIGHTS_LIMITS = "30/minute"


def client_ip(request: Request) -> str:
    # Fly's proxy sets Fly-Client-IP; absent locally and in tests.
    return request.headers.get("fly-client-ip") or request.client.host


# ponytail: in-memory, fixed window; valid only with exactly one Fly machine.
limiter = Limiter(key_func=client_ip)


async def rate_limited(request: Request, _: RateLimitExceeded) -> JSONResponse:
    limit, keys = request.state.view_rate_limit
    reset_at = limiter.limiter.get_window_stats(limit, *keys).reset_time
    retry = max(1, math.ceil(reset_at - time.time()))
    if request.url.path == "/chat":  # blocks metric counts chat only, not feedback
        analytics.record([{"type": "rate_limited"}])
    return JSONResponse(
        status_code=429,
        content={"error": f"Too many messages. Please try again in {retry} seconds."},
        headers={"Retry-After": str(retry)},
    )
