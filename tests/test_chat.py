import json
import os
import subprocess
import sys

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.main import app

http = TestClient(app)


def frames(res):
    return [
        json.loads(part.removeprefix("data: "))
        for part in res.text.split("\n\n")
        if part.startswith("data: ")
    ]


def test_reply_streams_as_multiple_text_frames(model):
    model(chunks=["Nicolas ", "builds ", "agents."])
    res = http.post("/chat", json={"message": "What does he do?"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert frames(res) == [
        {"text": "Nicolas "},
        {"text": "builds "},
        {"text": "agents."},
    ]


def test_model_call_uses_brief_third_person_prompt(model):
    fake = model(chunks=["ok"])
    http.post("/chat", json={"message": "hi"})
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 600
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"] == {"effort": "low"}
    assert "third person" in call["system"].lower()


def test_each_message_answered_alone(model):
    fake = model(chunks=["ok"])
    http.post("/chat", json={"message": "first"})
    http.post("/chat", json={"message": "second"})
    assert fake.calls[1]["messages"] == [{"role": "user", "content": "second"}]


def test_message_at_limit_accepted(model):
    model(chunks=["ok"])
    assert http.post("/chat", json={"message": "a" * 1000}).status_code == 200


def test_too_long_message_refused_without_model_call(model):
    fake = model(chunks=["never"])
    res = http.post("/chat", json={"message": "a" * 1001})
    assert res.status_code == 422
    assert "1,000" in res.json()["error"]
    assert fake.calls == []


@pytest.mark.parametrize("body", [{"message": ""}, {"message": "   "}, {}])
def test_empty_or_missing_message_refused(model, body):
    fake = model(chunks=["never"])
    res = http.post("/chat", json=body)
    assert res.status_code == 422
    assert isinstance(res.json()["error"], str)
    assert fake.calls == []


def test_model_failure_sends_readable_error_frame(model):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    model(chunks=["Partial "], error=anthropic.APIConnectionError(request=request))
    res = http.post("/chat", json={"message": "hi"})
    assert res.status_code == 200
    out = frames(res)
    assert out[0] == {"text": "Partial "}
    assert set(out[-1]) == {"error"}
    assert "api.anthropic.com" not in out[-1]["error"]


def test_unexpected_failure_still_sends_error_frame(model):
    model(chunks=["Partial "], error=ValueError("bad SDK payload"))
    out = frames(http.post("/chat", json={"message": "hi"}))
    assert set(out[-1]) == {"error"}
    assert "bad SDK payload" not in out[-1]["error"]


def test_invalid_conversation_id_refused_with_generic_error(model):
    fake = model(chunks=["never"])
    res = http.post("/chat", json={"message": "hi", "conversation_id": "nope"})
    assert res.status_code == 422
    assert res.json() == {"error": "Invalid request."}
    assert fake.calls == []


def test_refusal_sends_error_frame(model):
    model(chunks=[], stop_reason="refusal")
    out = frames(http.post("/chat", json={"message": "hi"}))
    assert set(out[-1]) == {"error"}


def test_missing_anthropic_key_fails_boot():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        check=False,
        env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "ANTHROPIC_API_KEY" in proc.stderr
