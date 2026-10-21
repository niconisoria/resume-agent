# CLAUDE.md — resume-agent

## Iron Laws

1. **Scope = docs/architecture.md** — build only what [docs/architecture.md](docs/architecture.md) describes. Anything else is out of scope.
2. **Spec first** — no code without a spec file in `docs/specs/`.
3. **Test first** — write failing tests before implementation.
4. **Verify before done** — run the app and confirm the feature works end-to-end (local + deployed on Fly) before closing a milestone.
5. **No secrets in code** — credentials/tokens go in `.env` locally (never committed) and Fly secrets in prod.
6. **No PII in Postgres** — store `sha256(ip + salt)` only; emails go to the private handoff repo, never the DB.
7. **No agent frameworks** — Anthropic SDK native tool use only. No LangChain/LlamaIndex/Redis/captcha/auth unless docs/architecture.md changes.
8. **CLAUDE.md < 200 lines** — move detail into linked docs, not here.

---

## Tech Stack

| Layer         | Choice                                                         |
| ------------- | -------------------------------------------------------------- |
| Language      | Python 3.12+                                                   |
| Framework     | FastAPI, SSE streaming (Deep Chat format)                      |
| LLM           | Anthropic SDK — chat `claude-haiku-5-5`, judge `claude-sonnet-5-5` |
| Embeddings    | Voyage AI                                                      |
| Database      | Neon Postgres + pgvector                                       |
| Handoff queue | GitHub Issues in a private repo (REST API)                     |
| Observability | Arize Phoenix Cloud + OpenInference, `arize-phoenix-evals`     |
| Rate limiting | `slowapi` in-memory (single instance) + daily budget in Postgres |
| Deploy        | Docker on Fly.io (single machine)                              |
| Package mgr   | `uv`                                                           |
| Test          | `uv run pytest`                                                |
| Lint          | `uv run ruff check .`                                          |
| Format        | `uv run ruff format .`                                         |
| Evals         | `uv run python evals/run_evals.py`                             |
| Ingest        | `uv run python scripts/ingest.py`                              |
| Deploy        | `fly deploy`                                                   |

---

## Project Map

```
resume-agent/
├── CLAUDE.md              ← you are here (symlink → AGENTS.md)
├── docs/
│   ├── architecture.md    ← stack, API contract, data model, guardrails, evals
│   └── specs/             ← one .md per feature (brainstorm → stories → flows → impl)
├── app/
│   ├── main.py            ← FastAPI app, routes, CORS, rate limits
│   ├── agent.py           ← tool-use loop, system prompt, streaming
│   ├── tools.py           ← tool schemas + implementations
│   ├── retrieval.py       ← embed query, pgvector search
│   ├── handoff.py         ← create handoff GitHub issue
│   ├── budget.py          ← daily cost check
│   ├── analytics.py       ← event logging, /insights aggregation, topic labels
│   ├── db.py              ← connection, queries
│   ├── config.py          ← settings from env
│   └── tracing.py         ← Phoenix / OpenInference setup
├── knowledge/             ← markdown knowledge base
├── scripts/ingest.py      ← chunk + embed + upsert knowledge/
├── evals/                 ← golden.jsonl, run_evals.py, REPORT.md
├── migrations/            ← plain SQL, numbered (001_init.sql)
├── tests/
├── Dockerfile
├── fly.toml
└── .github/workflows/ci.yml
```

Frontend (Deep Chat widget + `/insights` page) lives in the `niconisoria.github.io` repo (Astro user site; CV at `/resume/`).

---

## Dev Server

Run in background:

```
uv run --env-file .env uvicorn app.main:app --reload
```

Health check: `curl localhost:8000/health`.

---

## Test

- framework: pytest
- test_cmd: uv run pytest

---

## Commits

Conventional Commits, caveman style: `<type>(<scope>): <imperative summary>`, ≤50 chars, no period. Body only for non-obvious why, breaking changes, migrations. No AI attribution.

---

## Docs

- [docs/architecture.md](docs/architecture.md) — design, decisions, guardrails, eval thresholds
- [docs/specs/](docs/specs/) — feature specs (start here for any new work)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Anthropic tool use](https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview)
- [Deep Chat connect/stream](https://deepchat.dev/docs/connect)
- [pgvector](https://github.com/pgvector/pgvector)
- [Arize Phoenix](https://arize.com/docs/phoenix)
- [Fly.io](https://fly.io/docs/)
