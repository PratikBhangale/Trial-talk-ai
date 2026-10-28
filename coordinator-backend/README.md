# TrialTalk backend: staff and participant APIs

Serverless backend for two portals: the staff portal (coordinators, PIs), the participant website,
Staff are their own admins: staff accounts are managed inside the staff portal. It follows the "Staff portal action" and "Reminders"
flows in the build plan:

```
Cognito (staff pool | participant pool) → API Gateway HTTP API (JWT authorizer per app client)
  → staff_api / participant_api Lambda (per-action check) → RDS Data API → Aurora PostgreSQL (RLS)
  → staff_api /staff/accounts → Cognito staff pool (no database access for these routes)
participant SOS button → participant_api → trial.emergency_alerts + immediate SES email → staff alerts widget
trigger Lambdas + agent → OpenTelemetry spans → trial.telemetry_spans → staff Observability dashboard
staff reminder actions → EventBridge Scheduler rem-<reminder_id> → reminder Lambda → WhatsApp template
participant chat → participant_api → Layer 0 emergency check → AgentCore Runtime (trial_agent)
```

```
coordinator-backend/
  lambdas/                  one code asset shared by every function
    common/                 db (Data API + set_config), auth (claims, require), http router, telemetry (OpenTelemetry),
                            scheduling (EventBridge Scheduler), whatsapp, safety (Layer 0), legacy_mirror
    staff_api/              /staff/* routes
    participant_api/        /me/* routes (incl. chat)
    (staff_api/accounts.py  /staff/accounts routes: staff accounts, roles, sites; Cognito only)
    reminder/               fired by Scheduler or "Send now"
    auth_triggers/          staff pre-token; participant passwordless login (code on WhatsApp)
    migrate/                schema.sql + custom resource that applies it and creates DB logins
  infra/                    deploy_portal.py (tt-staff-api, tt-staff-pre-token, tt-portal-api, tt-staff pool), portal_ui.py, staff_token.py
  tests/                    unit tests + integration tests against a real local Postgres; fakes in local_env.py
  devserver/                local test server; serves ../coordinator-frontend over the real handlers
```

## API

All bodies are JSON. Send the Cognito **ID token** as `Authorization: Bearer <token>`.

### Staff (`coordinator` or `PI` group; scoped to `custom:site_ids`)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/staff/me` | Role, sites and studies from the token |
| GET | `/staff/participants?q=&status=&site_id=` | List or search participants at your sites |
| POST | `/staff/participants` | Enroll a participant. Needs `full_name`, `phone_e164`, `study_id`, `site_id`; optional `email`, `date_of_birth`, address fields, `preferred_language`, `timezone`, `status`, `whatsapp_opt_in`. Also creates their website login |
| GET | `/staff/participants/{pid}` | Profile and all reminders |
| PATCH | `/staff/participants/{pid}` | Edit contact, language, time zone, status or opt-in. Changing name, DOB or phone needs `change_reason` |
| DELETE | `/staff/participants/{pid}` | **Withdraw** (`{"reason": "..."}` is required). See "Deleting participants" |
| GET | `/staff/participants/{pid}/events?record_type=` | Clinical timeline (latest versions) |
| GET | `/staff/participants/{pid}/adherence` | DOSE responses over the last 30 days |
| GET | `/staff/participants/{pid}/communications` | WhatsApp, email and web messages |
| GET | `/staff/requests` | Participant requests waiting for review |
| GET / POST | `/staff/participants/{pid}/reminders` | List or create reminders |
| PATCH | `/staff/reminders/{rid}` | Edit, pause (`status: paused`) or resume (`status: active`). Optional `reason` goes into `change_log` |
| DELETE | `/staff/reminders/{rid}` | Cancel. Deletes the schedule; the row is kept |
| POST | `/staff/reminders/{rid}/send-now` | Send immediately through the reminder Lambda |

**Dose reminders** are how staff set a dosing schedule:

```json
POST /staff/participants/P-1001/reminders
{
  "reminder_type": "dose", "label": "Evening dose",
  "dose_label": "evening", "dose_instructions": "1 tablet with food",
  "time": "20:00", "days": ["MON", "WED", "FRI"],
  "starts_on": "2026-11-01", "ends_on": "2027-05-01",
  "escalate_after_misses": 3
}
```

- Leave out `days` for every day. A raw `cron_expr` such as `cron(0 20 * * ? *)` also works, and so does `send_at` (ISO timestamp) for a one-time reminder.
- Times are in the participant's time zone.
- `dose_label` and `dose_instructions` become template parameters `{{1}}` and `{{2}}`. The default buttons are Taken / Skipped / Remind me in 30 min.
- An active reminder requires `whatsapp_opt_in`. Otherwise save it with `"status": "draft"`.
- Templates must be on the `approvedTemplates` list.

As the plan requires, staff only *schedule* doses here. No API or agent tool changes treatment.

### Emergency alerts (staff, same roles and site scope)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/staff/alerts?status=active\|resolved\|all` | Alerts at your sites, with participant name, phone and site. Default `active` (open + acknowledged), open and life-threatening first. `open` in the response is the count still waiting for a human |
| POST | `/staff/alerts/{aid}/acknowledge` | "I have seen this." Idempotent; the first acknowledger is recorded |
| POST | `/staff/alerts/{aid}/resolve` | `{"note": "..."}` is required: what happened and what was done. Resolved alerts stay for the record |

Alerts come from two places, both stored in `trial.emergency_alerts` (the one table beyond the plan's four, because alerts need a status lifecycle that append-only `clinical_events` cannot give them):
- the participant's SOS button (`POST /me/emergency`, below), severity `life_threatening` by default;
- Layer 0 emergency keywords in the web chat, which used to send only an email.

Repeat presses while an alert is unresolved (30 minutes) are folded into it as `press_count`, so staff see one alert, not fifty, and the site gets one email. A press after resolution opens a new alert. The email carries no message content.

### Staff accounts (staff are their own admins)

There is no admin group, portal or API. Any coordinator or PI can manage accounts, through `/staff/accounts`. These routes only call Cognito on the staff pool and never read participant data.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/staff/accounts?q=` | Staff accounts with role, sites, studies, enabled, Cognito status |
| POST | `/staff/accounts` | Invite: `email`, `site_ids`, `study_ids`, `name`. Every invite joins the `PI` group (a `role` in the body is ignored; PATCH can change it), so only a PI can invite. Cognito emails a temporary password; MFA setup is required at first sign-in |
| GET / PATCH | `/staff/accounts/{username}` | Read, or change `role`, `site_ids`, `study_ids`, `name`. Takes effect at the user's next token refresh |
| POST | `/staff/accounts/{username}/disable` | Block sign-in and sign out everywhere (optional `reason`). Issued ID tokens last until expiry, at most 60 minutes |
| POST | `/staff/accounts/{username}/enable` | Re-enable |
| POST | `/staff/accounts/{username}/reset-password` | Cognito emails a reset code |

Everyone needs a role and at least one site. Because database site access comes from these accounts, nobody can grant more than they hold:
- You only see accounts that share a site with you, and only manage accounts whose sites are all yours.
- You can only grant your own sites and studies. Only a PI can grant the PI role or change a PI's account.
- You can't change your own role, sites or studies, or disable your own account. Your name you can change.

Each change prints an `audit` line to CloudWatch.

### Observability (OpenTelemetry)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/staff/observability?hours=` | Dashboard payload: requests, error rate, p50/p95 latency, tokens and estimated cost; request, latency and token series; behavior drift (last 24 h against the 7 days before) |
| GET | `/staff/observability/traces?limit=` | Most recent traces, one row per trace |
| GET | `/staff/observability/traces/{trace_id}` | Every span of one trace (the waterfall) |

`common/telemetry.py` uses the real OpenTelemetry SDK: a server span per request named by route template (`GET /staff/alerts`, never the URL), child spans for every database call (SQL verb only), the agent and WhatsApp, a `http.server.request.duration` histogram and counters such as `trialtalk.alerts.raised`. It continues an incoming W3C `traceparent`. No participant ids, SQL text or bodies go into telemetry (a test checks this).

The endpoints read `trial.telemetry_spans`, which the trigger Lambdas write (`TELEMETRY_DB_SINK=1`) along with the agent's own spans from web chat, and token counts from `trial.communications`. A trace's root is its earliest span with no stored parent, so a trace whose root span was never stored still shows up, once. A request counts as failed when any span of its trace is `ERROR`, not just the root.

- Without the SDK installed `common/telemetry.py` records nothing; the dashboard still shows whatever spans are stored.
- `tt-staff-api` does not store its own request spans (`TELEMETRY_DB_SINK=0`): the dashboard polls it every few seconds, and those spans would bury the pipeline traces. To export them anyway, set `OTEL_EXPORTER_OTLP_ENDPOINT` and bundle `opentelemetry-exporter-otlp-proto-http`, which the zip does not include.

### Participant (`custom:participant_id` from the token; never from the request)

| Method | Path | What it does |
| --- | --- | --- |
| GET / PATCH | `/me` | Profile. Participants can edit email, address, language and time zone. Name, DOB and phone go through a request |
| GET | `/me/reminders` | Active reminders, plus `dose_schedule` (label and "Every day at 8:00 PM"; db/ doesn't let participants read the dose instructions) |
| GET | `/me/history?record_type=` | Own visits, doses, symptoms and consent questions (read-only) |
| GET / POST | `/me/requests` | `{"kind": "reminder_change" \| "reminder_pause" \| "profile_change" \| "withdrawal" \| "whatsapp_opt_out" \| "contact_team" \| "history_correction", "message": "..."}`. Requests show up in `/staff/requests` |
| POST | `/me/emergency` | The SOS button. An empty body is a complete request: it raises a `life_threatening` alert, emails the site team and returns the emergency instruction in the participant's language. Optional `message` and `severity` (`life_threatening` / `urgent` / `concern`) |
| GET | `/me/emergency` | Own recent alerts and their status (open / acknowledged / resolved), so the page can show "your team has seen this" |
| GET | `/me/chat` | Last 50 web chat messages |
| POST | `/me/chat` | `{"text": "..."}` returns `{"reply", "emergency"}`. Emergency keywords (en/es) get the fixed emergency reply, a staff alert and an immediate team email, and never reach the model. Everything else goes to the deployed `trial_agent` |

## Decisions and departures from the plan

1. **"Delete" a participant = withdraw.** `pii.participants` and `trial.clinical_events` are never deleted (Part 11 audit trail). DELETE does four things:
   - sets status to `withdrawn` and `whatsapp_opt_in` to false
   - cancels every open reminder and deletes its schedule
   - disables the website login
   - removes the WhatsApp phone lookup

   Hard deletion is revoked from every role in the schema.
2. **Web is a new channel.** Website requests and staff replies to them are logged with channel `'web'` in `communications`, and `participant_role` can read its own web rows. The participant website has no chat; participants talk to the agent on WhatsApp.
3. **DynamoDB bridge.** Aurora is the source of truth, but the deployed agent (`store.get_profile`) and `whatsapp/handler.py` still read DynamoDB `trial_agent`. To keep them working, `common/legacy_mirror.py` copies the profile, dose times and phone lookup there after every staff change. `legacyTable` turns it on; it is `""` (off) because account `180026596905` has no `trial_agent` table, and the mirror writes inside the staff transaction, so a missing table would fail every staff change. Nothing in the DynamoDB path was deleted.
4. **Account and region.** Account `180026596905`, `us-east-1`, next to the Aurora cluster, the agent runtime and the WhatsApp account. A cluster in another region also works: the Lambdas call the Data API in `DB_REGION`, taken from the cluster ARN.
5. **Small schema additions**, all listed at the top of `schema.sql`:
   - a `P-1001…` id sequence
   - RLS policies for `messaging_role` and `agent_role`. The plan turns on RLS but defines no policies for these roles, so without this they would see zero rows
   - `messaging_role` can SELECT communications (the router has to match button replies)
   - `participant_role` can read `template_params` so it can show dose instructions
6. **One DB login per Lambda family** (`staff_api` → `portal_role`, `participant_api` → `participant_role`, `messaging_api` → `messaging_role`), each with its own secret. API Lambdas cannot read the master secret, so even a Lambda bug can't get around RLS.

## Not done yet

- Staff AE pages and PI sign-off (SIGNATURE rows), and the AppSync live alerts
- The router's handling of Taken / Remind-me button taps (the reminder sends payloads of the form `rem:<id>:<button>`)
- The follow-up and missed-dose escalation checks, and the nightly reconcile Lambda
- The WhatsApp join code
- WAF: it can't attach to an HTTP API. Put it on CloudFront or the Cognito pools

## Testing

```bash
# unit tests only
python -m pytest coordinator-backend/tests
# plus integration tests: real handlers + schema + RLS on a throwaway local PostgreSQL 16
conda create -p .pg -c conda-forge python=3.12 postgresql=16 psycopg pytest tzdata
.pg/python -m pip install opentelemetry-sdk   # optional: telemetry tests and the Observability widget
TT_PG_BIN=.pg/Library/bin .pg/python -m pytest coordinator-backend/tests   # Linux/macOS: .pg/bin
```

The integration tests swap the RDS Data API for a fake that runs each statement on the local server, logged in as the same database user the Lambda would use (`tt_portal`, `tt_participant`, `tt_messaging`). That means grants, RLS and `set_config` scoping all get exercised, not mocked. The fakes and the Postgres setup are in `tests/local_env.py`.

The local database is built from `db/01_schema.sql` and `db/02_roles_rls.sql`, the schema's source of truth, plus `db/05_telemetry.sql` and `db/06_alerts_web.sql` (the `web` channel, participant web chat, `trial.emergency_alerts` and the router's alert rights).

### Test portals (local, no AWS)

```bash
TT_PG_BIN=.pg/Library/bin .pg/python coordinator-backend/devserver/server.py      # then open http://127.0.0.1:8787  (macOS: prefix LC_ALL=en_US.UTF-8)
```

The server starts a throwaway Postgres built the same way and seeds synthetic data:
- `db/03_seed.sql`, the same rows as the deployed database: P-0142 (Maria Lopez) and P-0143 at RIVERSIDE, P-0201 at LAKEVIEW, with their visits, symptoms, an AE, dose history, reminders and messages. Their website logins and reminder schedules are created the way `staff_api` would have
- one bootstrap staff user, `lead@example.org` (a PI at both sites), created the way you would in the Cognito console
- three more staff users, invited through `/staff/accounts` as that user, named after the seed's records: `coordinator@riverside-clinic.org`, the PI `dr.patel@riverside-clinic.org`, and `coordinator@lakeview-health.org`

Participants you enroll get the next free id after the highest one you can see (P-0144, …). db/ gives `participant_id` no default.

Each portal is its own page, as it will be deployed:

The pages live in [`../coordinator-frontend`](../coordinator-frontend): plain HTML, CSS and JS with no build step (`common.css` / `common.js` shared, `widgets.js` for the staff dashboard widgets, `vendor/` for two small MIT libraries). They only talk to `/api/...` and, for the dev sign-in, `/dev/...`.

- **`/staff`** (coordinators and PIs): one page per task in the sidebar. **Overview** (open alerts, enrollment by site, participants who may need a check-in), **Emergency alerts** (refreshes every 4 s, sounds on a new one, acknowledge / resolve), **Participants** (enroll, edit, withdraw; dose, visit or one-time reminders: pause, resume, change the time, send now, cancel; timeline, adherence, communications), **Requests** (quick or written replies, each with a 5 s undo), **Team** (invite, edit, disable, reset staff accounts), **Token usage** and **Observability** (requests, error rate, latency, counters).
- **`/participant`**: an orange **I need to report an adverse event** button (one press, no input; sends `severity: "urgent"`) that shows the instruction and then a live status timeline; dose schedule, profile, history, requests, and chat. The emergency keyword buttons show the Layer 0 path.

`--seed-telemetry` fills the staff **Observability** dashboard: about 7 days of synthetic traces (`whatsapp.ingest` -> `whatsapp.route` -> `agent.call` -> `agent.invoke` -> `tool.*`, some ERRORs, a mid-week agent config change) in `trial.telemetry_spans`, plus a token-bearing outbound message per agent reply in `trial.communications`. Every synthetic span has `"trialtalk.synthetic": true` in its attributes, messages use a `synthetic-...` session id, nothing carries a name, phone or real message text, and **Reset data** re-creates it. Web chat also records what the agent returns: tokens and model on the reply's `communications` row, and the agent's spans (stored as service `trial-agent`) via `telemetry.persist_spans`; the fake agent (`FakeAgentCore`) returns the real contract with random 300-900 / 60-250 token counts.

What would have gone to AWS (schedules, WhatsApp, SES emails, agent calls, both user pools) is still recorded by the fakes and readable at `GET /dev/outbox`; `POST /dev/fire/<reminder_id>` fires a schedule the way Scheduler would.

Sign in at `/` with a phone number and the shared local password `TrialTalk-Demo-2026!` (checked by `POST /dev/sign-in`; the page fills in the default login, Jamie Fox at `+1 555 010 0100`). Participants use the phone number on their record; the local staff numbers are listed in [the frontend README](../coordinator-frontend/README.md#sign-in-and-sign-out). Once signed in, the account menu can switch local accounts or load the **Worst case** test data. The server builds the token claims from fake user pools the way Cognito would, including the real staff pre-token trigger. Sign-in follows those pools:
- Whatever the Team widget changes applies on the next request.
- Disabled staff and withdrawn participants are refused.
- Each portal only accepts its own users.

Everything below API Gateway is real code against real RLS. Chat replies come from a local echo agent; `--real-agent` sends them to the deployed runtime instead, which needs boto3 and AWS credentials. Stop the server with Ctrl+C, which deletes the database.

## Deploy

**The source of truth is what is deployed in AWS** (account `180026596905`, us-east-1; resources named `tt-*`), not a CDK stack. The staff API goes out with `infra/deploy_portal.py`, next to the trigger Lambdas in [`../triggers`](../triggers/README.md):

```bash
python coordinator-backend/infra/deploy_portal.py --profile admin-ai-demo
python coordinator-backend/infra/staff_token.py --email you@example.org   # sign in as staff
python coordinator-backend/infra/portal_ui.py --email you@example.org     # staff page on the real API: http://127.0.0.1:8788/staff
```

The zip (all of `lambdas/` except `migrate/`) also bundles the pinned OpenTelemetry SDK from [`../infra-requirements-otel.txt`](../infra-requirements-otel.txt), and the trace and metric endpoints read the spans the trigger Lambdas store in AWS. `tt-staff-api` itself is deployed with `TELEMETRY_DB_SINK=0`, so the dashboard's own polling doesn't fill the table. The OTel wheels are pure Python, so the build host's architecture does not matter. `tests/test_deploy_zip.py` checks the packaging without AWS.

The reminder Lambda is `tt-reminder` (`triggers/lambdas/reminder`); `lambdas/reminder` here is not deployed. `lambdas/migrate` is not deployed either: the database is built from [`../db`](../db). The CDK app that used to live in `infra/` was removed (it is in git history); it created a second, unused stack set.

### Console steps (the plan leaves these to the console)

- **The first staff user.** Create one user in the `trialtalk-staff` pool, add them to the `PI` group and set `custom:site_ids` to every site (e.g. `RIVERSIDE,LAKEVIEW`) and `custom:study_ids` (e.g. `KOA-301`). Nobody can grant more than they hold, so this first user must hold everything the others will need. After that, invite everyone else from the staff portal's Team widget (`/staff/accounts`).
- **WhatsApp templates**, IRB-approved, one per language:
  - `dose_reminder_v2`: utility, `{{1}}` dose label, `{{2}}` instructions, 3 quick-reply buttons
  - `visit_reminder_v1`
  - `daily_checkin_v1`
  - `login_code`: authentication category, with a copy-code button
