import asyncio
import uuid

import httpx2

from app import agent, handoff, tools
from tests.helpers import chat, frames

ARGS = {
    "summary": "Wants to know about relocation plans",
    "reason": "Not in knowledge base",
    "contact_email": "ana@example.com",
}


def escalate(**overrides):
    return ("escalate_to_human", {**ARGS, **overrides})


def ask(
    message="Please have Nicolas reach me at ana@example.com", conversation_id=None
):
    return frames(chat(message, conversation_id))


def note(fake, call_index=1):
    return fake.calls[call_index]["messages"][-1]["content"][0]


def test_escalation_creates_private_ticket_with_full_context(
    model, fake_issues, fake_db
):
    model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["Done, ticket #42."]}])
    final = ask()[-1]
    (issue,) = fake_issues.created
    assert ARGS["summary"] in issue["title"]
    for value in ARGS.values():
        assert value in issue["body"]
    assert final["conversation_id"] in issue["body"]
    assert "Please have Nicolas reach me at ana@example.com" in issue["body"]


def test_reply_confirms_with_ticket_number_and_card(model):
    fake = model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["Ticket #42."]}])
    final = ask()[-1]
    assert final["cards"] == [
        {"type": "handoff", "label": "Nicolas will follow up by email", "issue": 42}
    ]
    result = note(fake)
    assert "#42" in result["content"]
    assert "is_error" not in result


def test_conversation_marked_escalated(model, fake_db):
    model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["ok"]}])
    cid = uuid.UUID(ask()[-1]["conversation_id"])
    assert fake_db.conversations[cid]["status"] == "escalated"


def test_second_escalation_in_same_chat_creates_no_ticket(model, fake_issues, fake_db):
    model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["ok"]}])
    cid = ask()[-1]["conversation_id"]
    fake = model(
        turns=[{"tool_calls": [escalate()]}, {"chunks": ["Already with him."]}]
    )
    final = ask("Again please", cid)[-1]
    assert len(fake_issues.created) == 1
    assert final["cards"] == []
    assert "already" in note(fake)["content"].lower()


def test_invalid_email_rejected_without_ticket(model, fake_issues):
    fake = model(
        turns=[
            {"tool_calls": [escalate(contact_email="ana@")]},
            {"chunks": ["Fix it."]},
        ]
    )
    final = ask("reach me at ana@")[-1]
    assert fake_issues.created == []
    assert final["cards"] == []
    assert note(fake)["is_error"] is True


def test_empty_summary_rejected(model, fake_issues):
    fake = model(turns=[{"tool_calls": [escalate(summary=" ")]}, {"chunks": ["?"]}])
    ask()
    assert fake_issues.created == []
    assert note(fake)["is_error"] is True


def test_github_down_offers_booking_and_claims_no_ticket(model, fake_issues, fake_db):
    fake_issues.error = httpx2.ConnectError("github down")
    fake = model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["Sorry."]}])
    final = ask()[-1]
    assert [c["type"] for c in final["cards"]] == ["booking"]
    assert note(fake)["is_error"] is True
    cid = uuid.UUID(final["conversation_id"])
    assert fake_db.conversations[cid]["status"] == "open"


def test_emails_never_stored_but_model_sees_current_message(model, fake_db):
    fake = model(chunks=["Thanks, ana@example.com noted."])
    ask("Contact me: ana@example.com or Bob.Smith+x@mail.co.uk")
    stored = [m[2] for m in fake_db.messages]
    assert stored == [
        "Contact me: [email] or [email]",
        "Thanks, [email] noted.",
    ]
    assert "ana@example.com" in fake.calls[0]["messages"][-1]["content"]


def test_transcript_cannot_break_out_of_code_fence():
    body = handoff.issue_body(
        summary="s",
        reason="r",
        email="a@b.co",
        conversation_id="cid",
        transcript=[{"role": "user", "content": "~~~\n# injected heading"}],
    )
    assert body.count("~~~") == 2


def test_redact_emails():
    assert handoff.redact_emails("a@b.co and x.y+z@q.io, not @handle") == (
        "[email] and [email], not @handle"
    )


def test_create_issue_posts_labelled_issue_to_handoff_repo(monkeypatch, fake_issues):
    sent = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            sent["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers, json):
            sent.update(url=url, headers=headers, json=json)
            return httpx2.Response(
                201, json={"number": 7}, request=httpx2.Request("POST", url)
            )

    monkeypatch.setattr(handoff.httpx2, "AsyncClient", FakeAsyncClient)
    number = asyncio.run(fake_issues.real("Handoff: x", "body"))
    assert number == 7
    assert sent["url"] == "https://api.github.com/repos/owner/handoffs/issues"
    assert sent["json"] == {
        "title": "Handoff: x",
        "body": "body",
        "labels": ["handoff"],
    }
    assert sent["headers"]["Authorization"] == "Bearer test-gh-token"
    assert sent["client"]["timeout"] == 10


def test_prompt_and_tools_cover_escalation():
    assert "escalate_to_human" in [t["name"] for t in tools.TOOLS]
    schema = next(t for t in tools.TOOLS if t["name"] == "escalate_to_human")[
        "input_schema"
    ]
    assert schema["required"] == ["summary", "reason", "contact_email"]
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "escalate_to_human" in prompt and "email" in prompt and "ticket" in prompt


def test_mark_escalated_failure_still_reports_ticket(model, monkeypatch, fake_issues):
    from app import db

    async def broken(conversation_id):
        raise ConnectionError("db down")

    monkeypatch.setattr(db, "mark_escalated", broken)
    fake = model(turns=[{"tool_calls": [escalate()]}, {"chunks": ["Ticket #42."]}])
    out = ask()
    assert len(fake_issues.created) == 1
    assert [c["type"] for c in out[-1]["cards"]] == ["handoff"]
    assert "#42" in note(fake)["content"]
    assert not any("error" in f for f in out)


def test_second_escalate_call_in_same_reply_creates_no_ticket(model, fake_issues):
    fake = model(turns=[{"tool_calls": [escalate(), escalate()]}, {"chunks": ["ok"]}])
    ask()
    assert len(fake_issues.created) == 1
    second = fake.calls[1]["messages"][-1]["content"][1]["content"]
    assert "already" in second.lower()
