import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import analytics, db
from tests.helpers import ask

ALLOWED_KEYS = {
    "type",
    "conversation_id",
    "message_id",
    "tool_name",
    "latency_ms",
    "first_token_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "no_hits",
}


def of_type(fake_events, kind):
    return [r for r in fake_events.rows if r["type"] == kind]


def test_answer_row_has_timing_tokens_cost_and_ids(model, fake_events):
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"})], "usage": (100, 10)},
            {"chunks": ["Answer."], "usage": (200, 30)},
        ]
    )
    final = ask()[-1]
    (row,) = of_type(fake_events, "answer")
    assert row["conversation_id"] == uuid.UUID(final["conversation_id"])
    assert str(row["message_id"]) == final["message_id"]
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 40
    assert row["cost_usd"] == pytest.approx(300 * 0.10 / 1e6 + 40 * 0.50 / 1e6)
    assert isinstance(row["latency_ms"], int) and isinstance(row["first_token_ms"], int)
    assert 0 <= row["first_token_ms"] <= row["latency_ms"]


def test_each_tool_call_recorded_by_name(model, fake_events):
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"}), ("book_call", {})]},
            {"tool_calls": [("get_cv", {"language": "en"})]},
            {"chunks": ["ok"]},
        ]
    )
    ask()
    names = [r["tool_name"] for r in of_type(fake_events, "tool_call")]
    assert names == ["search_knowledge", "book_call", "get_cv"]


def test_handoff_ticket_recorded(model, fake_events):
    args = {"summary": "s", "reason": "r", "contact_email": "ana@example.com"}
    model(turns=[{"tool_calls": [("escalate_to_human", args)]}, {"chunks": ["#42"]}])
    ask()
    assert len(of_type(fake_events, "escalation")) == 1


def test_failed_reply_still_records_spend_and_tools(model, fake_events):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"})], "usage": (150, 15)},
            {"chunks": ["x"], "error": anthropic.APIConnectionError(request=request)},
        ]
    )
    out = ask()
    assert set(out[-2]) == {"error"}
    (row,) = of_type(fake_events, "answer")
    assert row["message_id"] is None
    assert (row["input_tokens"], row["output_tokens"]) == (150, 15)
    assert [r["tool_name"] for r in of_type(fake_events, "tool_call")] == [
        "search_knowledge"
    ]


def test_failure_before_any_model_response_records_no_answer(model, fake_events):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    model(chunks=[], error=anthropic.APIConnectionError(request=request))
    ask()
    assert of_type(fake_events, "answer") == []


def test_per_ip_limit_block_recorded(model, fake_events):
    model(chunks=["ok"])
    for _ in range(11):
        ask(ip="8.8.8.8")
    (row,) = of_type(fake_events, "rate_limited")
    assert row.get("conversation_id") is None


def test_chat_turn_cap_block_recorded(model, fake_db, fake_events):
    model(chunks=["ok"])
    cid = uuid.UUID(ask()[-1]["conversation_id"])
    fake_db.conversations[cid]["turns"] = 20
    ask("more", cid)
    (row,) = of_type(fake_events, "rate_limited")
    assert row["conversation_id"] == cid


def test_rows_hold_no_text_ips_or_emails(model, fake_events):
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "secret query"})]},
            {"chunks": ["visible answer"]},
        ]
    )
    ask("my email is ana@example.com", ip="203.0.113.9")
    assert fake_events.rows
    for row in fake_events.rows:
        assert set(row) <= ALLOWED_KEYS
        flat = " ".join(str(v) for v in row.values())
        for leak in [
            "ana@example.com",
            "203.0.113.9",
            "visible answer",
            "secret query",
        ]:
            assert leak not in flat


def test_cost_usd_uses_haiku_list_price():
    assert analytics.cost_usd(1_000_000, 1_000_000) == pytest.approx(0.60)
    assert analytics.cost_usd(0, 0) == 0


def test_record_writes_rows_in_background_and_swallows_db_errors(
    monkeypatch, fake_events
):
    calls = []

    class Cursor:
        async def executemany(self, sql, params):
            calls.append((sql, params))
            raise ConnectionError("db down")

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(cursor=lambda: _ctx(Cursor()))

    @asynccontextmanager
    async def _ctx(obj):
        yield obj

    monkeypatch.setattr(db, "pool", SimpleNamespace(connection=connection))

    async def run():
        fake_events.real([{"type": "answer", "input_tokens": 1}])
        await analytics.drain()

    asyncio.run(run())  # must not raise
    (sql, params) = calls[0]
    assert "insert into events" in sql.lower()
    assert params[0]["type"] == "answer"
    assert params[0]["tool_name"] is None


def test_tab_closed_mid_reply_still_records_spend(model, fake_events):
    from app.main import chat_events

    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"})], "usage": (100, 10)},
            {"chunks": ["first", " second"], "usage": (200, 30)},
        ]
    )

    async def close_after_first_frame():
        stream = chat_events(uuid.uuid4(), "hi", [], False)
        await anext(stream)  # visitor sees text, then closes the tab
        await stream.aclose()

    asyncio.run(close_after_first_frame())
    (row,) = of_type(fake_events, "answer")
    assert row["message_id"] is None
    assert row["input_tokens"] >= 100  # spend so far reaches the daily budget
    assert [r["tool_name"] for r in of_type(fake_events, "tool_call")] == [
        "search_knowledge"
    ]
