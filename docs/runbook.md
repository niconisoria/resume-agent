# Runbook

Commands assume the repo root and a filled-in `.env`. Local `.env` points at the production Neon database, so local chats show up in `/insights`.

## Deploy

Normal path: push to `main`. CI (`.github/workflows/ci.yml`) runs:

1. `check`: ruff + pytest.
2. `changes`: decides whether the app or eval changed.
3. `eval`: golden-set eval when `app/`, `evals/` or dependencies changed; fails below threshold.
4. `deploy`: `flyctl deploy --ha=false`, then a `/health` smoke check and a one-machine check. Skipped for docs-only pushes and for commits that are no longer the tip of `main`.

**Migrations first.** The app never migrates at boot. Before pushing code that needs a new `migrations/NNN_*.sql`, apply it:

```sh
uv run --env-file .env python -m scripts.ingest
```

**Manual fallback** (CI down):

```sh
fly deploy --ha=false
fly machine list   # must show exactly one machine
```

Never deploy without `--ha=false`: two machines split the in-memory rate limits.

## Secrets

Keys and tokens live in Fly secrets (prod), GitHub Actions secrets (CI) and `.env` (local). Public values (`ALLOWED_ORIGIN`, `CALENDAR_URL`, `CV_URL_*`, `DAILY_BUDGET_USD`, `PHOENIX_PROJECT_NAME`) live in `fly.toml [env]` and change through a normal commit.

Set a Fly secret without printing it (one restart):

```sh
grep '^ANTHROPIC_API_KEY=' .env | fly secrets import
```

Set a GitHub Actions secret. Unset the handoff token first, or `gh` uses it and gets 403:

```sh
unset GITHUB_TOKEN GH_TOKEN
gh secret set CI_ANTHROPIC_API_KEY
```

| Secret | Fly | CI | Rotate |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✓ | as `CI_ANTHROPIC_API_KEY` (separate key with its own spend limit) | Console → new key, update, delete old |
| `VOYAGE_API_KEY` | ✓ | ✓ | Voyage dashboard |
| `DATABASE_URL` | ✓ (pooled URL) | ✓ (read-only `ci_reader` role) | Neon → reset role password |
| `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY` | ✓ | ✓ | Phoenix space settings (keys are space-wide) |
| `GITHUB_TOKEN` | ✓ | placeholder | Fine-grained token, Issues read/write on the handoff repo only |
| `GITHUB_HANDOFF_REPO` | ✓ | placeholder | — |
| `IP_HASH_SALT` | ✓ | placeholder | Changing it breaks nothing but makes old and new IP hashes incomparable |
| `FLY_API_TOKEN` | — | ✓ | Expires 2027-10-22. `fly tokens create deploy -x 8760h \| gh secret set FLY_API_TOKEN`. An expired token shows up as a failed Deploy step |

## Limits and budget

| Limit | Where | Default |
|---|---|---|
| Messages per IP | `CHAT_LIMITS` in `app/ratelimit.py` | 10/minute, 60/day |
| Feedback / insights per IP | `FEEDBACK_LIMITS`, `INSIGHTS_LIMITS` | 30/minute |
| Turns per chat | `MAX_TURNS` in `app/db.py` | 20 |
| Message length | `MAX_MESSAGE_CHARS` in `app/main.py` | 1,000 |
| Daily model spend | `DAILY_BUDGET_USD` in `fly.toml` | $2 |
| Monthly backstop | Anthropic Console spend limit | set in Console |

When the daily budget is spent, chat replies with a fallback (book a call, get the CV) until 00:00 UTC; `/insights` counts these as `budget_exceeded`. Spend is the sum of `events.cost_usd` for the UTC day, including topic labels. Raising the cap is a `fly.toml` change and a deploy.

## Knowledge

1. Edit markdown in `knowledge/`. One `##` section = one topic = one chunk.
2. Re-ingest (only changed files are re-embedded; all in one Voyage call):
   ```sh
   uv run --env-file .env python -m scripts.ingest
   ```
3. Run the eval locally. CI does not run it for knowledge edits, because CI never re-ingests:
   ```sh
   uv run --env-file .env python -m evals.run_evals
   ```
4. Commit `knowledge/` and the refreshed `evals/REPORT.md`.

If good questions stop finding sources, tune `MIN_SIMILARITY` in `app/retrieval.py` (currently 0.25) against the eval. The free Voyage tier allows about 3 requests per minute: ingest sends one batched call, the eval retries on 429, and chat does not retry (a 429 there ends the reply with an error frame).

## Handoff triage

Escalations open an issue labelled `handoff` in the private handoff repo (`GITHUB_HANDOFF_REPO`). GitHub emails a notification.

1. Read the summary, reason and transcript in the issue.
2. Reply to the visitor at the contact email in the issue (the widget never shows replies).
3. Close the issue. Optionally add a label for the outcome.

A chat can escalate only once. A burst of junk issues means someone is opening many new chats; the per-IP daily limit bounds it. If it gets noisy, add a per-IP daily escalation cap.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Chat returns 503 | Neon unreachable or waking | `fly logs`; the pool reconnects on its own after a wake (~5 s) |
| Everyone gets the "resting for today" reply | Daily budget spent | Wait for 00:00 UTC or raise `DAILY_BUDGET_USD` |
| No traces in Phoenix | Phoenix waking (503) or key rotated | Traces from that window are dropped by design; chat is unaffected |
| Eval job fails with "secrets missing" | A CI secret was removed | Re-add it; the eval did not run, this is not a pass |
| Deploy skipped "superseded" | A newer commit reached `main` | Expected; the newer run deploys |
| Answers ignore a knowledge edit | Not re-ingested | Run ingest |

## Data

- Postgres holds conversations, messages (emails redacted), events and the knowledge chunks. IPs are stored only as `sha256(ip + salt)`.
- One-off cleanup of rows before a cutoff (prints counts unless `--yes`):
  ```sh
  uv run --env-file .env python -m scripts.purge_before 2026-10-25T00:00:00Z
  ```
