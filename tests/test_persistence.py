import hashlib
import uuid

import anthropic
import httpx2

from tests.helpers import chat, frames


def test_first_message_ends_with_chat_and_message_ids(model, fake_db):
    model(chunks=["Hello", " there"])
    final = frames(chat("hi"))[-1]
    assert set(final) == {"conversation_id", "message_id", "citations", "cards"}
    conversation_id = uuid.UUID(final["conversation_id"])
    assert conversation_id in fake_db.conversations
    saved = [m for m in fake_db.messages if m[1] == "assistant"]
    assert saved[0][2] == "Hello there"
    assert str(saved[0][3]) == final["message_id"]


def test_follow_up_sends_earlier_messages_as_history(model):
    fake = model(chunks=["Ruby."])
    conversation_id = frames(chat("Main language?"))[-1]["conversation_id"]
    chat("Since when?", conversation_id)
    assert fake.calls[1]["messages"] == [
        {"role": "user", "content": "Main language?"},
        {"role": "assistant", "content": "Ruby."},
        {"role": "user", "content": "Since when?"},
    ]


def test_unknown_chat_id_starts_fresh_chat(model):
    fake = model(chunks=["ok"])
    unknown = uuid.uuid4()
    res = chat("hi", unknown)
    assert res.status_code == 200
    final = frames(res)[-1]
    assert final["conversation_id"] != str(unknown)
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_continues_from_another_network(model):
    fake = model(chunks=["ok"])
    conversation_id = frames(chat("first", ip="1.1.1.1"))[-1]["conversation_id"]
    final = frames(chat("second", conversation_id, ip="9.9.9.9"))[-1]
    assert final["conversation_id"] == conversation_id
    assert len(fake.calls[1]["messages"]) == 3


def test_twenty_first_message_refused_without_model_call(model, fake_db):
    fake = model(chunks=["ok"])
    conversation_id = uuid.UUID(frames(chat("first"))[-1]["conversation_id"])
    fake_db.conversations[conversation_id]["turns"] = 20
    res = chat("one more", conversation_id)
    assert res.status_code == 429
    assert "20-message limit" in res.json()["error"]
    assert len(fake.calls) == 1


def test_failed_reply_not_remembered_but_counted(model, fake_db):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    model(chunks=["Partial"], error=anthropic.APIConnectionError(request=request))
    out = frames(chat("hi"))
    final = out[-1]
    assert set(out[-2]) == {"error"}
    assert final["message_id"] is None
    conversation_id = uuid.UUID(final["conversation_id"])
    assert fake_db.conversations[conversation_id]["turns"] == 1
    assert [m[1] for m in fake_db.messages] == ["user"]


def test_ip_stored_only_as_salted_hash(model, fake_db):
    model(chunks=["ok"])
    chat("hi", ip="203.0.113.7")
    (conv,) = fake_db.conversations.values()
    expected = hashlib.sha256(b"203.0.113.7test-salt").hexdigest()
    assert conv["ip_hash"] == expected
    assert "203.0.113.7" not in conv["ip_hash"]


def test_storage_outage_gives_readable_error_without_model_call(model, fake_db):
    fake = model(chunks=["never"])
    fake_db.down = True
    res = chat("hi")
    assert res.status_code == 503
    assert res.json()["error"].startswith("Sorry")
    assert fake.calls == []


def test_db_timeout_covers_neon_wake_but_still_fails():
    from app import db

    # Neon wake measured ~5s live; 10s leaves headroom, outage still ends in 503.
    assert 5 < db.pool.timeout <= 10


def test_empty_reply_not_saved(model, fake_db):
    model(chunks=[])
    final = frames(chat("hi"))[-1]
    assert final["message_id"] is None
    assert [m[1] for m in fake_db.messages] == ["user"]


def test_save_failure_still_finishes_stream(model, fake_db, monkeypatch):
    from app import db

    async def broken(*args):
        raise ConnectionError("db down")

    model(chunks=["ok"])
    monkeypatch.setattr(db, "save_reply", broken)
    out = frames(chat("hi"))
    assert out[0] == {"text": "ok"}
    assert out[-1]["message_id"] is None
    assert out[-1]["conversation_id"]


def test_pool_survives_neon_sleep():
    from psycopg_pool import AsyncConnectionPool

    from app import db

    # Dead connections (Neon closed them while idle) are detected before use...
    assert db.pool._check == AsyncConnectionPool.check_connection
    # ...and idle ones are recycled before Neon's 5-minute suspend closes them.
    assert db.pool.max_idle < 300
