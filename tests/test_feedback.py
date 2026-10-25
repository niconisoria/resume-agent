import uuid

from tests.helpers import ask, http


def answer(model, ip="5.5.5.5"):
    model(chunks=["An answer."])
    final = ask(ip=ip)[-1]
    return uuid.UUID(final["message_id"])


def rate(message_id, rating, ip="5.5.5.5"):
    return http.post(
        "/feedback",
        json={"message_id": str(message_id), "rating": rating},
        headers={"Fly-Client-IP": ip},
    )


def test_rating_saved_and_confirmed(model, fake_db):
    mid = answer(model)
    res = rate(mid, 1)
    assert res.status_code == 200
    assert res.json() == {"status": "saved"}
    assert fake_db.feedback == {mid: 1}


def test_changing_vote_replaces_earlier_one(model, fake_db):
    mid = answer(model)
    rate(mid, 1)
    assert rate(mid, -1).status_code == 200
    assert fake_db.feedback == {mid: -1}


def test_unknown_message_refused(fake_db):
    res = rate(uuid.uuid4(), 1)
    assert res.status_code == 404
    assert res.json() == {"error": "Answer not found."}


def test_user_message_cannot_be_rated(model, fake_db):
    answer(model)
    user_mid = next(m[3] for m in fake_db.messages if m[1] == "user")
    assert rate(user_mid, 1).status_code == 404
    assert fake_db.feedback == {}


def test_rating_other_than_up_or_down_refused(model, fake_db):
    mid = answer(model)
    for bad in [0, 2, "up", None]:
        res = rate(mid, bad)
        assert res.status_code == 422
        assert res.json() == {"error": "Rating must be 1 (👍) or -1 (👎)."}
    assert fake_db.feedback == {}


def test_malformed_message_id_refused():
    res = http.post("/feedback", json={"message_id": "nope", "rating": 1})
    assert res.status_code == 422
    assert res.json() == {"error": "Invalid request."}


def test_feedback_limit_is_separate_from_chat(model, fake_db):
    mid = answer(model, ip="6.6.6.6")
    codes = [rate(mid, 1, ip="6.6.6.6").status_code for _ in range(31)]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429
    assert "try again" in rate(mid, 1, ip="6.6.6.6").json()["error"].lower()
    # Chat quota for the same IP is untouched (1 of 10 used).
    chat = http.post(
        "/chat", json={"message": "hi"}, headers={"Fly-Client-IP": "6.6.6.6"}
    )
    assert chat.status_code == 200


def test_storage_outage_gives_readable_error(model, fake_db):
    mid = answer(model)
    fake_db.down = True
    res = rate(mid, 1)
    assert res.status_code == 503
    assert res.json()["error"].startswith("Sorry")


def test_feedback_limit_block_not_logged_as_chat_block(model, fake_db, fake_events):
    mid = answer(model, ip="4.4.4.4")
    for _ in range(31):
        rate(mid, 1, ip="4.4.4.4")
    assert [r for r in fake_events.rows if r["type"] == "rate_limited"] == []
