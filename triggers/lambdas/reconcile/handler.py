"""tt-reconcile: nightly EventBridge Scheduler cron -> this Lambda. Build plan Reminders step 7.

Compares active trial.reminders rows with the rem-<reminder_id> schedules and repairs drift:
creates schedules missing for active rows, deletes schedules whose row is no longer active.
DRY_RUN=1 (default) only reports. Input {"dry_run": false} overrides for one run.

Second duty: retention for trial.telemetry_spans. Spans older than TELEMETRY_RETENTION_DAYS (default 30, 0 disables)
are deleted; under DRY_RUN they are only counted. A prune failure is logged and never fails the reconcile run.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import schedules
import telemetry
from db import Tx

scheduler = schedules.scheduler


def handler(event, context):
    try:
        with telemetry.span("reconcile.run") as span:
            result = reconcile(event)
            span.set_attribute("trialtalk.reconcile.dry_run", bool(result["dry_run"]))
            for key in ("created", "deleted", "skipped"):
                span.set_attribute(f"trialtalk.reconcile.{key}", len(result[key]))
            span.set_attribute("trialtalk.reconcile.active_rows", result["active_rows"])
        return result
    finally:
        telemetry.flush()


def reconcile(event):
    dry = event.get("dry_run", os.environ.get("DRY_RUN", "1") == "1")
    with Tx() as tx:
        rows = {r["reminder_id"]: r for r in tx.q("""
            SELECT reminder_id::text, schedule_kind, cron_expr,
                   to_char(send_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') AS send_at, timezone,
                   starts_on::text AS starts_on, ends_on::text AS ends_on
            FROM trial.reminders WHERE status = 'active'""")}
    existing = set()
    for page in scheduler.get_paginator("list_schedules").paginate(GroupName=schedules.GROUP, NamePrefix="rem-"):
        existing |= {s["Name"][4:] for s in page["Schedules"]}

    created, deleted, skipped = [], [], []
    for rid, r in rows.items():
        if rid in existing:
            continue
        spec = schedule_spec(r)
        if not spec:
            skipped.append({"reminder_id": rid, "why": "one-time send_at already passed"})
            continue
        if not dry:
            scheduler.create_schedule(Name=f"rem-{rid}", GroupName=schedules.GROUP, FlexibleTimeWindow={"Mode": "OFF"},
                                      Target={**schedules.TARGET, "Input": json.dumps({"reminder_id": rid})}, **spec)
        created.append(rid)
    for rid in existing - rows.keys():
        if not dry:
            scheduler.delete_schedule(Name=f"rem-{rid}", GroupName=schedules.GROUP)
        deleted.append(rid)

    result = {"dry_run": dry, "active_rows": len(rows), "schedules": len(existing),
              "created": created, "deleted": deleted, "skipped": skipped, "pruned": prune_spans(dry)}
    print(json.dumps(result))
    return result


def prune_spans(dry):
    """Delete (or, when dry, count) telemetry spans past retention. Returns the count, or None if skipped or failed."""
    try:
        try:
            days = int(os.environ.get("TELEMETRY_RETENTION_DAYS", "30"))
        except ValueError:
            print(f"prune: bad TELEMETRY_RETENTION_DAYS={os.environ['TELEMETRY_RETENTION_DAYS']!r}, using 30")
            days = 30
        if days <= 0:
            return None
        with telemetry.span("reconcile.prune", **{"trialtalk.retention_days": days}) as span:
            cutoff = "start_ts < now() - make_interval(days => :d::int)"
            if dry:
                sql = f"SELECT count(*)::int AS n FROM trial.telemetry_spans WHERE {cutoff}"
            else:
                sql = (f"WITH gone AS (DELETE FROM trial.telemetry_spans WHERE {cutoff} RETURNING 1) "
                       "SELECT count(*)::int AS n FROM gone")
            with Tx() as tx:
                row = tx.one(sql, d=days)
            pruned = int((row or {}).get("n") or 0)
            span.set_attribute("trialtalk.pruned", pruned)
            return pruned
    except Exception as e:  # never let retention break the reconcile run
        print(f"prune failed: {type(e).__name__}: {e}")
        return None


def schedule_spec(r):
    tz = ZoneInfo(r["timezone"])
    if r["schedule_kind"] == "one_time":
        at = datetime.fromisoformat(r["send_at"]).replace(tzinfo=timezone.utc)
        if at <= datetime.now(timezone.utc):
            return None
        return dict(ScheduleExpression=f"at({at.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S})",
                    ScheduleExpressionTimezone="UTC", ActionAfterCompletion="DELETE")
    spec = dict(ScheduleExpression=r["cron_expr"], ScheduleExpressionTimezone=r["timezone"])
    if r["starts_on"]:
        start = datetime.fromisoformat(r["starts_on"]).replace(tzinfo=tz)
        if start > datetime.now(timezone.utc):
            spec["StartDate"] = start
    if r["ends_on"]:
        spec["EndDate"] = datetime.fromisoformat(r["ends_on"]).replace(hour=23, minute=59, tzinfo=tz)
    return spec
