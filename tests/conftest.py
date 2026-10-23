import os
import uuid
from types import SimpleNamespace

import pytest

# Test env must exist before app.config is imported.
os.environ["ALLOWED_ORIGIN"] = "https://allowed.example"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = "postgresql://test@localhost/test"
os.environ["IP_HASH_SALT"] = "test-salt"


@pytest.fixture(autouse=True)
def reset_rate_limits():
    from app.ratelimit import limiter

    limiter.reset()


class FakeStream:
    def __init__(self, chunks=(), error=None, stop_reason="end_turn"):
        self.chunks, self.error, self.stop_reason = chunks, error, stop_reason

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
        return SimpleNamespace(stop_reason=self.stop_reason)


class FakeClient:
    def __init__(self, stream):
        self.calls = []
        self._stream = stream
        self.messages = SimpleNamespace(stream=self._open)

    def _open(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream


@pytest.fixture
def model(monkeypatch):
    from app import agent  # lazy: env above must be set before app imports

    def install(**kwargs):
        fake = FakeClient(FakeStream(**kwargs))
        monkeypatch.setattr(agent, "client", fake)
        return fake

    return install


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
            self.conversations[conversation_id] = {"ip_hash": ip_hash, "turns": 0}
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
        return conversation_id, history

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
    return fake
