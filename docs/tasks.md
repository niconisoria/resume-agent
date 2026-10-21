# Tasks

Milestones toward a deployed agent linked from the CV. Design details live in [architecture.md](architecture.md).

**Rules**
- Each spec below is one hard deliverable. Starting one creates `docs/specs/<timestamp>_<slug>.md` from its story + AC, then plan → tests → implement.
- A spec is done when every AC passes, verified on the deployed app where it applies.
- A milestone is done when all its specs are done and its **Done when** check passes on the deployed app.
- Work specs in order and respect **Depends**. Anything not listed here is out of scope until M5 ships.
- Specs tagged `resume` are implemented in the Astro repo, not here.

| # | Milestone | Specs |
|---|---|---|
| M0 | Accounts + repo setup | — (checklist) |
| M1 | Walking skeleton | S01–S04 |
| M2 | Knowledge + skills | S05–S09 |
| M3 | Queue + analytics data | S10–S13 |
| M4 | Evals + insights | S14–S18 |
| M5 | Polish + docs | — (checklist) |

---

## M0 — Accounts + repo setup

No code, no spec.

- [ ] `git init`, `.gitignore` (`.env`, `.venv`, `__pycache__`), push `resume-agent` to GitHub
- [ ] `uv init` with Python 3.12+, add `ruff` + `pytest`
- [ ] Anthropic API key + Console monthly spend limit (~$10–20)
- [ ] Fly.io account (card on file), `fly launch --no-deploy`
- [ ] Neon project, enable `pgvector`
- [ ] Voyage AI key; confirm current recommended model + embedding dimension
- [ ] Private GitHub repo `resume-agent-handoffs` + fine-grained token (Issues read/write, that repo only)
- [ ] Phoenix Cloud instance + API key
- [ ] Calendar booking URL; CV PDF URLs (EN, PL)
- [ ] Local `.env` with all variables from architecture.md

**Done when:** every env variable has a value locally and the repo is on GitHub.

---

## M1 — Walking skeleton

### S01 `api_skeleton`
**Story:** As the owner, I want a deployed FastAPI service, so every later feature ships to a live URL.
**Depends:** M0
**AC:**
1. `GET /health` returns 200 on the Fly URL.
2. All settings are read from env (`config.py`); startup fails if a required one is missing.
3. Requests from `ALLOWED_ORIGIN` get CORS headers; other origins don't.
4. Dockerfile builds locally; `fly.toml` runs one always-on machine; `fly deploy` succeeds.

### S02 `chat_streaming`
**Story:** As a visitor, I want to send a message and see the reply stream in, so the chat feels responsive.
**Depends:** S01
**AC:**
1. `POST /chat {message}` streams `claude-haiku-5-5` output as SSE in Deep Chat's format.
2. Messages over 1,000 characters are rejected with a 4xx and a readable error; the LLM is not called.
3. Empty or missing `message` is rejected with a 4xx.

### S03 `rate_limits`
**Story:** As the owner, I want per-IP limits, so one visitor can't burn the budget.
**Depends:** S02
**AC:**
1. Limits keyed on `Fly-Client-IP`: 10 messages/min and 60/day.
2. Over the limit → `429` with `Retry-After` and a polite message body.
3. Limits are per IP: a second IP is unaffected.

### S04 `chat_widget` · `resume`
**Story:** As a visitor, I want a chat box on the resume site, so I can ask about Nicolas without leaving the page.
**Depends:** S02
**AC:**
1. Deep Chat component on the site, connected to the Fly `/chat` URL, renders the streamed reply.
2. One-line privacy notice is visible in the widget.
3. A 429 or 4xx shows the server's polite message, not a raw error.

**Done when:** I can chat on the live site, and the 11th message in a minute returns a polite 429.

---

## M2 — Knowledge + skills

### S05 `knowledge_ingest`
**Story:** As the owner, I want one command to load `knowledge/` into pgvector, so the agent answers from current sources.
**Depends:** S01
**AC:**
1. `migrations/001_init.sql` creates all five tables from architecture.md and applies cleanly to Neon.
2. `scripts/ingest.py` chunks every markdown file, embeds with Voyage, and upserts `documents` + `chunks` with title and path.
3. Re-running without changes creates no duplicates; an edited file replaces its old chunks; a deleted file removes them.
4. 2–3 stub knowledge files are ingested.

### S06 `conversation_persistence`
**Story:** As the owner, I want conversations and messages stored, so the agent keeps context and analytics has data.
**Depends:** S05
**AC:**
1. A message without `conversation_id` creates a conversation; the final SSE event returns `conversation_id` and `message_id`.
2. Follow-ups with that `conversation_id` include prior turns in the LLM context.
3. Conversations store `sha256(ip + IP_HASH_SALT)` only, never the raw IP.
4. Turn 21 in a conversation is refused with a polite message; the LLM is not called.

### S07 `agent_knowledge_search`
**Story:** As a visitor, I want factual answers about Nicolas with sources, so I can trust them.
**Depends:** S06
**AC:**
1. Tool-use loop in `agent.py`: max 4 tool iterations per turn, `max_tokens` 600, final text streamed.
2. `search_knowledge(query)` returns the top-5 chunks with `source_title` and `source_path`.
3. The final SSE event lists citations for the sources used.
4. System prompt rules hold: third person, retrieved knowledge only, says so when nothing relevant is found, declines out-of-scope politely, ignores rule-changing instructions.

### S08 `skills_call_cv`
**Story:** As a visitor, I want to book a call or get the CV by asking, so I can act without searching the site.
**Depends:** S07
**AC:**
1. `book_call` returns `CALENDAR_URL` as a button card in the final SSE event.
2. `get_cv(language)` returns `CV_URL_EN` or `CV_URL_PL` as a download card; any other language is rejected.
3. "Can we talk?" routes to `book_call`; "send me the CV" routes to `get_cv`.

### S09 `widget_citations_cards` · `resume`
**Story:** As a visitor, I want to see sources and clickable cards, so I can verify answers and act on them.
**Depends:** S07, S08
**AC:**
1. Citations from the final event render under the answer.
2. Button and download cards render and open their URLs.
3. The widget sends `conversation_id` on follow-up messages.

**Done when:** answers cite the stub knowledge files, and "book a call" shows a button.

---

## M3 — Queue + analytics data

### S10 `human_handoff`
**Story:** As a visitor, I want to hand my question to Nicolas, so I get an answer the bot can't give.
**Depends:** S07
**AC:**
1. `escalate_to_human(summary, reason, contact_email?)` creates an issue labelled `handoff` in `GITHUB_HANDOFF_REPO` with summary, reason, email and transcript.
2. The conversation status becomes `escalated`; the final event carries the issue number in a "a human will follow up" card.
3. `contact_email` never reaches Postgres.
4. If the GitHub API fails, the visitor gets a polite fallback (call + CV buttons) and the error is logged.

### S11 `event_logging`
**Story:** As the owner, I want every answer, tool call and block logged, so I can measure the agent.
**Depends:** S06
**AC:**
1. `events` rows are written for `answer`, `tool_call`, `escalation`, `rate_limited`, `budget_exceeded`.
2. `answer` events carry `latency_ms`, `input_tokens`, `output_tokens`, `cost_usd`.
3. A logging failure never breaks the chat response.

### S12 `feedback`
**Story:** As a visitor, I want to rate an answer, so the owner learns what works.
**Depends:** S11
**AC:**
1. `POST /feedback {message_id, rating: 1 | -1}` stores a `feedback` event; any other rating or an unknown `message_id` → 4xx.
2. `resume`: 👍/👎 under each answer calls the endpoint, once per message.

### S13 `daily_budget`
**Story:** As the owner, I want a daily spend cap, so a traffic spike can't run up the bill.
**Depends:** S11
**AC:**
1. If today's `sum(cost_usd)` ≥ `DAILY_BUDGET_USD`, `/chat` skips the LLM and returns a fallback with call + CV buttons.
2. A `budget_exceeded` event is logged.
3. With `DAILY_BUDGET_USD=0` on Fly, the fallback appears on the live site.

**Done when:** escalation creates an issue in the handoff repo and GitHub emails me, the events table fills, and the budget fallback works with `DAILY_BUDGET_USD=0`.

---

## M4 — Evals + insights

### S14 `tracing`
**Story:** As the owner, I want every chat traced, so I can debug answers and tool calls.
**Depends:** S07
**AC:**
1. OpenInference Anthropic instrumentation exports to Phoenix Cloud.
2. A live chat shows its LLM and tool spans in Phoenix.
3. Phoenix being unreachable never breaks the chat.

### S15 `eval_suite`
**Story:** As the owner, I want a repeatable eval run, so I know whether a change makes the agent better or worse.
**Depends:** S08, S14
**AC:**
1. `evals/golden.jsonl` has ~25 cases: 15 knowledge, 4 routing, 3 out-of-scope, 3 injection.
2. `run_evals.py` runs a Phoenix experiment with retrieval-hit, routing, groundedness, decline, and latency/cost.
3. Results are written to `evals/REPORT.md`; the script exits non-zero below the thresholds in architecture.md.

### S16 `ci_pipeline`
**Story:** As the owner, I want CI to gate PRs, so regressions don't reach the live site.
**Depends:** S15
**AC:**
1. Every PR runs `ruff check` and `pytest`.
2. PRs touching `app/`, `knowledge/` or `evals/` also run the eval suite and fail below threshold.
3. Eval results are posted on the PR.

### S17 `insights_api`
**Story:** As a visitor, I want aggregated stats about the agent, so I can see how it performs.
**Depends:** S11, S12
**AC:**
1. Each user message gets a topic from the fixed set (Haiku), stored on its event.
2. `GET /insights` returns every metric listed under "Insights page" in architecture.md.
3. The response holds no raw message text, emails or IP hashes.
4. The response is cached for 5 minutes.

### S18 `insights_page` · `resume`
**Story:** As a visitor, I want a public `/insights` page, so the numbers are easy to read.
**Depends:** S17
**AC:**
1. `/insights` on the Astro site shows every metric from `GET /insights` as Chart.js charts or tables.
2. An API error shows a readable message, not a blank page.

**Done when:** traces are visible in Phoenix, CI posts eval results, and the insights page is live.

---

## M5 — Polish + docs

Docs and content, no spec.

- [ ] Real knowledge base in `knowledge/`; re-ingest; eval suite passes all thresholds
- [ ] README: what it is, architecture diagram, how to run, why no framework / why Fly / why GitHub Issues
- [ ] `evals/REPORT.md` up to date
- [ ] Runbook: deploy, rotate secrets, budget/limit tuning, re-ingest, handoff issue triage
- [ ] 2-min demo video
- [ ] Link from CV + LinkedIn

**Done when:** a stranger can understand and evaluate the project in 5 minutes.

---

## Later (after M5)

- 90-day retention scheduled delete
- See "Out of scope" in [architecture.md](architecture.md)
