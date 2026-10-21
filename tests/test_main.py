import os
import subprocess
import sys

from fastapi.testclient import TestClient

from app.main import app

ORIGIN = os.environ["ALLOWED_ORIGIN"]
client = TestClient(app)


def test_health_returns_ok():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_allowed_origin_gets_cors_headers():
    res = client.get("/health", headers={"Origin": ORIGIN})
    assert res.headers["access-control-allow-origin"] == ORIGIN


def test_other_origin_gets_no_cors_headers():
    res = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in res.headers


def preflight(origin, method="POST"):
    return client.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": method},
    )


def test_preflight_from_allowed_origin_passes():
    res = preflight(ORIGIN)
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == ORIGIN


def test_preflight_from_other_origin_rejected():
    res = preflight("https://evil.example")
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_preflight_with_unlisted_method_rejected():
    assert preflight(ORIGIN, method="DELETE").status_code == 400


def test_missing_allowed_origin_fails_boot():
    env = {k: v for k, v in os.environ.items() if k != "ALLOWED_ORIGIN"}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        check=False,
        env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "ALLOWED_ORIGIN" in proc.stderr
