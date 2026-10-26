"""Bridge to the prototype's DynamoDB table while the agent and WhatsApp Lambda still read it.

Aurora is the source of truth. After a staff change we copy the few fields the deployed agent
(store.get_profile) and WhatsApp Lambda (find_participant) need. Remove this module once both
read Aurora. Disabled when LEGACY_TABLE is empty.
"""

import os
import re

import boto3

LEGACY_TABLE = os.environ.get("LEGACY_TABLE", "")

_table = None


def _tbl():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(LEGACY_TABLE)
    return _table


def _dose_time(cron_expr: str) -> str | None:
    m = re.match(r"^cron\((\d{1,2}) (\d{1,2}) ", cron_expr or "")
    if not m:
        return None
    minute, hour = int(m.group(1)), int(m.group(2))
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def sync(participant: dict, reminders: list[dict], old_phone: str | None = None) -> None:
    if not LEGACY_TABLE:
        return
    pid = participant["participant_id"]
    phone = participant["phone_e164"].lstrip("+")
    active = participant["status"] not in ("withdrawn", "screen_failed")

    if old_phone and old_phone.lstrip("+") != phone:
        _tbl().delete_item(Key={"PK": f"PHONE#{old_phone.lstrip('+')}", "SK": "LOOKUP"})

    if not active:
        _tbl().delete_item(Key={"PK": f"PHONE#{phone}", "SK": "LOOKUP"})
        return

    dose_times = [t for r in reminders
                  if r["reminder_type"] == "dose" and r["status"] == "active" and r["schedule_kind"] == "recurring"
                  for t in [_dose_time(r["cron_expr"])] if t]
    _tbl().put_item(Item={
        "PK": f"P#{pid}", "SK": "PROFILE",
        "name": participant["full_name"],
        "trial_id": participant["study_id"],
        "enrolled_on": str(participant["created_at"])[:10],
        "dose_times": dose_times or ["(no dose schedule set)"],
    })
    _tbl().put_item(Item={"PK": f"PHONE#{phone}", "SK": "LOOKUP", "participant_id": pid})
