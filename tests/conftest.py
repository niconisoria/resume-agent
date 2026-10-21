import os
from types import SimpleNamespace

import pytest

# Test env must exist before app.config is imported.
os.environ["ALLOWED_ORIGIN"] = "https://allowed.example"
os.environ["ANTHROPIC_API_KEY"] = "test-key"


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
