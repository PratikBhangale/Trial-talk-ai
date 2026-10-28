# AGENTS.md

Guide for coding agents working in this repository. For what the project is and how to run it, read `README.md` first.

## What this is

TrialTalk: WhatsApp (AWS End User Messaging Social) -> SNS -> `tt-ingest` -> SQS FIFO -> `tt-router` -> Bedrock AgentCore agent (`agent/trial_agent`) -> reply; staff and participant portals (`coordinator-frontend`) -> API Gateway + Cognito -> Lambdas (`coordinator-backend/lambdas`) -> Aurora PostgreSQL via the RDS Data API with row-level security (`db/`); SES for alerts and escalations; OpenTelemetry spans stored in `trial.telemetry_spans` and shown on the staff Observability dashboard.

The source of truth is what is deployed in AWS (account 180026596905, us-east-1, resources `tt-*`), not a CDK stack. The active branch is `trialtalk_ui`.

## Components and their docs

| Path | Docs |
|---|---|
| `coordinator-backend/` (staff/participant API Lambdas, local dev server, deploy scripts) | `coordinator-backend/README.md` |
| `coordinator-frontend/` (plain HTML/JS/CSS, no build step) | `coordinator-frontend/README.md` |
| `triggers/` (ingest, router, reminder, reconcile Lambdas; deploy script) | `triggers/README.md` |
| `db/` (schema, roles/RLS, seed, SQL tests; run with `db/run_sql.py`) | `db/README.md` |
| `agent/` (AgentCore project; agent code in `agent/trial_agent`) | `agent/README.md`, `agent/AGENTS.md` |
| OpenTelemetry design | `docs/superpowers/specs/2026-10-28-otel-observability-design.md` |

## Build, run and test

There is no build step for Python or the frontend. Use a venv (`.venv/` is ignored by version control). A venv with `pytest`, `psycopg[binary]`, `boto3`, `PyYAML`, `tzdata` and `opentelemetry-sdk==1.45.1` covers the suites below; the agent suite also needs `strands-agents` and `bedrock-agentcore[strands-agents]`.

```bash
# Backend. Unit tests always run; integration tests (real handlers + schema + RLS on a throwaway PostgreSQL)
# run only when TT_PG_BIN points at a PostgreSQL bin dir (initdb, pg_ctl). AWS_DEFAULT_REGION can be any value.
AWS_DEFAULT_REGION=us-east-1 python -m pytest coordinator-backend/tests
AWS_DEFAULT_REGION=us-east-1 TT_PG_BIN=/opt/homebrew/opt/postgresql@16/bin LC_ALL=en_US.UTF-8 python -m pytest coordinator-backend/tests
python -m pytest coordinator-backend/tests/<file>.py -k <test_name>     # a single test (same env vars as above)

# Triggers (boto3 is faked by triggers/tests/conftest.py; telemetry tests need opentelemetry-sdk==1.45.1)
cd triggers && python -m pytest tests/

# Agent (offline, AWS and Strands stubbed)
cd agent/trial_agent && AWS_DEFAULT_REGION=us-east-1 python -m pytest test_tracing.py -v

# Local portals with synthetic data, no AWS (http://127.0.0.1:8787/staff)
TT_PG_BIN=<postgres bin dir> LC_ALL=en_US.UTF-8 python coordinator-backend/devserver/server.py --seed-telemetry
```

Database SQL tests (`db/04_db_tests.sql`) run against a real database through `db/run_sql.py`; see `db/README.md`. Deploys (`triggers/infra/deploy.py`, `coordinator-backend/infra/deploy_portal.py`, `agentcore deploy`) touch AWS: do not run them unless asked.

## Rules

- **No PHI or identifiers in telemetry.** Spans and metrics carry route templates (not URLs), SQL verbs (not statements), counts, durations, token counts and model ids. Never put message text, phone numbers, names or participant ids in span names or attributes. Synthetic spans carry `"trialtalk.synthetic": true`.
- **Keep the two `telemetry.py` copies byte-identical.** `coordinator-backend/lambdas/common/telemetry.py` is canonical; `triggers/lambdas/common/telemetry.py` is the copy. Edit the backend one, copy it over, and run `coordinator-backend/tests/test_telemetry_sink.py`, whose `test_telemetry_copies_identical` enforces the sync. The agent has its own `agent/trial_agent/tracing.py`, which is separate.
- Telemetry must never fail or delay a request or change a reply: every exporter and persist path swallows its exceptions.
- Database access goes through the Data API as the least-privileged login for the service (`tt_agent`, `tt_messaging`, `tt_portal`, `tt_participant`) and sets session context; read `db/README.md` before writing queries. Schema changes go in `db/`, which is the source of truth.
- Layer 0 safety checks (opt-out, emergency keywords, dose buttons) happen in code in `tt-router` before any model call; do not move them behind the agent.
- Use synthetic data only. Never commit credentials, the build plan PDF, or `.staff_token`.
