import time

import pytest

from tests.helpers import chat, http


@pytest.fixture
def fake_model(model):
    return model(chunks=["ok"])


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    def advance(seconds):
        now[0] += seconds

    return advance


def send(ip="1.1.1.1"):
    return chat(ip=ip)


@pytest.mark.usefixtures("fake_model")
def test_eleventh_message_in_a_minute_refused():
    assert [send().status_code for _ in range(10)] == [200] * 10
    res = send()
    assert res.status_code == 429


@pytest.mark.usefixtures("fake_model")
def test_refusal_says_when_to_retry_in_seconds():
    for _ in range(10):
        send()
    res = send()
    retry = int(res.headers["retry-after"])
    assert 1 <= retry <= 60
    assert res.json() == {
        "error": f"Too many messages. Please try again in {retry} seconds."
    }


def test_refused_message_never_reaches_model(fake_model):
    for _ in range(10):
        send()
    send()
    assert len(fake_model.calls) == 10


@pytest.mark.usefixtures("fake_model")
def test_sixty_first_message_in_a_day_refused(clock):
    for _ in range(6):
        assert [send().status_code for _ in range(10)] == [200] * 10
        clock(61)
    res = send()
    assert res.status_code == 429
    assert int(res.headers["retry-after"]) > 3600


@pytest.mark.usefixtures("fake_model")
def test_minute_window_resets(clock):
    for _ in range(10):
        send()
    clock(61)
    assert send().status_code == 200


@pytest.mark.usefixtures("fake_model")
def test_second_visitor_unaffected():
    for _ in range(11):
        send("1.1.1.1")
    assert send("2.2.2.2").status_code == 200


@pytest.mark.usefixtures("fake_model")
def test_socket_ip_used_without_fly_header():
    for _ in range(10):
        assert chat().status_code == 200
    assert chat().status_code == 429
    assert send("3.3.3.3").status_code == 200


def test_health_never_limited():
    assert all(http.get("/health").status_code == 200 for _ in range(100))
