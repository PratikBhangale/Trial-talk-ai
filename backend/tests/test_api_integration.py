"""End-to-end through the real handlers, schema, grants and RLS (needs TT_PG_BIN; see conftest)."""

from conftest import OTHER_SITE_CLAIMS, STAFF_CLAIMS

MARIA = {"full_name": "Maria Lopez", "phone_e164": "+17325550142", "email": "maria.l@example.com",
         "study_id": "KOA-301", "site_id": "RIVERSIDE", "timezone": "America/Chicago",
         "preferred_language": "es", "whatsapp_opt_in": True}

EVENING_DOSE = {"reminder_type": "dose", "label": "Evening dose", "dose_label": "evening",
                "dose_instructions": "1 tablet with food", "time": "20:00",
                "starts_on": "2026-09-03", "ends_on": "2027-03-03", "escalate_after_misses": 3}


def enroll(call_staff, **overrides):
    status, body = call_staff("POST", "/staff/participants", {**MARIA, **overrides})
    assert status == 201, body
    return body["participant"]["participant_id"]


# ------------------------------------------------------------------ staff: participants

def test_create_list_and_read_participant(call_staff, aws):
    pid = enroll(call_staff)
    assert pid.startswith("P-")
    assert aws.cognito.users[pid]["custom:participant_id"] == pid

    status, body = call_staff("GET", "/staff/participants", query={"q": "lopez"})
    assert status == 200 and [p["participant_id"] for p in body["participants"]] == [pid]

    status, body = call_staff("GET", f"/staff/participants/{pid}")
    assert status == 200 and body["participant"]["timezone"] == "America/Chicago"


def test_other_site_cannot_see_or_create(call_staff):
    pid = enroll(call_staff)
    assert call_staff("GET", "/staff/participants", claims=OTHER_SITE_CLAIMS)[1]["participants"] == []
    assert call_staff("GET", f"/staff/participants/{pid}", claims=OTHER_SITE_CLAIMS)[0] == 404
    assert call_staff("POST", "/staff/participants", {**MARIA, "phone_e164": "+17325550199"},
                      claims=OTHER_SITE_CLAIMS)[0] == 403


def test_rls_blocks_cross_site_insert_even_if_api_check_missed(aws, monkeypatch):
    """Layer 5: Aurora refuses rows outside app.site_ids no matter what the Lambda does."""
    import pytest
    from common import db
    monkeypatch.setattr(db, "SECRET_ARN", "staff")
    with pytest.raises(Exception, match="row-level security"):
        with db.tx(site_ids=["LAKESIDE"]) as t:
            t.execute("INSERT INTO pii.participants (participant_id, full_name, phone_e164, study_id, site_id, status) "
                      "VALUES ('P-9001', 'X', '+15550000001', 'KOA-301', 'RIVERSIDE', 'enrolled')")


def test_ids_skip_ones_taken_at_sites_you_cannot_see(call_staff):
    """db/ has no id default: staff_api picks the next id, but RLS hides other sites' ids from it."""
    assert enroll(call_staff) == "P-1001"
    status, body = call_staff("POST", "/staff/participants", {**MARIA, "phone_e164": "+17325550150",
                                                              "site_id": "LAKESIDE"}, claims=OTHER_SITE_CLAIMS)
    assert status == 201 and body["participant"]["participant_id"] == "P-1002"  # P-1001 was invisible but taken


def test_duplicate_phone_is_409(call_staff):
    enroll(call_staff)
    assert call_staff("POST", "/staff/participants", MARIA)[0] == 409


def test_update_participant_identity_needs_reason(call_staff, aws):
    pid = enroll(call_staff)
    assert call_staff("PATCH", f"/staff/participants/{pid}", {"phone_e164": "+17325550143"})[0] == 400
    status, body = call_staff("PATCH", f"/staff/participants/{pid}",
                              {"phone_e164": "+17325550143", "change_reason": "new number, verified by phone"})
    assert status == 200 and body["participant"]["phone_e164"] == "+17325550143"
    assert aws.cognito.users[pid]["phone_number"] == "+17325550143"
    assert call_staff("PATCH", f"/staff/participants/{pid}", {"status": "withdrawn"})[0] == 400


def test_withdraw_cancels_reminders_and_keeps_records(call_staff, aws, admin):
    pid = enroll(call_staff)
    rid = call_staff("POST", f"/staff/participants/{pid}/reminders", EVENING_DOSE)[1]["reminder"]["reminder_id"]
    assert f"rem-{rid}" in aws.scheduler.schedules

    assert call_staff("DELETE", f"/staff/participants/{pid}")[0] == 400  # reason required
    status, body = call_staff("DELETE", f"/staff/participants/{pid}", {"reason": "participant request"})
    assert status == 200 and body["participant"]["status"] == "withdrawn" and body["cancelled_reminders"] == 1
    assert f"rem-{rid}" not in aws.scheduler.schedules
    assert pid in aws.cognito.disabled
    # Nothing was deleted
    assert admin.execute("SELECT count(*) FROM pii.participants WHERE participant_id = %s", (pid,)).fetchone()[0] == 1
    log = admin.execute("SELECT status, change_log FROM trial.reminders WHERE reminder_id = %s", (rid,)).fetchone()
    assert log[0] == "cancelled" and log[1][-1]["reason"].startswith("participant withdrawn")


# ------------------------------------------------------------------ staff: reminders and dosing

def test_dose_reminder_lifecycle(call_staff, aws):
    pid = enroll(call_staff)
    status, body = call_staff("POST", f"/staff/participants/{pid}/reminders", EVENING_DOSE)
    assert status == 201, body
    r = body["reminder"]
    assert r["cron_expr"] == "cron(0 20 * * ? *)" and r["timezone"] == "America/Chicago"
    assert r["template_params"] == {"1": "evening", "2": "1 tablet with food"}
    assert r["buttons"] == ["Taken", "Skipped", "Remind me in 30 min"]
    assert r["template_language"] == "es" and r["next_send_at"] and r["scheduler_arn"]
    sched = aws.scheduler.schedules[f"rem-{r['reminder_id']}"]
    assert sched["ScheduleExpressionTimezone"] == "America/Chicago" and sched["State"] == "ENABLED"

    # Change the time and the dose instructions
    status, body = call_staff("PATCH", f"/staff/reminders/{r['reminder_id']}",
                              {"time": "21:30", "dose_instructions": "2 tablets with food", "reason": "PI order"})
    assert status == 200, body
    r2 = body["reminder"]
    assert r2["cron_expr"] == "cron(30 21 * * ? *)" and r2["template_params"]["2"] == "2 tablets with food"
    fields = {c["field"] for c in r2["change_log"]}
    assert {"cron_expr", "template_params"} <= fields

    # Pause -> schedule disabled; resume -> enabled
    assert call_staff("PATCH", f"/staff/reminders/{r['reminder_id']}", {"status": "paused"})[1]["reminder"][
        "next_send_at"] is None
    assert aws.scheduler.schedules[f"rem-{r['reminder_id']}"]["State"] == "DISABLED"
    call_staff("PATCH", f"/staff/reminders/{r['reminder_id']}", {"status": "active"})
    assert aws.scheduler.schedules[f"rem-{r['reminder_id']}"]["State"] == "ENABLED"

    # Cancel
    status, body = call_staff("DELETE", f"/staff/reminders/{r['reminder_id']}")
    assert status == 200 and body["reminder"]["status"] == "cancelled"
    assert f"rem-{r['reminder_id']}" not in aws.scheduler.schedules
    assert call_staff("PATCH", f"/staff/reminders/{r['reminder_id']}", {"time": "08:00"})[0] == 409


def test_reminder_validation(call_staff):
    pid = enroll(call_staff, whatsapp_opt_in=False)
    url = f"/staff/participants/{pid}/reminders"
    assert call_staff("POST", url, {**EVENING_DOSE, "dose_label": None})[0] == 400
    assert call_staff("POST", url, {**EVENING_DOSE, "time": "8pm"})[0] == 400
    assert call_staff("POST", url, {**EVENING_DOSE, "buttons": ["a", "b", "c", "d"]})[0] == 400
    assert call_staff("POST", url, EVENING_DOSE)[0] == 409  # not opted in to WhatsApp
    status, body = call_staff("POST", url, {**EVENING_DOSE, "status": "draft"})
    assert status == 201 and body["reminder"]["scheduler_arn"] is None


def test_send_now_sends_template_and_logs_dose(call_staff, aws, admin):
    pid = enroll(call_staff)
    rid = call_staff("POST", f"/staff/participants/{pid}/reminders", EVENING_DOSE)[1]["reminder"]["reminder_id"]
    status, body = call_staff("POST", f"/staff/reminders/{rid}/send-now")
    assert status == 200 and body["sent"] is True, body

    msg = aws.whatsapp.sent[-1]
    assert msg["to"] == "+17325550142" and msg["template"]["name"] == "dose_reminder_v2"
    assert msg["template"]["language"]["code"] == "es"
    assert [p["text"] for p in msg["template"]["components"][0]["parameters"]] == ["evening", "1 tablet with food"]
    assert msg["template"]["components"][1]["parameters"][0]["payload"] == f"rem:{rid}:Taken"

    dose = admin.execute("SELECT record_type, response, dose_label, created_by FROM trial.clinical_events "
                         "WHERE reminder_id = %s", (rid,)).fetchone()
    assert dose == ("DOSE", "no_response", "evening", "reminder-lambda")
    assert admin.execute("SELECT send_count FROM trial.reminders WHERE reminder_id = %s", (rid,)).fetchone()[0] == 1

    status, body = call_staff("GET", f"/staff/participants/{pid}/adherence")
    assert body["counts"] == {"no_response": 1} and body["taken_rate"] == 0
    status, body = call_staff("GET", f"/staff/participants/{pid}/events")
    assert status == 200 and [(e["record_type"], e["dose_label"]) for e in body["events"]] == [("DOSE", "evening")]


def test_clinical_events_are_append_only(aws, admin, call_staff, monkeypatch):
    import pytest
    from common import db
    pid = enroll(call_staff)
    admin.execute("INSERT INTO trial.clinical_events (participant_id, record_type, source, created_by) "
                  "VALUES (%s, 'VISIT', 'portal', 'staff:x')", (pid,))
    for secret in ("staff", "messaging", "participant"):
        monkeypatch.setattr(db, "SECRET_ARN", secret)
        with pytest.raises(Exception, match="permission denied"):
            with db.tx(site_ids=["RIVERSIDE"], participant_id=pid) as t:
                t.execute("DELETE FROM trial.clinical_events")
        with pytest.raises(Exception, match="permission denied"):
            with db.tx(site_ids=["RIVERSIDE"], participant_id=pid) as t:
                t.execute("UPDATE trial.clinical_events SET status = 'x'")


# ------------------------------------------------------------------ participant website

def test_participant_sees_own_profile_reminders_and_dose_schedule(call_staff, call_participant):
    pid = enroll(call_staff)
    other = enroll(call_staff, full_name="John Doe", phone_e164="+17325550150")
    call_staff("POST", f"/staff/participants/{pid}/reminders", EVENING_DOSE)
    call_staff("POST", f"/staff/participants/{other}/reminders", EVENING_DOSE)

    status, body = call_participant(pid, "GET", "/me")
    assert status == 200 and body["participant"]["participant_id"] == pid

    status, body = call_participant(pid, "GET", "/me/reminders")
    assert status == 200 and len(body["reminders"]) == 1
    dose = body["dose_schedule"][0]
    assert dose["label"] == "Evening dose" and dose["schedule"] == "Every day at 8:00 PM"
    # db/ keeps template_params (the dose instructions) from participants, along with the staff-only columns
    assert not {"template_params", "change_log", "template_name"} & dose.keys()


def test_participant_can_edit_contact_but_not_identity(call_staff, call_participant):
    pid = enroll(call_staff)
    status, body = call_participant(pid, "PATCH", "/me", {"city": "Springfield", "timezone": "America/New_York"})
    assert status == 200 and body["participant"]["city"] == "Springfield"
    assert call_participant(pid, "PATCH", "/me", {"full_name": "Someone Else"})[0] == 400
    assert call_participant(pid, "PATCH", "/me", {"phone_e164": "+15550000000"})[0] == 400


def test_participant_cannot_reach_other_participants(call_staff, call_participant):
    pid = enroll(call_staff)
    assert call_participant("P-9999", "GET", "/me")[0] == 404
    assert call_participant("P-9999", "GET", "/me/reminders")[1]["reminders"] == []


def test_participant_history_hides_staff_only_records(call_staff, call_participant, admin):
    pid = enroll(call_staff)
    admin.execute("INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text, created_by)"
                  " VALUES (%s, 'SYMPTOM', 'whatsapp', 'rash near the injection spot', 'agent')", (pid,))
    admin.execute("INSERT INTO trial.clinical_events (participant_id, record_type, source, summary, created_by,"
                  " staff_notes) VALUES (%s, 'AE', 'whatsapp', 'possible AE', 'agent', 'grade 1')", (pid,))
    status, body = call_participant(pid, "GET", "/me/history")
    assert status == 200 and [e["record_type"] for e in body["events"]] == ["SYMPTOM"]
    assert "staff_notes" not in body["events"][0]


def test_participant_requests_reach_staff_queue(call_staff, call_participant):
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/requests",
                                    {"kind": "reminder_change", "message": "Can my evening dose reminder be at 9pm?"})
    assert status == 201, body
    status, body = call_staff("GET", "/staff/requests")
    assert [r["participant_id"] for r in body["requests"]] == [pid]
    assert call_staff("GET", "/staff/requests", claims=OTHER_SITE_CLAIMS)[1]["requests"] == []
