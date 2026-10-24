# Evals

Golden-set quality check for the agent: retrieval hit, tool routing, groundedness and decline correctness, judged against thresholds.

```
uv run --env-file .env python -m evals.run_evals
```

- Cases: `golden.jsonl` (edit freely; a new file content creates a new Phoenix dataset).
- Output: `REPORT.md`, a Phoenix experiment, exit code 1 below threshold.
- Runs in CI on changes to `app/`, `evals/` or dependencies, and gates the Fly deploy.
- Knowledge edits are not covered by CI: run `python -m scripts.ingest`, then the eval, locally.
