import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi.errors import RateLimitExceeded

from app import db
from app.agent import ReplyRefused, stream_reply
from app.config import ALLOWED_ORIGIN, IP_HASH_SALT
from app.ratelimit import CHAT_LIMITS, client_ip, limiter, rate_limited

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MESSAGE_ERROR = f"Message must be between 1 and {MAX_MESSAGE_CHARS:,} characters."
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
    on_message = any(err["loc"][-1] == "message" for err in exc.errors())
    error = MESSAGE_ERROR if on_message else "Invalid request."
    return JSONResponse(status_code=422, content={"error": error})


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def ip_hash(ip: str) -> str:
    return hashlib.sha256(f"{ip}{IP_HASH_SALT}".encode()).hexdigest()


async def chat_events(conversation_id: uuid.UUID, message: str, history: list[dict]):
    parts: list[str] = []
    citations: list[dict] = []
    message_id = None
    try:
        async for event in stream_reply(message, history):
            if "text" in event:
                parts.append(event["text"])
                yield sse(event)
            else:
                citations = event["citations"]
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
                message_id = await db.save_reply(conversation_id, reply)
            except Exception:
                log.exception("save_reply failed")
    # Always last: the widget keeps the chat id even when the reply failed.
    yield sse(
        {
            "conversation_id": str(conversation_id),
            "message_id": str(message_id) if message_id else None,
            "citations": citations,
        }
    )


@app.post("/chat")
@limiter.limit(CHAT_LIMITS)
async def chat(request: Request, body: ChatIn):  # slowapi needs `request`
    try:
        conversation_id, history = await db.start_turn(
            body.conversation_id, ip_hash(client_ip(request)), body.message
        )
    except db.TurnLimit:
        return JSONResponse(status_code=429, content={"error": TURN_LIMIT_ERROR})
    except Exception:
        log.exception("start_turn failed")
        return JSONResponse(status_code=503, content={"error": UNAVAILABLE_ERROR})
    return StreamingResponse(
        chat_events(conversation_id, body.message, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
