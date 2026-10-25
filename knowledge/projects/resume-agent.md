# Resume Agent (personal project)

## What it is

Resume Agent is the chat assistant on Nicolas's portfolio site, the one answering these questions. It is a RAG (retrieval-augmented generation) assistant that answers questions about his career with citations from a knowledge base. Source code: github.com/niconisoria/resume-agent.

## How it works

- A FastAPI service streams Claude answers over SSE, citing chunks retrieved from a PostgreSQL + pgvector knowledge base embedded with Voyage AI.
- Tools let the assistant book a call, send the CV, or hand off to a human through a GitHub Issues queue.
- The agent loop is written directly on the Anthropic SDK with no agent framework, keeping every LLM and tool step explicit and traced in Arize Phoenix.
- Deploys to Fly.io are gated on a model-graded eval suite in CI (retrieval, routing, groundedness, declines), so a quality regression blocks the release.
- Abuse and spend are capped with per-IP rate limits and a daily budget.
- A public insights page (niconisoria.github.io/insights/) shows resolution rate, feedback, topics, p95 latency and cost per conversation.

## Stack

Python, FastAPI, Anthropic API, PostgreSQL + pgvector, Voyage AI, Arize Phoenix, Docker, Fly.io.
