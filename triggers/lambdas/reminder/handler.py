"""tt-reminder: EventBridge Scheduler -> this Lambda. Build plan flow D. No agent involved.

Input:
  {"reminder_id": "..."}                                   scheduled send (mode "send")
  {"reminder_id": "...", "manual": true, "requested_by": "staff:..."}
                                                           staff portal "Send now" (also for paused / draft)
  {"reminder_id": "...", "mode": "snooze"}                 "Remind me in 30 min" re-send
  {"reminder_id": "...", "mode": "follow_up", "dose_event_id": "..."}
                                                           no reply after follow_up_after_min?

Send: approved WhatsApp template with the reminder's params; button payloads carry
"<ACTION>:<reminder_id>" so tt-router can match the tap to this reminder. If the template is
rejected (e.g. still pending with Meta), falls back to the same text as reply buttons, which
WhatsApp only delivers inside the 24-hour window. Then logs the message, a DOSE row
(response 'no_response') and the reminder counters, and schedules the follow-up check.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

import schedules
import telemetry
import wa
from db import Tx

ses = boto3.client("sesv2")
COORDINATOR_EMAIL = os.environ.get("COORDINATOR_EMAIL")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")

BUTTON_ACTIONS = {"taken": "TAKEN", "skipped": "SKIPPED"}
# Not offered on reminders, even if the reminder row lists it.
HIDDEN_BUTTONS = {"remind me in 30 min"}


def handler(event, context):
    try:
        mode = event.get("mode", "send")
        with telemetry.span("reminder.send", **{"trialtalk.reminder.mode": str(mode)}) as span:
            if mode == "follow_up":
                result = follow_up(event)
            else:
                result = send(event["reminder_id"], snooze=mode == "snooze", manual=bool(event.get("manual")))
            span.set_attribute("trialtalk.reminder.result", str(result.get("result")))
        print(json.dumps({**result, "requested_by": event.get("requested_by")}))
        return result
    finally:
        telemetry.flush()


def _tag(**attrs):
    """Set attributes on the active span (reminder.send). No-op without telemetry; never raises."""
    if not telemetry.enabled():
        return
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
    except Exception:
        pass


def send(reminder_id, snooze=False, manual=False):
    with Tx() as tx:
        r = tx.one("""
            SELECT r.reminder_type, r.label, r.status, r.schedule_kind, r.template_name, r.template_language,
                   r.template_params::text AS template_params, array_to_string(r.buttons, '|') AS buttons,
                   r.ends_on::text AS ends_on, r.follow_up_after_min,
                   p.participant_id, p.phone_e164, p.whatsapp_opt_in, p.timezone
            FROM trial.reminders r JOIN pii.participants p USING (participant_id)
            WHERE r.reminder_id = :id::uuid""", id=reminder_id)
    if not r:
        return {"result": "skipped", "why": "reminder not found"}
    _tag(**{"trialtalk.reminder.type": r["reminder_type"]})
    # The row, not the schedule, is the source of truth (guards against stale schedules).
    # Staff can "Send now" a paused or draft reminder; schedules only fire active ones.
    allowed = ("active", "completed") if snooze else ("active", "paused", "draft") if manual else ("active",)
    if r["status"] not in allowed:
        return {"result": "skipped", "why": f"status is {r['status']}"}
    if not r["whatsapp_opt_in"]:
        return {"result": "skipped", "why": "participant opted out"}
    today = datetime.now(ZoneInfo(r["timezone"])).date().isoformat()
    if r["ends_on"] and today > r["ends_on"]:
        return {"result": "skipped", "why": "past ends_on"}

    params = json.loads(r["template_params"])
    body_params = [params[k] for k in sorted(params, key=lambda k: int(k) if k.isdigit() else k)]
    titles = [b for b in (r["buttons"] or "").split("|") if b and b.lower() not in HIDDEN_BUTTONS]
    buttons = [(f"{BUTTON_ACTIONS.get(b.lower(), b.upper())}:{reminder_id}", b) for b in titles]

    msg_type, status, msg_id = "template", "sent", None
    try:
        # Meta rejects an unapproved template only after accepting the send, so check first.
        if not wa.template_approved(r["template_name"], r["template_language"]):
            raise LookupError(f"template {r['template_name']}/{r['template_language']} is not approved")
        msg_id = wa.send_template(r["phone_e164"], r["template_name"], r["template_language"], body_params,
                                  [i for i, _ in buttons])
    except (ClientError, LookupError) as e:
        print(f"template send not possible ({e}); trying reply buttons")
        text = f"Reminder: {r['label']}." + (" Please tap a button below to let your study team know." if buttons else "")
        msg_type = "interactive" if buttons else "text"
        try:
            msg_id = wa.send_buttons(r["phone_e164"], text, buttons) if buttons else wa.send_text(r["phone_e164"], text)
        except ClientError as e2:
            status = "failed"
            print(f"fallback failed too: {e2.response['Error']}")
    _tag(**{"trialtalk.msg_type": msg_type, "trialtalk.reminder.delivery_status": status})

    with Tx() as tx:
        comm = tx.one("""
            INSERT INTO trial.communications (participant_id, channel, direction, msg_type, template_name,
              to_addr, provider_message_id, delivery_status, reminder_id)
            VALUES (:pid, 'whatsapp', 'outbound', :mt, :tpl, :to, :mid, :st, :rid::uuid)
            RETURNING comm_id""",
            pid=r["participant_id"], mt=msg_type, tpl=r["template_name"], to=r["phone_e164"], mid=msg_id,
            st=status, rid=reminder_id)
        if status == "failed":
            return {"result": "failed", "comm_id": comm["comm_id"]}

        dose = None
        if r["reminder_type"] == "dose":
            dose = tx.one("""
                INSERT INTO trial.clinical_events (participant_id, record_type, source, source_comm_id,
                  reminder_id, dose_label, scheduled_at, response, details, created_by)
                VALUES (:pid, 'DOSE', 'scheduler', :cid::uuid, :rid::uuid, :label, now(), 'no_response',
                  jsonb_build_object('reminder_sent_at', now(), 'snooze', :snz::boolean), 'reminder-lambda')
                RETURNING event_id""",
                pid=r["participant_id"], cid=comm["comm_id"], rid=reminder_id, label=params.get("1"),
                snz=str(snooze).lower())

        done = r["schedule_kind"] == "one_time" and not snooze and not manual
        counters = tx.one("""
            UPDATE trial.reminders SET send_count = send_count + 1, last_sent_at = now(),
              status = CASE WHEN :done::boolean THEN 'completed' ELSE status END,
              next_send_at = CASE WHEN :done::boolean THEN NULL ELSE next_send_at END
            WHERE reminder_id = :rid::uuid
            RETURNING send_count, status""", rid=reminder_id, done=str(done).lower())

    follow_up_at = None
    if dose and r["follow_up_after_min"]:
        follow_up_at = schedules.once_in(
            f"fu-{dose['event_id']}", r["follow_up_after_min"],
            {"mode": "follow_up", "reminder_id": reminder_id, "dose_event_id": dose["event_id"]})
    return {"result": "sent", "via": msg_type, "comm_id": comm["comm_id"],
            "dose_event_id": dose and dose["event_id"], "follow_up_at": follow_up_at, **counters}


def follow_up(event):
    """If the dose is still unanswered, count misses in 7 days and email the coordinator at the threshold."""
    reminder_id, dose_event_id = event["reminder_id"], event["dose_event_id"]
    with Tx() as tx:
        still_open = tx.one("""SELECT participant_id FROM trial.current_events
                               WHERE event_id = :e::uuid AND response = 'no_response'""", e=dose_event_id)
        if not still_open:
            return {"result": "answered"}
        pid = still_open["participant_id"]
        r = tx.one("""SELECT r.label, r.escalate_after_misses, p.site_id,
                        (SELECT count(*) FROM trial.current_events
                         WHERE participant_id = r.participant_id AND record_type = 'DOSE'
                           AND response IN ('skipped','no_response')
                           AND scheduled_at > now() - interval '7 days') AS misses,
                        (SELECT count(*) FROM trial.communications
                         WHERE reminder_id = r.reminder_id AND channel = 'email'
                           AND created_at > now() - interval '24 hours') AS emailed_today
                      FROM trial.reminders r JOIN pii.participants p USING (participant_id)
                      WHERE r.reminder_id = :rid::uuid""", rid=reminder_id)
    misses, limit = int(r["misses"]), r["escalate_after_misses"]
    if not limit or misses < limit:
        return {"result": "no_response", "misses_7d": misses}
    if int(r["emailed_today"]) or not (COORDINATOR_EMAIL and SENDER_EMAIL):
        return {"result": "no_response", "misses_7d": misses, "escalated": False}

    subject = f"[TT {pid}] {misses} missed doses in 7 days"
    body = (f"Participant {pid} (site {r['site_id']}) has {misses} missed or skipped doses in the last 7 days "
            f"(threshold {limit}). Latest: {r['label']}, no reply.\n\nPlease follow up with them.")
    try:
        resp = ses.send_email(FromEmailAddress=SENDER_EMAIL, Destination={"ToAddresses": [COORDINATOR_EMAIL]},
                              Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}})
    except ClientError as e:
        return {"result": "no_response", "misses_7d": misses, "escalated": False, "error": str(e)}
    with Tx() as tx:
        tx.q("""INSERT INTO trial.communications (participant_id, channel, direction, msg_type, from_addr, to_addr,
                  subject, body_text, related_event_id, reminder_id, provider_message_id, delivery_status)
                VALUES (:pid, 'email', 'outbound', 'report', :fr, :to, :sub, :body, :ev::uuid, :rid::uuid, :mid, 'sent')""",
             pid=pid, fr=SENDER_EMAIL, to=COORDINATOR_EMAIL, sub=subject, body=body, ev=dose_event_id,
             rid=reminder_id, mid=resp["MessageId"])
    return {"result": "escalated", "misses_7d": misses}
