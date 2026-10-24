import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from app.retrieval import Hit
from evals import run_evals as ev

GOLDEN = Path(__file__).parent.parent / "evals" / "golden.jsonl"
KNOWLEDGE = Path(__file__).parent.parent / "knowledge"


def case(**overrides):
    base = {
        "id": "x",
        "category": "knowledge",
        "question": "q",
        "expected_sources": ["skills.md"],
        "expected_tool": "search_knowledge",
        "should_decline": False,
    }
    return base | overrides


def result(**overrides):
    base = {
        "answer": "a",
        "hits": [],
        "tools": [],
        "latency_ms": 100,
        "cost_usd": 0.001,
    }
    return base | overrides


def test_golden_set_shape_and_mix():
    cases = ev.load_cases(GOLDEN)
    assert len(cases) == 25
    assert len({c["id"] for c in cases}) == 25
    counts = Counter(c["category"] for c in cases)
    assert counts == {"knowledge": 15, "routing": 4, "out_of_scope": 3, "injection": 3}
    for c in cases:
        assert set(c) == {
            "id",
            "category",
            "question",
            "expected_sources",
            "expected_tool",
            "should_decline",
        }
        for path in c["expected_sources"]:
            assert (KNOWLEDGE / path).exists(), path
        assert c["should_decline"] == (c["category"] in {"out_of_scope", "injection"})


def test_retrieval_hit_scoring():
    hit = {"path": "skills.md", "content": "Ruby"}
    assert ev.score_retrieval(case(), result(hits=[hit])) is True
    assert (
        ev.score_retrieval(case(), result(hits=[{"path": "profile.md", "content": ""}]))
        is False
    )
    assert ev.score_retrieval(case(expected_sources=[]), result()) is None


def test_routing_scoring():
    assert (
        ev.score_routing(case(expected_tool="get_cv"), result(tools=["get_cv"])) is True
    )
    assert (
        ev.score_routing(case(expected_tool="get_cv"), result(tools=["book_call"]))
        is False
    )
    assert ev.score_routing(case(expected_tool=None), result(tools=[])) is None


def test_summary_against_thresholds():
    rows = [
        {"retrieval_hit": True, "routing": True, "groundedness": True, "decline": True},
        {
            "retrieval_hit": False,
            "routing": None,
            "groundedness": True,
            "decline": True,
        },
    ]
    summary = ev.summarize(rows)
    assert summary["retrieval_hit"]["score"] == 0.5
    assert summary["retrieval_hit"]["passed"] is False
    assert summary["routing"]["score"] == 1.0  # None skipped
    assert summary["groundedness"]["passed"] is True
    assert ev.all_passed(summary) is False
    assert ev.all_passed(ev.summarize(rows[:1])) is True


def test_report_lists_scores_costs_and_failures():
    rows = [
        {
            "id": "k01",
            "category": "knowledge",
            "question": "What did he build?",
            "retrieval_hit": False,
            "routing": True,
            "groundedness": False,
            "decline": True,
            "grounded_reason": "claims Kafka, not in sources",
            "latency_ms": 1200,
            "cost_usd": 0.0009,
        }
    ]
    report = ev.render_report(rows, ev.summarize(rows))
    assert "| retrieval_hit | 0.00 | 0.80 | ❌ |" in report
    assert "k01" in report and "claims Kafka" in report
    assert "1200" in report and "0.0009" in report


def test_harness_never_files_tickets_or_writes_chats(
    model, fake_issues, fake_db, fake_events, fake_search
):
    fake_search.hits = [Hit("Skills", "skills.md", "Ruby: 6", 0.6)]
    args = {"summary": "s", "reason": "r", "contact_email": "a@b.co"}
    model(
        turns=[
            {
                "tool_calls": [
                    ("search_knowledge", {"query": "ruby"}),
                    ("escalate_to_human", args),
                ]
            },
            {"chunks": ["Six years [1]."], "usage": (1000, 100)},
        ]
    )
    out = asyncio.run(ev.run_case(case(question="Ruby?")))
    assert out["answer"] == "Six years [1]."
    assert out["tools"] == ["search_knowledge", "escalate_to_human"]
    assert out["hits"] == [{"path": "skills.md", "content": "Ruby: 6"}]
    assert out["cost_usd"] > 0
    assert fake_issues.created == []
    assert fake_db.messages == [] and fake_db.conversations == {}
    assert fake_events.rows == []


def test_score_case_uses_judge_only_where_relevant():
    calls = []

    async def judge(prompt):
        calls.append(prompt)
        return ev.Verdict(verdict=True, reason="ok")

    knowledge = asyncio.run(
        ev.score_case(
            case(), result(hits=[{"path": "skills.md", "content": "Ruby"}]), judge
        )
    )
    assert (
        knowledge["groundedness"] is True and knowledge["decline"] is False
    )  # judge said declined
    routing = asyncio.run(
        ev.score_case(case(category="routing", expected_sources=[]), result(), judge)
    )
    assert routing["groundedness"] is None
    assert (
        len(calls) == 3
    )  # 2 for knowledge (grounded + declined), 1 for routing (declined)


@pytest.mark.parametrize("passed, code", [(True, 0), (False, 1)])
def test_exit_code_follows_thresholds(passed, code):
    summary = {
        "m": {"score": 1.0 if passed else 0.0, "threshold": 0.5, "passed": passed}
    }
    assert ev.exit_code(summary) == code


def test_golden_file_is_valid_jsonl():
    for line in GOLDEN.read_text().splitlines():
        json.loads(line)


class FakePhoenix:
    def __init__(self, existing=None):
        from types import SimpleNamespace

        self.created, self.runs, self.existing = [], [], existing
        self.datasets = SimpleNamespace(
            get_dataset=self._get, create_dataset=self._create
        )
        self.experiments = SimpleNamespace(
            run_experiment=self._run, get_experiment_url=lambda **kw: "https://phx/exp"
        )

    def _get(self, dataset):
        if self.existing is None:
            raise LookupError(dataset)
        return self.existing

    def _create(self, **kw):
        from types import SimpleNamespace

        self.created.append(kw)
        return SimpleNamespace(id="ds1")

    def _run(self, dataset, task, evaluators, **kw):
        q = {"question": "Ruby?"}
        self.runs.append(
            {"answer": task(q), "scores": {e.__name__: e(q) for e in evaluators}}
        )
        return {"experiment_id": "e1"}


ROW = {
    "question": "Ruby?",
    "answer": "Six years.",
    "retrieval_hit": True,
    "routing": None,
    "groundedness": True,
    "decline": True,
}


def test_experiment_replays_rows_and_marks_na():
    phx = FakePhoenix()
    url = ev.record_experiment([case(question="Ruby?")], [ROW], client=phx)
    assert url == "https://phx/exp"
    (run,) = phx.runs
    assert run["answer"] == "Six years."
    assert run["scores"]["retrieval_hit"] == 1.0
    assert run["scores"]["routing"] == (None, "n/a", "not applicable")


def test_dataset_reused_for_same_golden_content():
    from types import SimpleNamespace

    phx = FakePhoenix(existing=SimpleNamespace(id="ds-old"))
    ev.record_experiment([case(question="Ruby?")], [ROW], client=phx)
    assert phx.created == []
    assert ev.dataset_name() == ev.dataset_name()
    assert ev.dataset_name().startswith("resume-agent-golden-")


def test_no_phoenix_settings_skips_experiment(monkeypatch):
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.delenv("PHOENIX_API_KEY", raising=False)
    assert ev.record_experiment([case()], [ROW]) is None


def test_rate_limit_waits_excluded_from_latency(model, fake_search, monkeypatch):
    import httpx2

    from app import retrieval

    calls = {"n": 0}

    def flaky_embed(texts, input_type):
        calls["n"] += 1
        if calls["n"] == 1:
            req = httpx2.Request("POST", "https://api.voyageai.com/v1/embeddings")
            raise httpx2.HTTPStatusError(
                "429", request=req, response=httpx2.Response(429, request=req)
            )
        return [[0.0] * 1024]

    monkeypatch.setattr(retrieval, "embed", flaky_embed)
    monkeypatch.setattr(ev, "RETRY_WAIT_S", 0.2)
    monkeypatch.setattr(ev.time, "sleep", lambda s: None)  # fake wait, still counted
    wrapped = []
    with ev._sandboxed():
        wrapped.append(retrieval.embed(["q"], "query"))
    assert calls["n"] == 2
    assert ev._waited[0] == 0.2
