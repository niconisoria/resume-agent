import json
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.agent import ReplyRefused, stream_reply
from app.config import ALLOWED_ORIGIN

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MESSAGE_ERROR = f"Message must be between 1 and {MAX_MESSAGE_CHARS:,} characters."
MODEL_ERROR = "Sorry, the assistant can't answer right now. Please try again later."
REFUSAL_ERROR = "Sorry, the assistant can't help with that."

app = FastAPI(title="resume-agent")
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


async def chat_events(message: str):
    try:
        async for text in stream_reply(message):
            yield sse({"text": text})
    except ReplyRefused:
        yield sse({"error": REFUSAL_ERROR})
    except Exception:
        # HTTP 200 already sent: any failure must surface as a frame, never a dead stream.
        log.exception("reply stream failed")
        yield sse({"error": MODEL_ERROR})


@app.post("/chat")
async def chat(body: ChatIn):
    return StreamingResponse(
        chat_events(body.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
