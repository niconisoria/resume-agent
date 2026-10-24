import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi.errors import RateLimitExceeded

from app import analytics, budget, db
from app.agent import ReplyRefused, Turn, booking_card, cv_card, stream_reply
from app.config import ALLOWED_ORIGIN, IP_HASH_SALT
from app.handoff import redact_emails
from app.ratelimit import (
    CHAT_LIMITS,
    FEEDBACK_LIMITS,
    client_ip,
    limiter,
    rate_limited,
)

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MESSAGE_ERROR = f"Message must be between 1 and {MAX_MESSAGE_CHARS:,} characters."
BUDGET_REPLY = (
    "Nicolas's assistant is resting for today. You can book a call with him "
    "or grab his CV below."
)
RATING_ERROR = "Rating must be 1 (👍) or -1 (👎)."
FIELD_ERRORS = {"message": MESSAGE_ERROR, "rating": RATING_ERROR}
UNAVAILABLE_ERROR = (
    "Sorry, the assistant can't answer right now. Please try again later."
)
REFUSAL_ERROR = "Sorry, the assistant can't help with that."
TURN_LIMIT_ERROR = (
    f"This chat reached its {db.MAX_TURNS}-message limit. Please start a new one."
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.pool.open()
    yield
    await analytics.drain()
    await db.pool.close()


app = FastAPI(title="resume-agent", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limited)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: uuid.UUID | None = None

    @field_validator("message", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    fields = [err["loc"][-1] for err in exc.errors()]
    error = next(
        (FIELD_ERRORS[f] for f in fields if f in FIELD_ERRORS), "Invalid request."
    )
    return JSONResponse(status_code=422, content={"error": error})


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def ip_hash(ip: str) -> str:
    return hashlib.sha256(f"{ip}{IP_HASH_SALT}".encode()).hexdigest()


async def chat_events(
    conversation_id: uuid.UUID, message: str, history: list[dict], escalated: bool
):
    parts: list[str] = []
    done: dict = {"citations": [], "cards": []}
    turn = Turn(conversation_id=conversation_id, escalated=escalated)
    message_id = None
    started = time.perf_counter()
    first_text_at = None
    try:
        async for event in stream_reply(message, history, turn):
            if "text" in event:
                first_text_at = first_text_at or time.perf_counter()
                parts.append(event["text"])
                yield sse(event)
            else:
                done = event
    except ReplyRefused:
        yield sse({"error": REFUSAL_ERROR})
    except Exception:
        # HTTP 200 already sent: any failure must surface as a frame, never a dead stream.
        log.exception("reply stream failed")
        yield sse({"error": UNAVAILABLE_ERROR})
    else:
        reply = "".join(parts)
        # Empty assistant turns are rejected by the API and would break the chat.
        if reply.strip():
            try:
                message_id = await db.save_reply(conversation_id, redact_emails(reply))
            except Exception:
                log.exception("save_reply failed")
    # Always last: the widget keeps the chat id even when the reply failed.
    yield sse(
        {
            "conversation_id": str(conversation_id),
            "message_id": str(message_id) if message_id else None,
            "citations": done["citations"],
            "cards": done["cards"],
        }
    )
    analytics.record(_events(turn, message_id, started, first_text_at))


def _ms(start: float, end: float | None) -> int | None:
    return None if end is None else round((end - start) * 1000)


def _events(turn: Turn, message_id, started: float, first_text_at: float | None):
    """Event rows for one reply: ids, names and numbers only, never text.

    An `answer` row is written whenever the model spent tokens, so cost is counted
    even for failed replies (`message_id` is then null).
    """
    cid = turn.conversation_id
    rows = [
        {"type": "tool_call", "conversation_id": cid, "tool_name": n}
        for n in turn.tools
    ]
    if turn.ticket:
        rows.append({"type": "escalation", "conversation_id": cid})
    if turn.input_tokens or turn.output_tokens:
        rows.append(
            {
                "type": "answer",
                "conversation_id": cid,
                "message_id": message_id,
                "latency_ms": _ms(started, time.perf_counter()),
                "first_token_ms": _ms(started, first_text_at),
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cost_usd": analytics.cost_usd(turn.input_tokens, turn.output_tokens),
            }
        )
    return rows


async def budget_events(conversation_id: uuid.UUID | None):
    """Fallback reply once the daily budget is spent: no model, nothing stored."""
    yield sse({"text": BUDGET_REPLY})
    yield sse(
        {
            "conversation_id": str(conversation_id) if conversation_id else None,
            "message_id": None,
            "citations": [],
            "cards": [booking_card(), cv_card("en")],
        }
    )


@app.post("/chat")
@limiter.limit(CHAT_LIMITS)
async def chat(request: Request, body: ChatIn):  # slowapi needs `request`
    try:
        blocked = await budget.over_budget()
    except Exception:
        # Spend unknown: fail closed, never call the model blind.
        log.exception("budget check failed")
        return JSONResponse(status_code=503, content={"error": UNAVAILABLE_ERROR})
    if blocked:
        # No conversation_id: the sent id is unverified here (events FK would reject
        # unknown ids and drop the row), and the budget is global anyway.
        analytics.record([{"type": "budget_exceeded"}])
        return StreamingResponse(
            budget_events(body.conversation_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    try:
        # Emails go only to the handoff ticket, never into stored messages.
        conversation_id, history, status = await db.start_turn(
            body.conversation_id,
            ip_hash(client_ip(request)),
            redact_emails(body.message),
        )
    except db.TurnLimit:
        analytics.record(
            [{"type": "rate_limited", "conversation_id": body.conversation_id}]
        )
        return JSONResponse(status_code=429, content={"error": TURN_LIMIT_ERROR})
    except Exception:
        log.exception("start_turn failed")
        return JSONResponse(status_code=503, content={"error": UNAVAILABLE_ERROR})
    return StreamingResponse(
        chat_events(conversation_id, body.message, history, status == "escalated"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


class FeedbackIn(BaseModel):
    message_id: uuid.UUID
    rating: Literal[1, -1]


@app.post("/feedback")
@limiter.limit(FEEDBACK_LIMITS)
async def feedback(request: Request, body: FeedbackIn):  # slowapi needs `request`
    try:
        saved = await db.save_feedback(body.message_id, body.rating)
    except Exception:
        log.exception("save_feedback failed")
        return JSONResponse(status_code=503, content={"error": UNAVAILABLE_ERROR})
    if not saved:
        return JSONResponse(status_code=404, content={"error": "Answer not found."})
    return {"status": "saved"}


@app.get("/health")
def health():
    return {"status": "ok"}
