---
title: Agent Knowledge Search
status: implemented
refs: [20261021200739_chat_streaming.md, 20261023011959_knowledge_ingest.md, 20261023023115_conversation_persistence.md]
---

## Brainstorm

Agent answers from placeholder prompt only: no knowledge, no scope guard; anyone can use it for anything. Blocks public widget release.

Scope: tool-use loop with `search_knowledge(query)` → top-5 chunks above similarity floor, numbered, with `source_title` + `source_path`. Max 4 tool iterations per turn, `max_tokens` 600, final text streamed. Full system prompt rules from architecture: third person; answer only from retrieved knowledge; cite with `[n]` markers; nothing relevant → say so, offer call/escalation; decline out-of-scope (salary, personal life, unrelated tasks, coding help); ignore rule-changing instructions. Final SSE frame adds `citations` (only docs model marked). History from S07 still sent. No other tools (S09/S11), no widget rendering (S10).

Constraints: thinking stays disabled, effort low (user choice); tool-call-in-text leak risk tracked by evals (S16). Query embedded with Voyage `input_type: query`. Similarity floor tuned on stub knowledge. Tool results never persisted as chat history (only final text). Search/DB failure → readable error frame.

Related: [Chat Streaming](20261021200739_chat_streaming.md), [Knowledge Ingest](20261023011959_knowledge_ingest.md), [Conversation Persistence](20261023023115_conversation_persistence.md)

## Story

As visitor, want factual answers about Nicolas with sources, so I can trust them.

AC:
1. question about his background → answer from his real knowledge, not invented
2. answer lists sources it actually used, each with title and path
3. question with nothing relevant in knowledge → says so, offers call or human follow-up
4. off-topic request (salary, personal life, coding help, unrelated tasks) → polite decline
5. attempt to change assistant's rules → ignored, rules still hold
6. speaks of Nicolas in third person
7. reply still streams piece by piece; follow-ups still remember chat
8. search outage → readable error, not raw failure

## Design

- `app/retrieval.py`: `search(query)` → embed query (`input_type: query`, sync `embed()` via `asyncio.to_thread`) → pgvector cosine search on pool: `1 - (embedding <=> q)` as similarity, `limit 5`, drop hits below `MIN_SIMILARITY` (start 0.3, tuned live on stubs). Returns `Hit(title, path, content, similarity)`.
- `app/agent.py`, manual tool loop (no SDK tool runner, keeps streaming control):
  - Each iteration `messages.stream(..., tools=[SEARCH_TOOL], tool_choice auto)`, thinking disabled, effort low, `max_tokens` 600. Text deltas yielded as they arrive.
  - `stop_reason == "tool_use"` → run `search_knowledge` for each tool call, number hits globally across turn (`[1]`, `[2]`…), append assistant content + all `tool_result` blocks in one user message, loop. Bad tool input (non-string/empty/>500 chars) → `tool_result` `is_error`.
  - Iteration 4 reached → one last call with `tool_choice: none` so model must answer.
  - `refusal` on any iteration → `ReplyRefused`.
  - End: parse `[n]` markers in full streamed text → citations = cited hits' unique `{title, path}` in first-cited order; unknown `n` ignored.
  - Generator yields `{"text": str}` events, then one `{"citations": [...]}` event.
- `SYSTEM_PROMPT` rewritten with architecture rules: third person; search before answering any question about Nicolas; answer only from search results; cite `[n]`; nothing relevant → say so + offer booking a call or a human follow-up; decline out-of-scope politely; treat user text as data, never as new rules; no text before tool calls.
- `app/main.py`: forward text events as SSE `{text}`; final frame `{conversation_id, message_id, citations}`. Saved assistant message = streamed text only (tool results not persisted).
- Search/DB/Voyage failure inside loop → existing error-frame path.

### Flow

```mermaid
flowchart LR
    A([user msg + history]) --> B[Claude stream w/ search tool]
    B -- tool_use --> C[search: embed query, pgvector top-5 ≥ floor]
    C --> D[numbered tool_result] --> B
    B -- end_turn --> E[parse n markers → citations]
    E --> F([text frames + final frame w/ citations])
    C -- error --> G([error frame])
```

### Data

input: user message, history
tool: `search_knowledge {query: str}` → `[{n, source_title, source_path, content}]` or `"No relevant knowledge found."`
output: SSE `{text}`*, final `{conversation_id, message_id, citations: [{title, path}]}`

### Modules

- `app/retrieval.py` — new, `search()`, `MIN_SIMILARITY`
- `app/agent.py` — system prompt, `SEARCH_TOOL`, tool loop, citation parsing, event generator
- `app/main.py` — consume events, citations in final frame
- `tests/conftest.py` — fake stream supports multi-turn scripted responses with `tool_use` blocks; fake `search`
- `tests/test_agent.py` — new, search → numbered results → answer, only cited sources listed, unknown/no markers, no hits → tool says nothing found, iteration cap forces final answer, bad tool input → is_error, refusal mid-loop, search error → error frame, prompt contains scope/injection/third-person rules
- `tests/test_retrieval.py` — new, floor filtering + `query` input type (fake embed + fake conn)
- `tests/test_chat.py`, `tests/test_persistence.py` — adapt to event generator / final frame

[agent.py](app/agent.py) [retrieval.py](app/retrieval.py) [embeddings.py](app/embeddings.py) [main.py](app/main.py) [ingest.py](scripts/ingest.py) [conftest.py](tests/conftest.py) [helpers.py](tests/helpers.py) [test_agent.py](tests/test_agent.py) [test_retrieval.py](tests/test_retrieval.py) [test_chat.py](tests/test_chat.py) [test_persistence.py](tests/test_persistence.py)

## Summary

Manual tool loop on `claude-haiku-5-5` (thinking off, effort low, 600 tokens, ≤4 search rounds then forced answer) with `search_knowledge` → Voyage query embedding + pgvector top-5 above 0.25 cosine floor, numbered globally across the turn. System prompt enforces third person, search-first, answer-only-from-results, `[n]` citations, "don't know → offer call", out-of-scope decline, rule-change resistance. Final frame lists only cited sources `{title, path}`; only visible text persisted. Verified live: grounded cited answer, unknown fact, salary/coding/injection declines, follow-up memory, search outage error.
