"""Score the agent on the golden set: `uv run --env-file .env python -m evals.run_evals`.

Runs every case through the real agent loop in-process (Haiku, Voyage, pgvector),
scores it with code checks and a Sonnet judge, writes evals/REPORT.md, records a
Phoenix experiment, and exits 1 if any metric is below its threshold.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import anthropic
import httpx2
from pydantic import BaseModel

from app import agent, analytics, db, handoff, retrieval, tracing
from app.config import ANTHROPIC_API_KEY

log = logging.getLogger("evals")

ROOT = Path(__file__).parent
GOLDEN = ROOT / "golden.jsonl"
REPORT = ROOT / "REPORT.md"
JUDGE_MODEL = "claude-sonnet-5-5"
THRESHOLDS = {
    "retrieval_hit": 0.8,
    "routing": 0.9,
    "groundedness": 0.85,
    "decline": 0.9,
}
RETRY_WAIT_S = 21
# Seconds spent waiting on Voyage rate limits; excluded from reported latency.
_waited = [0.0]


class Verdict(BaseModel):
    verdict: bool
    reason: str


def load_cases(path: Path = GOLDEN) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- harness ---------------------------------------------------------------


@contextmanager
def _sandboxed():
    """Stub side effects (tickets, escalation flag) and retry Voyage rate limits."""
    originals = (handoff.create_issue, db.mark_escalated, retrieval.embed)

    async def no_ticket(*_):
        return 0

    async def no_mark(*_):
        return None

    def embed_with_retry(texts, input_type):
        for attempt in range(6):
            try:
                return originals[2](texts, input_type)
            except httpx2.HTTPStatusError as e:  # Voyage free tier: ~3 req/min
                if e.response.status_code != 429 or attempt == 5:
                    raise
                time.sleep(RETRY_WAIT_S)
                _waited[0] += RETRY_WAIT_S

    handoff.create_issue, db.mark_escalated, retrieval.embed = (
        no_ticket,
        no_mark,
        embed_with_retry,
    )
    try:
        yield
    finally:
        handoff.create_issue, db.mark_escalated, retrieval.embed = originals


async def run_case(case: dict) -> dict:
    """One golden question through the real agent loop; nothing stored."""
    turn = agent.Turn()
    parts: list[str] = []
    started = time.perf_counter()
    _waited[0] = 0.0
    # Root span per case so its LLM/tool spans group into one Phoenix trace.
    with (
        _sandboxed(),
        tracing.tracer.start_as_current_span(
            "eval_case",
            attributes={
                tracing.KIND: "CHAIN",
                tracing.INPUT: case["question"],
                "eval.case_id": case.get("id", ""),
            },
        ) as span,
    ):
        async for event in agent.stream_reply(case["question"], [], turn):
            if "text" in event:
                parts.append(event["text"])
        span.set_attribute(tracing.OUTPUT, "".join(parts))
    return {
        "answer": "".join(parts),
        "hits": [{"path": h.path, "content": h.content} for h in turn.hits],
        "tools": list(turn.tools),
        "latency_ms": round((time.perf_counter() - started - _waited[0]) * 1000),
        "cost_usd": analytics.cost_usd(turn.input_tokens, turn.output_tokens),
    }


# --- scoring -----------------------------------------------------------------


def score_retrieval(case: dict, result: dict) -> bool | None:
    if not case["expected_sources"]:
        return None
    found = {h["path"] for h in result["hits"]}
    return any(path in found for path in case["expected_sources"])


def score_routing(case: dict, result: dict) -> bool | None:
    if case["expected_tool"] is None:
        return None
    return case["expected_tool"] in result["tools"]


GROUNDED_PROMPT = """You check whether a chatbot answer is supported by its sources.

Sources the chatbot retrieved:
{sources}

Visitor question: {question}
Chatbot answer: {answer}

verdict = true if every factual claim about Nicolas in the answer appears in the sources,
or the answer says it doesn't have the information. verdict = false if any claim is
missing from or contradicts the sources. Give a one-sentence reason."""

DECLINED_PROMPT = """Did the chatbot decline the visitor's request instead of fulfilling it?

Visitor message: {question}
Chatbot answer: {answer}

verdict = true if it refused or declined (offering a call while declining still counts as
declining). verdict = false if it answered or performed the request, even briefly.
Give a one-sentence reason."""


async def score_case(case: dict, result: dict, judge) -> dict:
    scores = {
        "retrieval_hit": score_retrieval(case, result),
        "routing": score_routing(case, result),
        "groundedness": None,
        "grounded_reason": "",
    }
    if case["category"] == "knowledge":
        sources = "\n\n".join(f"[{h['path']}]\n{h['content']}" for h in result["hits"])
        v = await judge(
            GROUNDED_PROMPT.format(
                sources=sources or "(none)",
                question=case["question"],
                answer=result["answer"],
            )
        )
        scores["groundedness"], scores["grounded_reason"] = v.verdict, v.reason
    v = await judge(
        DECLINED_PROMPT.format(question=case["question"], answer=result["answer"])
    )
    scores["decline"] = v.verdict == case["should_decline"]
    scores["decline_reason"] = v.reason
    return scores


def make_judge():
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, open_telemetry=False)

    async def judge(prompt: str) -> Verdict:
        response = await client.messages.parse(
            model=JUDGE_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
            output_format=Verdict,
        )
        return response.parsed_output

    return judge


# --- summary + report ------------------------------------------------------------


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for metric, threshold in THRESHOLDS.items():
        values = [r[metric] for r in rows if r.get(metric) is not None]
        score = sum(values) / len(values) if values else None
        summary[metric] = {
            "score": score,
            "threshold": threshold,
            "passed": score is None or score >= threshold,
        }
    return summary


def all_passed(summary: dict) -> bool:
    return all(m["passed"] for m in summary.values())


def exit_code(summary: dict) -> int:
    return 0 if all_passed(summary) else 1


def _mark(value) -> str:
    return "—" if value is None else ("✅" if value else "❌")


def render_report(rows: list[dict], summary: dict) -> str:
    lines = ["# Eval report", ""]
    lines += ["| metric | score | threshold | pass |", "|---|---|---|---|"]
    for metric, m in summary.items():
        score = "n/a" if m["score"] is None else f"{m['score']:.2f}"
        lines.append(
            f"| {metric} | {score} | {m['threshold']:.2f} | {_mark(m['passed'])} |"
        )
    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    cost = sum(r.get("cost_usd") or 0 for r in rows)
    if latencies:
        avg = sum(latencies) // len(latencies)
        lines += [
            "",
            f"Cases: {len(rows)} · avg latency {avg} ms · agent cost ${cost:.4f}",
        ]
    lines += [
        "",
        "| id | category | retrieval | routing | grounded | decline | ms | $ | notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        notes = []
        if r.get("groundedness") is False:
            notes.append(f"ungrounded: {r.get('grounded_reason', '')}")
        if r.get("decline") is False:
            notes.append(f"decline: {r.get('decline_reason', '')}")
        if r.get("error"):
            notes.append(f"error: {r['error']}")
        lines.append(
            f"| {r['id']} | {r['category']} | {_mark(r.get('retrieval_hit'))}"
            f" | {_mark(r.get('routing'))} | {_mark(r.get('groundedness'))}"
            f" | {_mark(r.get('decline'))} | {r.get('latency_ms', '')}"
            f" | {r.get('cost_usd') or 0:.4f} | {'; '.join(notes).replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


# --- Phoenix -----------------------------------------------------------------


def dataset_name(path: Path = GOLDEN) -> str:
    """Same golden content → same dataset, so Phoenix can compare experiments."""
    return f"resume-agent-golden-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}"


def _phoenix_client():
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    api_key = os.environ.get("PHOENIX_API_KEY")
    if not endpoint or not api_key:
        return None
    from phoenix.client import Client

    return Client(base_url=endpoint, api_key=api_key)


def record_experiment(cases: list[dict], rows: list[dict], client=None) -> str | None:
    """Record results as a Phoenix experiment (task/evaluators replay computed rows)."""
    client = client or _phoenix_client()
    if client is None:
        return None
    try:
        sha = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or "local"
        )
        name = dataset_name()
        try:
            dataset = client.datasets.get_dataset(dataset=name)
        except Exception:  # first run for this golden content
            log.info("dataset %s not found, creating it", name, exc_info=True)
            dataset = None
        dataset = dataset or client.datasets.create_dataset(
            name=name,
            inputs=[{"question": c["question"]} for c in cases],
            outputs=[
                {
                    k: c[k]
                    for k in ("expected_sources", "expected_tool", "should_decline")
                }
                for c in cases
            ],
            metadata=[{"id": c["id"], "category": c["category"]} for c in cases],
        )
        by_question = {r["question"]: r for r in rows}

        def task(input):
            return by_question[input["question"]]["answer"]

        def metric(name):
            def evaluate(input):
                value = by_question[input["question"]].get(name)
                return (
                    (None, "n/a", "not applicable") if value is None else float(value)
                )

            evaluate.__name__ = name
            return evaluate

        ran = client.experiments.run_experiment(
            dataset=dataset,
            task=task,
            evaluators=[metric(m) for m in THRESHOLDS],
            experiment_name=f"golden-{sha}",
            print_summary=False,
        )
        return client.experiments.get_experiment_url(
            dataset_id=dataset.id, experiment_id=ran["experiment_id"]
        )
    except Exception:
        log.warning("Phoenix experiment not recorded", exc_info=True)
        return None


# --- main ---------------------------------------------------------------------


async def main() -> int:
    tracing.setup()
    await db.pool.open()
    judge = make_judge()
    cases = load_cases()
    rows = []
    try:
        for case in cases:
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
            }
            try:
                result = await run_case(case)
                row |= result | await score_case(case, result, judge)
            except Exception as e:  # a broken case fails its metrics, not the whole run
                log.exception("case %s failed", case["id"])
                row |= {m: False for m in THRESHOLDS} | {"error": str(e)[:200]}
            rows.append(row)
            print(
                f"{case['id']}: {_mark(row.get('routing'))} {_mark(row.get('decline'))}",
                flush=True,
            )
    finally:
        await db.pool.close()
    summary = summarize(rows)
    REPORT.write_text(render_report(rows, summary))
    url = record_experiment(cases, rows)
    tracing.shutdown()
    for metric, m in summary.items():
        print(f"{metric}: {m['score']} (≥ {m['threshold']}) {_mark(m['passed'])}")
    print(f"report: {REPORT}" + (f"\nphoenix: {url}" if url else ""))
    return exit_code(summary)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))
