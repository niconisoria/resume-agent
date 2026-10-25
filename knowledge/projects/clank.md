# Clank (personal project)

## What it is

Clank is a spec-driven development pipeline for Claude Code that Nicolas built. It chains four Claude Code skills end to end: define, implement, review and validate. Each stage is gated on a hard artifact (a written spec, passing tests, a clean audit) before the pipeline advances. Source code: github.com/niconisoria/clank.

## Evaluation pipeline

- Model-graded evaluation: for each task, Clank generates a solution under the target skill's own definition, then grades it against per-task solution criteria with a second, independent Claude call acting as judge.
- A numeric pass threshold, with per-skill and full-suite eval runs reporting a pass/fail summary plus token-cost estimates per run.

## Stack

Claude Code Skills, Python, Anthropic API.
