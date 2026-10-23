import re

import httpx2

from app.config import GITHUB_HANDOFF_REPO, GITHUB_TOKEN

# ponytail: pragmatic email shape check, not RFC 5322; Nicolas replies by hand anyway.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)*\.[A-Za-z]{2,}")


def redact_emails(text: str) -> str:
    return EMAIL_RE.sub("[email]", text)


def is_email(value: str) -> bool:
    return EMAIL_RE.fullmatch(value.strip()) is not None


def issue_body(
    summary: str, reason: str, email: str, conversation_id: str, transcript: list[dict]
) -> str:
    # Visitor text sits inside a ~~~ fence; neutralise fences so it can't escape.
    lines = "\n\n".join(
        f"{m['role']}: {m['content']}".replace("~~~", "~ ~ ~") for m in transcript
    )
    return (
        f"**Summary:** {summary}\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Reply to:** {email}\n\n"
        f"**Conversation:** `{conversation_id}`\n\n"
        f"### Transcript\n\n~~~text\n{lines}\n~~~\n"
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
