"""Reminder Lambda: EventBridge Scheduler (or staff 'Send now') -> approved WhatsApp template. No agent.

Input: {"reminder_id": "..."} (+ "manual": true from Send now).
Runs as messaging_api (member of messaging_role).
"""

import json
import os
from datetime import datetime, timezone

import boto3

from common import db, scheduling, whatsapp

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

_agentcore = None


def agentcore():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore")
    return _agentcore


def handler(event, context):
    rid = event.get("reminder_id")
    manual = bool(event.get("manual"))
    if not rid:
        return {"sent": False, "reason": "missing reminder_id"}

    with db.tx() as t:
        r = t.one(
            "SELECT r.reminder_id, r.participant_id, r.reminder_type, r.label, r.template_name, r.template_language,"
            "  r.template_params, to_json(r.buttons) AS buttons, r.schedule_kind, r.cron_expr, r.send_at, r.timezone,"
            "  r.starts_on, r.ends_on, r.status, p.phone_e164, p.whatsapp_opt_in, p.status AS participant_status "
            "FROM trial.reminders r JOIN pii.participants p USING (participant_id) "
            "WHERE r.reminder_id = :rid::uuid", rid=rid)
        if not r:
            return {"sent": False, "reason": "not found"}

        # Guards against stale schedules: the row is the source of truth.
        allowed = ("active", "paused", "draft") if manual else ("active",)
        if r["status"] not in allowed:
            return {"sent": False, "reason": f"reminder is {r['status']}"}
        if not r["whatsapp_opt_in"] or r["participant_status"] == "withdrawn":
            return {"sent": False, "reason": "participant not reachable on WhatsApp"}

        params = r["template_params"] or {}
        buttons = r["buttons"] or []
        wamid = whatsapp.send_template(
            to_e164=r["phone_e164"], name=r["template_name"], language=r["template_language"],
            body_params=whatsapp.ordered_params(params),
            quick_reply_payloads=[f"rem:{rid}:{b}" for b in buttons],  # router matches taps back to this reminder
        )
        now = datetime.now(timezone.utc)

        t.execute(
            "INSERT INTO trial.communications (participant_id, channel, direction, msg_type, template_name, to_addr,"
            "  body_text, provider_message_id, delivery_status, reminder_id) "
            "VALUES (:pid, 'whatsapp', 'outbound', 'template', :tpl, :to, :body, :wamid, 'sent', :rid::uuid)",
            pid=r["participant_id"], tpl=r["template_name"], to=r["phone_e164"],
            body=f"{r['label']} ({', '.join(whatsapp.ordered_params(params))})", wamid=wamid, rid=rid)

        if r["reminder_type"] == "dose":
            t.execute(
                "INSERT INTO trial.clinical_events (participant_id, record_type, status, source, dose_label,"
                "  scheduled_at, response, reminder_id, details, created_by) "
                "VALUES (:pid, 'DOSE', 'pending', 'scheduler', :label, :at::timestamptz, 'no_response', :rid::uuid,"
                "  :details::jsonb, 'reminder-lambda')",
                pid=r["participant_id"], label=params.get("1"), at=now.isoformat(), rid=rid,
                details={"reminder_sent_at": now.isoformat(), "manual": manual})

        nxt = scheduling.next_send_at(r, after=now)
        done = r["schedule_kind"] == "one_time" and not manual
        t.execute(
            "UPDATE trial.reminders SET last_sent_at = :now::timestamptz, send_count = send_count + 1,"
            "  next_send_at = :nxt::timestamptz, status = CASE WHEN :done THEN 'completed' ELSE status END "
            "WHERE reminder_id = :rid::uuid",
            now=now.isoformat(), nxt=nxt, done=done, rid=rid)

    _remember(r, now)
    return {"sent": True, "reminder_id": rid, "provider_message_id": wamid}


def _remember(r: dict, now: datetime) -> None:
    """Let the agent know a reminder went out (it sees reminders only through Memory)."""
    if not MEMORY_ID:
        return
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(r["timezone"]))
    try:
        agentcore().create_event(
            memoryId=MEMORY_ID,
            actorId=r["participant_id"],
            sessionId=f"wa-{r['participant_id']}-{local:%Y-%m-%d}",
            eventTimestamp=now,
            payload=[{"conversational": {
                "role": "ASSISTANT",
                "content": {"text": f"Sent {r['label'].lower()} reminder at {local:%I:%M %p}".replace(" 0", " ")},
            }}],
        )
    except Exception as e:
        print(f"Memory CreateEvent failed: {e}")
