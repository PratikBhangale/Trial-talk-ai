# TrialTalk backend: staff, participant and admin APIs

Serverless backend for three portals: the staff portal (coordinators, PIs), the participant website,
and the admin portal (staff accounts and access). It follows the "Staff portal action" and "Reminders"
flows in the build plan:

```
Cognito (staff pool | participant pool) → API Gateway HTTP API (JWT authorizer per app client)
  → staff_api / participant_api Lambda (per-action check) → RDS Data API → Aurora PostgreSQL (RLS)
  → admin_api Lambda (admin group) → Cognito staff pool (no database access)
staff reminder actions → EventBridge Scheduler rem-<reminder_id> → reminder Lambda → WhatsApp template
participant chat → participant_api → Layer 0 emergency check → AgentCore Runtime (trial_agent)
```

```
backend/
  lambdas/                  one code asset shared by every function
    common/                 db (Data API + set_config), auth (claims, require), http router,
                            scheduling (EventBridge Scheduler), whatsapp, safety (Layer 0), legacy_mirror
    staff_api/              /staff/* routes
    participant_api/        /me/* routes (incl. chat)
    admin_api/              /admin/* routes: staff accounts, roles, sites (Cognito only)
    reminder/               fired by Scheduler or "Send now"
    auth_triggers/          staff pre-token; participant passwordless login (code on WhatsApp)
    migrate/                schema.sql + custom resource that applies it and creates DB logins
  infra/                    CDK (TypeScript): TrialTalk-Data, TrialTalk-Scheduler, TrialTalk-Portal
  tests/                    unit tests + integration tests against a real local Postgres; fakes in local_env.py
  devserver/                local test portals (staff, participant, admin) over the real handlers
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

### Admin (`admin` group in the staff pool, signed in through the admin app client)

The admin portal has its own app client, so its tokens are rejected on `/staff` and staff tokens are rejected on `/admin`. A user can be in a role group and `admin` at the same time; they sign in to each portal separately. `admin_api` only calls Cognito on the staff pool and has no database login, so an admin can grant access but never reads participant data.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/admin/me` | The signed-in admin |
| GET | `/admin/staff?q=` | Staff accounts with role, admin flag, sites, studies, enabled, Cognito status |
| POST | `/admin/staff` | Invite: `email`, `role` (`coordinator` / `PI` / empty), `admin`, `site_ids`, `study_ids`, `name`. Cognito emails a temporary password; MFA setup is required at first sign-in |
| GET / PATCH | `/admin/staff/{username}` | Read, or change `role`, `admin`, `site_ids`, `study_ids`, `name`. Takes effect at the user's next token refresh |
| POST | `/admin/staff/{username}/disable` | Block sign-in and sign out everywhere (optional `reason`). Issued ID tokens last until expiry, at most 60 minutes |
| POST | `/admin/staff/{username}/enable` | Re-enable |
| POST | `/admin/staff/{username}/reset-password` | Cognito emails a reset code |

Coordinators and PIs need at least one site. Admins can't disable themselves or remove their own admin access, so the last admin can't lock everyone out. Each change prints an `audit` line to CloudWatch.

### Participant (`custom:participant_id` from the token; never from the request)

| Method | Path | What it does |
| --- | --- | --- |
| GET / PATCH | `/me` | Profile. Participants can edit email, address, language and time zone. Name, DOB and phone go through a request |
| GET | `/me/reminders` | Active reminders, plus `dose_schedule` (label, instructions, "Every day at 8:00 PM") |
| GET | `/me/history?record_type=` | Own visits, doses, symptoms and consent questions (read-only) |
| GET / POST | `/me/requests` | `{"kind": "reminder_change" \| "reminder_pause" \| "profile_change" \| "withdrawal" \| "whatsapp_opt_out" \| "contact_team" \| "history_correction", "message": "..."}`. Requests show up in `/staff/requests` |
| GET | `/me/chat` | Last 50 web chat messages |
| POST | `/me/chat` | `{"text": "..."}` returns `{"reply", "emergency"}`. Emergency keywords (en/es) get the fixed emergency reply and an immediate team email, and never reach the model. Everything else goes to the deployed `trial_agent` |

## Decisions and departures from the plan

1. **"Delete" a participant = withdraw.** `pii.participants` and `trial.clinical_events` are never deleted (Part 11 audit trail). DELETE does four things:
   - sets status to `withdrawn` and `whatsapp_opt_in` to false
   - cancels every open reminder and deletes its schedule
   - disables the website login
   - removes the WhatsApp phone lookup

   Hard deletion is revoked from every role in the schema.
2. **Web chat is a new channel.** The plan's participant website has no chatbot. I added `'web'` to `communications.channel`, and `participant_role` can read its own web rows.
3. **DynamoDB bridge.** Aurora is the source of truth, but the deployed agent (`store.get_profile`) and `whatsapp/handler.py` still read DynamoDB `trial_agent`. To keep them working, `common/legacy_mirror.py` copies the profile, dose times and phone lookup there after every staff change. Set context `legacyTable` to `""` to turn this off once the agent reads Aurora. Nothing in the DynamoDB path was deleted.
4. **Region.** The stacks default to `us-east-2`, next to the deployed agent runtime (`cdk.json` → `region`). The target `us-east-1` is a context change plus redeploying the agent there.
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
python -m pytest backend/tests
# plus integration tests: real handlers + schema + RLS on a throwaway local PostgreSQL 16
conda create -p .pg -c conda-forge python=3.12 postgresql=16 psycopg pytest tzdata
TT_PG_BIN=.pg/Library/bin .pg/python -m pytest backend/tests   # Linux/macOS: .pg/bin
```

The integration tests swap the RDS Data API for a fake that runs each statement on the local server, logged in as the same database user the Lambda would use. That means grants, RLS and `set_config` scoping all get exercised, not mocked. The fakes and the Postgres setup are in `tests/local_env.py`.

### Test portals (local, no AWS)

```bash
TT_PG_BIN=.pg/Library/bin .pg/python backend/devserver/server.py      # then open http://127.0.0.1:8787
```

The server starts a throwaway Postgres and seeds synthetic data:
- a bootstrap admin, `admin@example.org`
- four staff users, created through `admin_api`: a coordinator and a PI at RIVERSIDE, a coordinator at LAKESIDE, and a coordinator covering both sites
- participants P-1001…P-1004, three at RIVERSIDE and one at LAKESIDE

Each portal is its own page, as it will be deployed:

- **`/staff`** (coordinators and PIs): enroll, edit and withdraw participants; create dose, visit or one-time reminders; pause, resume, change the time, send now or cancel; timeline, adherence, communications and the request queue.
- **`/participant`**: dose schedule, profile, history, requests, and chat. The emergency keyword buttons show the Layer 0 path.
- **`/admin`**: invite staff, set the role, admin access, sites and studies, disable or enable accounts, reset passwords.
- **`/inspector`** (dev only, not a portal): what would have gone to AWS, meaning EventBridge schedules (**Fire** runs one the way Scheduler would), the WhatsApp outbox, SES emails, agent calls and both user pools.

Each page has a dev sign-in picker in place of Cognito. The server builds the token claims from fake user pools the way Cognito would, including the real staff pre-token trigger. Sign-in follows those pools:
- Whatever the admin portal changes applies on the staff portal's next request.
- Disabled staff and withdrawn participants are refused.
- Each portal only accepts its own users.

Everything below API Gateway is real code against real RLS. Every API call appears in the page's request log. Chat replies come from a local echo agent; `--real-agent` sends them to the deployed runtime instead, which needs boto3 and AWS credentials. Stop the server with Ctrl+C, which deletes the database.

## Deploy

1. Check AWS auth: `aws sts get-caller-identity` (account `461729033902`).
2. Set the context in `infra/cdk.json`:
   - `whatsappPhoneNumberId` (the `phone-number-id-…` ARN of the WhatsApp number)
   - `alertFrom` and `alertTo` (an SES-verified sender, plus the coordinator address)
   - `staffCallbackUrls`, `adminCallbackUrls` and `corsOrigins` for the portal URLs
3. Deploy:

```bash
cd backend/infra
npm install
npx cdk bootstrap aws://461729033902/us-east-2   # once per account/region
npx cdk deploy --all
```

The first deploy creates Aurora and runs the migration (several minutes). Outputs include the API URL and both user pools' ids and client ids.

### Console steps (the plan leaves these to the console)

- **The first admin.** Create one user in the `trialtalk-staff` pool and add them to the `admin` group. After that, invite and manage staff from the admin portal (`/admin` routes). Doing it by hand also works: add each user to the `coordinator` or `PI` group and set `custom:site_ids` (e.g. `RIVERSIDE`) and `custom:study_ids` (e.g. `KOA-301`).
- **WhatsApp templates**, IRB-approved, one per language:
  - `dose_reminder_v2`: utility, `{{1}}` dose label, `{{2}}` instructions, 3 quick-reply buttons
  - `visit_reminder_v1`
  - `daily_checkin_v1`
  - `login_code`: authentication category, with a copy-code button
