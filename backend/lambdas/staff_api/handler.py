"""Staff API (coordinator / PI). Routes under /staff, behind the staff user pool JWT authorizer.

Database access runs as staff_api (member of portal_role); RLS limits rows to the caller's sites.
"""

import json
import os
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3

from common import db, legacy_mirror, scheduling, telemetry
from common.auth import Staff, require, staff_claims
from common.http import HttpError, Router, json_body, query_params
from staff_api import accounts

PARTICIPANT_POOL_ID = os.environ.get("PARTICIPANT_POOL_ID", "")
REMINDER_FUNCTION_ARN = os.environ.get("REMINDER_FUNCTION_ARN", "")
APPROVED_TEMPLATES = {t for t in os.environ.get("APPROVED_TEMPLATES", "").split(",") if t}
DEFAULT_TEMPLATES = json.loads(os.environ.get("DEFAULT_TEMPLATES", "{}")) or {
    "dose": "dose_reminder_v2", "visit": "visit_reminder_v1", "diary": "daily_checkin_v1", "custom": "",
}
DEFAULT_BUTTONS = {"dose": ["Taken", "Skipped", "Remind me in 30 min"], "visit": ["Confirm", "Reschedule"]}

router = Router()
router.include(accounts.router)
_cognito = None
_lambda = None


def cognito():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def handler(event, context):
    return router.dispatch(event, staff_claims)


# ------------------------------------------------------------------ shared SQL

PARTICIPANT_COLS = """participant_id, full_name, email, phone_e164, date_of_birth, address_line1, address_line2,
  city, state, postal_code, country, travel_radius_miles, preferred_language, timezone, study_id, site_id,
  status, whatsapp_opt_in, opt_in_at, created_at, updated_at"""

REMINDER_COLS = """reminder_id, participant_id, reminder_type, label, template_name, template_language,
  template_params, to_json(buttons) AS buttons, schedule_kind, cron_expr, send_at, timezone, starts_on, ends_on,
  visit_event_id, follow_up_after_min, escalate_after_misses, status, scheduler_arn, next_send_at,
  last_sent_at, send_count, change_log, created_by, created_at, updated_by, updated_at"""

EDITABLE = {"email", "address_line1", "address_line2", "city", "state", "postal_code", "country",
            "travel_radius_miles", "preferred_language", "timezone", "status", "whatsapp_opt_in"}
IDENTITY = {"full_name", "date_of_birth", "phone_e164"}  # changes need a change_reason
STATUSES = {"prospect", "screening", "enrolled", "screen_failed"}  # 'withdrawn' only through DELETE
REMINDER_TYPES = {"dose", "visit", "diary", "custom"}
OPEN_REMINDER = ("draft", "active", "paused")
ID_ATTEMPTS = 50  # ids tried past the highest one this user can see before giving up


def get_participant(t: db.Tx, pid: str) -> dict:
    row = t.one(f"SELECT {PARTICIPANT_COLS} FROM pii.participants WHERE participant_id = :pid", pid=pid)
    if not row:
        raise HttpError(404, "Participant not found")
    return row


def get_reminder(t: db.Tx, rid: str) -> dict:
    _uuid(rid)
    row = t.one(f"SELECT {REMINDER_COLS} FROM trial.reminders WHERE reminder_id = :rid::uuid", rid=rid)
    if not row:
        raise HttpError(404, "Reminder not found")
    return row


def list_reminders(t: db.Tx, pid: str) -> list[dict]:
    return t.query(f"SELECT {REMINDER_COLS} FROM trial.reminders WHERE participant_id = :pid "
                   "ORDER BY status = 'cancelled', created_at", pid=pid)


def _uuid(value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""):
        raise HttpError(400, "Invalid id")


def _tz(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HttpError(400, f"Unknown time zone {name!r}")
    return name


def _phone(value: str) -> str:
    phone = re.sub(r"[\s\-()]", "", value or "")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        raise HttpError(400, "phone_e164 must look like +17325550142")
    return phone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(staff: Staff, action: str, **fields) -> None:
    """Structured audit line for CloudWatch (identity edits, withdrawals). pgaudit covers the SQL."""
    print(json.dumps({"audit": action, "by": staff.actor, "at": _now(), **fields}, default=str))


def _sync_mirror(t: db.Tx, pid: str, old_phone: str | None = None) -> None:
    if legacy_mirror.LEGACY_TABLE:
        legacy_mirror.sync(get_participant(t, pid), list_reminders(t, pid), old_phone)


# ------------------------------------------------------------------ participants

@router.route("GET", "/staff/me")
def whoami(event, staff: Staff):
    return 200, {"email": staff.email, "role": staff.role, "site_ids": staff.site_ids, "study_ids": staff.study_ids}


@router.route("GET", "/staff/participants")
def list_participants(event, staff: Staff):
    require(staff, "participant:read")
    q = query_params(event)
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(
            f"SELECT {PARTICIPANT_COLS} FROM pii.participants "
            "WHERE (:status::text IS NULL OR status = :status) "
            "  AND (:site::text IS NULL OR site_id = :site) "
            "  AND (:q::text IS NULL OR full_name ILIKE '%' || :q || '%' OR participant_id ILIKE '%' || :q || '%' "
            "       OR phone_e164 LIKE '%' || :q || '%') "
            "ORDER BY created_at DESC LIMIT 200",
            status=q.get("status"), site=q.get("site_id"), q=q.get("q"),
        )
    return 200, {"participants": rows}


@router.route("POST", "/staff/participants")
def create_participant(event, staff: Staff):
    body = json_body(event)
    for field in ("full_name", "phone_e164", "study_id", "site_id"):
        if not body.get(field):
            raise HttpError(400, f"{field} is required")
    require(staff, "participant:write", site_id=body["site_id"], study_id=body["study_id"])
    status = body.get("status", "enrolled")
    if status not in STATUSES:
        raise HttpError(400, f"status must be one of {sorted(STATUSES)}")
    opt_in = bool(body.get("whatsapp_opt_in", False))
    values = dict(
        full_name=body["full_name"].strip(), email=body.get("email"), phone=_phone(body["phone_e164"]),
        dob=body.get("date_of_birth"), a1=body.get("address_line1"), a2=body.get("address_line2"),
        city=body.get("city"), state=body.get("state"), postal=body.get("postal_code"),
        country=body.get("country"), radius=body.get("travel_radius_miles"),
        lang=body.get("preferred_language"), tz=_tz(body["timezone"]) if body.get("timezone") else None,
        study=body["study_id"], site=body["site_id"], status=status, opt_in=opt_in,
    )

    with db.tx(site_ids=staff.site_ids) as t:
        # participant_id has no default in db/. Start after the highest id this user can see; RLS hides other
        # sites' ids, so a taken one comes back as no row (ON CONFLICT DO NOTHING) and the next is tried.
        start = t.one("SELECT coalesce(max(substring(participant_id from 3)::int), 1000) AS n "
                      "FROM pii.participants WHERE participant_id ~ '^P-[0-9]+$'")["n"]
        for n in range(start + 1, start + 1 + ID_ATTEMPTS):
            row = t.one(
                "INSERT INTO pii.participants (participant_id, full_name, email, phone_e164, date_of_birth,"
                "  address_line1, address_line2, city, state, postal_code, country, travel_radius_miles,"
                "  preferred_language, timezone, study_id, site_id, status, whatsapp_opt_in, opt_in_at) "
                "VALUES (:pid, :full_name, :email, :phone, :dob::date, :a1, :a2, :city, :state, :postal,"
                "  coalesce(:country, 'US'), :radius, coalesce(:lang, 'en'), coalesce(:tz, 'America/New_York'),"
                "  :study, :site, :status, :opt_in, CASE WHEN :opt_in THEN now() END) "
                "ON CONFLICT (participant_id) DO NOTHING RETURNING participant_id",
                pid=f"P-{n:04d}", **values,
            )
            if row:
                break
        else:
            raise HttpError(503, "Could not allocate a participant id; try again")
        participant = get_participant(t, row["participant_id"])
        if PARTICIPANT_POOL_ID:
            _create_login(participant)
        _sync_mirror(t, participant["participant_id"])
    return 201, {"participant": participant}


def _create_login(p: dict) -> None:
    """Participant website login: phone number + one-time code on WhatsApp (custom auth)."""
    pool = PARTICIPANT_POOL_ID
    cognito().admin_create_user(
        UserPoolId=pool, Username=p["participant_id"], MessageAction="SUPPRESS",
        UserAttributes=[
            {"Name": "phone_number", "Value": p["phone_e164"]},
            {"Name": "phone_number_verified", "Value": "true"},
            {"Name": "custom:participant_id", "Value": p["participant_id"]},
        ],
    )
    # Never used to sign in (custom auth only); a permanent password just takes the user out of FORCE_CHANGE_PASSWORD.
    cognito().admin_set_user_password(UserPoolId=pool, Username=p["participant_id"],
                                      Password=secrets.token_urlsafe(24) + "aA1!", Permanent=True)


@router.route("GET", "/staff/participants/{pid}")
def read_participant(event, staff: Staff, pid: str):
    require(staff, "participant:read")
    with db.tx(site_ids=staff.site_ids) as t:
        p = get_participant(t, pid)
        reminders = list_reminders(t, pid)
    return 200, {"participant": p, "reminders": reminders}


@router.route("PATCH", "/staff/participants/{pid}")
def update_participant(event, staff: Staff, pid: str):
    body = json_body(event)
    changes = {k: v for k, v in body.items() if k in EDITABLE | IDENTITY}
    unknown = set(body) - EDITABLE - IDENTITY - {"change_reason"}
    if unknown:
        raise HttpError(400, f"Cannot update: {sorted(unknown)}")
    if not changes:
        raise HttpError(400, "Nothing to update")
    if IDENTITY & changes.keys() and not body.get("change_reason"):
        raise HttpError(400, "change_reason is required to change name, date of birth or phone")
    if "status" in changes and changes["status"] not in STATUSES:
        raise HttpError(400, "Use DELETE to withdraw; status must be one of " + str(sorted(STATUSES)))
    if "phone_e164" in changes:
        changes["phone_e164"] = _phone(changes["phone_e164"])
    if "timezone" in changes:
        _tz(changes["timezone"])

    with db.tx(site_ids=staff.site_ids) as t:
        before = get_participant(t, pid)
        require(staff, "participant:write", site_id=before["site_id"])
        if before["status"] == "withdrawn":
            raise HttpError(409, "Participant is withdrawn")

        casts = {"date_of_birth": "::date", "travel_radius_miles": "::int", "whatsapp_opt_in": "::boolean"}
        sets = [f"{k} = :{k}{casts.get(k, '')}" for k in changes]
        if changes.get("whatsapp_opt_in") is True and not before["whatsapp_opt_in"]:
            sets.append("opt_in_at = now()")
        t.execute(f"UPDATE pii.participants SET {', '.join(sets)}, updated_at = now() WHERE participant_id = :pid",
                  pid=pid, **changes)

        # Reminders follow the participant's time zone; opting out pauses them.
        if "timezone" in changes and changes["timezone"] != before["timezone"]:
            for r in list_reminders(t, pid):
                if r["status"] in OPEN_REMINDER:
                    _apply_reminder_change(t, staff, r, {"timezone": changes["timezone"]}, "participant time zone changed")
        if changes.get("whatsapp_opt_in") is False:
            for r in list_reminders(t, pid):
                if r["status"] == "active":
                    _apply_reminder_change(t, staff, r, {"status": "paused"}, "participant opted out of WhatsApp")

        after = get_participant(t, pid)
        if PARTICIPANT_POOL_ID and "phone_e164" in changes and changes["phone_e164"] != before["phone_e164"]:
            cognito().admin_update_user_attributes(
                UserPoolId=PARTICIPANT_POOL_ID, Username=pid,
                UserAttributes=[{"Name": "phone_number", "Value": after["phone_e164"]},
                                {"Name": "phone_number_verified", "Value": "true"}])
        _sync_mirror(t, pid, old_phone=before["phone_e164"])

    if IDENTITY & changes.keys():
        _audit(staff, "participant.identity_change", participant_id=pid, fields=sorted(IDENTITY & changes.keys()),
               reason=body["change_reason"])
    return 200, {"participant": after}


@router.route("DELETE", "/staff/participants/{pid}")
def withdraw_participant(event, staff: Staff, pid: str):
    """'Delete' = withdraw. Records are kept (Part 11); reminders are cancelled and WhatsApp stops."""
    body = json_body(event) if event.get("body") else {}
    reason = body.get("reason") or query_params(event).get("reason")
    if not reason:
        raise HttpError(400, "reason is required to withdraw a participant")

    with db.tx(site_ids=staff.site_ids) as t:
        p = get_participant(t, pid)
        require(staff, "participant:withdraw", site_id=p["site_id"])
        if p["status"] == "withdrawn":
            return 200, {"participant": p, "cancelled_reminders": 0}
        t.execute("UPDATE pii.participants SET status = 'withdrawn', whatsapp_opt_in = false, updated_at = now() "
                  "WHERE participant_id = :pid", pid=pid)
        cancelled = 0
        for r in list_reminders(t, pid):
            if r["status"] in OPEN_REMINDER:
                _apply_reminder_change(t, staff, r, {"status": "cancelled"}, f"participant withdrawn: {reason}")
                cancelled += 1
        after = get_participant(t, pid)
        if PARTICIPANT_POOL_ID:
            try:
                cognito().admin_disable_user(UserPoolId=PARTICIPANT_POOL_ID, Username=pid)
            except cognito().exceptions.UserNotFoundException:
                pass
        _sync_mirror(t, pid)

    _audit(staff, "participant.withdraw", participant_id=pid, reason=reason)
    return 200, {"participant": after, "cancelled_reminders": cancelled}


@router.route("GET", "/staff/participants/{pid}/events")
def participant_events(event, staff: Staff, pid: str):
    require(staff, "events:read")
    q = query_params(event)
    with db.tx(site_ids=staff.site_ids) as t:
        get_participant(t, pid)
        rows = t.query(
            "SELECT event_id, record_type, status, occurred_at, source, parent_event_id, verbatim_text, summary,"
            "  pain_score_band, body_location, onset_date, to_json(characteristics) AS characteristics, severity_hint,"
            "  reason_code, visit_code, dose_label, scheduled_at, window_start, window_end, response, response_text,"
            "  ae_term, severity_grade, serious, relatedness, outcome, staff_notes, details,"
            "  supersedes_event_id, change_reason, created_by, created_at "  # db/'s view predates reminder_id
            "FROM trial.current_events WHERE participant_id = :pid "
            "  AND (:rt::text IS NULL OR record_type = :rt) "
            "ORDER BY occurred_at DESC LIMIT 200",
            pid=pid, rt=q.get("record_type"),
        )
    return 200, {"events": rows}


@router.route("GET", "/staff/participants/{pid}/adherence")
def participant_adherence(event, staff: Staff, pid: str):
    require(staff, "events:read")
    with db.tx(site_ids=staff.site_ids) as t:
        get_participant(t, pid)
        rows = t.query(
            "SELECT coalesce(response, 'no_response') AS response, count(*) AS n FROM trial.current_events "
            "WHERE participant_id = :pid AND record_type = 'DOSE' AND scheduled_at > now() - interval '30 days' "
            "GROUP BY 1", pid=pid)
    counts = {r["response"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    return 200, {"window_days": 30, "counts": counts, "total": total,
                 "taken_rate": round(counts.get("taken", 0) / total, 3) if total else None}


@router.route("GET", "/staff/participants/{pid}/communications")
def participant_communications(event, staff: Staff, pid: str):
    require(staff, "events:read")
    with db.tx(site_ids=staff.site_ids) as t:
        get_participant(t, pid)
        rows = t.query(
            "SELECT comm_id, channel, direction, msg_type, template_name, subject, body_text, delivery_status,"
            "  related_event_id, reminder_id, created_at "
            "FROM trial.communications WHERE participant_id = :pid ORDER BY created_at DESC LIMIT 100", pid=pid)
    return 200, {"communications": rows}


@router.route("GET", "/staff/requests")
def participant_requests(event, staff: Staff):
    """Queue of participant website requests, unanswered first, each with the replies sent so far."""
    require(staff, "requests:read")
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(
            "SELECT c.comm_id, c.participant_id, p.full_name, p.site_id, c.subject, c.body_text, c.created_at "
            "FROM trial.communications c JOIN pii.participants p USING (participant_id) "
            "WHERE c.msg_type = 'participant_request' AND c.created_at > now() - interval '30 days' "
            "ORDER BY c.created_at DESC LIMIT 100")
        replies = t.query(
            "SELECT r.comm_id, r.parsed_action->>'reply_to_comm_id' AS reply_to_comm_id, r.body_text, r.from_addr,"
            "  r.created_at FROM trial.communications r WHERE r.msg_type = 'request_reply' "
            "  AND r.parsed_action->>'reply_to_comm_id' IN (SELECT comm_id::text FROM trial.communications "
            "    WHERE msg_type = 'participant_request' AND created_at > now() - interval '30 days') "
            "ORDER BY r.created_at")
    for r in rows:
        r["replies"] = [x for x in replies if x["reply_to_comm_id"] == r["comm_id"]]
        r["answered"] = bool(r["replies"])
    rows.sort(key=lambda r: r["answered"])  # stable: unanswered first, newest first within each group
    return 200, {"requests": rows, "unanswered": sum(not r["answered"] for r in rows)}


@router.route("POST", "/staff/requests/{cid}/reply")
def reply_to_request(event, staff: Staff, cid: str):
    """Answer a participant request (a one-click quick reply or a longer message). The participant sees it
    under their request on the website. More than one reply is allowed.

    db/ has no reply column, so the link to the request goes in parsed_action. msg_type is 'request_reply', not
    'staff_reply': db/ uses that for inbound staff emails (e.g. the PI answering an AE report)."""
    require(staff, "requests:write")
    _uuid(cid)
    text = (json_body(event).get("text") or "").strip()
    if not text:
        raise HttpError(400, "text is required")
    if len(text) > 4000:
        raise HttpError(400, "Replies are limited to 4000 characters")
    with db.tx(site_ids=staff.site_ids) as t:
        req = t.one("SELECT comm_id, participant_id, subject FROM trial.communications "
                    "WHERE comm_id = :id::uuid AND msg_type = 'participant_request'", id=cid)
        if not req:
            raise HttpError(404, "Request not found")
        reply = t.one(
            "INSERT INTO trial.communications (participant_id, channel, direction, msg_type, subject, body_text,"
            "  from_addr, parsed_action) VALUES (:pid, 'web', 'outbound', 'request_reply', :subject, :body,"
            "  :by, :action::jsonb) RETURNING comm_id, body_text, from_addr, created_at",
            pid=req["participant_id"], subject="Re: " + (req["subject"] or ""), body=text, by=staff.email,
            action={"reply_to_comm_id": req["comm_id"]})  # as the database spells it, not the URL
        _audit(staff, "request.reply", request_id=cid, participant_id=req["participant_id"])
    return 201, {"reply": reply}


# ------------------------------------------------------------------ emergency alerts

ALERT_COLS = """a.alert_id, a.participant_id, p.full_name, p.phone_e164, p.site_id, a.severity, a.source, a.message,
  a.keywords, a.status, a.press_count, a.last_pressed_at, a.created_at, a.acknowledged_by, a.acknowledged_at,
  a.resolved_by, a.resolved_at, a.resolution_note"""
ALERT_FILTERS = {"active": "a.status <> 'resolved'", "resolved": "a.status = 'resolved'", "all": "true"}


@router.route("GET", "/staff/alerts")
def list_alerts(event, staff: Staff):
    """Emergency alerts at your sites. Default is everything unresolved, open and life-threatening first."""
    require(staff, "alerts:read")
    scope = query_params(event).get("status") or "active"
    if scope not in ALERT_FILTERS:
        raise HttpError(400, f"status must be one of {sorted(ALERT_FILTERS)}")
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(
            f"SELECT {ALERT_COLS} FROM trial.emergency_alerts a JOIN pii.participants p USING (participant_id) "
            f"WHERE {ALERT_FILTERS[scope]} AND (a.status <> 'resolved' OR a.created_at > now() - interval '30 days') "
            "ORDER BY (a.status = 'resolved'), (a.status = 'acknowledged'), "
            "  CASE a.severity WHEN 'life_threatening' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END, a.created_at DESC "
            "LIMIT 100")
    return 200, {"alerts": rows, "open": sum(r["status"] == "open" for r in rows)}


def _alert(t: db.Tx, aid: str) -> dict:
    _uuid(aid)
    row = t.one(f"SELECT {ALERT_COLS} FROM trial.emergency_alerts a JOIN pii.participants p USING (participant_id) "
                "WHERE a.alert_id = :id::uuid", id=aid)
    if not row:
        raise HttpError(404, "Alert not found")
    return row


@router.route("POST", "/staff/alerts/{aid}/acknowledge")
def acknowledge_alert(event, staff: Staff, aid: str):
    """'I have seen this and am on it.' Idempotent; the first acknowledger is the one recorded."""
    require(staff, "alerts:write")
    with db.tx(site_ids=staff.site_ids) as t:
        alert = _alert(t, aid)
        if alert["status"] == "open":
            t.execute("UPDATE trial.emergency_alerts SET status = 'acknowledged', acknowledged_by = :by,"
                      "  acknowledged_at = now() WHERE alert_id = :id::uuid AND status = 'open'",
                      by=staff.actor, id=aid)
            _audit(staff, "alert.acknowledge", alert_id=aid, participant_id=alert["participant_id"])
        return 200, {"alert": _alert(t, aid)}


@router.route("POST", "/staff/alerts/{aid}/resolve")
def resolve_alert(event, staff: Staff, aid: str):
    """Close the loop: what happened and what was done. The note is required and kept."""
    require(staff, "alerts:write")
    note = (json_body(event).get("note") or "").strip()
    if not note:
        raise HttpError(400, "note is required: say what happened and what was done")
    with db.tx(site_ids=staff.site_ids) as t:
        alert = _alert(t, aid)
        if alert["status"] == "resolved":
            raise HttpError(409, "Alert is already resolved")
        t.execute("UPDATE trial.emergency_alerts SET status = 'resolved', resolved_by = :by, resolved_at = now(),"
                  "  resolution_note = :note, acknowledged_by = coalesce(acknowledged_by, :by),"
                  "  acknowledged_at = coalesce(acknowledged_at, now()) WHERE alert_id = :id::uuid",
                  by=staff.actor, note=note[:2000], id=aid)
        _audit(staff, "alert.resolve", alert_id=aid, participant_id=alert["participant_id"])
        return 200, {"alert": _alert(t, aid)}


# ------------------------------------------------------------------ token usage

@router.route("GET", "/staff/usage")
def token_usage(event, staff: Staff):
    """Agent token usage at your sites over the last `days` (default 30): totals, per day and per participant."""
    require(staff, "ops:read")
    try:
        days = min(max(int(query_params(event).get("days") or 30), 1), 90)
    except ValueError:
        raise HttpError(400, "days must be a number")
    window = "c.created_at > now() - make_interval(days => :days::int) AND c.input_tokens IS NOT NULL"
    with db.tx(site_ids=staff.site_ids) as t:
        totals = t.one(f"SELECT coalesce(sum(c.input_tokens), 0) AS input_tokens, "
                       f"coalesce(sum(c.output_tokens), 0) AS output_tokens, count(*) AS calls "
                       f"FROM trial.communications c WHERE {window}", days=days)
        by_day = t.query(f"SELECT c.created_at::date AS day, sum(c.input_tokens) AS input_tokens, "
                         f"sum(c.output_tokens) AS output_tokens, count(*) AS calls "
                         f"FROM trial.communications c WHERE {window} GROUP BY 1 ORDER BY 1", days=days)
        by_participant = t.query(
            f"SELECT c.participant_id, p.full_name, p.site_id, sum(c.input_tokens) AS input_tokens, "
            f"sum(c.output_tokens) AS output_tokens, count(*) AS calls "
            f"FROM trial.communications c JOIN pii.participants p USING (participant_id) WHERE {window} "
            "GROUP BY 1, 2, 3 ORDER BY sum(c.input_tokens) + sum(c.output_tokens) DESC LIMIT 20", days=days)
    return 200, {"days": days, "totals": totals, "by_day": by_day, "by_participant": by_participant}


# ------------------------------------------------------------------ observability

@router.route("GET", "/staff/observability")
def observability(event, staff: Staff):
    """OpenTelemetry window for the staff portal widget: recent spans and request metrics. No PHI in telemetry."""
    require(staff, "ops:read")
    return 200, telemetry.snapshot()


# ------------------------------------------------------------------ reminders and dosing

def _schedule_fields(body: dict, current: dict | None = None) -> dict:
    """Turn {time, days} / cron_expr / send_at into schedule columns."""
    out = {}
    if "time" in body or "days" in body:
        try:
            at = body.get("time") or _current_time(current)
            out = {"schedule_kind": "recurring", "cron_expr": scheduling.build_cron(at, body.get("days")),
                   "send_at": None}
        except ValueError as e:
            raise HttpError(400, str(e))
    elif body.get("cron_expr"):
        if not re.fullmatch(r"cron\([^()]+\)", body["cron_expr"]):
            raise HttpError(400, "cron_expr must look like cron(0 20 * * ? *)")
        out = {"schedule_kind": "recurring", "cron_expr": body["cron_expr"], "send_at": None}
    elif body.get("send_at"):
        try:
            send_at = scheduling._as_utc(body["send_at"])
        except ValueError:
            raise HttpError(400, "send_at must be an ISO 8601 timestamp")
        if send_at <= datetime.now(timezone.utc):
            raise HttpError(400, "send_at must be in the future")
        out = {"schedule_kind": "one_time", "cron_expr": None, "send_at": send_at.isoformat()}
    return out


def _current_time(current: dict | None) -> str:
    m = re.match(r"^cron\((\d{1,2}) (\d{1,2}) ", (current or {}).get("cron_expr") or "")
    if not m:
        raise ValueError("time is required")
    return f"{int(m.group(2)):02d}:{int(m.group(1)):02d}"


def _dose_params(body: dict, existing: dict) -> dict:
    """Dose templates use {{1}} = dose label (e.g. 'evening') and {{2}} = instructions (e.g. '1 tablet with food')."""
    params = dict(body.get("template_params") or existing)
    if body.get("dose_label"):
        params["1"] = body["dose_label"]
    if body.get("dose_instructions"):
        params["2"] = body["dose_instructions"]
    return params


def _check_template(name: str) -> None:
    if not name:
        raise HttpError(400, "template_name is required (an approved WhatsApp template)")
    if APPROVED_TEMPLATES and name not in APPROVED_TEMPLATES:
        raise HttpError(400, f"Template {name!r} is not on the approved list")


def _check_buttons(buttons) -> list[str] | None:
    if buttons is None:
        return None
    if not isinstance(buttons, list) or len(buttons) > 3 or not all(isinstance(b, str) and b for b in buttons):
        raise HttpError(400, "buttons must be a list of up to 3 labels (WhatsApp limit)")
    return buttons


@router.route("GET", "/staff/participants/{pid}/reminders")
def get_reminders(event, staff: Staff, pid: str):
    require(staff, "reminder:read")
    with db.tx(site_ids=staff.site_ids) as t:
        get_participant(t, pid)
        return 200, {"reminders": list_reminders(t, pid)}


@router.route("POST", "/staff/participants/{pid}/reminders")
def create_reminder(event, staff: Staff, pid: str):
    body = json_body(event)
    rtype = body.get("reminder_type")
    if rtype not in REMINDER_TYPES:
        raise HttpError(400, f"reminder_type must be one of {sorted(REMINDER_TYPES)}")
    if not body.get("label"):
        raise HttpError(400, "label is required, e.g. 'Evening dose'")
    if rtype == "dose" and not (body.get("dose_label") or (body.get("template_params") or {}).get("1")):
        raise HttpError(400, "dose_label is required for a dose reminder, e.g. 'evening'")
    sched = _schedule_fields(body)
    if not sched:
        raise HttpError(400, "Give time (+ optional days), cron_expr, or send_at")
    status = body.get("status", "active")
    if status not in ("active", "draft"):
        raise HttpError(400, "status must be active or draft")
    template = body.get("template_name") or DEFAULT_TEMPLATES.get(rtype, "")
    _check_template(template)
    buttons = _check_buttons(body.get("buttons", DEFAULT_BUTTONS.get(rtype)))
    params = _dose_params(body, {}) if rtype == "dose" else dict(body.get("template_params") or {})

    with db.tx(site_ids=staff.site_ids) as t:
        p = get_participant(t, pid)
        require(staff, "reminder:write", site_id=p["site_id"])
        if p["status"] == "withdrawn":
            raise HttpError(409, "Participant is withdrawn")
        if status == "active" and not p["whatsapp_opt_in"]:
            raise HttpError(409, "Participant has not opted in to WhatsApp; save as draft instead")
        tz = _tz(body.get("timezone") or p["timezone"])

        row = t.one(
            "INSERT INTO trial.reminders (participant_id, reminder_type, label, template_name, template_language,"
            "  template_params, buttons, schedule_kind, cron_expr, send_at, timezone, starts_on, ends_on,"
            "  visit_event_id, follow_up_after_min, escalate_after_misses, status, change_log, created_by, updated_by) "
            "VALUES (:pid, :rtype, :label, :template, :lang, :params::jsonb,"
            "  (SELECT array_agg(x) FROM jsonb_array_elements_text(:buttons::jsonb) x),"
            "  :kind, :cron, :send_at::timestamptz, :tz, :starts::date, :ends::date, :visit::uuid, :follow::int,"
            "  :escalate::int, :status, :log::jsonb, :by, :by) RETURNING reminder_id",
            pid=pid, rtype=rtype, label=body["label"], template=template,
            lang=body.get("template_language") or p["preferred_language"], params=params, buttons=buttons,
            kind=sched["schedule_kind"], cron=sched["cron_expr"], send_at=sched["send_at"], tz=tz,
            starts=body.get("starts_on"), ends=body.get("ends_on"), visit=body.get("visit_event_id"),
            follow=body.get("follow_up_after_min"), escalate=body.get("escalate_after_misses"), status=status,
            log=[{"at": _now(), "by": staff.actor, "field": "*", "old": None, "new": "created",
                  "reason": body.get("reason")}],
            by=staff.actor,
        )
        reminder = get_reminder(t, row["reminder_id"])
        if status != "draft":
            arn = scheduling.create(reminder)
            nxt = scheduling.next_send_at(reminder)
            t.execute("UPDATE trial.reminders SET scheduler_arn = :arn, next_send_at = :nxt::timestamptz "
                      "WHERE reminder_id = :rid::uuid", arn=arn, nxt=nxt, rid=reminder["reminder_id"])
            reminder = get_reminder(t, reminder["reminder_id"])
        if rtype == "dose":
            _sync_mirror(t, pid)
    return 201, {"reminder": reminder}


@router.route("PATCH", "/staff/reminders/{rid}")
def update_reminder(event, staff: Staff, rid: str):
    body = json_body(event)
    with db.tx(site_ids=staff.site_ids) as t:
        r = get_reminder(t, rid)
        p = get_participant(t, r["participant_id"])
        require(staff, "reminder:write", site_id=p["site_id"])
        if r["status"] in ("cancelled", "completed"):
            raise HttpError(409, f"Reminder is {r['status']}")

        changes = {}
        for field in ("label", "starts_on", "ends_on", "follow_up_after_min", "escalate_after_misses",
                      "template_language"):
            if field in body:
                changes[field] = body[field]
        if "template_name" in body:
            _check_template(body["template_name"])
            changes["template_name"] = body["template_name"]
        if "buttons" in body:
            changes["buttons"] = _check_buttons(body["buttons"])
        if "timezone" in body:
            changes["timezone"] = _tz(body["timezone"])
        if r["reminder_type"] == "dose" and ({"dose_label", "dose_instructions", "template_params"} & body.keys()):
            changes["template_params"] = _dose_params(body, r["template_params"])
        elif "template_params" in body:
            changes["template_params"] = body["template_params"]
        changes.update(_schedule_fields(body, r))
        if "status" in body:
            if body["status"] not in ("active", "paused"):
                raise HttpError(400, "status can be set to active or paused (DELETE cancels)")
            if body["status"] == "active" and not p["whatsapp_opt_in"]:
                raise HttpError(409, "Participant has not opted in to WhatsApp")
            changes["status"] = body["status"]
        if not changes:
            raise HttpError(400, "Nothing to update")

        reminder = _apply_reminder_change(t, staff, r, changes, body.get("reason"))
        if r["reminder_type"] == "dose":
            _sync_mirror(t, r["participant_id"])
    return 200, {"reminder": reminder}


@router.route("DELETE", "/staff/reminders/{rid}")
def cancel_reminder(event, staff: Staff, rid: str):
    body = json_body(event) if event.get("body") else {}
    with db.tx(site_ids=staff.site_ids) as t:
        r = get_reminder(t, rid)
        p = get_participant(t, r["participant_id"])
        require(staff, "reminder:write", site_id=p["site_id"])
        if r["status"] == "cancelled":
            return 200, {"reminder": r}
        reminder = _apply_reminder_change(t, staff, r, {"status": "cancelled"},
                                          body.get("reason") or query_params(event).get("reason"))
        if r["reminder_type"] == "dose":
            _sync_mirror(t, r["participant_id"])
    return 200, {"reminder": reminder}


@router.route("POST", "/staff/reminders/{rid}/send-now")
def send_now(event, staff: Staff, rid: str):
    with db.tx(site_ids=staff.site_ids) as t:
        r = get_reminder(t, rid)
        p = get_participant(t, r["participant_id"])
        require(staff, "reminder:send", site_id=p["site_id"])
    if r["status"] not in ("active", "paused", "draft"):
        raise HttpError(409, f"Reminder is {r['status']}")
    resp = lambda_client().invoke(
        FunctionName=REMINDER_FUNCTION_ARN, InvocationType="RequestResponse",
        Payload=json.dumps({"reminder_id": rid, "manual": True, "requested_by": staff.actor}).encode())
    result = json.loads(resp["Payload"].read() or "{}")
    if resp.get("FunctionError"):
        raise HttpError(502, "Reminder could not be sent")
    return 200, result


def _apply_reminder_change(t: db.Tx, staff: Staff, r: dict, changes: dict, reason: str | None) -> dict:
    """Update the row, append change_log, and keep the EventBridge schedule in step."""
    changes = {k: v for k, v in changes.items() if r.get(k) != v}
    if not changes:
        return r
    log = [{"at": _now(), "by": staff.actor, "field": k, "old": r.get(k), "new": v, "reason": reason}
           for k, v in changes.items()]
    casts = {"template_params": "::jsonb", "send_at": "::timestamptz", "starts_on": "::date", "ends_on": "::date",
             "follow_up_after_min": "::int", "escalate_after_misses": "::int"}
    sets = []
    for k in changes:
        if k == "buttons":
            sets.append("buttons = (SELECT array_agg(x) FROM jsonb_array_elements_text(:buttons::jsonb) x)")
        else:
            sets.append(f"{k} = :{k}{casts.get(k, '')}")
    t.execute(f"UPDATE trial.reminders SET {', '.join(sets)}, change_log = change_log || :log::jsonb, "
              "updated_by = :by, updated_at = now() WHERE reminder_id = :rid::uuid",
              rid=r["reminder_id"], log=log, by=staff.actor, **changes)
    new = get_reminder(t, r["reminder_id"])

    if new["status"] == "cancelled":
        scheduling.delete(new["reminder_id"])
        arn, nxt = None, None
    elif new["status"] == "draft":
        arn, nxt = None, None
    else:
        arn = scheduling.update(new) if new["scheduler_arn"] else scheduling.create(new)
        nxt = scheduling.next_send_at(new) if new["status"] == "active" else None
    t.execute("UPDATE trial.reminders SET scheduler_arn = coalesce(:arn, scheduler_arn), "
              "next_send_at = :nxt::timestamptz WHERE reminder_id = :rid::uuid",
              arn=arn, nxt=nxt, rid=new["reminder_id"])
    return get_reminder(t, new["reminder_id"])
