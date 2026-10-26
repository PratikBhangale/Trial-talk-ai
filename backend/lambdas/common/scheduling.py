"""EventBridge Scheduler for trial.reminders. The row is the source of truth; schedule rem-<reminder_id> only fires it."""

import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3

GROUP = os.environ.get("SCHEDULE_GROUP", "trialtalk-reminders")
TARGET_ARN = os.environ.get("REMINDER_FUNCTION_ARN", "")
ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
KMS_KEY_ARN = os.environ.get("SCHEDULE_KMS_KEY_ARN", "")

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_SIMPLE_CRON = re.compile(r"^cron\((\d{1,2}) (\d{1,2}) (\*|\?) \* (\?|\*|[A-Z,]+) \*\)$")

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client("scheduler")
    return _client


def schedule_name(reminder_id: str) -> str:
    return f"rem-{reminder_id}"


def build_cron(at: str, days: list[str] | None = None) -> str:
    """'20:00' (+ optional ['MON','THU']) -> EventBridge cron in the participant's local time."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at or "")
    if not m:
        raise ValueError("time must be HH:MM (24-hour)")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not days:
        return f"cron({minute} {hour} * * ? *)"
    days = [d.upper()[:3] for d in days]
    if any(d not in DAYS for d in days):
        raise ValueError(f"days must be from {DAYS}")
    return f"cron({minute} {hour} ? * {','.join(sorted(set(days), key=DAYS.index))} *)"


def next_send_at(reminder: dict, after: datetime | None = None) -> datetime | None:
    """Next fire time in UTC, or None if done / not computable (complex cron)."""
    tz = ZoneInfo(reminder["timezone"])
    now = (after or datetime.now(timezone.utc)).astimezone(tz)
    starts_on = _as_date(reminder.get("starts_on"))
    ends_on = _as_date(reminder.get("ends_on"))

    if reminder["schedule_kind"] == "one_time":
        send_at = _as_utc(reminder["send_at"])
        return send_at if send_at > now else None

    m = _SIMPLE_CRON.match(reminder.get("cron_expr") or "")
    if not m:
        return None
    minute, hour, _, dow = m.groups()
    allowed = set(range(7)) if dow in ("?", "*") else {DAYS.index(d) for d in dow.split(",")}

    day = now.date()
    if starts_on and starts_on > day:
        day = starts_on
    for _ in range(8):
        candidate = datetime.combine(day, time(int(hour), int(minute)), tz)
        if candidate > now and candidate.weekday() in allowed:
            if ends_on and candidate.date() > ends_on:
                return None
            return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)
    return None


def _as_date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _as_utc(v) -> datetime:
    if isinstance(v, datetime):
        dt = v
    else:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _expression(reminder: dict) -> str:
    if reminder["schedule_kind"] == "recurring":
        return reminder["cron_expr"]
    local = _as_utc(reminder["send_at"]).astimezone(ZoneInfo(reminder["timezone"]))
    return f"at({local.strftime('%Y-%m-%dT%H:%M:%S')})"


def _request(reminder: dict, enabled: bool) -> dict:
    target = {
        "Arn": TARGET_ARN,
        "RoleArn": ROLE_ARN,
        "Input": json.dumps({"reminder_id": reminder["reminder_id"]}),
        "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600},
    }
    req = {
        "Name": schedule_name(reminder["reminder_id"]),
        "GroupName": GROUP,
        "ScheduleExpression": _expression(reminder),
        "ScheduleExpressionTimezone": reminder["timezone"],
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": target,
        "State": "ENABLED" if enabled else "DISABLED",
    }
    tz = ZoneInfo(reminder["timezone"])
    if reminder["schedule_kind"] == "recurring":
        if reminder.get("starts_on"):
            req["StartDate"] = datetime.combine(_as_date(reminder["starts_on"]), time.min, tz)
        if reminder.get("ends_on"):
            req["EndDate"] = datetime.combine(_as_date(reminder["ends_on"]), time.max, tz)
    else:
        req["ActionAfterCompletion"] = "DELETE"
    if KMS_KEY_ARN:
        req["KmsKeyArn"] = KMS_KEY_ARN
    return req


def create(reminder: dict) -> str:
    return client().create_schedule(**_request(reminder, enabled=reminder["status"] == "active"))["ScheduleArn"]


def update(reminder: dict) -> str:
    try:
        return client().update_schedule(**_request(reminder, enabled=reminder["status"] == "active"))["ScheduleArn"]
    except client().exceptions.ResourceNotFoundException:
        # A one-time schedule deletes itself after firing; recreate it (nightly reconcile does the same).
        return create(reminder)


def delete(reminder_id: str) -> None:
    try:
        client().delete_schedule(Name=schedule_name(reminder_id), GroupName=GROUP)
    except client().exceptions.ResourceNotFoundException:
        pass
