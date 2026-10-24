import os
import uuid
from types import SimpleNamespace

import pytest

# Test env must exist before app.config is imported.
os.environ["ALLOWED_ORIGIN"] = "https://allowed.example"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = "postgresql://test@localhost/test"
os.environ["IP_HASH_SALT"] = "test-salt"
os.environ["CALENDAR_URL"] = "https://cal.example/book"
os.environ["CV_URL_EN"] = "https://cv.example/en.pdf"
os.environ["CV_URL_PL"] = "https://cv.example/pl.pdf"
os.environ["GITHUB_TOKEN"] = "test-gh-token"
os.environ["GITHUB_HANDOFF_REPO"] = "owner/handoffs"


@pytest.fixture(autouse=True)
def reset_rate_limits():
    from app.ratelimit import limiter

    limiter.reset()


class FakeStream:
    """One scripted model response: text chunks, optional tool calls."""

    def __init__(
        self, chunks=(), error=None, stop_reason=None, tool_calls=(), usage=(100, 20)
    ):
        self.chunks, self.error = chunks, error
        self.usage = SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
        self.tool_calls = [
            SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)
            for i, (name, args) in enumerate(tool_calls)
        ]
        self.stop_reason = stop_reason or ("tool_use" if tool_calls else "end_turn")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return self._texts()

    async def _texts(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def get_final_message(self):
        text = (
            [SimpleNamespace(type="text", text="".join(self.chunks))]
            if self.chunks
            else []
        )
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=text + self.tool_calls,
            usage=self.usage,
        )


class FakeClient:
    """Plays scripted turns in order; the last one repeats."""

    def __init__(self, turns):
        self.calls = []
        self._turns = turns
        self.messages = SimpleNamespace(stream=self._open)

    def _open(self, **kwargs):
        # Snapshot: the agent keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(**self._turns[min(len(self.calls), len(self._turns)) - 1])


@pytest.fixture
def model(monkeypatch):
    from app import agent  # lazy: env above must be set before app imports

    def install(turns=None, **turn):
        fake = FakeClient(turns or [turn])
        monkeypatch.setattr(agent, "client", fake)
        return fake

    return install


@pytest.fixture(autouse=True)
def fake_search(monkeypatch):
    """Default: knowledge search finds nothing. Set `.hits` to script results."""
    from app import retrieval

    state = SimpleNamespace(hits=[], queries=[], error=None, real=retrieval.search)

    async def search(query):
        state.queries.append(query)
        if state.error:
            raise state.error
        return state.hits

    monkeypatch.setattr(retrieval, "search", search)
    return state


class FakeDB:
    """In-memory stand-in for app.db (start_turn / save_reply)."""

    def __init__(self):
        self.conversations = {}  # id -> {"ip_hash", "turns"}
        self.messages = []  # (conversation_id, role, content, message_id)
        self.down = False

    async def start_turn(self, conversation_id, ip_hash, text):
        from app import db

        if self.down:
            raise ConnectionError("db down")
        if conversation_id not in self.conversations:
            conversation_id = uuid.uuid4()
            self.conversations[conversation_id] = {
                "ip_hash": ip_hash,
                "turns": 0,
                "status": "open",
            }
        conv = self.conversations[conversation_id]
        if conv["turns"] >= db.MAX_TURNS:
            raise db.TurnLimit
        history = [
            {"role": role, "content": content}
            for cid, role, content, _ in self.messages
            if cid == conversation_id
        ]
        conv["turns"] += 1
        self.messages.append((conversation_id, "user", text, uuid.uuid4()))
        return conversation_id, history, conv["status"]

    async def mark_escalated(self, conversation_id):
        self.conversations[conversation_id]["status"] = "escalated"

    async def save_reply(self, conversation_id, text):
        message_id = uuid.uuid4()
        self.messages.append((conversation_id, "assistant", text, message_id))
        return message_id


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    from app import db

    fake = FakeDB()
    monkeypatch.setattr(db, "start_turn", fake.start_turn)
    monkeypatch.setattr(db, "save_reply", fake.save_reply)
    monkeypatch.setattr(db, "mark_escalated", fake.mark_escalated)
    return fake


@pytest.fixture(autouse=True)
def fake_issues(monkeypatch):
    """GitHub handoff issues. Default: created as #42. Set `.error` to fail."""
    from app import handoff

    state = SimpleNamespace(created=[], error=None, real=handoff.create_issue)

    async def create_issue(title, body):
        if state.error:
            raise state.error
        state.created.append({"title": title, "body": body})
        return 42

    monkeypatch.setattr(handoff, "create_issue", create_issue)
    return state


@pytest.fixture(autouse=True)
def fake_events(monkeypatch):
    """Captures analytics.record rows instead of writing to the DB."""
    from app import analytics

    state = SimpleNamespace(rows=[], real=analytics.record)
    monkeypatch.setattr(analytics, "record", lambda rows: state.rows.extend(rows))
    return state
