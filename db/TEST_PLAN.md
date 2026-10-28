# TrialTalk database: build and Lambda test plan

Goal: stand up the Aurora database from the build plan, then prove that each kind of Lambda that will read and write it in the real system works through its real trigger (SQS, EventBridge Scheduler, S3, API/direct invoke). WhatsApp, Bedrock and SES are **stubbed**: the test Lambdas do only the database part of each flow.

## Files

| File | What it does |
|---|---|
| `01_schema.sql` | 4 tables, `current_events` view, indexes, integrity triggers |
| `02_roles_rls.sql` | group roles, one login user per Lambda type, column grants, RLS policies |
| `03_seed.sql` | synthetic data: 3 participants across 2 sites, the rash → AE loop, dose history, reminders |
| `04_db_tests.sql` | 19 SQL tests of permissions, RLS, append-only rules, dedupe, versioning |
| `99_teardown.sql` | drops everything so you can start over |

All five were run on Postgres 16.2 locally, and all 19 tests pass.

## Changes from the PDF's SQL (and why)

1. **RLS policies for `agent_role` and `messaging_role` added.** Once RLS is enabled on a table, any role with no matching policy sees **zero rows**. As written in the PDF, the router couldn't look up phones and the agent couldn't read anything. Agent rows are now scoped to `app.participant_id`. Messaging can see all rows, because it looks up by phone before it knows the participant; column grants still keep names and addresses away from it.
2. **`messaging_role` gets SELECT on `communications` and `clinical_events`.** You need it for `INSERT … RETURNING comm_id` (the dedupe pattern), for `UPDATE … WHERE provider_message_id = …` (status events), and for finding the DOSE row that a "Taken" tap supersedes. Without it all three fail with permission denied.
3. **Append-only rules are triggers, not just REVOKEs.** `REVOKE … FROM PUBLIC` does nothing, because PUBLIC never had those rights, and the table owner ignores grants anyway. Triggers now block UPDATE, DELETE and TRUNCATE on `clinical_events`, and DELETE on `communications` and `reminders`, even for the owner.
4. **Versioning is enforced.** `supersedes_event_id` is now a unique index, so a record can't fork into two "latest" versions. A trigger checks that the new version has the same participant and record_type.
5. **An `updated_at` trigger** on `participants` and `reminders`, so no Lambda has to remember to set it.
6. **The portal gets SELECT only on `current_events`** (the PDF also granted INSERT on the view).
7. **`pgcrypto` was dropped.** `gen_random_uuid()` is built into PG13+.

## Step 1: Build the database (AWS console)

1. **Aurora PostgreSQL**: engine **16.x**, Serverless v2, us-east-1, initial database name **`trialtalk`**, master credentials managed in Secrets Manager, KMS encryption on, **RDS Data API enabled** (Connectivity section).
   - Capacity: min 0.5 ACU for testing. You'll switch to 0 in Phase 5 to test auto-pause.
2. Open **RDS → Query editor**, connect as the master user to `trialtalk`, and run in order: `01_schema.sql`, `02_roles_rls.sql` (**replace the 4 `CHANGE_ME` passwords first**), `03_seed.sql`, `04_db_tests.sql`.
   - Pass = the last result shows **19 rows, T01–T19**. A failure stops with `FAIL Txx: …`.
   - If the query editor won't run a whole file (it sometimes struggles with `$$` blocks), use psql from CloudShell in the cluster's VPC instead (`psql -f`).
3. **Secrets Manager**: create one secret per login user, of type "Credentials for Amazon RDS database", pointing at the cluster:
   `trialtalk/db/tt_agent`, `trialtalk/db/tt_messaging`, `trialtalk/db/tt_portal`, `trialtalk/db/tt_participant`.

## Step 2: Test Lambdas

Six small Python 3.12 Lambdas. Each one gets **only its own secret**: IAM allows `rds-data:*` on the cluster ARN and `secretsmanager:GetSecretValue` on that one secret ARN. None of them need a VPC, because the Data API is an HTTPS endpoint.

| Lambda | DB user | Real trigger | What it does in the DB (stands in for) |
|---|---|---|---|
| `tt-router` | tt_messaging | SQS FIFO | dedupe-insert inbound row; phone → participant lookup; **button "Taken"** → new DOSE version (`response='taken'`, supersedes the `no_response` row); **status event** → update `delivery_status`; STOP → `whatsapp_opt_in=false` |
| `tt-reminder` | tt_messaging | EventBridge Scheduler | read reminder; skip unless `active`; insert outbound template row + DOSE `no_response` row; bump `send_count/last_sent_at/next_send_at` |
| `tt-email-parser` | tt_messaging | S3 put on `tt-mail-test/in/` | parse subject token `[TT P-0142 AE-…]`; insert `staff_reply` row with `related_event_id`, `s3_raw_key` |
| `tt-agent-tools` | tt_agent | direct invoke (later: AgentCore Gateway) | `log_symptom`, `log_ae`, `get_open_events`, `search_communications`; `participant_id` comes from the event's invocation state, never from tool args |
| `tt-portal-api` | tt_portal | direct invoke / Function URL | create VISIT, correct VISIT (supersede + reason), PI AE assessment (new AE version), create/pause reminder (append `change_log`) |
| `tt-participant-api` | tt_participant | direct invoke | read own visits and reminders; request a reschedule (VISIT_CHANGE) |

### Shared Data API helper (the one thing every Lambda must get right)

RLS reads `app.participant_id` / `app.site_ids` from a setting that only lasts for **one transaction**. A plain `execute_statement` call auto-commits on its own, so the setting is gone before the next statement runs. Always wrap the work in a transaction:

```python
import boto3, json, os
rds = boto3.client("rds-data")
DB = dict(resourceArn=os.environ["CLUSTER_ARN"], secretArn=os.environ["SECRET_ARN"], database="trialtalk")

def run(statements, *, participant_id=None, site_ids=None):
    """statements: list of (sql, params_dict). Returns list of record lists."""
    tx = rds.begin_transaction(**DB)["transactionId"]
    try:
        if participant_id:
            rds.execute_statement(**DB, transactionId=tx,
                sql="SELECT set_config('app.participant_id', :v, true)",
                parameters=[{"name": "v", "value": {"stringValue": participant_id}}])
        if site_ids:
            rds.execute_statement(**DB, transactionId=tx,
                sql="SELECT set_config('app.site_ids', :v, true)",
                parameters=[{"name": "v", "value": {"stringValue": ",".join(site_ids)}}])
        out = []
        for sql, params in statements:
            r = rds.execute_statement(**DB, transactionId=tx, sql=sql, formatRecordsAs="JSON",
                parameters=[{"name": k, "value": {"isNull": True} if v is None else {"stringValue": str(v)}}
                            for k, v in params.items()])
            out.append(json.loads(r.get("formattedRecords", "[]")))
        rds.commit_transaction(resourceArn=DB["resourceArn"], secretArn=DB["secretArn"], transactionId=tx)
        return out
    except Exception:
        rds.rollback_transaction(resourceArn=DB["resourceArn"], secretArn=DB["secretArn"], transactionId=tx)
        raise
```

Data API notes:
- Pass everything as strings and **cast in SQL**: `:id::uuid`, `:d::jsonb`, `:ts::timestamptz`, `:chars::text[]` (send `'{itchy,not_spreading}'`). This avoids the Data API's `typeHint` quirks, and arrays aren't supported as parameters at all.
- Dedupe insert: `INSERT … ON CONFLICT (provider_message_id) DO NOTHING RETURNING comm_id`. No row back means it was already processed, so stop.
- Catch `DatabaseResumingException` (cluster waking from 0 ACU) and retry with backoff.

### Test events (use the seed IDs)

```jsonc
// tt-router: SQS message body, simplified Meta webhook "value". Confirm against a real
// End User Messaging Social event once the WABA is connected.
{"messages":[{"from":"17325550142","id":"wamid.TEST-0001","type":"text","text":{"body":"knee hurts today"}}]}
{"messages":[{"from":"17325550142","id":"wamid.TEST-0002","type":"button",
              "button":{"payload":"Taken","reminder_id":"a0000000-0000-4000-8000-000000000001"}}]}
{"statuses":[{"id":"wamid.SEED002","status":"read"}]}

// tt-reminder (Scheduler input)
{"reminder_id":"a0000000-0000-4000-8000-000000000001"}

// tt-agent-tools
{"tool":"log_symptom","invocation_state":{"participant_id":"P-0142","comm_id":"c0000000-0000-4000-8000-000000000001"},
 "args":{"verbatim_text":"rash is spreading now","pain_score_band":"4-6",
         "supersedes_event_id":"e0000000-0000-4000-8000-000000000003"}}

// tt-portal-api
{"action":"ae_assessment","claims":{"email":"dr.patel@riverside-clinic.org","role":"pi","site_ids":["RIVERSIDE"]},
 "event_id":"e0000000-0000-4000-8000-000000000004","ae_term":"Injection site rash","severity_grade":1,
 "serious":false,"relatedness":"probable","change_reason":"PI assessment"}
```

## Step 3: Test phases

Check every test with SQL in the query editor (as master). Useful checks are at the end.

### Phase 1: Direct invoke, happy path (console "Test" button)
| # | Invoke | Expect in DB |
|---|---|---|
| 1.1 | router: text message | 1 new inbound `communications` row for P-0142 |
| 1.2 | router: same event again | still 1 row (dedupe); Lambda returns "duplicate" |
| 1.3 | router: status `read` for `wamid.SEED002` | `delivery_status='read'` |
| 1.4 | reminder: a0…01 | +1 outbound template row with `reminder_id`, +1 DOSE `no_response`, `send_count` +1, `updated_at` bumped |
| 1.5 | router: "Taken" button | new DOSE row `response='taken'` superseding the latest `no_response`; `current_events` shows only the new one |
| 1.6 | reminder: after pausing a0…01 via portal-api | Lambda skips; nothing written |
| 1.7 | agent-tools: log_symptom (supersede e0…03) | new SYMPTOM version, `change_reason` set, `created_by='agent'` |
| 1.8 | portal-api: AE assessment | new AE version with assessment columns; e0…04 drops out of `current_events` |
| 1.9 | participant-api: request reschedule of e0…01 | VISIT_CHANGE row, `created_by='participant:P-0142'` |

### Phase 2: Real triggers
| # | Trigger | How | Expect |
|---|---|---|---|
| 2.1 | **SQS FIFO → router** | `aws sqs send-message --queue-url <q> --message-group-id +17325550142 --message-deduplication-id <uuid> --message-body file://text.json` | same as 1.1; queue drains |
| 2.2 | **EventBridge Scheduler → reminder** (one-time) | `aws scheduler create-schedule --name rem-a0…01-test --schedule-expression "at(<now+3min>)" --schedule-expression-timezone America/Chicago --flexible-time-window Mode=OFF --action-after-completion DELETE --target file://target.json` (input `{"reminder_id": …}`) | fires once at the local time; rows as in 1.4; schedule deletes itself |
| 2.3 | **Scheduler recurring** | `rate(5 minutes)` for 15 min, then delete it | `send_count` +3, 3 DOSE rows |
| 2.4 | **S3 → parser** | `aws s3 cp pi_reply.eml s3://tt-mail-test/in/` (subject `Re: [TT P-0142 AE-7f3a] …`) | `staff_reply` row, `related_event_id = e0…04`, `s3_raw_key` set |
| 2.5 | **Portal via Function URL** (optional) | curl with the JSON body | same as 1.8 |

On PowerShell, pass JSON with `file://` rather than inline. The quoting is painful.

### Phase 3: Reliability (what goes wrong in production)
| # | Test | Expect |
|---|---|---|
| 3.1 | **Redelivery**: send the same `wamid` twice with different SQS dedup IDs, 10 min apart (past FIFO's 5-min window) | exactly 1 row: the DB unique key catches what SQS doesn't |
| 3.2 | **Ordering**: 20 messages for one phone (one group) plus 20 spread over 4 other phones, sent fast | per participant, `created_at` order matches send order |
| 3.3 | **Burst**: router reserved concurrency = 2, send 200 messages | no Data API throttling or Aurora errors; queue backs up, then drains |
| 3.4 | **Poison message**: malformed body, `maxReceiveCount=3` | lands in the DLQ; nothing written; CloudWatch alarm on DLQ depth fires |
| 3.5 | **Concurrent correction**: invoke two log_symptom supersedes of the same event at once | one succeeds, one fails with a unique violation (version chain can't fork); the tool should re-read and retry |
| 3.6 | **Stale schedule**: cancel reminder in DB but leave its schedule | reminder Lambda skips (status guard) |

### Phase 4: Security through the Lambdas (not just SQL)
| # | Test | Expect |
|---|---|---|
| 4.1 | agent-tools with `participant_id=P-0142` but args targeting P-0201's event | error / empty; no row written |
| 4.2 | agent-tools: change the helper to skip `set_config` | every read returns empty (fail closed) |
| 4.3 | portal-api with `site_ids=["LAKEVIEW"]` reading P-0142 | not found |
| 4.4 | portal-api tries `UPDATE clinical_events` | permission denied |
| 4.5 | Point tt-agent-tools at the `tt_messaging` secret | `AccessDeniedException` from Secrets Manager (IAM scoping works) |
| 4.6 | CloudTrail | Data API calls show which Lambda role ran them |

### Phase 5: Cold start
Set min ACU to 0, wait for the cluster to pause (default ~5 min idle), then send 2.1. Expect the first call to hit `DatabaseResumingException`, the retry to succeed within ~15 s, and the message to be processed once. That latency decides whether the demo runs at 0 or 0.5 ACU.

### Phase 6: End-to-end chains (the scenarios, DB-only)
- **Scenario 5 (adherence):** schedule fires → DOSE `no_response` → "Taken" via SQS → missed-doses query drops by one.
- **Scenario 4 (AE):** router text → agent-tools `log_symptom` → `log_ae` → (stub "report" comm row) → S3 PI reply → parser → portal AE assessment. Then run the coordinator-digest join from the PDF and check the timeline is complete.

## Checks

```sql
-- latest writes, newest first
SELECT created_at, participant_id, record_type, status, response, created_by, supersedes_event_id
FROM trial.clinical_events ORDER BY created_at DESC LIMIT 20;

SELECT created_at, participant_id, direction, msg_type, provider_message_id, delivery_status, reminder_id
FROM trial.communications ORDER BY created_at DESC LIMIT 20;

SELECT label, status, send_count, last_sent_at, next_send_at, updated_at FROM trial.reminders;

-- dedupe holds: should return nothing
SELECT provider_message_id, count(*) FROM trial.communications
WHERE provider_message_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;

-- version chains never fork: should return nothing
SELECT supersedes_event_id, count(*) FROM trial.clinical_events
WHERE supersedes_event_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;
```

## Open questions to settle while testing
- **SNS → SQS FIFO:** a FIFO queue can only subscribe to a **FIFO** SNS topic. Check that End User Messaging Social can publish to one. If it can't, you need a small bridge (a Lambda or EventBridge Pipe) that sets `MessageGroupId`. Phase 2.1 bypasses SNS, so this doesn't block the DB tests.
- **"DB fires a Lambda"** (e.g., the AE banner in the portal): Aurora has no change stream like DynamoDB's. The simplest option is for the Lambda that writes the AE row to publish to AppSync itself. A Postgres trigger calling `aws_lambda.invoke` also works, but needs an IAM role on the cluster and a network path to Lambda. Recommend the first.
- **Digests** (`communications.participant_id IS NULL`) are invisible to the portal under RLS. That's fine if the portal never lists digests.
- **`reminders.change_log`** is appended by the portal Lambda, not a trigger. Test 1.6 should check that a pause adds a `{at, by, field, old, new, reason}` entry.
