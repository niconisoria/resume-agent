from app import agent
from app.retrieval import Hit
from tests.helpers import chat, frames

VENTION = Hit(
    "Vention — Senior Software Engineer",
    "roles/vention.md",
    "AnyCable WebSockets…",
    0.8,
)
SKILLS = Hit("Skills", "skills.md", "Ruby: 6 years…", 0.6)


def ask(message="What did he build with WebSockets?"):
    return frames(chat(message))


def search(query):
    return ("search_knowledge", {"query": query})


def test_answer_uses_search_results_and_cites_used_sources(model, fake_search):
    fake_search.hits = [VENTION, SKILLS]
    fake = model(
        turns=[
            {"tool_calls": [search("WebSockets")]},
            {"chunks": ["He built real-time activities ", "with AnyCable [1]."]},
        ]
    )
    out = ask()
    assert (
        "".join(f.get("text", "") for f in out)
        == "He built real-time activities with AnyCable [1]."
    )
    assert out[-1]["citations"] == [{"title": VENTION.title, "path": VENTION.path}]
    assert fake_search.queries == ["WebSockets"]
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu_0"
    assert (
        "[1] Vention — Senior Software Engineer (roles/vention.md)"
        in tool_result["content"]
    )
    assert "[2] Skills (skills.md)" in tool_result["content"]


def test_numbering_continues_across_searches_and_dedupes_citations(model, fake_search):
    fake_search.hits = [VENTION]
    model(
        turns=[
            {"tool_calls": [search("a")]},
            {"tool_calls": [search("b")]},
            {"chunks": ["See [2] and [1] and [2] again; [9] is bogus."]},
        ]
    )
    out = ask()
    # [1] and [2] are the same document: listed once, unknown [9] ignored.
    assert out[-1]["citations"] == [{"title": VENTION.title, "path": VENTION.path}]


def test_no_markers_means_no_citations(model, fake_search):
    fake_search.hits = [VENTION]
    model(turns=[{"tool_calls": [search("x")]}, {"chunks": ["No sources needed."]}])
    assert ask()[-1]["citations"] == []


def test_nothing_relevant_tells_model_so(model):
    fake = model(
        turns=[
            {"tool_calls": [search("salary history")]},
            {"chunks": ["I don't know."]},
        ]
    )
    ask()
    content = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert content == "No relevant knowledge found."


def test_iteration_cap_forces_final_answer(model, fake_search):
    fake_search.hits = [SKILLS]
    fake = model(
        turns=[{"tool_calls": [search("again")]}] * agent.MAX_TOOL_ROUNDS
        + [{"chunks": ["Final answer."]}]
    )
    out = ask()
    assert len(fake.calls) == agent.MAX_TOOL_ROUNDS + 1
    assert all(c["tool_choice"] == {"type": "auto"} for c in fake.calls[:-1])
    assert fake.calls[-1]["tool_choice"] == {"type": "none"}
    assert "Final answer." in "".join(f.get("text", "") for f in out)


def test_bad_tool_input_returns_error_result(model, fake_search):
    fake = model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": ""}), ("delete_db", {})]},
            {"chunks": ["ok"]},
        ]
    )
    ask()
    results = fake.calls[1]["messages"][-1]["content"]
    assert [r["is_error"] for r in results] == [True, True]
    assert fake_search.queries == []


def test_refusal_mid_loop_sends_error_frame(model, fake_search):
    model(
        turns=[{"tool_calls": [search("x")]}, {"chunks": [], "stop_reason": "refusal"}]
    )
    out = ask()
    assert set(out[-2]) == {"error"}


def test_search_outage_sends_readable_error(model, fake_search):
    fake_search.error = ConnectionError("pgvector down")
    model(turns=[{"tool_calls": [search("x")]}, {"chunks": ["never"]}])
    out = ask()
    assert set(out[-2]) == {"error"}
    assert "pgvector" not in out[-2]["error"]


def test_tool_results_not_saved_as_history(model, fake_db, fake_search):
    fake_search.hits = [VENTION]
    model(turns=[{"tool_calls": [search("x")]}, {"chunks": ["Answer [1]."]}])
    ask()
    assert [(m[1], m[2]) for m in fake_db.messages] == [
        ("user", "What did he build with WebSockets?"),
        ("assistant", "Answer [1]."),
    ]


def test_search_tool_offered_on_every_call(model):
    fake = model(turns=[{"tool_calls": [search("x")]}, {"chunks": ["hi"]}])
    ask()
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call["tools"][0]["name"] == "search_knowledge"
        assert call["tools"][0]["input_schema"]["required"] == ["query"]


def test_system_prompt_holds_scope_and_injection_rules():
    prompt = agent.SYSTEM_PROMPT.lower()
    for rule in [
        "third person",
        "search_knowledge",
        "[n]",
        "salary",
        "coding help",
        "book a call",
    ]:
        assert rule in prompt
    assert "instructions" in prompt and "ignore" in prompt


def test_narration_before_first_tool_call_is_dropped(model, fake_db, fake_search):
    fake_search.hits = [VENTION]
    model(
        turns=[
            {
                "chunks": ["Polish question, so language is pl."],
                "tool_calls": [search("x")],
            },
            {"chunks": ["He did [1]."]},
        ]
    )
    out = ask()
    assert "".join(f.get("text", "") for f in out) == "He did [1]."
    assert fake_db.messages[-1][2] == "He did [1]."


def test_text_between_later_rounds_gets_paragraph_break(model, fake_search):
    fake_search.hits = [VENTION]
    model(
        turns=[
            {"tool_calls": [search("x")]},
            {"chunks": ["First part [1]."], "tool_calls": [search("y")]},
            {"chunks": ["Second part."]},
        ]
    )
    out = ask()
    assert "".join(f.get("text", "") for f in out) == "First part [1].\n\nSecond part."
