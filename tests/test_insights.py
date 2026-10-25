import asyncio
import datetime as dt
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent, analytics
from app.main import app
from tests.helpers import frames

http = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_cache():
    analytics._cache = None


# --- topic labels -------------------------------------------------------------


class LabelClient:
    def __init__(self, topic="skills", error=None):
        self.calls, self.topic, self.error = [], topic, error
        self.messages = SimpleNamespace(parse=self._parse)

    async def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            parsed_output=analytics.Label(topic=self.topic),
            usage=SimpleNamespace(input_tokens=200, output_tokens=10),
        )


def test_topics_are_the_fixed_set():
    assert analytics.TOPICS == (
        "experience",
        "skills",
        "projects",
        "ai_work",
        "availability",
        "logistics",
        "out_of_scope",
        "other",
    )


def test_label_returns_topic_and_cost(monkeypatch):
    fake = LabelClient("projects")
    monkeypatch.setattr(agent, "client", fake)
    assert asyncio.run(analytics.label("What did he build?")) == (
        "projects",
        pytest.approx(analytics.cost_usd(200, 10)),
    )
    (call,) = fake.calls
    assert call["model"] == agent.MODEL
    assert call["output_format"] is analytics.Label
    assert call["thinking"] == {"type": "disabled"}
    assert "What did he build?" in json.dumps(call["messages"])


def test_label_failure_returns_none(monkeypatch):
    monkeypatch.setattr(agent, "client", LabelClient(error=RuntimeError("down")))
    assert asyncio.run(analytics.label("hi")) is None


class FakePool:
    def __init__(self):
        self.written = []

    @asynccontextmanager
    async def connection(self):
        yield self

    @asynccontextmanager
    async def cursor(self):
        yield self

    async def executemany(self, sql, rows):
        self.written.extend(rows)


def run_records(record, *calls):
    """Call `record` inside a loop (it schedules tasks), then wait for the writes."""

    async def go():
        for args in calls:
            record(*args)
        await analytics.drain()

    asyncio.run(go())


@pytest.fixture
def pool(monkeypatch):
    from app import db

    fake = FakePool()
    monkeypatch.setattr(db, "pool", fake)
    return fake


def test_answer_row_gets_topic_and_label_cost(monkeypatch, pool, fake_events):
    monkeypatch.setattr(agent, "client", LabelClient("availability"))
    rows = [
        {"type": "tool_call"},
        {"type": "answer", "message_id": "m1", "cost_usd": 0.001},
    ]
    run_records(fake_events.real, (rows, "Is he open to work?"))
    tool, answer = pool.written
    assert tool["topic"] is None
    assert answer["topic"] == "availability"
    assert answer["cost_usd"] == pytest.approx(0.001 + analytics.cost_usd(200, 10))


def test_label_failure_still_writes_row(monkeypatch, pool, fake_events):
    monkeypatch.setattr(agent, "client", LabelClient(error=RuntimeError("down")))
    run_records(
        fake_events.real,
        ([{"type": "answer", "message_id": "m1", "cost_usd": 0.001}], "hi"),
    )
    (answer,) = pool.written
    assert answer["topic"] is None and answer["cost_usd"] == 0.001


def test_failed_reply_not_labelled(monkeypatch, pool, fake_events):
    fake = LabelClient()
    monkeypatch.setattr(agent, "client", fake)
    row = {"type": "answer", "message_id": None, "cost_usd": 0.001}
    run_records(fake_events.real, ([row], "hi"))
    assert fake.calls == [] and pool.written[0]["cost_usd"] == 0.001


def test_no_label_call_without_answer_row(monkeypatch, pool, fake_events):
    fake = LabelClient()
    monkeypatch.setattr(agent, "client", fake)
    run_records(
        fake_events.real, ([{"type": "rate_limited"}],), ([{"type": "tool_call"}], "hi")
    )
    assert fake.calls == [] and len(pool.written) == 2


# --- chat wiring ----------------------------------------------------------------


def ask(message):
    return frames(http.post("/chat", json={"message": message}))


def answer_row(fake_events):
    return next(r for r in fake_events.rows if r["type"] == "answer")


def test_chat_passes_redacted_message_for_label(model, fake_events):
    model(chunks=["ok"])
    ask("Reach me at ana@example.com about skills")
    assert fake_events.messages == ["Reach me at [email] about skills"]


def test_no_hits_when_search_finds_nothing(model, fake_events, fake_search):
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"})]},
            {"chunks": ["?"]},
        ]
    )
    ask("Does he know Cobol?")
    assert answer_row(fake_events)["no_hits"] is True


def test_no_hits_false_when_search_finds_something(model, fake_events, fake_search):
    from app.retrieval import Hit

    fake_search.hits = [Hit("Skills", "skills.md", "Python", 0.5)]
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "x"})]},
            {"chunks": ["ok"]},
        ]
    )
    ask("Python?")
    assert answer_row(fake_events)["no_hits"] is False


def test_no_hits_false_without_search(model, fake_events):
    model(chunks=["Hello!"])
    ask("hi")
    assert answer_row(fake_events)["no_hits"] is False


# --- /insights ------------------------------------------------------------------

RAW = {
    "days": [(dt.date(2026, 10, 1), 2, 5), (dt.date(2026, 10, 2), 1, 1)],
    "totals": (3, 1, 3, 1, 2345.6, Decimal("0.012"), 4, 2),
    "topics": [("skills", 4, 1), ("projects", 2, 0)],
}


@pytest.fixture
def fetch(monkeypatch):
    state = SimpleNamespace(calls=0, raw=RAW, error=None)

    async def _fetch():
        state.calls += 1
        if state.error:
            raise state.error
        return state.raw

    monkeypatch.setattr(analytics, "_fetch", _fetch)
    return state


def test_insights_shape(fetch):
    res = http.get("/insights")
    assert res.status_code == 200
    body = res.json()
    assert body["window_days"] == 30
    assert dt.datetime.fromisoformat(body["generated_at"]).tzinfo is not None
    assert body["days"] == [
        {"date": "2026-10-01", "conversations": 2, "messages": 5},
        {"date": "2026-10-02", "conversations": 1, "messages": 1},
    ]
    assert body["resolution"] == {
        "conversations": 3,
        "escalated": 1,
        "resolved_share": pytest.approx(2 / 3),
    }
    assert body["feedback"] == {"up": 3, "down": 1, "up_share": 0.75}
    assert body["p95_latency_ms"] == 2346
    assert body["cost_per_conversation_usd"] == pytest.approx(0.004)
    assert body["blocked"] == {"rate_limited": 4, "budget_exceeded": 2}


def test_topics_list_every_topic_most_asked_first(fetch):
    topics = http.get("/insights").json()["topics"]
    assert [t["topic"] for t in topics][:2] == ["skills", "projects"]
    assert {t["topic"] for t in topics} == set(analytics.TOPICS)
    assert topics[0] == {"topic": "skills", "messages": 4, "no_hits": 1}
    assert topics[-1]["messages"] == 0


def test_empty_window_gives_null_shares(fetch):
    fetch.raw = {"days": [], "totals": (0, 0, 0, 0, None, None, 0, 0), "topics": []}
    body = http.get("/insights").json()
    assert body["resolution"]["resolved_share"] is None
    assert body["feedback"]["up_share"] is None
    assert body["p95_latency_ms"] is None
    assert body["cost_per_conversation_usd"] is None


def test_no_text_or_identity_fields(fetch):
    text = http.get("/insights").text
    for key in ["content", "message_id", "conversation_id", "ip", "email", "text"]:
        assert f'"{key}"' not in text


def test_cached_for_five_minutes(fetch, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])
    http.get("/insights")
    now[0] += 299
    http.get("/insights")
    assert fetch.calls == 1
    now[0] += 2
    http.get("/insights")
    assert fetch.calls == 2


def test_db_error_is_503(fetch):
    fetch.error = ConnectionError("db down")
    res = http.get("/insights")
    assert res.status_code == 503 and "error" in res.json()


def test_cors_allows_site(fetch):
    res = http.get("/insights", headers={"Origin": "https://allowed.example"})
    assert res.headers["access-control-allow-origin"] == "https://allowed.example"


def test_rate_limited(fetch):
    codes = [http.get("/insights").status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


# --- purge ----------------------------------------------------------------------


class FakeConn:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchone(self):
        return (7,)


CUTOFF = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)


@pytest.fixture
def purge_before():
    from scripts import purge_before

    return purge_before


def test_purge_dry_run_only_counts(purge_before):
    conn = FakeConn()
    counts = purge_before.purge(conn, CUTOFF, apply=False)
    assert counts == {"events": 7, "conversations": 7, "messages": 7}
    assert not any(s.lower().startswith("delete") for s in conn.sql)


def test_purge_deletes_events_before_conversations(purge_before):
    conn = FakeConn()
    purge_before.purge(conn, CUTOFF, apply=True)
    deletes = [s for s in conn.sql if s.lower().startswith("delete")]
    assert [d.split()[2] for d in deletes] == ["events", "conversations"]


def test_purge_requires_timezone(purge_before):
    with pytest.raises(SystemExit):
        purge_before.parse_cutoff("2026-10-25T00:00:00")
    assert purge_before.parse_cutoff("2026-10-25T00:00:00Z") == CUTOFF
