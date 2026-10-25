import json

from fastapi.testclient import TestClient

from app.main import app

http = TestClient(app)


def frames(res):
    """Parse an SSE response into its JSON payloads."""
    return [
        json.loads(part.removeprefix("data: "))
        for part in res.text.split("\n\n")
        if part.startswith("data: ")
    ]


def chat(message="hi", conversation_id=None, ip=None):
    """POST /chat as the widget does; `ip` is sent as Fly-Client-IP."""
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = str(conversation_id)
    headers = {"Fly-Client-IP": ip} if ip else {}
    return http.post("/chat", json=body, headers=headers)


def ask(message="hi", conversation_id=None, ip=None):
    """POST /chat and return its SSE frames."""
    return frames(chat(message, conversation_id, ip))
