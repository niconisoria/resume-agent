# SCIM Bridge (personal project)

## What it is

SCIM Bridge is a Python/FastAPI service Nicolas built that translates Okta SCIM 2.0 provisioning requests into Brivo Access API calls, managing the full user and group lifecycle under rate limits and partial failures. Source code: github.com/niconisoria/scim-bridge.

## Highlights

- Saga-based orchestrator with automatic rollback for multi-step Brivo operations, keeping data consistent on failure.
- Redis-backed ID mapping and idempotency locks to prevent duplicate provisioning from concurrent retries.
- Rate limiting, retry logic and structured logging with correlation IDs for reliability and traceability.
- A mock Brivo API and full test suite (pytest, fakeredis, respx) for local development and CI without live dependencies.

## Stack

Python 3.14, FastAPI, Redis, Docker, pytest.
