# TrialTalk OpenTelemetry Observability — Design

**Date:** 2026-10-28 · **Branch:** `otel` (based on `trialtalk_ui` + agent sync from `whatsapp-agent@4674ac90`)

## Intent

Give the team (and hackathon judges) a live, end-to-end view of the system: every
WhatsApp message and web-chat request becomes one trace flowing SNS → ingest → SQS →
router → agent (model + tools) → reply, with token usage, latency, request counts,
timestamps, estimated cost, and behavior drift shown on a rebuilt Observability page
in the staff portal.

Recon facts this design builds on (verified 2026-10-28 against the live account):
- `trial.communications` already has `input_tokens`, `output_tokens`, `latency_ms`
  columns in the **live** Aurora DB, but **0 rows have ever had tokens** — the agent
  returns only `{"reply"}` and the router never writes them.
- Deployed Lambdas have **no OTel SDK** (no layers), so the existing
  `coordinator-backend/lambdas/common/telemetry.py` silently no-ops in AWS.
- Live AgentCore runtime `ContinuumAI_trial_agent-UL2ODbF7Ie` matches the synced
  `agent/` code.

## Goals

1. One W3C trace per message across all services, stored durably.
2. Real token usage flowing into the existing columns and Token-usage page.
3. A rebuilt staff Observability page: KPIs, time series, trace waterfall,
   recent-traces table, drift panel, estimated cost (in — labeled "estimate").
4. Full local demo on the dev server (fakes + throwaway Postgres), including a
   synthetic 7-day backfill so baselines and charts render immediately.
5. Deploy scripts ready for AWS; actual AWS deployment gated on explicit user go.

## Non-goals

- Fixing pre-existing bugs (missing `emergency_alerts` table, button-payload
  mismatch, ingest ordering). The dashboard may make them visible; it won't fix them.
- CloudWatch/X-Ray/OTLP backends. OTLP export stays available behind
  `OTEL_EXPORTER_OTLP_ENDPOINT` but nothing depends on it.
- Repo clean-up (339 MB `ContinuumAI/` folder on `whatsapp-agent`) — separate work.

## Anti-goals (failure even if it "works")

- Telemetry that can break the product: any exporter/DB failure must never fail or
  delay a participant-facing message.
- PHI in telemetry: message text, phone numbers, names, or participant ids in span
  attributes. (Existing codebase rule; this design keeps it.)
- A dashboard that lies: synthetic demo data must carry a `trialtalk.synthetic`
  attribute; estimated cost must be labeled an estimate.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Span storage | New `trial.telemetry_spans` table, written by a batch exporter over the RDS Data API | Durable + fleet-wide (ring buffer dies with the Lambda); every dashboard number becomes plain SQL; works identically on the dev server's fakes |
| Agent telemetry | Agent collects its own spans in-memory and **returns** `{reply, usage, trace}`; the *caller* (router / participant_api / legacy handler) persists them | One DB writer per path, no new agent IAM permissions, works with dev fakes, old callers ignore the extra keys |
| Agent span source | Manual spans (`tracing.py`) + `AgentResult.metrics` for tokens — not Strands' own OTel plumbing | Deterministic; no dependency on Strands internals beyond the documented result object |
| Trace propagation | W3C `traceparent` string: SNS→ingest (new root), ingest→SQS message body key `traceparent`, router→agent payload key `traceparent` | Standard, survives JSON round-trips |
| Metrics | Derived at query time from spans + communications; no second metrics pipeline | Fewer moving parts; hackathon scale |
| Drift | "Behavior drift": last 24 h vs the prior 7 days on six measures (tokens in/out per reply, agent p95 latency, reply length, KB-search rate, escalation rate) + config-change markers from `trialtalk.config_hash` span attribute changes | Honest, computable, demo-friendly |
| Cost | `(input_tokens × $3 + output_tokens × $15) / 1M`, labeled "est." | Sonnet 4.x public pricing; single constant, easy to change |
| Charts | Hand-rolled SVG module `coordinator-frontend/charts.js` | Repo ethos is no-framework/no-build; a vendored lib is overkill for 3 chart types |
| Shared module | Canonical `coordinator-backend/lambdas/common/telemetry.py`, byte-identical copy at `triggers/lambdas/common/telemetry.py`, guarded by a sync test | Repo already duplicates `db.py` across bundles; the test stops divergence |
| Lambda packaging | Pinned `opentelemetry-api`/`-sdk` pip-installed into the deploy zips | No layers exist today; zips are how this repo ships |

## Canonical span names & attributes

Roots: `whatsapp.ingest`, `whatsapp.route`, `whatsapp.legacy`, `reminder.send`,
`reconcile.run`, `agent.invoke` (agent side), plus existing `"<METHOD> <route>"` API
spans. Router's client span around the runtime call: `agent.call`.
Tool children: `tool.get_participant_profile`, `tool.search_trial_docs`,
`tool.escalate_to_coordinator`.
Attributes: `gen_ai.usage.input_tokens/output_tokens`, `gen_ai.request.model`,
`trialtalk.config_hash`, `trialtalk.kb.results`, `trialtalk.kb.top_score`,
`trialtalk.escalation.severity`, `trialtalk.msg_type`, `trialtalk.synthetic`,
standard `http.*` / `messaging.*`. Queue wait is **not** a span: the dashboard draws
the gap between `whatsapp.ingest` end and `whatsapp.route` start.

## Observability page layout (staff portal, new design system)

```
KPI row:    Requests 24h · p95 latency · Error rate · Tokens today · Est. cost today
Charts:     Requests & errors (15-min buckets, 24h)  |  Tokens in/out per day (14d,
            config-change markers as vertical rules with model/hash tooltip)
Pipeline:   Recent traces table (time, entry service, duration, tokens, status)
            → click opens a waterfall: ingest ▓ queue(gap) ░ router ▓ agent ▓▓ send ▓
Drift:      six cards, each "current vs baseline" with ▲/▼ % badge; direction-aware
            coloring (latency up = bad; neutral for token mix)
Empty DB →  friendly empty states, never errors. Poll every 5 s while visible.
```

## Left to the builder

Exact OTel pin (latest stable 1.x at implementation), SQL formulations, chart
internals, CSS details within the existing token system, test fixture shapes, and
the Strands result-object access pattern (verify against the pinned version;
fall back to zeros + a logged warning if the shape differs).
