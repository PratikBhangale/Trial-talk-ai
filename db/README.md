# TrialTalk database

Aurora PostgreSQL 16 (Serverless v2) in **us-east-1**. Lambdas reach it through the **RDS Data API**, so they need no VPC and no connection pool. The SQL files in this folder are the source of truth for the schema. Where they differ from the SQL in the build plan PDF, follow these files; [TEST_PLAN.md](TEST_PLAN.md) lists the differences and why.

## What's deployed

| Thing | Value |
|---|---|
| Cluster | `trialtalk` (writer instance `trialtalk-writer`), 0.5–2 ACU |
| Database name | `trialtalk` |
| Cluster ARN | `aws rds describe-db-clusters --db-cluster-identifier trialtalk --query "DBClusters[0].DBClusterArn"` |
| Login secrets (one per service) | `trialtalk/db/tt_agent`, `trialtalk/db/tt_messaging`, `trialtalk/db/tt_portal`, `trialtalk/db/tt_participant` |
| Master secret | RDS-managed (`rds!cluster-…`). For running migrations only. **Never give it to a Lambda.** |

The database is loaded with the synthetic data from `03_seed.sql` (see [Test data](#test-data)).

## Files

| File | Purpose |
|---|---|
| `01_schema.sql` | the 4 tables, the `current_events` view, indexes and integrity triggers |
| `02_roles_rls.sql` | roles, login users, column grants and row-level security. Passwords are `CHANGE_ME` placeholders; the real ones live only in Secrets Manager |
| `03_seed.sql` | synthetic test data with fixed IDs |
| `04_db_tests.sql` | 19 SQL tests (permissions, RLS, append-only, dedupe, versioning) |
| `05_telemetry.sql` | `trial.telemetry_spans`, where services store OpenTelemetry spans (see [Telemetry](#telemetry)) |
| `06_alerts_web.sql` | `trial.emergency_alerts` (SOS button and emergency keywords), the `web` channel on `trial.communications`, and the telemetry retention grant (see [Alerts and the web channel](#alerts-and-the-web-channel)) |
| `99_teardown.sql` | drops everything (destroys data) |
| `run_sql.py` | runs the files above through the Data API |
| `TEST_PLAN.md` | plan for testing the Lambdas against the database |

## The four tables

All four join on `participant_id` (e.g. `'P-0142'`).

| Table | Holds | How rows change |
|---|---|---|
| `pii.participants` | who the person is: name, phone, address, site, status, WhatsApp opt-in | updated in place; `updated_at` is set by a trigger |
| `trial.clinical_events` | everything clinical, one row per record; `record_type` says what it is: `PRESCREEN`, `CONSENT_Q`, `VISIT`, `VISIT_CHANGE`, `SYMPTOM`, `AE`, `DOSE`, `SIGNATURE` | **append-only.** UPDATE and DELETE are blocked by a trigger, even for the owner |
| `trial.communications` | every WhatsApp and email message, its attachments and the agent's token usage | insert; only `delivery_status`, `attachments` and `attachment_text` can be updated; never deleted |
| `trial.reminders` | every reminder staff set: template, schedule, state | updated in place; never deleted (set `status = 'cancelled'`) |

Which typed columns and `details` keys each `record_type` uses is in the build plan PDF ("Data model" section) and in the comments in `01_schema.sql`.

## Telemetry

`trial.telemetry_spans` (from `05_telemetry.sql`) stores OpenTelemetry spans: ids, span name, service, start time, duration, status and a small `attrs` jsonb. It is separate from the four tables above and **carries no PHI by design**: no participant identifiers, no message text. The exporter in `lambdas/common/telemetry.py` keeps an attribute allowlist and drops everything else before writing, so never widen it with anything identifying. For that reason the table has no row-level security: `tt_messaging`, `tt_portal` and `tt_participant` get INSERT and SELECT (the exporter's `ON CONFLICT (span_id) DO NOTHING` needs SELECT).

## Alerts and the web channel

`06_alerts_web.sql` adds three things on top of 01-05. **`trial.emergency_alerts`** holds one row per emergency, raised by the participant's SOS button, or by the router when a WhatsApp message contains emergency keywords. Unlike `clinical_events` it has a lifecycle (`open` -> `acknowledged` -> `resolved`) and is updated in place, but rows are never deleted. Repeat presses within 30 minutes fold into the open alert (`press_count`). `tt_portal` triages alerts at the user's sites, `tt_participant` raises and re-presses its own, and `tt_messaging` (the router) can read, insert and update `press_count`, `last_pressed_at` and `message`. The **`web` channel** is now allowed on `trial.communications` for rows written by the participant website (chat, requests, staff replies); participants can read their own web rows and insert their own web chat. Finally `tt_messaging` gets `DELETE` on `trial.telemetry_spans` for the nightly retention prune. Grants and RLS for these live in the same file rather than in `02_roles_rls.sql`. Because `01_schema.sql` already created the channel CHECK, `06` drops and re-adds it, so run `06` once, after `05`.

## Rules every service must follow

**1. Corrections are new rows, never updates.** To change a clinical record, insert a new row with `supersedes_event_id = <old event_id>` and a `change_reason`. The database enforces this:
- the new row must have the same `participant_id` and `record_type` as the old one
- a row can be superseded only once (if two writers race, the second gets a unique violation: re-read and retry)

Status changes work the same way. For example, the PI assessing an AE inserts a new AE row.

**2. Read through `trial.current_events`** when you want the latest version of each record. It hides every row that has been superseded. Read `trial.clinical_events` directly only when you want the full history.

**3. Dedupe inbound messages on `provider_message_id`** (WhatsApp `wamid` or SES message ID):
```sql
INSERT INTO trial.communications (...) VALUES (...)
ON CONFLICT (provider_message_id) DO NOTHING
RETURNING comm_id;      -- no row back = already processed, stop
```

**4. The reminder row is the source of truth**, not the EventBridge schedule. The reminder Lambda must skip when `status <> 'active'`.

## Which database user to connect as

Each service gets **only its own secret** in IAM (`secretsmanager:GetSecretValue` on that one ARN, plus `rds-data:*` on the cluster).

| Service | Secret | Can see | Can write |
|---|---|---|---|
| Agent tool Lambdas | `tt_agent` | **one participant only**; no name, email, phone or address | insert clinical events (not the staff assessment columns) and communications; reminders are read-only |
| Router, reminder, email parser | `tt_messaging` | all participants (lookup by phone), but only `participant_id`, phone, language, timezone, opt-in, site, status | insert events and messages; update `delivery_status`/attachments, reminder counters/status, and `whatsapp_opt_in` (STOP) |
| Staff portal API | `tt_portal` | **only participants at the user's sites**, all columns | insert events, messages and reminders; update participants and reminders |
| Participant website API | `tt_participant` | **only their own** profile, visits, reminders, symptoms, doses and consent Qs (no AEs, no staff columns) | update own contact details; insert `VISIT_CHANGE` requests and `participant_request` messages |

Staff-only AE columns (`ae_term`, `severity_grade`, `serious`, `relatedness`, `expected`, `action_taken`, `outcome`, `staff_notes`) can be written only through `tt_portal`.

## Session context (required for agent, portal and participant)

Row-level security reads a session setting to decide which rows you see:

| User | Set this | From |
|---|---|---|
| `tt_agent`, `tt_participant` | `app.participant_id` = `'P-0142'` | the router's invocation state / the Cognito token. **Never from the model's tool arguments** |
| `tt_portal` | `app.site_ids` = `'RIVERSIDE,LAKEVIEW'` | the staff user's Cognito claims |

If it isn't set, you see **zero rows** (fail closed). `tt_messaging` doesn't need one.

The setting lasts only for **one transaction**, and a plain Data API `execute_statement` call commits on its own. So always open a transaction, set the context, and run your queries in that same transaction:

```python
import boto3, json, os
rds = boto3.client("rds-data")
DB = dict(resourceArn=os.environ["CLUSTER_ARN"], secretArn=os.environ["SECRET_ARN"], database="trialtalk")

def run(statements, *, participant_id=None, site_ids=None):
    """statements: list of (sql, params_dict). Returns a list of row lists."""
    tx = rds.begin_transaction(**DB)["transactionId"]
    try:
        for key, val in (("app.participant_id", participant_id),
                         ("app.site_ids", ",".join(site_ids) if site_ids else None)):
            if val:
                rds.execute_statement(**DB, transactionId=tx, sql=f"SELECT set_config('{key}', :v, true)",
                                      parameters=[{"name": "v", "value": {"stringValue": val}}])
        out = []
        for sql, params in statements:
            r = rds.execute_statement(**DB, transactionId=tx, sql=sql, formatRecordsAs="JSON",
                parameters=[{"name": k, "value": {"isNull": True} if v is None else {"stringValue": str(v)}}
                            for k, v in params.items()])
            out.append(json.loads(r.get("formattedRecords") or "[]"))
        rds.commit_transaction(resourceArn=DB["resourceArn"], secretArn=DB["secretArn"], transactionId=tx)
        return out
    except Exception:
        rds.rollback_transaction(resourceArn=DB["resourceArn"], secretArn=DB["secretArn"], transactionId=tx)
        raise

# e.g. the agent's log_symptom tool
run([("""INSERT INTO trial.clinical_events (participant_id, record_type, source, source_comm_id,
           verbatim_text, pain_score_band, characteristics, created_by)
         VALUES (:pid, 'SYMPTOM', 'whatsapp', :comm::uuid, :text, :band, :chars::text[], 'agent')
         RETURNING event_id""",
      {"pid": "P-0142", "comm": comm_id, "text": "rash near the injection spot", "band": "4-6",
       "chars": "{itchy,not_spreading}"})],
    participant_id="P-0142")
```

## Data API tips

- **Pass every parameter as a string and cast in SQL:** `:id::uuid`, `:ts::timestamptz`, `:d::jsonb`, `:tags::text[]` (send arrays as `'{a,b}'`). The Data API doesn't accept arrays as parameters, and its type hints are fiddly.
- **Use `formatRecordsAs="JSON"`** to get rows as dicts.
- **Cold start:** if the cluster is scaled to 0 ACU, the first call raises `DatabaseResumingException`. Retry with backoff (it takes about 15 s).
- **`created_by` is required** on clinical events. Use `agent`, `reminder-lambda`, `staff:<email>` or `participant:<id>`.

## Running the scripts

The RDS **Query editor can't run these files**: it splits on every `;`, including those inside comments and function bodies. Use `run_sql.py`:

```bash
pip install boto3 "botocore[crt]"      # the crt extra is needed for `aws login` profiles
python3 run_sql.py --profile <your-profile> \
  --cluster-arn <cluster ARN> --secret-arn <master secret ARN> \
  01_schema.sql 02_roles_rls.sql 03_seed.sql 04_db_tests.sql 05_telemetry.sql 06_alerts_web.sql
```

- It sends one statement at a time and stops at the first failure.
- Before running `02_roles_rls.sql` on a **new** cluster, replace the `CHANGE_ME` passwords and create matching secrets. Don't commit the real passwords.
- To reset the data, run `99_teardown.sql` first. Teardown also drops the login users, so the passwords in `02` must still match the existing secrets.
- For a quick ad-hoc query, the Query editor (or `aws rds-data execute-statement`) is fine.

## Test data

Fixed IDs from `03_seed.sql`, useful in Lambda test events:

| ID | What |
|---|---|
| `P-0142` | Maria Lopez, RIVERSIDE, enrolled, Spanish, phone `+17325550142` |
| `P-0143` | Sam Okafor, RIVERSIDE, prospect |
| `P-0201` | Lee Chen, LAKEVIEW, enrolled (use to test site and participant isolation) |
| `e0000000-0000-4000-8000-000000000001` | P-0142's WK04 visit |
| `…000000000002` → `…000000000003` | rash SYMPTOM, then its second version with the photo |
| `…000000000004` | AE escalating that symptom (`status = 'reported'`) |
| `…000000000005`–`…000000000008` | P-0142's last 4 evening doses (1 taken, 3 no response) |
| `a0000000-0000-4000-8000-000000000001` | P-0142's evening dose reminder (recurring) |
| `a0000000-0000-4000-8000-000000000002` | P-0142's 24h visit reminder (one-time) |
| `c0000000-0000-4000-8000-000000000001` | the inbound WhatsApp message that produced the symptom |
| `c0000000-0000-4000-8000-000000000004` | Dr. Patel's email reply to the AE report |
