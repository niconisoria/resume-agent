"""The four tools the agent can call: schemas, handlers and per-reply state.

Handlers never raise for bad model input: they return an `is_error` tool result the
model can read and recover from. Outages (search, GitHub) are handled per tool.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field

from app import db, handoff, retrieval, tracing
from app.config import CALENDAR_URL, CV_URLS

MAX_QUERY_CHARS = 500
MAX_FIELD_CHARS = 1000

log = logging.getLogger(__name__)

SEARCH_TOOL = {
    "name": "search_knowledge",
    "description": (
        "Search Nicolas's knowledge base (profile, roles, projects, skills). "
        "Returns numbered excerpts with source title and path; cite them as [n]."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, in plain words.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

BOOK_CALL_TOOL = {
    "name": "book_call",
    "description": "Show the visitor a button to book a call with Nicolas.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

GET_CV_TOOL = {
    "name": "get_cv",
    "description": "Show the visitor a button to download Nicolas's CV (PDF).",
    "input_schema": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "pl"],
                "description": "CV language: en (English) or pl (Polish).",
            }
        },
        "required": ["language"],
        "additionalProperties": False,
    },
}

ESCALATE_TOOL = {
    "name": "escalate_to_human",
    "description": (
        "Open a private ticket so Nicolas follows up with the visitor by email. "
        "Requires the visitor's email address."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "The visitor's question or request.",
            },
            "reason": {"type": "string", "description": "Why a human is needed."},
            "contact_email": {"type": "string", "description": "Visitor's email."},
        },
        "required": ["summary", "reason", "contact_email"],
        "additionalProperties": False,
    },
}

TOOLS = [SEARCH_TOOL, BOOK_CALL_TOOL, GET_CV_TOOL, ESCALATE_TOOL]


@dataclass
class Turn:
    """Per-reply state: search hits (citation numbering), UI cards, chat context."""

    conversation_id: uuid.UUID | None = None
    transcript: list[dict] = field(default_factory=list)
    escalated: bool = False
    hits: list[retrieval.Hit] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    tools: list[str] = field(default_factory=list)
    ticket: int | None = None
    cards: dict[tuple[str, str], dict] = field(default_factory=dict)

    def add_card(self, card: dict) -> str:
        """Add card once; returns the tool-result note for the model."""
        key = (card["type"], card.get("url"))
        if key in self.cards:
            return f"{card['label']} button is already shown; it appears only once."
        self.cards[key] = card
        return f"{card['label']} button is now shown to the visitor."


def booking_card() -> dict:
    return {"type": "booking", "label": "Book a call with Nicolas", "url": CALENDAR_URL}


def cv_card(language: str) -> dict:
    return {
        "type": "cv",
        "label": f"Download CV ({language.upper()})",
        "url": CV_URLS[language],
        "language": language,
    }


def _result(call, content: str, error: bool = False) -> dict:
    result = {"type": "tool_result", "tool_use_id": call.id, "content": content}
    if error:
        result["is_error"] = True
    return result


async def _search(call, turn: Turn) -> dict:
    query = call.input.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        return _result(
            call,
            f"query must be a non-empty string up to {MAX_QUERY_CHARS} chars.",
            error=True,
        )
    found = await retrieval.search(query.strip())
    if not found:
        return _result(call, "No relevant knowledge found.")
    # Numbering continues across searches in one reply, so [n] stays unambiguous.
    start = len(turn.hits) + 1
    turn.hits.extend(found)
    lines = [
        f"[{start + i}] {hit.title} ({hit.path})\n{hit.content}"
        for i, hit in enumerate(found)
    ]
    return _result(call, "\n\n".join(lines))


async def _book_call(call, turn: Turn) -> dict:
    return _result(call, turn.add_card(booking_card()))


async def _get_cv(call, turn: Turn) -> dict:
    language = call.input.get("language")
    if language not in CV_URLS:
        return _result(call, 'language must be "en" or "pl".', error=True)
    return _result(call, turn.add_card(cv_card(language)))


async def _escalate(call, turn: Turn) -> dict:
    if turn.escalated:
        return _result(
            call, "This chat is already with Nicolas; no new ticket created."
        )
    fields = [call.input.get(k) for k in ("summary", "reason", "contact_email")]
    if not all(
        isinstance(f, str) and f.strip() and len(f) <= MAX_FIELD_CHARS for f in fields
    ):
        return _result(
            call, "summary, reason and contact_email are required.", error=True
        )
    summary, reason, email = (f.strip() for f in fields)
    if not handoff.is_email(email):
        return _result(
            call,
            "contact_email is not a valid email; ask the visitor to fix it.",
            error=True,
        )
    body = handoff.issue_body(
        summary, reason, email, str(turn.conversation_id), turn.transcript
    )
    try:
        number = await handoff.create_issue(f"Handoff: {summary[:80]}", body)
    except Exception:
        log.exception("handoff issue failed")
        turn.add_card(booking_card())
        return _result(
            call,
            "Ticket system unavailable; no ticket created. A booking button is already "
            "shown; do not call any other tool. Write one short apology and mention it.",
            error=True,
        )
    turn.escalated = True
    turn.ticket = number
    try:
        await db.mark_escalated(turn.conversation_id)
    except Exception:
        # Ticket exists: report success anyway so the visitor doesn't retry into a duplicate.
        log.exception("mark_escalated failed for ticket #%s", number)
    turn.add_card(
        {"type": "handoff", "label": "Nicolas will follow up by email", "issue": number}
    )
    return _result(call, f"Ticket #{number} created; Nicolas will follow up by email.")


HANDLERS = {
    "search_knowledge": _search,
    "book_call": _book_call,
    "get_cv": _get_cv,
    "escalate_to_human": _escalate,
}


async def run(call, turn: Turn) -> dict:
    """Run one tool call inside its own trace span; returns the tool_result block."""
    turn.tools.append(call.name)
    with tracing.tracer.start_as_current_span(
        f"tool.{call.name}",
        attributes={
            tracing.KIND: "TOOL",
            tracing.TOOL_NAME: call.name,
            tracing.INPUT: json.dumps(call.input, ensure_ascii=False),
        },
    ) as span:
        handler = HANDLERS.get(call.name)
        if handler is None:
            result = _result(call, f"Unknown tool: {call.name}", error=True)
        else:
            result = await handler(call, turn)
        span.set_attribute(tracing.OUTPUT, result["content"])
        return result
