"""Clinic view for the staff portal: trials at a glance and one trial's drill-down.

Everything here is aggregate counts, plus the newest critical alerts (participant id, name, status) of a trial. It
runs under the caller's row-level security: every figure is computed from the participants staff can see (their
sites), so a trial's numbers never include another site's patients, and a trial none of the staff's patients are in
does not exist for them (404, not an empty page).

Definitions (the labels in the portal say the same):
  patients        participants in the trial, withdrawn and screen_failed excluded
  engaged         a patient with an inbound WhatsApp message in the last 14 days (pct_not_engaging is the rest)
  adherence_pct   DOSE events scheduled in the last 14 days: taken / (taken + skipped + no_response), null when none
  symptoms        current (not superseded) SYMPTOM events, all time
  critical        emergency alerts raised by the trial's participants, all time (critical_open: not yet resolved)
  agent_handled   symptoms minus the urgent ones: logged by the agent without paging anyone. Urgent means severity
                  urgent, or status escalated/urgent (the router escalates emergency keywords that way)
"""

from datetime import datetime, timedelta, timezone

from common import db
from common.auth import Staff, require
from common.http import HttpError, Router

router = Router()

ENGAGE_DAYS = 14
ADHERENCE_DAYS = 14
SERIES_DAYS = 14
CRITICAL_LIST_LIMIT = 20
INACTIVE = "('withdrawn', 'screen_failed')"
URGENT = "(e.severity_hint = 'urgent' OR e.status IN ('escalated', 'urgent'))"

# One row per study that has a participant this caller can see. trial.studies is reference data (no RLS); a study
# with no row there falls back to its id as the name.
STUDY_ROWS = f"""
WITH pp AS (
  SELECT p.study_id, (p.status NOT IN {INACTIVE}) AS active,
         EXISTS (SELECT 1 FROM trial.communications c
                 WHERE c.participant_id = p.participant_id AND c.direction = 'inbound' AND c.channel = 'whatsapp'
                   AND c.created_at > now() - interval '{ENGAGE_DAYS} days') AS engaged
  FROM pii.participants p
  WHERE (:study::text IS NULL OR p.study_id = :study)
), pats AS (
  SELECT study_id, count(*) FILTER (WHERE active) AS patients,
         count(*) FILTER (WHERE active AND engaged) AS engaged
  FROM pp GROUP BY study_id
), dose AS (
  SELECT p.study_id, count(*) FILTER (WHERE e.response = 'taken') AS taken,
         count(*) FILTER (WHERE e.response IN ('taken', 'skipped', 'no_response')) AS due
  FROM trial.current_events e JOIN pii.participants p USING (participant_id)
  WHERE e.record_type = 'DOSE' AND e.scheduled_at <= now()
    AND e.scheduled_at > now() - interval '{ADHERENCE_DAYS} days'
    AND (:study::text IS NULL OR p.study_id = :study)
  GROUP BY p.study_id
), sym AS (
  SELECT p.study_id, count(*) AS symptoms, count(*) FILTER (WHERE {URGENT}) AS urgent
  FROM trial.current_events e JOIN pii.participants p USING (participant_id)
  WHERE e.record_type = 'SYMPTOM' AND (:study::text IS NULL OR p.study_id = :study)
  GROUP BY p.study_id
), alerts AS (
  SELECT p.study_id, count(*) AS critical, count(*) FILTER (WHERE a.status <> 'resolved') AS critical_open
  FROM trial.emergency_alerts a JOIN pii.participants p USING (participant_id)
  WHERE (:study::text IS NULL OR p.study_id = :study)
  GROUP BY p.study_id
)
SELECT pats.study_id, coalesce(s.name, pats.study_id) AS name, s.phase, s.condition, s.description,
       pats.patients, pats.engaged,
       coalesce(dose.taken, 0) AS taken, coalesce(dose.due, 0) AS due,
       coalesce(sym.symptoms, 0) AS symptoms, coalesce(sym.urgent, 0) AS urgent,
       coalesce(alerts.critical, 0) AS critical, coalesce(alerts.critical_open, 0) AS critical_open
FROM pats LEFT JOIN trial.studies s USING (study_id)
LEFT JOIN dose USING (study_id) LEFT JOIN sym USING (study_id) LEFT JOIN alerts USING (study_id)
ORDER BY pats.study_id
"""


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def trial_row(r: dict) -> dict:
    """A STUDY_ROWS row as the API's per-trial object."""
    patients, engaged = int(r["patients"]), int(r["engaged"])
    symptoms, urgent, due = int(r["symptoms"]), int(r["urgent"]), int(r["due"])
    return {
        "study_id": r["study_id"], "name": r["name"], "phase": r["phase"], "condition": r["condition"],
        "patients": patients,
        "engaged_pct": _pct(engaged, patients),
        "adherence_pct": round(100.0 * int(r["taken"]) / due, 1) if due else None,
        "symptoms": symptoms,
        "critical": int(r["critical"]),
        "agent_handled": symptoms - urgent,
    }


def summarize(rows: list[dict]) -> dict:
    patients = sum(int(r["patients"]) for r in rows)
    engaged = sum(int(r["engaged"]) for r in rows)
    return {"trials": len(rows), "patients": patients, "pct_not_engaging": _pct(patients - engaged, patients),
            "critical_open": sum(int(r["critical_open"]) for r in rows)}


def series(rows: list[dict], today) -> list[dict]:
    """The last SERIES_DAYS days ending today (UTC), oldest first, zero where a day has no symptoms."""
    by_day = {str(r["day"]): r for r in rows}
    out = []
    for back in range(SERIES_DAYS - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        r = by_day.get(day)
        out.append({"day": day, "total": int(r["total"]) if r else 0, "critical": int(r["critical"]) if r else 0})
    return out


@router.route("GET", "/staff/clinic")
def clinic(event, staff: Staff):
    """Every trial the caller has patients in, with a summary across them."""
    require(staff, "ops:read")
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(STUDY_ROWS, study=None)
    return 200, {"summary": summarize(rows), "trials": [trial_row(r) for r in rows]}


@router.route("GET", "/staff/clinic/{study_id}")
def clinic_study(event, staff: Staff, study_id: str):
    """One trial: its numbers, symptoms per day (last 14), the newest critical alerts and who is not engaging."""
    require(staff, "ops:read")
    today = datetime.now(timezone.utc).date()
    with db.tx(site_ids=staff.site_ids) as t:
        row = t.one(STUDY_ROWS, study=study_id)
        if not row:
            raise HttpError(404, "Trial not found")
        days = t.query(
            "SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day, count(*) AS total, "
            f"       count(*) FILTER (WHERE {URGENT}) AS critical "
            "FROM trial.current_events e JOIN pii.participants p USING (participant_id) "
            "WHERE e.record_type = 'SYMPTOM' AND p.study_id = :study "
            "  AND (e.occurred_at AT TIME ZONE 'UTC')::date >= :since::date "
            "GROUP BY 1",
            study=study_id, since=(today - timedelta(days=SERIES_DAYS - 1)).isoformat())
        critical = t.query(
            "SELECT a.participant_id, p.full_name, a.created_at, a.status, a.alert_id "
            "FROM trial.emergency_alerts a JOIN pii.participants p USING (participant_id) "
            "WHERE p.study_id = :study ORDER BY a.created_at DESC LIMIT :n",
            study=study_id, n=CRITICAL_LIST_LIMIT)
    trial = trial_row(row)
    engaged = int(row["engaged"])
    return 200, {
        "study": {k: row[k] for k in ("study_id", "name", "phase", "condition", "description")},
        "summary": trial,
        "symptoms_by_day": series(days, today),
        "critical_list": critical,
        "engagement": {"engaged": engaged, "not_engaging": trial["patients"] - engaged},
    }
