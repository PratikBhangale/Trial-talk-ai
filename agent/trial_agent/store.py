"""Participant data from Aurora (RDS Data API), as the agent's restricted login (tt_agent).
Row-level security: every read is scoped to one participant via app.participant_id."""

import json
import os
import time

import boto3

CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
SECRET_ARN = os.environ["DB_SECRET_ARN"]      # tt_agent login, never the master secret
DB_NAME = os.environ.get("DB_NAME", "trialtalk")
rds = boto3.client("rds-data", region_name="us-east-1")
DB = dict(resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN)


def _read(pid: str, statements: list[str]) -> list[list[dict]]:
    """Run reads in one transaction scoped to this participant. Retries once if the DB is waking up."""
    for attempt in range(3):
        try:
            tx = rds.begin_transaction(**DB, database=DB_NAME)["transactionId"]
            break
        except rds.exceptions.DatabaseResumingException:
            time.sleep(10)  # cluster scaled to zero; it takes ~15s to wake
    else:
        raise RuntimeError("Database did not wake up in time")

    param = [{"name": "pid", "value": {"stringValue": pid}}]
    try:
        rds.execute_statement(**DB, database=DB_NAME, transactionId=tx,
                              sql="SELECT set_config('app.participant_id', :pid, true)", parameters=param)
        results = []
        for sql in statements:
            r = rds.execute_statement(**DB, database=DB_NAME, transactionId=tx, sql=sql,
                                      parameters=param, formatRecordsAs="JSON")
            results.append(json.loads(r.get("formattedRecords") or "[]"))
        rds.commit_transaction(**DB, transactionId=tx)
        return results
    except Exception:
        rds.rollback_transaction(**DB, transactionId=tx)
        raise


def get_profile(pid: str) -> dict | None:
    """The participant's non-personal profile plus their active dose reminders, or None."""
    participant, reminders = _read(pid, [
        """SELECT participant_id, study_id, site_id, status, preferred_language, timezone
           FROM pii.participants WHERE participant_id = :pid""",
        """SELECT label, cron_expr, timezone, template_params
           FROM trial.reminders
           WHERE participant_id = :pid AND reminder_type = 'dose' AND status = 'active'""",
    ])
    if not participant:
        return None
    profile = participant[0]
    profile["dose_reminders"] = reminders
    return profile