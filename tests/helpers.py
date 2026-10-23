import json


def frames(res):
    """Parse an SSE response into its JSON payloads."""
    return [
        json.loads(part.removeprefix("data: "))
        for part in res.text.split("\n\n")
        if part.startswith("data: ")
    ]
