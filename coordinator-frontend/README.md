# Coordinator frontend

The TrialTalk portals: plain HTML, CSS and JS, no framework and no build step.

| File | What it is |
| --- | --- |
| `staff.html` | Staff portal (coordinators and PIs): overview, emergency alerts, participants, requests, team, token usage, observability |
| `participant.html` | Participant site: report an adverse event, medication, chat, requests, details, history |
| `index.html` | Local landing page that links the two portals |
| `common.css`, `common.js` | Shared design tokens, layout, components, the API helper and the dev sign-in |
| `widgets.js` | Staff dashboard widgets: alerts, token usage, observability, team |
| `orb.js`, `vendor/thinking-orbs/` | The chat "thinking" indicator (MIT) |
| `vendor/phosphor/` | Licence for the Phosphor icons, whose paths are inlined in `common.js` (MIT) |

## Run it

`coordinator-backend/devserver/server.py` serves this folder over the real Lambda handlers and a throwaway Postgres; see [the backend README](../coordinator-backend/README.md#test-portals-local-no-aws). `coordinator-backend/infra/portal_ui.py` serves `staff.html` against the deployed API instead.

## Sign-in and sign-out

`/` (`index.html`) is one sign-in page for both portals: a **phone number and password** open whichever portal the account belongs to (staff or participant). Signing out from the account menu returns there. A portal page opened while signed out sends you to sign in and back again.

Everything about where the pages run is in `CONFIG` at the top of `common.js`:

- **Local** (`localhost`, the dev server): `/dev/sign-in` checks the phone number and password. Every local account shares the password `TrialTalk-Demo-2026!`. Participants use the phone number on their record (P-0142 Maria Lopez: `+1 732 555 0142`); staff have synthetic numbers: Jamie Fox (PI, both sites) `+1 555 010 0100`, Riley Chen `…0101`, Dr. Patel `…0102`, Morgan Diaz `…0103`. The account menu can still switch between local accounts and load the worst-case data.
- **Hosted** (any other host, such as Amplify): sign-in goes straight from the browser to Cognito (`USER_PASSWORD_AUTH`, no SDK), including the first-time new-password step and an authenticator or SMS code. Requests carry `Authorization: Bearer <ID token>`, refreshed before it expires; sign-out revokes the refresh token. The account menu shows only who you are and Sign out.

**The default login.** `CONFIG.demo` (`+1 555 010 0100` / `TrialTalk-Demo-2026!`) is filled in on the sign-in page. Locally it always opens Jamie Fox's staff account. **It is readable by anyone who opens the page**, so on a hosted build it lets anyone in: keep it only while the hosted database holds synthetic data, and delete it from `CONFIG` before anything real is reachable.

### What the hosted sign-in needs in AWS (backend work)

The pages are ready; these are not yet in place:

1. **Phone number as the sign-in name in the staff pool.** `tt-staff` signs people in by email and its users have no phone number. Add `phone_number` to staff users and allow it as a sign-in alias (or recreate the pool with phone sign-in), and let the app client `tt-staff-web` (`6tfennq5nb6hu8i50u2cvh3mo1`) use `USER_PASSWORD_AUTH`.
2. **The default login, if wanted hosted:** a staff user with phone `+15550100100` and the demo password, set as permanent so there is no new-password step. The pool forces authenticator-app setup at first sign-in, which this page does not walk through (it says to contact the study team), so that user needs MFA settled in the console first.
3. **Participants with a password.** The participant pool and `participant_api` are not deployed, and participants were designed to sign in with a WhatsApp one-time code (their password is random and unused). Phone-and-password sign-in for them needs a real password set per participant, then the participant client ID and API URL in `CONFIG.clients.participant` and `CONFIG.apiBase.participant`.
4. **CORS on `tt-portal-api`:** add the hosted origin (the Amplify URL) to `CORS_ORIGINS` in `coordinator-backend/infra/deploy_portal.py`, allowing the `Authorization` and `Content-Type` headers.

## Where it meets the backend

Every request goes through one function, `api(method, path, body)` in `common.js`. It calls `CONFIG.apiBase[portal] + path` with the headers from `Auth.headers()` and returns the JSON (or shows the error and throws). The only other request is the quick-reply "send on page leave" in `staff.html` (search for `keepalive`), which uses `Auth.headersNow()`.

**Local-only parts**, hidden automatically on a hosted build: the account switcher and **Demo / Worst case** data switch (`renderAccountMenu`, `signInAs`, `seedWorstCase`, `switchDataset`, which call `/dev/identities` and `/dev/reset`), the Staff / Participant tabs in the top bar, and the "local development build" footer.

## Endpoints the pages call

Participant (`/api/me...`): `GET /me`, `PATCH /me`, `GET /me/reminders`, `GET /me/history`, `GET|POST /me/requests`, `GET|POST /me/chat`, `GET|POST /me/emergency`.

Staff (`/api/staff/...`): `GET /staff/me`; `GET|POST /staff/participants`, `GET|PATCH|DELETE /staff/participants/{pid}` and its `/events`, `/adherence`, `/communications`, `POST .../reminders`; `PATCH|DELETE /staff/reminders/{rid}`, `POST .../send-now`; `GET /staff/requests`, `POST /staff/requests/{cid}/reply`; `GET /staff/alerts?status=active|resolved`, `POST /staff/alerts/{aid}/acknowledge|resolve`; `GET|POST /staff/accounts`, `PATCH /staff/accounts/{username}`, `POST .../disable|enable|reset-password`; `GET /staff/usage?days=30`; `GET /staff/observability`.

## Things to know

- **Path parameters.** The router matches the raw, still-encoded path and does not decode parameters, so `pathSeg()` in `common.js` sends `@` and `+` in usernames unencoded. If the router starts decoding parameters, `pathSeg` can become plain `encodeURIComponent`.
- **The participant's report button** sends `POST /me/emergency` with `severity: "urgent"` (an adverse-event report). The API's default when no severity is sent is still `life_threatening`, and emergency words in chat still raise a life-threatening alert.
- **Timestamps** without a zone (`2026-10-27 21:46:24`) are read as UTC (`parseTs` in `common.js`).
- **Polling.** Alerts refresh every 4 s and requests every 15 s. The participant's report status refreshes every 4 s while a report is open.
- **Themes.** Light, dark and system, stored in `localStorage` as `tt.theme`; all colours are tokens at the top of `common.css`.
