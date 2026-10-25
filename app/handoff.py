import re

import httpx2

from app.config import GITHUB_HANDOFF_REPO, GITHUB_TOKEN

# ponytail: pragmatic email shape check, not RFC 5322; Nicolas replies by hand anyway.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)*\.[A-Za-z]{2,}")


def redact_emails(text: str) -> str:
    return EMAIL_RE.sub("[email]", text)


def is_email(value: str) -> bool:
    return EMAIL_RE.fullmatch(value.strip()) is not None


def _fence(text: str) -> str:
    # Visitor-influenced text renders inert (no markdown, no @mentions);
    # neutralise ~~~ so it can't close the fence early.
    return f"~~~text\n{text.replace('~~~', '~ ~ ~')}\n~~~"


def issue_body(
    summary: str, reason: str, email: str, conversation_id: str, transcript: list[dict]
) -> str:
    lines = "\n\n".join(f"{m['role']}: {m['content']}" for m in transcript)
    return (
        f"**Summary:**\n\n{_fence(summary)}\n\n"
        f"**Reason:**\n\n{_fence(reason)}\n\n"
        f"**Reply to:** {email}\n\n"
        f"**Conversation:** `{conversation_id}`\n\n"
        f"### Transcript\n\n{_fence(lines)}\n"
    )


async def create_issue(title: str, body: str) -> int:
    async with httpx2.AsyncClient(timeout=10) as client:
        res = await client.post(
            f"https://api.github.com/repos/{GITHUB_HANDOFF_REPO}/issues",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "labels": ["handoff"]},
        )
        res.raise_for_status()
        return res.json()["number"]
