# Coordinator frontend

The TrialTalk portals: plain HTML, CSS and JS, no framework and no build step.

| File | What it is |
| --- | --- |
| `staff.html` | Staff portal (coordinators and PIs): overview, emergency alerts, participants, requests, team, token usage, observability |
| `participant.html` | Participant site: report an adverse event, medication, chat, requests, details, history |
| `index.html` | Local landing page that links the two portals |
| `common.css`, `common.js` | Shared design tokens, layout, components, the API helper and the dev sign-in |
| `widgets.js` | Staff dashboard widgets: alerts, token usage, observability, team |
| `charts.js` | `window.ttCharts`: hand-rolled SVG line and column charts and the trace waterfall used by the Observability page (no libraries) |
| `orb.js`, `vendor/thinking-orbs/` | The chat "thinking" indicator (MIT) |
| `vendor/phosphor/` | Licence for the Phosphor icons, whose paths are inlined in `common.js` (MIT) |

## Run it

`coordinator-backend/devserver/server.py` serves this folder over the real Lambda handlers and a throwaway Postgres; see [the backend README](../coordinator-backend/README.md#test-portals-local-no-aws). `coordinator-backend/infra/portal_ui.py` serves `staff.html` against the deployed API instead.

## Where it meets the backend

Every request goes through one function, `api(method, path, body)` in `common.js`, which calls `fetch('/api' + path)` and returns the JSON (or shows the error and throws). Nothing else in the pages builds URLs or headers, except the quick-reply "send on page leave" in `staff.html` (search for `keepalive`).

**Auth is the one seam to change.** In dev, `api()` sends the chosen user as `X-Dev-Identity: staff:<email>` or `participant:<id>`, and the dev server turns that into Cognito-shaped claims. For a hosted build, replace that header in `api()` (and in the `keepalive` call) with `Authorization: Bearer <Cognito ID token>`, and point `'/api'` at the API Gateway URL if the pages are not served behind the same origin.

**Dev-only parts to remove or hide** in a hosted build:
- The account menu's sign-in picker and the **Demo / Worst case** data switch (`renderAccountMenu`, `signInAs`, `seedWorstCase`, `switchDataset` in `common.js`). They call `/dev/identities` and `/dev/reset`, which exist only on the dev server.
- The footer text "local development build".

## Endpoints the pages call

Participant (`/api/me...`): `GET /me`, `PATCH /me`, `GET /me/reminders`, `GET /me/history`, `GET|POST /me/requests`, `GET|POST /me/chat`, `GET|POST /me/emergency`.

Staff (`/api/staff/...`): `GET /staff/me`; `GET|POST /staff/participants`, `GET|PATCH|DELETE /staff/participants/{pid}` and its `/events`, `/adherence`, `/communications`, `POST .../reminders`; `PATCH|DELETE /staff/reminders/{rid}`, `POST .../send-now`; `GET /staff/requests`, `POST /staff/requests/{cid}/reply`; `GET /staff/alerts?status=active|resolved`, `POST /staff/alerts/{aid}/acknowledge|resolve`; `GET|POST /staff/accounts`, `PATCH /staff/accounts/{username}`, `POST .../disable|enable|reset-password`; `GET /staff/usage?days=30`; `GET /staff/observability?hours=24`, `GET /staff/observability/traces?limit=50`, `GET /staff/observability/traces/{trace_id}`.

## Things to know

- **Path parameters.** The router matches the raw, still-encoded path and does not decode parameters, so `pathSeg()` in `common.js` sends `@` and `+` in usernames unencoded. If the router starts decoding parameters, `pathSeg` can become plain `encodeURIComponent`.
- **The participant's report button** sends `POST /me/emergency` with `severity: "urgent"` (an adverse-event report). The API's default when no severity is sent is still `life_threatening`, and emergency words in chat still raise a life-threatening alert.
- **Timestamps** without a zone (`2026-10-27 21:46:24`) are read as UTC (`parseTs` in `common.js`).
- **Polling.** Alerts refresh every 4 s and requests every 15 s. The participant's report status refreshes every 4 s while a report is open. Observability refreshes every 5 s, but only while its page is open and the browser tab is visible (`obsActive()` in `widgets.js`); its charts redraw only when their data changed.
- **Observability page.** KPIs, requests/errors and entry-span latency (24 h), tokens per UTC day (14 d) with agent config-change flags, the newest traces (each opens a waterfall that draws the SQS queue wait between `whatsapp.ingest` and `whatsapp.route` as its own row), and six drift cards (last 24 h vs the 7 days before; red only when the change is in that measure's bad direction). Every cost is an estimate at list prices and is labelled one. Locally, start the dev server with `--seed-telemetry` for a week of synthetic traces. Each line/column chart has a "Show as table" view, so no value is only reachable by hovering.
- **Chart colours.** Series use `--ch-1` / `--ch-2` (defined at the top of `common.css` from the accent and info tokens, stepped for dark mode) and `--bad-solid` for errors.
- **Themes.** Light, dark and system, stored in `localStorage` as `tt.theme`; all colours are tokens at the top of `common.css`.
