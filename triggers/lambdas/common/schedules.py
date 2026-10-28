"""EventBridge Scheduler helpers. All TrialTalk schedules live in one group and target tt-reminder."""
import json
import os
from datetime import datetime, timedelta, timezone

import boto3

scheduler = boto3.client("scheduler")
GROUP = os.environ.get("SCHEDULE_GROUP", "trialtalk")
TARGET = dict(Arn=os.environ.get("REMINDER_FUNCTION_ARN", ""), RoleArn=os.environ.get("SCHEDULER_ROLE_ARN", ""))


def once_in(name, minutes, payload):
    """One-time schedule `minutes` from now that invokes tt-reminder with `payload`, then deletes itself."""
    at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    scheduler.create_schedule(
        Name=name[:64], GroupName=GROUP, ScheduleExpression=f"at({at})", ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="DELETE",
        Target={**TARGET, "Input": json.dumps(payload)})
    return at


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
