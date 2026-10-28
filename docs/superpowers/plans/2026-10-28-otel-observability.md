# TrialTalk OpenTelemetry Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End-to-end OpenTelemetry across agent, triggers and backend, persisted to Postgres, surfaced on a rebuilt staff Observability dashboard with tokens, latency, traces, cost and drift.

**Architecture:** One W3C trace per message (SNS→ingest→SQS→router→agent→send); spans batch-written to a new `trial.telemetry_spans` table via the RDS Data API; the agent returns `{reply, usage, trace}` and callers persist; the dashboard is plain SQL over spans + communications, rendered by a hand-rolled SVG chart module.

**Tech Stack:** Python 3.12, opentelemetry-sdk (pinned 1.x), Strands agent, Aurora Data API, vanilla JS/SVG frontend (no build step).

**Spec:** `docs/superpowers/specs/2026-10-28-otel-observability-design.md` (read it first; canonical span names, attributes, layout and decisions live there).

## Global Constraints

- Telemetry must never fail or delay a request: every exporter/persist path swallows exceptions (print once, continue).
- No PHI in span attributes: no message text, phone numbers, names, participant ids. Exporter enforces an attribute allowlist (prefixes `http.`, `db.`, `messaging.`, `gen_ai.`, `trialtalk.`, `faas.`, `rpc.`) and drops string values > 200 chars.
- Frontend: vanilla HTML/CSS/JS, no framework, no build step; colors only via existing `common.css` tokens; works in light/dark/system themes and at 390 px width.
- Schema: additive only; follow `db/` style (plain `CREATE`, matching `DROP` in `99_teardown.sql`).
- `coordinator-backend/lambdas/common/telemetry.py` and `triggers/lambdas/common/telemetry.py` stay byte-identical (Task 2 adds the test that enforces it).
- Backend tests run with `/opt/anaconda3/bin/python -m pytest -q` from `coordinator-backend/` (39 pass / 36 skip baseline); integration tests stay skippable without `TT_PG_BIN`.
- Do not touch `ContinuumAI/` history, unrelated bugs, or other branches. No AWS writes in any task except the explicitly gated Task 9 checklist.
- Commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Review Focus

1. DB sink down (rds-data raises) while a participant message is processing → reply still sent, spans lost silently. Pinned: Task 2 step 2 (exporter) and Task 4 step 6 (router).
2. Hostile/accidental PHI attribute (`body_text`, `phone`) on a span → dropped by allowlist before insert. Pinned: Task 2 step 2.
3. Old agent deployed (response lacks `usage`/`trace`) → router writes NULL tokens, no crash, trace still recorded. Pinned: Task 4 step 6.
4. Empty telemetry tables → `/staff/observability` returns zeros/empty arrays (HTTP 200), UI shows empty states. Pinned: Task 5 step 2, Task 6 step 5.
5. Missing or corrupt `traceparent` in an SQS message or agent payload → new trace starts, processing continues. Pinned: Task 2 step 2 (helper) and Task 4 step 6.

## Execution Notes (orchestrator)

- **Wave 1 (parallel):** Task 1 (sonnet) · Task 2 (sonnet) · Task 3 (opus)
- **Wave 2 (parallel):** Task 4 (opus, needs T2) · Task 5 (sonnet, needs T1)
- **Wave 3 (parallel):** Task 6 (opus, needs T5) · Task 7 (sonnet, needs T3+T5) · Task 8 (sonnet, needs T2)
- **Task 9:** orchestrator (Fable) — final verification, push, gated AWS checklist.
- File ownership is disjoint per wave; no two concurrent tasks modify the same file.
- Implementers of Task 6 must load the `dataviz` skill; Task 6 verification uses the `browser-automation` skill.

---

### Task 1: Telemetry schema (`db/05_telemetry.sql`)

**Files:**
- Create: `db/05_telemetry.sql`
- Modify: `db/99_teardown.sql`, `db/README.md` (one paragraph: what the table is, the no-PHI rule), `coordinator-backend/tests/local_env.py` (add `05_telemetry.sql` to the SQL bootstrap list — find the existing list that applies `01`/`02`/pending)
- Test: `coordinator-backend/tests/test_telemetry_store.py`

**Interfaces:**
- Produces: table `trial.telemetry_spans(span_id text PRIMARY KEY, trace_id text NOT NULL, parent_id text, name text NOT NULL, service text NOT NULL, kind text NOT NULL DEFAULT 'INTERNAL', start_ts timestamptz NOT NULL, duration_ms numeric NOT NULL, status text NOT NULL DEFAULT 'UNSET', attrs jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now())`
- Indexes: `(trace_id)`, `(start_ts DESC)`, `(name, start_ts DESC)`
- Grants: `INSERT` to `messaging_role, portal_role`; `SELECT` to `portal_role`. No RLS (table carries no PHI by design — header comment states the rule and points at the exporter allowlist).

- [ ] **Step 1: Write the failing integration test** — `test_portal_can_insert_and_select_span()` (insert a row as the portal login with `attrs='{"http.route":"/x"}'`, select it back, assert all columns round-trip) and `test_messaging_can_insert_but_not_select()` (insert OK; `SELECT` raises). Mark both with the repo's existing integration-skip pattern from `tests/conftest.py`.
- [ ] **Step 2: Run to verify both fail** — `pytest tests/test_telemetry_store.py -v` with `TT_PG_BIN` set. Expected: FAIL (`relation "trial.telemetry_spans" does not exist`). Without `TT_PG_BIN`: both SKIP (also verify this).
- [ ] **Step 3: Write `db/05_telemetry.sql`** per Interfaces, add the `DROP TABLE IF EXISTS trial.telemetry_spans;` to `99_teardown.sql`, add the file to `local_env.py`'s bootstrap list, add the README paragraph.
- [ ] **Step 4: Run to verify pass** — same command, expected: 2 PASS. Then full suite: `pytest -q` → no regressions.
- [ ] **Step 5: Commit** — `feat(db): add trial.telemetry_spans table for OTel span storage`

### Task 2: Telemetry core — DB exporter, allowlist, propagation helpers

**Files:**
- Modify: `coordinator-backend/lambdas/common/telemetry.py`
- Create: `triggers/lambdas/common/telemetry.py` (byte-identical copy, header comment `# Copy of coordinator-backend/lambdas/common/telemetry.py — keep in sync (test_telemetry_sink.py enforces)`... the comment must exist in BOTH files so they stay identical)
- Test: `coordinator-backend/tests/test_telemetry_sink.py`

**Interfaces:**
- Consumes: Task 1 table shape (writes via `boto3.client("rds-data").batch_execute_statement`, NOT via the `db` module — the two bundles' db modules differ; env `DB_CLUSTER_ARN`, `DB_SECRET_ARN`, `DB_NAME` as the handlers already use).
- Produces (new/changed public API, everything else unchanged):
  - `init()` additionally attaches `BatchSpanProcessor(_DbExporter())` when `DB_CLUSTER_ARN` and `DB_SECRET_ARN` are set and `TELEMETRY_DB_SINK != "0"`.
  - `flush() -> None` — force-flush all processors; call at the end of every Lambda handler; never raises.
  - `persist_spans(spans: list[dict], service: str, trace_id: str) -> None` — insert pre-built span dicts through the same filtered path; never raises. Dict keys (Task 3 produces exactly this shape): `span_id, parent_id, name, start (ISO-8601), duration_ms, status ("OK"|"ERROR"|"UNSET"), kind, attributes`.
  - `traceparent() -> str | None` — W3C header for the current span.
  - `context_from(tp: str | None)` — OTel context from a traceparent string; invalid/None → None (new trace).
  - `ALLOWED_ATTR_PREFIXES` as in Global Constraints; `_filter_attrs(attrs: dict) -> dict` applies prefixes + 200-char cap.
  - SQL: single `INSERT INTO trial.telemetry_spans (...) VALUES (...) ON CONFLICT (span_id) DO NOTHING` with parameterSets (attrs as jsonb via `typeHint`).

- [ ] **Step 1: Write the failing tests** in `test_telemetry_sink.py` using a monkeypatched fake rds-data client (follow `tests/conftest.py` fake-boto3 pattern): `test_spans_flushed_to_db` (make a span, `flush()`, assert one batch call whose parameterSet carries name/trace_id/duration and jsonb attrs), `test_allowlist_drops_phi_and_long_values` (attrs `body_text`, `phone`, `gen_ai.usage.input_tokens`, 300-char `http.route` → only the gen_ai key and a truncated-away route survive per the rule), `test_db_failure_never_raises` (client raises; `span()`+`flush()` completes), `test_persist_spans_inserts_agent_format` (agent-format dict list lands as rows with `service='trial-agent'`), `test_traceparent_roundtrip` (span → `traceparent()` parses; `context_from("garbage")` is None), `test_telemetry_copies_identical` (read both files, assert equal bytes).
- [ ] **Step 2: Run to verify fail** — `pytest tests/test_telemetry_sink.py -v`. Expected: FAIL on missing attributes (`flush`, `persist_spans`, …) and the missing copy file.
- [ ] **Step 3: Implement** in the canonical file per Interfaces, then `cp` to the triggers path.
- [ ] **Step 4: Run to verify pass** — file tests PASS, then full suite `pytest -q` → no regressions (existing telemetry tests must still pass: `request_span`, `snapshot`, ring behavior unchanged).
- [ ] **Step 5: Commit** — `feat(telemetry): durable DB span sink, attribute allowlist, W3C propagation helpers`

### Task 3: Agent — tracing module, usage capture, instrumented tools

**Files:**
- Create: `agent/trial_agent/tracing.py`, `agent/trial_agent/test_tracing.py`
- Modify: `agent/trial_agent/core.py`, `agent/trial_agent/app.py`, `agent/trial_agent/tools.py`, `agent/trial_agent/pyproject.toml` (add `opentelemetry-sdk>=1.30,<2` — pin the exact latest 1.x at implementation time)

**Interfaces:**
- Consumes: nothing from other tasks (standalone bundle).
- Produces — the response contract Tasks 4 and 7 rely on:
  - `app.invoke` payload gains optional `"traceparent": str`; response becomes `{"reply": str, "usage": {"input_tokens": int, "output_tokens": int, "latency_ms": int, "model": str}, "trace": {"trace_id": str, "spans": [SpanDict, ...]}}`; the error path returns `{"error"| "reply", "trace": {...}}` with whatever spans were collected.
  - `SpanDict = {"span_id": str, "parent_id": str | None, "name": str, "start": str  # ISO-8601 ms, "duration_ms": float, "status": "OK"|"ERROR"|"UNSET", "kind": str, "attributes": dict}` — exactly what `telemetry.persist_spans` (Task 2) accepts.
  - `tracing.py`: `collect(traceparent: str | None)` context manager yielding a collector; `tracing.span(name, **attrs)` context manager usable anywhere in the bundle (module-global tracer, parented by current context); `collector.result() -> {"trace_id", "spans"}`.
  - `core.handle_message(pid, text, conversation_key=None, traceparent=None) -> tuple[str, dict, dict]` (reply, usage, trace). Root span `agent.invoke` (kind SERVER) carries `gen_ai.usage.input_tokens/output_tokens`, `gen_ai.request.model`, `trialtalk.config_hash` (first 12 hex of sha256 of `root_agent.yaml` bytes, computed once at import).
  - `tools.py`: each of the three tools wraps its body in `tracing.span("tool.<name>", ...)` with the spec's attributes (`trialtalk.kb.results`, `trialtalk.kb.top_score` for KB; `trialtalk.escalation.severity`).
  - Usage source: `result.metrics.accumulated_usage` (`inputTokens`/`outputTokens` keys) — verify against the pinned strands version; if absent, zeros + one printed warning (spec: "Left to the builder").

- [ ] **Step 1: Write the failing tests** in `test_tracing.py` (pure Python, no AWS; stub the `Agent` class in `core` via monkeypatch returning an object with `.metrics.accumulated_usage = {"inputTokens": 120, "outputTokens": 45}` and `str() == "hi"`): `test_collect_produces_parented_spans` (nested `span()` calls → parent ids chain to one trace), `test_traceparent_continues_remote_trace` (`collect("00-"+ "ab"*16 + "-"+"cd"*8+"-01")` → trace_id == "ab"*16), `test_handle_message_returns_reply_usage_trace` (reply "hi", usage ints, trace has an `agent.invoke` span with the three gen_ai/config attrs), `test_invoke_error_still_returns_trace` (stub raises → response has `trace` and the span status is ERROR).
- [ ] **Step 2: Run to verify fail** — from `agent/trial_agent/`: `python -m pytest test_tracing.py -v` (venv with pyproject deps). Expected: FAIL (`No module named 'tracing'`).
- [ ] **Step 3: Implement** `tracing.py` (TracerProvider + in-memory ring exporter producing SpanDicts, ~100 lines), thread `traceparent`/usage/trace through `core.py` and `app.py`, wrap the three tools, add the dep.
- [ ] **Step 4: Run to verify pass** — same command: all PASS. Also `python -c "import app"` succeeds (AgentCore entry intact).
- [ ] **Step 5: Commit** — `feat(agent): per-message tracing, token usage and trace returned in the response`

### Task 4: Triggers pipeline instrumentation

**Files:**
- Modify: `triggers/lambdas/ingest/handler.py`, `triggers/lambdas/router/handler.py`, `triggers/lambdas/reminder/handler.py`, `triggers/lambdas/reconcile/handler.py`, `triggers/whatsapp/handler.py`
- Create: `triggers/tests/conftest.py`, `triggers/tests/test_tracing.py` (first tests in this bundle; lightweight fakes — monkeypatch `boto3.client` before importing handlers, mirroring the backend conftest's approach; set the env vars the handlers read at import)

**Interfaces:**
- Consumes: Task 2 API (`span`, `flush`, `persist_spans`, `traceparent`, `context_from`) from `triggers/lambdas/common/telemetry.py`; Task 3 response contract (fake it in tests — do not import agent code).
- Produces: SQS message bodies gain key `"traceparent"`; agent payload gains `"traceparent"`; `router.reply(m, pid, lang, text, **usage)` additionally writes `input_tokens=usage.get("input_tokens")`, `output_tokens=usage.get("output_tokens")` into the existing communications INSERT (extend the column list at the current `INSERT` in `reply()`); `ask_agent` returns `(reply, usage_dict)` where `usage_dict` now includes token keys when present.

Per handler (root spans per the spec's canonical names, `flush()` as the handler's final statement in a `finally`):
- **ingest** `whatsapp.ingest` (CONSUMER) per record: attrs `trialtalk.msg_type`, `trialtalk.duplicate` (bool); inject `telemetry.traceparent()` into the SQS `MessageBody` JSON.
- **router** `whatsapp.route` (CONSUMER) continuing `context_from(body.get("traceparent"))`; child `agent.call` (CLIENT) around `invoke_agent_runtime` carrying the gen_ai token attrs from the response; after a response with `"trace"`, call `persist_spans(trace["spans"], service="trial-agent", trace_id=trace["trace_id"])`.
- **reminder** `reminder.send` root: attrs `trialtalk.msg_type` (template/interactive), reminder type; **reconcile** `reconcile.run` root with created/deleted counts; **legacy whatsapp handler** `whatsapp.legacy` root per record + the same `agent.call`/persist/usage-attrs pattern as the router (it does not write communications — spans only).

- [ ] **Step 1: Write the failing tests**: `test_ingest_injects_traceparent` (fake SNS event → the SQS send body contains a valid `traceparent` matching the ingest span's trace), `test_router_continues_trace_and_writes_tokens` (SQS event with a known traceparent; fake agent returns the Task 3 contract with 120/45 tokens and 2 spans → communications INSERT params include both token values; `telemetry_spans` batch insert includes the agent spans with the same trace_id as the incoming traceparent), `test_router_handles_old_agent_response` (`{"reply": "ok"}` only → tokens NULL, no exception), `test_router_survives_telemetry_outage` (rds-data insert for spans raises → reply still sent), `test_corrupt_traceparent_starts_new_trace` (body traceparent "garbage" → handler completes; root span has a fresh 32-hex trace_id).
- [ ] **Step 2: Run to verify fail** — from `triggers/`: `python -m pytest tests/ -v`. Expected: FAIL/ERROR (no instrumentation, conftest wiring new).
- [ ] **Step 3: Implement** per Interfaces (smallest diffs that pass; keep each handler's existing behavior byte-for-byte when telemetry is a no-op).
- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/ -v` all PASS; backend suite still green (`cd ../coordinator-backend && pytest -q`).
- [ ] **Step 5: Commit** — `feat(triggers): end-to-end trace propagation and token recording through the WhatsApp pipeline`

### Task 5: Observability API

**Files:**
- Create: `coordinator-backend/lambdas/staff_api/observability.py`
- Modify: `coordinator-backend/lambdas/staff_api/handler.py` (route the three endpoints to the new module; add `est_cost_usd` to `/staff/usage` totals and `by_day` rows), `coordinator-backend/lambdas/common/http.py` **only if** `request_span` is not already wired into the Router dispatch (verify first; wire it if missing so API spans also persist)
- Test: `coordinator-backend/tests/test_observability.py`

**Interfaces:**
- Consumes: Task 1 table.
- Produces (all `require(staff, "ops:read")`, all shapes exact — Task 6 builds against them):
  - `GET /staff/observability?hours=24` → `{"summary": {"requests": int, "errors": int, "error_rate": float, "p50_ms": float|null, "p95_ms": float|null, "tokens_in": int, "tokens_out": int, "est_cost_usd": float, "traces": int}, "series": {"requests": [{"t": iso, "count": int, "errors": int}]  # 15-min buckets over `hours`, "tokens": [{"day": "YYYY-MM-DD", "input_tokens": int, "output_tokens": int, "est_cost_usd": float}]  # 14 days, "latency": [{"t": iso, "p50": float|null, "p95": float|null}]  # hourly over `hours`}, "drift": {"metrics": [{"key": str, "label": str, "current": float|null, "baseline": float|null, "change_pct": float|null, "bad_direction": "up"|"down"|"none"}], "config_changes": [{"at": iso, "model": str|null, "config_hash": str}]}}`
  - `GET /staff/observability/traces?limit=50` → `{"traces": [{"trace_id", "started_at", "root_name", "service", "duration_ms", "span_count": int, "tokens": int|null, "status"}]}` (root = `parent_id IS NULL`; tokens from the trace's `gen_ai.usage.*` attrs).
  - `GET /staff/observability/traces/{trace_id}` → `{"spans": [full rows, start_ts ascending]}`.
  - `PRICING = {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0}` and pure functions `est_cost(tokens_in, tokens_out) -> float` (round 4) and `drift_metrics(current: dict, baseline: dict) -> list[dict]` (change_pct None when baseline is 0/None; six keys per the spec).
  - Drift windows: current = last 24 h; baseline = 24 h–8 d ago. Rates = fraction of traces containing the tool span over traces containing `agent.invoke`. Config changes via `lag(attrs->>'trialtalk.config_hash')` over `agent.invoke` spans.

- [ ] **Step 1: Write the failing unit tests** (pure, no DB): `test_est_cost_math` (1M in + 1M out = 18.0; zeros = 0.0), `test_drift_change_pct_and_zero_baseline`, `test_drift_bad_direction_labels`.
- [ ] **Step 2: Write the failing integration tests** (skip without `TT_PG_BIN`): seed spans (two traces: one healthy with agent.invoke+tool spans and token attrs, one ERROR) + two token-bearing communications rows → `test_observability_shapes_and_math` (summary counts, p95 ≥ p50, cost matches seeded tokens), `test_traces_list_and_waterfall_roundtrip`, `test_empty_db_returns_zeros_not_500`, `test_requires_ops_read` (participant-role staff → 403).
- [ ] **Step 3: Run to verify fail** — `pytest tests/test_observability.py -v`: unit FAIL on import; integration FAIL/SKIP as applicable.
- [ ] **Step 4: Implement** `observability.py` + routing + usage-endpoint cost per Interfaces; check `http.py` request_span wiring.
- [ ] **Step 5: Run to verify pass** — file PASS, full suite green.
- [ ] **Step 6: Commit** — `feat(api): observability endpoints — summary, series, traces, drift, est. cost`

### Task 6: Frontend dashboard

**Files:**
- Create: `coordinator-frontend/charts.js`
- Modify: `coordinator-frontend/staff.html` (Observability page markup + script), `coordinator-frontend/widgets.js` (replace the old observability widget; add the cost tile/column on Token usage), `coordinator-frontend/common.css` (chart/waterfall/drift styles from existing tokens only), `coordinator-backend/devserver/server.py` (serve `/ui/charts.js` — one line in `PAGES`), `coordinator-frontend/README.md`
- Test: manual via dev server + `browser-automation` skill (no JS test harness exists)

**Interfaces:**
- Consumes: Task 5 response shapes verbatim; existing `api()`, `esc()`, `icon()`, `chip()`, `table()` helpers; existing CSS custom properties.
- Produces: `charts.js` exposing `window.ttCharts = {line(el, {series, yLabel, markers}), bars(el, {series, stacked, yLabel}), waterfall(el, {spans, traceStart})}` — SVG, theme-token colors, aria-labels, graceful at 390 px.

- [ ] **Step 1: Load the `dataviz` skill**, then build the page per the spec layout: KPI row (existing stat-tile pattern), two charts, recent-traces table (click → waterfall panel), drift cards with ▲/▼ badges and `bad_direction` coloring, config-change markers as vertical rules with tooltips, "estimate" label on all cost figures, empty states for every section, 5-s polling only while the tab is visible and the page is active.
- [ ] **Step 2: Start the dev server** (`TT_PG_BIN=... python coordinator-backend/devserver/server.py`), seed via Task 7's `--seed-telemetry` if already merged (else drive a few `/me/chat` calls), and verify with the browser-automation skill: load `/staff#observability` — zero console errors.
- [ ] **Step 3: Screenshot light + dark + 390 px** and visually check: axis legibility, waterfall stage labels, drift badges, empty-state rendering on a fresh reset. Fix what looks wrong; re-screenshot.
- [ ] **Step 4: Verify the Token-usage page** shows the est.-cost tile and per-day cost column against seeded data.
- [ ] **Step 5: Commit** — `feat(ui): observability dashboard — KPIs, charts, trace waterfall, drift panel`

### Task 7: Web-chat usage + dev-server demo data

**Files:**
- Modify: `coordinator-backend/lambdas/participant_api/handler.py` (chat path: pass `traceparent`, write returned tokens into its communications INSERT, `persist_spans` of the agent trace), `coordinator-backend/tests/local_env.py` (FakeAgentCore returns the Task 3 contract: randomized plausible usage 300–900/60–250 tokens, 2–4 spans including a tool span), `coordinator-backend/devserver/server.py` (`--seed-telemetry`: ~7 days of synthetic traces + token-bearing communications rows, every span attr set includes `"trialtalk.synthetic": true`; re-seed after `/dev/reset` when the flag is on), `coordinator-backend/README.md` (one paragraph)
- Test: `coordinator-backend/tests/test_chat_usage.py`; update any existing chat tests broken by the FakeAgentCore shape change

**Interfaces:**
- Consumes: Task 3 response contract; Task 2 `persist_spans`; Task 5 endpoint (seeded data must render there).
- Produces: `seed_telemetry(days: int = 7) -> None` in `devserver/server.py`.

- [ ] **Step 1: Write the failing test** `test_chat_records_usage_and_spans` (POST `/me/chat` with FakeAgentCore → the outbound communications row has both token values; a `trial-agent` span row exists with the same trace_id the fake returned).
- [ ] **Step 2: Run to verify fail**, implement, **Step 3: run to verify pass** + full suite green (fix any chat tests asserting the old fake shape).
- [ ] **Step 4: Boot the dev server with `--seed-telemetry`** and assert via `curl` that `/api/staff/observability` summary shows non-zero requests and tokens.
- [ ] **Step 5: Commit** — `feat(dev): web-chat token capture and synthetic 7-day telemetry seed`

### Task 8: Deploy packaging (no AWS writes)

**Files:**
- Create: `infra-requirements-otel.txt` at repo root (exact pins: `opentelemetry-api==X`, `opentelemetry-sdk==X` — resolve latest 1.x once, use everywhere)
- Modify: `triggers/infra/deploy.py`, `coordinator-backend/infra/deploy_portal.py` (zip build: `pip install -r ../infra-requirements-otel.txt -t <staging>` into each Lambda zip alongside the sources; factor the zip build into a `build_zip(...) -> Path` function if it isn't one; add env `TELEMETRY_DB_SINK=1` to the function configs), both READMEs' deploy sections
- Test: `coordinator-backend/tests/test_deploy_zip.py` — call the factored `build_zip` for one Lambda into a tmp dir; assert the zip contains `opentelemetry/`, `common/telemetry.py`, and the handler; no AWS client is constructed (import-time safety: monkeypatch boto3 to raise).

- [ ] **Step 1: Write the failing test**, **Step 2: verify fail**, **Step 3: implement**, **Step 4: verify pass** + both deploy scripts still import clean (`python -c "import ..."` with stubbed boto3), full suite green.
- [ ] **Step 5: Commit** — `feat(deploy): bundle pinned OTel SDK into Lambda zips, enable DB span sink`

### Task 9: Final verification, push, gated AWS rollout (orchestrator)

- [ ] **Step 1:** Full test matrix: backend (`pytest -q`), triggers (`pytest tests/ -q` from `triggers/`), agent (`pytest test_tracing.py -q` from `agent/trial_agent/`). All green, record counts.
- [ ] **Step 2:** `superpowers:requesting-code-review` on the whole branch diff vs `trialtalk_ui`; fix findings worth fixing.
- [ ] **Step 3:** Fresh dev-server run with `--seed-telemetry`; browser-automation pass over Overview→Observability→Token usage; both themes; attach screenshots to the summary.
- [ ] **Step 4:** Push: `git push -u origin otel`. (User already authorized the branch.)
- [ ] **Step 5 (GATED — requires the user's explicit go, AWS writes):** rollout checklist, in order: `python db/run_sql.py --cluster-arn ... --secret-arn <master> 05` → redeploy triggers zips (`python triggers/infra/deploy.py`) → redeploy staff-api (`python coordinator-backend/infra/deploy_portal.py`) → `agentcore deploy` from `agent/agentcore/` → verify read-only: send one test WhatsApp message, then `SELECT count(*) FROM trial.telemetry_spans` and the dashboard against the live API.
