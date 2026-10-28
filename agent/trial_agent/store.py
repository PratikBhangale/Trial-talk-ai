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

# Record types the agent may look up, and the columns it may see (no staff assessment fields)
HISTORY_TYPES = ("DOSE", "SYMPTOM", "VISIT", "VISIT_CHANGE", "CONSENT_Q")
HISTORY_COLUMNS = """record_type, occurred_at, status, source, summary, verbatim_text, severity_hint,
                     body_location, onset_date, dose_label, scheduled_at, response,
                     visit_code, window_start, window_end, flag_for_follow_up"""


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
        """SELECT label, cron_expr, template_params
           FROM trial.reminders
           WHERE participant_id = :pid AND reminder_type = 'dose' AND status = 'active'""",
    ])
    if not participant:
        return None
    profile = participant[0]
    profile["dose_reminders"] = reminders
    return profile





def get_clinical_history(pid: str, record_type: str = "all", days: int = 14) -> list[dict]:
    """This participant's current clinical records (latest versions only), newest first."""
    days = max(1, min(int(days), 90))
    rt = record_type.upper()
    types = HISTORY_TYPES if rt == "ALL" else tuple(t for t in HISTORY_TYPES if t == rt)
    if not types:
        return []
    type_list = ", ".join(f"'{t}'" for t in types)   # safe: values come from the fixed list above
    (rows,) = _read(pid, [
        f"""SELECT {HISTORY_COLUMNS}
            FROM trial.current_events
            WHERE participant_id = :pid
              AND record_type IN ({type_list})
              AND occurred_at > now() - interval '{days} days'
            ORDER BY occurred_at DESC
            LIMIT 50"""
    ])
    return rows


def _write(pid: str, sql: str, **params) -> list[dict]:
    """Run one write in a transaction scoped to this participant (row-level security checks the row)."""
    for attempt in range(3):
        try:
            tx = rds.begin_transaction(**DB, database=DB_NAME)["transactionId"]
            break
        except rds.exceptions.DatabaseResumingException:
            time.sleep(10)
    else:
        raise RuntimeError("Database did not wake up in time")

    def p(name, value):
        return {"name": name, "value": {"isNull": True} if value is None else {"stringValue": str(value)}}

    try:
        rds.execute_statement(**DB, database=DB_NAME, transactionId=tx,
                              sql="SELECT set_config('app.participant_id', :pid, true)",
                              parameters=[p("pid", pid)])
        r = rds.execute_statement(**DB, database=DB_NAME, transactionId=tx, sql=sql, formatRecordsAs="JSON",
                                  parameters=[p("pid", pid)] + [p(k, v) for k, v in params.items()])
        rds.commit_transaction(**DB, transactionId=tx)
        return json.loads(r.get("formattedRecords") or "[]")
    except Exception:
        rds.rollback_transaction(**DB, transactionId=tx)
        raise


def record_symptom(pid: str, participant_words: str, summary: str, severity_hint: str) -> str:
    """Add a SYMPTOM record flagged for follow-up. Returns its event_id."""
    rows = _write(pid, """
        INSERT INTO trial.clinical_events (participant_id, record_type, status, source,
          verbatim_text, summary, severity_hint, flag_for_follow_up, created_by)
        VALUES (:pid, 'SYMPTOM', 'escalated', 'whatsapp', :words, :summary, :severity, true, 'agent')
        RETURNING event_id""",
        words=participant_words, summary=summary, severity=severity_hint)
    return rows[0]["event_id"]


def log_email(pid: str, sender: str, recipient: str, subject: str, body: str,
              event_id: str | None, message_id: str | None, status: str) -> None:
    """Record an outbound alert email in the communications audit trail."""
    _write(pid, """
        INSERT INTO trial.communications (participant_id, channel, direction, msg_type, from_addr, to_addr,
          subject, body_text, related_event_id, provider_message_id, delivery_status)
        VALUES (:pid, 'email', 'outbound', 'report', :sender, :recipient, :subject, :body,
          :event_id::uuid, :message_id, :status)""",
        sender=sender, recipient=recipient, subject=subject, body=body,
        event_id=event_id, message_id=message_id, status=status)