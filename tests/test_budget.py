import uuid

from app import budget
from tests.helpers import chat, frames


def test_under_budget_answers_normally(model, fake_db):
    fake_db.spent = 1.99
    fake = model(chunks=["Normal answer."])
    out = frames(chat())
    assert out[0] == {"text": "Normal answer."}
    assert len(fake.calls) == 1


def test_at_budget_gets_friendly_reply_with_buttons(model, fake_db):
    fake_db.spent = 2.0
    fake = model(chunks=["never"])
    out = frames(chat())
    assert len(out) == 2
    assert "resting for today" in out[0]["text"]
    final = out[-1]
    assert final["message_id"] is None
    assert final["citations"] == []
    assert [(c["type"], c.get("language")) for c in final["cards"]] == [
        ("booking", None),
        ("cv", "en"),
    ]
    assert fake.calls == []


def test_blocked_message_not_stored_or_counted(model, fake_db):
    model(chunks=["ok"])
    cid = uuid.UUID(frames(chat("first"))[-1]["conversation_id"])
    stored_before = list(fake_db.messages)
    fake_db.spent = 5.0
    final = frames(chat("second", cid))[-1]
    assert final["conversation_id"] == str(cid)
    assert fake_db.messages == stored_before
    assert fake_db.conversations[cid]["turns"] == 1


def test_new_chat_blocked_has_no_chat_id(model, fake_db):
    fake_db.spent = 5.0
    model(chunks=["never"])
    assert frames(chat())[-1]["conversation_id"] is None
    assert fake_db.conversations == {}


def test_each_blocked_message_recorded(model, fake_db, fake_events):
    fake_db.spent = 5.0
    model(chunks=["never"])
    cid = uuid.uuid4()
    chat("a")
    chat("b", cid)
    rows = [r for r in fake_events.rows if r["type"] == "budget_exceeded"]
    # Never the unverified client id: an unknown id would violate the events FK.
    assert rows == [{"type": "budget_exceeded"}, {"type": "budget_exceeded"}]


def test_cap_zero_blocks_every_message(model, fake_db, monkeypatch):
    monkeypatch.setattr(budget, "DAILY_BUDGET_USD", 0.0)
    fake = model(chunks=["never"])
    assert "resting for today" in frames(chat())[0]["text"]
    assert fake.calls == []


def test_unknown_spend_gives_readable_error_without_model_call(model, fake_db):
    fake_db.down = True
    fake = model(chunks=["never"])
    res = chat()
    assert res.status_code == 503
    assert res.json()["error"].startswith("Sorry")
    assert fake.calls == []
