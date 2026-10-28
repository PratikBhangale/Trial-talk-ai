# TrialTalk triggers

The Lambdas in [`lambdas/`](lambdas) and the AWS wiring that starts them: build plan flows A (WhatsApp in) and D (reminders), plus the nightly reminder reconcile. Everything is deployed by `deploy.py` into account 180026596905, us-east-1. Every resource is named `tt-*` and tagged `project=trialtalk`.

The agent (`agent/`) is not changed. The router calls it through `InvokeAgentRuntime` with `{"participant_id", "text"}` and sends its `reply`.

## What triggers what

| Real-life moment | Trigger | Lambda | Writes |
|---|---|---|---|
| Participant sends a WhatsApp message or taps a button | End User Messaging Social → SNS `whatsapp_topic` | `tt-ingest` | inbound `communications` row (deduped on the WhatsApp message id), then queues the message on `tt-inbound.fifo` (group = phone) |
| WhatsApp reports sent / delivered / read / failed | same SNS topic | `tt-ingest` | `communications.delivery_status`, matched on the SNS header's `messageId` (never moves backwards) |
| A queued inbound message | SQS FIFO `tt-inbound.fifo` (batch 1, max 2 concurrent; 3 tries, then DLQ `tt-inbound-dlq.fifo`) | `tt-router` | Layer 0 checks, dose buttons, otherwise the agent; outbound row with latency and session id |
| Reminder time | EventBridge Scheduler `rem-<reminder_id>` (group `trialtalk`) | `tt-reminder` | outbound row, DOSE row (`no_response`), `send_count` / `last_sent_at`; one-time reminders become `completed` |
| "Remind me in 30 min" tapped | one-time schedule `snooze-*` created by `tt-router` | `tt-reminder` (`mode: snooze`) | as above |
| `follow_up_after_min` passed | one-time schedule `fu-<dose_event_id>` created by `tt-reminder` | `tt-reminder` (`mode: follow_up`) | if still unanswered and misses in 7 days ≥ `escalate_after_misses`, emails the coordinator (once a day) |
| 2 AM Chicago, nightly | Scheduler `tt-reconcile-nightly` | `tt-reconcile` | creates `rem-*` schedules missing for active reminders, deletes ones whose row isn't active. **Dry run by default** |

### What the router does with a message

1. **Unknown phone**: replies that the number is for enrolled participants.
2. **STOP / BAJA**: sets `whatsapp_opt_in = false` and confirms. **START / ALTA** turns it back on. Opted-out participants get no replies.
3. **Emergency keywords** (English and Spanish): logs an urgent `SYMPTOM` event, tells them to call 911, and emails the coordinator. The agent still answers afterwards.
4. **Dose buttons**: the button id carries `ACTION:<reminder_id>`.
   - **Taken**: records a new DOSE version with `taken` and thanks the participant.
   - **Skipped**: records `skipped`, then asks the agent to respond.
   - **Remind me in 30 min**: records `snoozed` and schedules a re-send.
5. **Everything else** goes to the agent. Its runtime session is per participant per local day.

## Deploy

```bash
pip install boto3 "botocore[crt]"                         # crt is needed for `aws login` profiles
python triggers/infra/deploy.py --profile admin-ai-demo            # create or update everything
```

It's safe to re-run after changing any Lambda code. Each Lambda gets its own IAM role, limited to the `tt_messaging` database login, the cluster, its own log group, and only the services it calls.

Every zip also bundles the OpenTelemetry SDK, pip-installed from the pinned [`../infra-requirements-otel.txt`](../infra-requirements-otel.txt) (once per run, for the build Python resolving for 3.12 with `--only-binary=:all:`). The OTel wheels are pure Python, so building on an x86 or Apple laptop is fine for the arm64 Lambdas. Without the SDK in the zip, `common/telemetry.py` silently no-ops. Each function is also deployed with `TELEMETRY_DB_SINK=1`, which turns on the database span sink (it needs `DB_CLUSTER_ARN` and `DB_SECRET_ARN`, which are already set).

The legacy `continuum-whatsapp` Lambda has no deploy script here (it was deployed by hand). It only gains telemetry if its zip is rebuilt with `common/telemetry.py` and the OTel deps; it is slated for replacement at cutover.

### Tests

```bash
cd triggers && python -m pytest tests/        # a venv with opentelemetry-sdk==1.45.1 and pytest; boto3 is faked by conftest
cd coordinator-backend && python -m pytest tests/test_deploy_zip.py   # zip packaging, no AWS and no pip
```

## Switching on (cutover)

`continuum-whatsapp` is also subscribed to `whatsapp_topic` and replies to every message. To avoid double replies, **`tt-ingest` only logs; forwarding to the router is off** until cutover:

```bash
aws sns list-subscriptions-by-topic --topic-arn arn:aws:sns:us-east-1:180026596905:whatsapp_topic
aws sns unsubscribe --subscription-arn <continuum-whatsapp's subscription arn>
python triggers/infra/deploy.py --profile admin-ai-demo --cutover
```

To roll back, subscribe `continuum-whatsapp` again (`aws sns subscribe --protocol lambda ...`) and set `FORWARD_TO_ROUTER=0` on `tt-ingest`.

## Staff portal API (dose schedules)

`deploy_portal.py` deploys the coordinator portal's backend, using the code from [`../coordinator-backend/lambdas`](../coordinator-backend/lambdas) unchanged:

```bash
python coordinator-backend/infra/deploy_portal.py --profile admin-ai-demo
```

| Piece | Name |
|---|---|
| HTTP API | `tt-portal-api`: https://ca0jle1s2g.execute-api.us-east-1.amazonaws.com |
| Staff sign-in | Cognito pool `tt-staff` (`us-east-1_GlJQOeTBW`), client `6tfennq5nb6hu8i50u2cvh3mo1`, groups `coordinator` / `PI`, pre-token trigger `tt-staff-pre-token` |
| Lambda | `tt-staff-api`, which connects to Aurora as `tt_portal` (RLS limits rows to the caller's sites) |

The routes are listed in [`coordinator-backend/README.md`](../coordinator-backend/README.md#api). For dose schedules:
- **Create:** `POST /staff/participants/{pid}/reminders` saves the row and creates the `rem-<reminder_id>` schedule in the `trialtalk` group. That schedule fires `tt-reminder`.
- **Edit, pause or resume:** `PATCH /staff/reminders/{rid}`. A paused reminder's schedule is disabled.
- **Cancel:** `DELETE /staff/reminders/{rid}` deletes the schedule and keeps the row.
- **Send now:** `POST /staff/reminders/{rid}/send-now` invokes `tt-reminder` directly. This also works for paused and draft reminders.

**Changing the time:** send `days` along with `time`. A `time` alone makes the reminder daily.

**Getting a token.** There's no web app yet, so sign in from the terminal:

```bash
python coordinator-backend/infra/staff_token.py --email divya.shah7999@gmail.com   # first time: the emailed temporary password, then a new one
curl -H "Authorization: Bearer $(cat .staff_token)" https://ca0jle1s2g.execute-api.us-east-1.amazonaws.com/staff/me
```

**Differences from the CDK stack in `the removed `coordinator-backend/infra` CDK app`.** That stack targets another account and would create its own Aurora.
- MFA is optional here, not required, so a token can be fetched from the CLI. Turn it back on when the web app exists.
- `USER_PASSWORD_AUTH` is enabled on the client for the same reason.
- There's no participant pool or `participant_api` yet. So `POST /staff/participants` (enrolling a participant, which also creates their website login) doesn't work.
- `LEGACY_TABLE` is empty, so nothing is mirrored to DynamoDB.

## Turning on real reminder schedules

Reminders created through the staff portal API get their `rem-*` schedule straight away. For rows added any other way (like the seed data):
- **One run:** invoke `tt-reconcile` with `{"dry_run": false}`.
- **Every night:** set `DRY_RUN=0` on it.

**This starts real daily WhatsApp messages** for every active reminder (the seed data includes P-0201's 8 AM dose reminder, which goes to a real phone).

## Known gaps

- **`dose_reminder_v2` is pending with Meta.** Until it's approved, reminders go out as reply buttons. WhatsApp delivers those only within 24 h of the participant's last message, and they fail silently (status `failed`) outside it.
- **Reminders don't write to AgentCore Memory.** The agent's memory is a Strands conversation log, and inserting a turn by hand would break the strict user/assistant alternation the model requires. The agent sees active reminders through the database instead.
- **No token usage on agent replies.** The agent returns only `reply`, so the outbound row has latency and session id but no token counts.
- **Inbound media isn't fetched yet** (`GetWhatsAppMessageMedia` → S3 → scan). The row stores the media id with `scan_status: not_fetched`, and the router asks for text.
- **`next_send_at` isn't computed** for recurring reminders.
- **PI email replies (flow C/T7) need a domain** with an MX record pointing at SES, plus SES production access.

### Using the staff page against the real API

Until there is a hosted web app, `portal_ui.py` serves `coordinator-frontend/staff.html` (unchanged) and forwards its calls to `tt-portal-api` with your Cognito token:

```bash
python coordinator-backend/infra/portal_ui.py --email divya.shah7999@gmail.com     # sign in in the terminal, then open http://127.0.0.1:8788/staff
```

Everything is real (Aurora, schedules, WhatsApp). **Send now** messages the participant. The Inspector and Reset buttons are local-only and do nothing here.
