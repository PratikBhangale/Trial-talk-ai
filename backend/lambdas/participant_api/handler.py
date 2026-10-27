"""Participant website API. Routes under /me, behind the participant user pool JWT authorizer.

Database access runs as participant_api (member of participant_role); RLS limits every row to
app.participant_id, which comes from the token's custom:participant_id, never from the request.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3

from common import db, telemetry
from common.auth import Participant, participant_claims
from common.http import HttpError, Router, json_body, query_params

ALERT_FROM = os.environ.get("ALERT_FROM", "")


def _recipients_by_site(raw: str) -> dict[str, list[str]]:
    """ALERT_TO is JSON, site -> addresses: {"RIVERSIDE": "a@x.org,b@x.org", "LAKEVIEW": "c@y.org"}.
    The key "*" sends every site's alerts to those addresses; use it only when one team covers all sites."""
    try:
        sites = json.loads(raw) if raw.strip() else {}
    except ValueError:
        sites = None
    if not isinstance(sites, dict):
        print("ALERT_TO must be a JSON object of site -> addresses; emergency emails are off")
        return {}
    return {site: [a.strip() for a in (v if isinstance(v, list) else str(v).split(",")) if a.strip()]
            for site, v in sites.items()}


ALERT_TO = _recipients_by_site(os.environ.get("ALERT_TO", ""))

router = Router()
_ses = None


def ses():
    global _ses
    if _ses is None:
        _ses = boto3.client("sesv2")
    return _ses


def handler(event, context):
    return router.dispatch(event, participant_claims)


def scoped(me: Participant):
    return db.tx(participant_id=me.participant_id)


# ------------------------------------------------------------------ profile

PROFILE_COLS = """participant_id, full_name, email, phone_e164, date_of_birth, address_line1, address_line2, city,
  state, postal_code, country, preferred_language, timezone, study_id, site_id, status, whatsapp_opt_in"""
SELF_EDITABLE = {"email", "address_line1", "address_line2", "city", "state", "postal_code", "country",
                 "preferred_language", "timezone"}
REQUEST_KINDS = {"profile_change", "reminder_change", "reminder_pause", "withdrawal", "whatsapp_opt_out",
                 "contact_team", "history_correction"}


def _profile(t: db.Tx, pid: str) -> dict:
    row = t.one(f"SELECT {PROFILE_COLS} FROM pii.participants WHERE participant_id = :pid", pid=pid)
    if not row:
        raise HttpError(404, "Participant not found")
    return row


@router.route("GET", "/me")
def get_me(event, me: Participant):
    with scoped(me) as t:
        return 200, {"participant": _profile(t, me.participant_id)}


@router.route("PATCH", "/me")
def update_me(event, me: Participant):
    body = json_body(event)
    blocked = set(body) - SELF_EDITABLE
    if blocked:
        raise HttpError(400, f"These can only be changed by request to the study team: {sorted(blocked)}")
    if not body:
        raise HttpError(400, "Nothing to update")
    if "timezone" in body:
        try:
            ZoneInfo(body["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HttpError(400, "Unknown time zone")
    with scoped(me) as t:
        sets = ", ".join(f"{k} = :{k}" for k in body)
        t.execute(f"UPDATE pii.participants SET {sets}, updated_at = now() WHERE participant_id = :pid",
                  pid=me.participant_id, **body)
        return 200, {"participant": _profile(t, me.participant_id)}


# ------------------------------------------------------------------ reminders, dosing, history

@router.route("GET", "/me/reminders")
def my_reminders(event, me: Participant):
    """Active reminders and dose schedule. Staff own reminders; participants can only request changes.

    db/ does not let participant_role read template_params, so the dose instructions are not shown here.
    """
    with scoped(me) as t:
        rows = t.query(
            "SELECT reminder_id, label, reminder_type, cron_expr, send_at, timezone, starts_on,"
            "  ends_on, status, next_send_at FROM trial.reminders "
            "WHERE participant_id = :pid AND status IN ('active','paused') ORDER BY next_send_at NULLS LAST",
            pid=me.participant_id)
    for r in rows:
        r["schedule"] = describe_schedule(r)
    doses = [r for r in rows if r["reminder_type"] == "dose"]
    return 200, {"reminders": rows, "dose_schedule": doses}


def describe_schedule(r: dict) -> str:
    """Human-readable 'Every day at 8:00 PM' / 'Mon, Thu at 9:00 AM' / 'Once on 2026-10-30 9:00 AM'."""
    from common.scheduling import _SIMPLE_CRON, _as_utc
    if r.get("send_at"):
        local = _as_utc(r["send_at"]).astimezone(ZoneInfo(r["timezone"]))
        return f"Once on {local:%Y-%m-%d} at {local:%I:%M %p}".replace(" 0", " ")
    m = _SIMPLE_CRON.match(r.get("cron_expr") or "")
    if not m:
        return "Custom schedule"
    minute, hour, _, dow = m.groups()
    clock = datetime(2000, 1, 1, int(hour), int(minute)).strftime("%I:%M %p").lstrip("0")
    days = "Every day" if dow in ("?", "*") else ", ".join(d.title() for d in dow.split(","))
    return f"{days} at {clock}"


@router.route("GET", "/me/history")
def my_history(event, me: Participant):
    """Own check-ins, doses, visits and symptoms, in the participant's own words. Read-only.

    Only the columns db/ grants participant_role (no dose_label).
    """
    rt = query_params(event).get("record_type")
    with scoped(me) as t:
        rows = t.query(
            "SELECT e.event_id, e.record_type, e.status, e.occurred_at, e.verbatim_text, e.pain_score_band,"
            "  e.body_location, e.onset_date, e.visit_code, e.scheduled_at, e.window_start,"
            "  e.window_end, e.response, e.response_text "
            "FROM trial.clinical_events e "
            "WHERE NOT EXISTS (SELECT 1 FROM trial.clinical_events n WHERE n.supersedes_event_id = e.event_id) "
            "  AND (:rt::text IS NULL OR e.record_type = :rt) "
            "ORDER BY e.occurred_at DESC LIMIT 200", rt=rt)
    return 200, {"events": rows}


@router.route("POST", "/me/requests")
def create_request(event, me: Participant):
    """Requests (profile change, reminder time change or pause, withdrawal...) go to the staff queue.

    No RETURNING: db/ gives participant_role no SELECT on communications, so the new row can't be read back.
    """
    body = json_body(event)
    kind = body.get("kind")
    text = (body.get("message") or "").strip()
    if kind not in REQUEST_KINDS:
        raise HttpError(400, f"kind must be one of {sorted(REQUEST_KINDS)}")
    if not text:
        raise HttpError(400, "message is required")
    subject = f"[TT {me.participant_id}] {kind}"
    with scoped(me) as t:
        t.execute(
            "INSERT INTO trial.communications (participant_id, channel, direction, msg_type, subject, body_text) "
            "VALUES (:pid, 'web', 'inbound', 'participant_request', :subject, :body)",
            pid=me.participant_id, subject=subject, body=text[:4000])
    return 201, {"request": {"kind": kind, "subject": subject}}


@router.route("GET", "/me/requests")
def list_requests(event, me: Participant):
    """Own requests, each with the study team's replies (newest request first)."""
    with scoped(me) as t:
        rows = t.query(
            "SELECT comm_id, subject, body_text, created_at FROM trial.communications "
            "WHERE participant_id = :pid AND msg_type = 'participant_request' ORDER BY created_at DESC LIMIT 50",
            pid=me.participant_id)
        replies = t.query(
            "SELECT comm_id, parsed_action->>'reply_to_comm_id' AS reply_to_comm_id, body_text, created_at "
            "FROM trial.communications WHERE participant_id = :pid AND msg_type = 'request_reply' "
            "ORDER BY created_at", pid=me.participant_id)
    for r in rows:
        r["replies"] = [{"body_text": x["body_text"], "created_at": x["created_at"]}
                        for x in replies if x["reply_to_comm_id"] == r["comm_id"]]
    return 200, {"requests": rows}


# ------------------------------------------------------------------ emergency (SOS)

SOS_SEVERITIES = {"life_threatening", "urgent", "concern"}
SOS_REPLY = {
    "en": ("Help is being arranged. If you are in danger, call 911 (or your local emergency number) now. "
           "Your study team has been alerted."),
    "es": ("Estamos pidiendo ayuda. Si está en peligro, llame al 911 (o a su número de emergencias local) ahora. "
           "Hemos avisado a su equipo del estudio."),
}
REPRESS_WINDOW_MIN = 30  # presses inside this window of an unresolved alert join it instead of opening a new one


def raise_alert(t: db.Tx, pid: str, *, severity: str, source: str, message: str | None = None,
                keywords: list[str] | None = None) -> tuple[dict, bool]:
    """Open an alert, or fold into the participant's unresolved one. Returns (alert, is_new).

    Folding keeps a panicking participant from burying staff in duplicates, and it never lowers severity.
    """
    existing = t.one(
        "SELECT alert_id FROM trial.emergency_alerts WHERE participant_id = :pid AND status <> 'resolved' "
        "  AND last_pressed_at > now() - make_interval(mins => :mins::int) ORDER BY created_at DESC LIMIT 1",
        pid=pid, mins=REPRESS_WINDOW_MIN)
    if existing:
        alert = t.one(
            "UPDATE trial.emergency_alerts SET press_count = press_count + 1, last_pressed_at = now(),"
            "  message = coalesce(:msg::text, message) WHERE alert_id = :id::uuid "
            "RETURNING alert_id, severity, status, press_count, created_at",
            id=existing["alert_id"], msg=message)
        return alert, False
    alert = t.one(
        "INSERT INTO trial.emergency_alerts (participant_id, severity, source, message, keywords) "
        "VALUES (:pid, :sev, :src, :msg::text, :kw::text[]) RETURNING alert_id, severity, status, press_count, created_at",
        pid=pid, sev=severity, src=source, msg=message,
        kw="{" + ",".join(f'"{k}"' for k in keywords) + "}" if keywords else None)
    telemetry.count("trialtalk.alerts.raised", severity=severity, source=source)
    return alert, True


@router.route("POST", "/me/emergency")
def emergency(event, me: Participant):
    """The SOS button. An empty body is a complete request: it raises a life-threatening alert immediately.

    `message` and `severity` are optional extras for a later, calmer press; nothing here may require input.
    """
    body = json_body(event) if event.get("body") else {}
    severity = body.get("severity") or "life_threatening"
    if severity not in SOS_SEVERITIES:
        raise HttpError(400, f"severity must be one of {sorted(SOS_SEVERITIES)}")
    message = (body.get("message") or "").strip()[:1000] or None
    with scoped(me) as t:
        p = _profile(t, me.participant_id)
        if p["status"] == "withdrawn":
            # Still an emergency. Withdrawn people get the instruction but the team is not paged for them.
            return 200, {"alert": None, "reply": SOS_REPLY.get(p["preferred_language"], SOS_REPLY["en"])}
        alert, is_new = raise_alert(t, me.participant_id, severity=severity, source="sos_button", message=message)
    if is_new:
        _alert_team(p, ["SOS button"], severity)
    return 200, {"alert": alert, "new": is_new,
                 "reply": SOS_REPLY.get(p["preferred_language"], SOS_REPLY["en"])}


@router.route("GET", "/me/emergency")
def my_alerts(event, me: Participant):
    with scoped(me) as t:
        rows = t.query(
            "SELECT alert_id, severity, source, status, press_count, created_at, acknowledged_at, resolved_at "
            "FROM trial.emergency_alerts WHERE participant_id = :pid ORDER BY created_at DESC LIMIT 10",
            pid=me.participant_id)
    return 200, {"alerts": rows}


def _alert_team(p: dict, hits: list[str], severity: str) -> None:
    """Immediate email to the participant's own site team. No diagnosis or message content, just the flag.

    Never falls back to another site's addresses; with no recipients the alert still shows in the staff widget.
    """
    to = ALERT_TO.get(p["site_id"]) or ALERT_TO.get("*")
    if not (ALERT_FROM and to):
        print(f"EMERGENCY alert for {p['participant_id']} but no ALERT_TO addresses for site {p['site_id']}")
        return
    try:
        ses().send_email(
            FromEmailAddress=ALERT_FROM,
            Destination={"ToAddresses": to},
            Content={"Simple": {
                "Subject": {"Data": f"[TT {p['participant_id']} URGENT] Emergency alert ({severity})"},
                "Body": {"Text": {"Data": (
                    f"Participant {p['participant_id']} (site {p['site_id']}) raised an emergency alert "
                    f"({', '.join(hits)}) on the TrialTalk website and was told to call emergency services.\n\n"
                    "Open the staff portal's Emergency alerts widget.")}},
            }},
        )
    except Exception as e:
        print(f"Emergency alert email failed: {e}")
