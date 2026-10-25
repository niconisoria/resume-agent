from fastapi.testclient import TestClient

from app import agent, tools
from app.main import app
from app.retrieval import Hit
from tests.helpers import frames

http = TestClient(app)

BOOKING = {
    "type": "booking",
    "label": "Book a call with Nicolas",
    "url": "https://cal.example/book",
}
CV_EN = {
    "type": "cv",
    "label": "Download CV (EN)",
    "url": "https://cv.example/en.pdf",
    "language": "en",
}
CV_PL = {
    "type": "cv",
    "label": "Download CV (PL)",
    "url": "https://cv.example/pl.pdf",
    "language": "pl",
}


def ask(message="hi"):
    return frames(http.post("/chat", json={"message": message}))


def tool(name, **args):
    return (name, args)


def test_skill_tools_offered(model):
    fake = model(chunks=["hi"])
    ask()
    names = [t["name"] for t in fake.calls[0]["tools"]]
    assert names[:3] == ["search_knowledge", "book_call", "get_cv"]
    cv = fake.calls[0]["tools"][2]["input_schema"]
    assert cv["properties"]["language"]["enum"] == ["en", "pl"]
    assert cv["required"] == ["language"]


def test_book_call_adds_booking_card(model):
    fake = model(
        turns=[{"tool_calls": [tool("book_call")]}, {"chunks": ["Sure, pick a slot."]}]
    )
    final = ask("Can we talk?")[-1]
    assert final["cards"] == [BOOKING]
    result = fake.calls[1]["messages"][-1]["content"][0]
    assert (
        result["content"]
        == "Book a call with Nicolas button is now shown to the visitor."
    )
    assert "is_error" not in result


def test_get_cv_english(model):
    model(
        turns=[
            {"tool_calls": [tool("get_cv", language="en")]},
            {"chunks": ["Here it is."]},
        ]
    )
    assert ask("Send me the CV")[-1]["cards"] == [CV_EN]


def test_get_cv_polish(model):
    model(
        turns=[{"tool_calls": [tool("get_cv", language="pl")]}, {"chunks": ["Proszę."]}]
    )
    assert ask("Poproszę CV")[-1]["cards"] == [CV_PL]


def test_invalid_language_is_tool_error_and_no_card(model):
    fake = model(
        turns=[
            {"tool_calls": [tool("get_cv", language="de")]},
            {"chunks": ["Only EN/PL."]},
        ]
    )
    final = ask("CV in German?")[-1]
    assert final["cards"] == []
    assert fake.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_cards_deduped_and_kept_in_order(model):
    model(
        turns=[
            {"tool_calls": [tool("book_call"), tool("get_cv", language="en")]},
            {"tool_calls": [tool("book_call")]},
            {"chunks": ["Done."]},
        ]
    )
    assert ask()[-1]["cards"] == [BOOKING, CV_EN]


def test_cards_and_citations_together(model, fake_search):
    fake_search.hits = [Hit("Skills", "skills.md", "Ruby: 6", 0.6)]
    model(
        turns=[
            {
                "tool_calls": [
                    ("search_knowledge", {"query": "ruby"}),
                    tool("book_call"),
                ]
            },
            {"chunks": ["Six years of Ruby [1]. Happy to talk."]},
        ]
    )
    final = ask()[-1]
    assert final["citations"] == [{"title": "Skills", "path": "skills.md"}]
    assert final["cards"] == [BOOKING]


def test_no_tools_no_cards(model):
    model(chunks=["Hello!"])
    assert ask()[-1]["cards"] == []


def test_prompt_covers_skill_rules():
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "book_call" in prompt and "get_cv" in prompt
    assert "polish" in prompt
    assert "never write urls" in prompt or "never paste urls" in prompt


def test_every_offered_tool_has_a_handler():
    assert [t["name"] for t in tools.TOOLS] == list(tools.HANDLERS)


def test_repeat_tool_call_tells_model_button_already_shown(model):
    fake = model(
        turns=[
            {"tool_calls": [tool("book_call")]},
            {"tool_calls": [tool("book_call")]},
            {"chunks": ["Done."]},
        ]
    )
    ask()
    second = fake.calls[2]["messages"][-1]["content"][0]["content"]
    assert "already shown" in second


def test_prompt_forbids_narrating_reasoning():
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "never describe your reasoning" in prompt
    assert "without saying where it appears" in prompt
