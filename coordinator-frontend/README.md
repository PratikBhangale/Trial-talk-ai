# Coordinator frontend

The TrialTalk portals: plain HTML, CSS and JS, no framework and no build step.

| File | What it is |
| --- | --- |
| `staff.html` | Staff portal (coordinators and PIs): clinic (every trial) and one trial, overview, emergency alerts, participants, requests, team, Ask, token usage, observability |
| `participant.html` | Participant site: report an adverse event, medication, chat, requests, details, history |
| `index.html` | Local landing page that links the two portals |
| `common.css`, `common.js` | Shared design tokens, layout, components, the API helper and the dev sign-in |
| `widgets.js` | Staff dashboard widgets: alerts, clinic and trial pages, Ask, token usage, observability, team |
| `charts.js` | `window.ttCharts`: hand-rolled SVG line and column charts and the trace waterfall used by the Observability page (no libraries) |
| `orb.js`, `vendor/thinking-orbs/` | The chat "thinking" indicator (MIT) |
| `vendor/phosphor/` | Licence for the Phosphor icons, whose paths are inlined in `common.js` (MIT) |

## Run it

`coordinator-backend/devserver/server.py` serves this folder over the real Lambda handlers and a throwaway Postgres; see [the backend README](../coordinator-backend/README.md#test-portals-local-no-aws). `coordinator-backend/infra/portal_ui.py` serves `staff.html` against the deployed API instead.

## Sign-in and sign-out

`/` (`index.html`) is one sign-in page for both portals: an **email address or phone number, and a password**, open whichever portal the account belongs to (staff or participant). Staff sign in by email, because the `tt-staff` pool uses email as the username and Cognito can't change that on an existing pool. Signing out from the account menu returns there. A portal page opened while signed out sends you to sign in and back again.

Everything about where the pages run is in `CONFIG` at the top of `common.js`:

- **Local** (`localhost`, the dev server): `/dev/sign-in` checks the login and password. Every local account shares the password `TrialTalk-Demo-2026!`. Participants use the phone number on their record (P-0142 Maria Lopez: `+1 732 555 0142`). Staff use their email, or a synthetic number: Jamie Fox (PI, both sites) `+1 555 010 0100`, Riley Chen `…0101`, Dr. Patel `…0102`, Morgan Diaz `…0103`. The account menu can still switch between local accounts and load the worst-case data.
- **Hosted** (any other host, such as Amplify): sign-in goes straight from the browser to Cognito (`USER_PASSWORD_AUTH`, no SDK), including the first-time new-password step and an authenticator or SMS code. Requests carry `Authorization: Bearer <ID token>`, refreshed before it expires; sign-out revokes the refresh token. The account menu shows only who you are and Sign out.

**The default login.** Locally, the sign-in page asks the dev server for it (`GET /dev/demo`: `+1 555 010 0100` with the shared dev password) and fills it in, opening Jamie Fox's staff account. No credentials are kept in `common.js`, because it is served publicly. Hosted there is none.

### Hosting on Amplify

`amplify.yml` at the repo root copies this folder into the layout the dev server serves (`/`, `/staff/`, `/participant/`, and `/ui/` for CSS, JS and vendor files), because the pages load everything from absolute `/ui/...` paths.

### What the hosted sign-in needs in AWS (backend work)

1. **A staff account to sign in with.** Staff sign in by email to `tt-staff` (`us-east-1_GlJQOeTBW`, account 180026596905). MFA in the pool is optional, so a password is enough; a first sign-in with an emailed temporary password asks for a new one. Temporary passwords expire after 3 days. After that an admin resets the user (`admin-reset-user-password`, or `admin-set-user-password` with `--permanent`).
2. **CORS on `tt-portal-api`:** the Amplify URL is in `CORS_ORIGINS` in `coordinator-backend/infra/deploy_portal.py`, and was applied to the live API with the CLI on 2026-10-28. Any other hosted origin needs adding there too.
3. **Participants with a password.** `CONFIG` points at the participant pool `trialtalk-participants` (`us-east-1_FVAiqMdUk`), its web client `60qtjo1628btf8aibe7o0808pe` and `trialtalk-api`, all from the CDK stack `TrialTalk-Portal` in account 180026596905. That pool was built for a WhatsApp one-time code (custom auth). Password sign-in needs the following, applied with the CLI on 2026-10-28:
   - `ALLOW_USER_PASSWORD_AUTH` on the web client.
   - Each participant user with a verified `phone_number` (the sign-in alias), `custom:participant_id` matching `pii.participants`, and a permanent password. The demo participant is user `P-0142`, phone `+1 732 555 0142`, password `TrialTalk-Demo-2026!`.
   - The Amplify URL in `trialtalk-api`'s CORS origins (and `tt-portal-api`'s, item 2).

   The client and CORS changes aren't in that stack's CDK source, so the next deploy of `TrialTalk-Portal` undoes them. Add them there.

## Where it meets the backend

Every request goes through one function, `api(method, path, body)` in `common.js`. It calls `CONFIG.apiBase[portal] + path` with the headers from `Auth.headers()` and returns the JSON (or shows the error and throws). The only other request is the quick-reply "send on page leave" in `staff.html` (search for `keepalive`), which uses `Auth.headersNow()`.

**Local-only parts**, hidden automatically on a hosted build: the account switcher and **Demo / Worst case** data switch (`renderAccountMenu`, `signInAs`, `seedWorstCase`, `switchDataset`, which call `/dev/identities` and `/dev/reset`), the Staff / Participant tabs in the top bar, and the "local development build" footer.

## Endpoints the pages call

Participant (`/api/me...`): `GET /me`, `PATCH /me`, `GET /me/reminders`, `GET /me/history`, `GET|POST /me/requests`, `GET|POST /me/chat`, `GET|POST /me/emergency`.

Staff (`/api/staff/...`): `GET /staff/me`; `GET|POST /staff/participants`, `GET|PATCH|DELETE /staff/participants/{pid}` and its `/events`, `/adherence`, `/communications`, `POST .../reminders`; `PATCH|DELETE /staff/reminders/{rid}`, `POST .../send-now`; `GET /staff/requests`, `POST /staff/requests/{cid}/reply`; `GET /staff/alerts?status=active|resolved`, `POST /staff/alerts/{aid}/acknowledge|resolve`; `GET|POST /staff/accounts`, `PATCH /staff/accounts/{username}`, `POST .../disable|enable|reset-password`; `GET /staff/usage?days=30`; `GET /staff/observability?hours=24`, `GET /staff/observability/traces?limit=50`, `GET /staff/observability/traces/{trace_id}`; `GET /staff/clinic`, `GET /staff/clinic/{study_id}`; `POST /staff/ask`.

## Things to know

- **Path parameters.** The router matches the raw, still-encoded path and does not decode parameters, so `pathSeg()` in `common.js` sends `@` and `+` in usernames unencoded. If the router starts decoding parameters, `pathSeg` can become plain `encodeURIComponent`.
- **The participant's report button** sends `POST /me/emergency` with `severity: "urgent"` (an adverse-event report). The API's default when no severity is sent is still `life_threatening`, and emergency words in chat still raise a life-threatening alert.
- **Timestamps** without a zone (`2026-10-27 21:46:24`) are read as UTC (`parseTs` in `common.js`).
- **Polling.** Alerts refresh every 4 s and requests every 15 s. The participant's report status refreshes every 4 s while a report is open. Observability refreshes every 5 s, but only while its page is open and the browser tab is visible (`obsActive()` in `widgets.js`); its charts redraw only when their data changed. Clinic, a trial page and Ask never poll: Clinic and a trial load when opened (Clinic also on Refresh), and Ask sends one request per question.
- **Observability page.** KPIs, requests/errors and entry-span latency (24 h), tokens per UTC day (14 d) with agent config-change flags, the newest traces (each opens a waterfall that draws the SQS queue wait between `whatsapp.ingest` and `whatsapp.route` as its own row), and six drift cards (last 24 h vs the 7 days before, or "vs 7-day rolling avg" when the API sends `baseline_kind: "rolling_7d_avg"`; red only when the change is in that measure's bad direction). Every cost is an estimate at list prices and is labelled one. Locally, start the dev server with `--seed-telemetry` for a week of synthetic traces. Each line/column chart has a "Show as table" view, so no value is only reachable by hovering.
- **Clinic and trial pages** (`#/clinic`, `#/clinic/<study_id>`). KPIs across trials, one card per trial, and a trial's drill-down: summary figures, symptoms per UTC day (14 d, agent-handled vs urgent symptoms), the newest emergency alerts (each opens its participant) and the WhatsApp engagement split. Engaged and adherence cover the last 14 days; symptoms and alerts are all time, and the labels say which. "Urgent symptoms" (`symptoms_by_day[].critical`) and "emergency alerts" (the trial's `critical`, `critical_list`) are kept apart in the wording.
- **Ask** (`#/ask`). A chat with the read-only clinic assistant (`POST /staff/ask`). The last 10 messages go along as `history`; a failed question (502, 400) is not added to it and offers Try again. Under each answer, "Looked at" expands the tool calls and a footnote gives the tokens used. History is per page load and per account.
- **Chart colours.** Series use `--ch-1` / `--ch-2` (defined at the top of `common.css` from the accent and info tokens, stepped for dark mode) and `--bad-solid` for errors.
- **Themes.** Light, dark and system, stored in `localStorage` as `tt.theme`; all colours are tokens at the top of `common.css`.
