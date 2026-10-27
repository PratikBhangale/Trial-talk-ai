"""Emergency alerts end to end: SOS button -> staff widget -> acknowledge -> resolve (needs TT_PG_BIN)."""

import pytest

from conftest import OTHER_SITE_CLAIMS
from test_api_integration import enroll


def press(call_participant, pid, body=None):
    return call_participant(pid, "POST", "/me/emergency", body)


def test_one_press_with_no_input_raises_a_life_threatening_alert(call_staff, call_participant, aws):
    pid = enroll(call_staff)
    status, out = press(call_participant, pid)  # no body at all: the button sends nothing
    assert status == 200 and out["new"] is True
    assert out["alert"]["severity"] == "life_threatening" and out["alert"]["status"] == "open"
    assert "911" in out["reply"] and "Estamos" in out["reply"]  # Maria prefers Spanish
    assert len(aws.emails) == 1 and "URGENT" in aws.emails[0]["Content"]["Simple"]["Subject"]["Data"]

    status, out = call_staff("GET", "/staff/alerts")
    assert status == 200 and out["open"] == 1
    a = out["alerts"][0]
    assert (a["participant_id"], a["full_name"], a["source"], a["site_id"]) == (pid, "Maria Lopez", "sos_button", "RIVERSIDE")


def test_repeat_presses_fold_into_one_alert_and_one_email(call_staff, call_participant, aws):
    pid = enroll(call_staff)
    for _ in range(3):
        press(call_participant, pid)
    status, out = press(call_participant, pid, {"message": "my chest hurts"})
    assert out["new"] is False and out["alert"]["press_count"] == 4
    alerts = call_staff("GET", "/staff/alerts")[1]["alerts"]
    assert len(alerts) == 1 and alerts[0]["press_count"] == 4 and alerts[0]["message"] == "my chest hurts"
    assert len(aws.emails) == 1


def test_acknowledge_then_resolve(call_staff, call_participant):
    pid = enroll(call_staff)
    press(call_participant, pid)
    aid = call_staff("GET", "/staff/alerts")[1]["alerts"][0]["alert_id"]

    status, out = call_staff("POST", f"/staff/alerts/{aid}/acknowledge")
    assert status == 200 and out["alert"]["status"] == "acknowledged"
    assert out["alert"]["acknowledged_by"] == "staff:coordinator@riverside.example.org"
    assert call_staff("GET", "/staff/alerts")[1]["open"] == 0  # acknowledged is no longer "open"

    assert call_staff("POST", f"/staff/alerts/{aid}/resolve", {})[0] == 400  # a note is required
    status, out = call_staff("POST", f"/staff/alerts/{aid}/resolve", {"note": "Called 911, ambulance dispatched"})
    assert status == 200 and out["alert"]["status"] == "resolved"
    assert call_staff("POST", f"/staff/alerts/{aid}/resolve", {"note": "again"})[0] == 409
    assert call_staff("GET", "/staff/alerts")[1]["alerts"] == []
    resolved = call_staff("GET", "/staff/alerts", query={"status": "resolved"})[1]["alerts"]
    assert resolved[0]["resolution_note"].startswith("Called 911")

    # The participant sees the progress, not staff notes.
    mine = call_participant(pid, "GET", "/me/emergency")[1]
    assert mine["alerts"][0]["status"] == "resolved" and "resolution_note" not in mine["alerts"][0]


def test_a_press_after_resolution_opens_a_new_alert(call_staff, call_participant):
    pid = enroll(call_staff)
    press(call_participant, pid)
    aid = call_staff("GET", "/staff/alerts")[1]["alerts"][0]["alert_id"]
    call_staff("POST", f"/staff/alerts/{aid}/resolve", {"note": "false alarm, phoned participant"})
    assert press(call_participant, pid)[1]["new"] is True
    assert len(call_staff("GET", "/staff/alerts", query={"status": "all"})[1]["alerts"]) == 2


def test_other_sites_cannot_see_or_touch_alerts(call_staff, call_participant):
    pid = enroll(call_staff)
    press(call_participant, pid)
    aid = call_staff("GET", "/staff/alerts")[1]["alerts"][0]["alert_id"]
    assert call_staff("GET", "/staff/alerts", claims=OTHER_SITE_CLAIMS)[1]["alerts"] == []
    assert call_staff("POST", f"/staff/alerts/{aid}/acknowledge", claims=OTHER_SITE_CLAIMS)[0] == 404
    assert call_staff("POST", f"/staff/alerts/{aid}/resolve", {"note": "x"}, claims=OTHER_SITE_CLAIMS)[0] == 404


def test_participants_cannot_see_each_others_alerts_or_edit_status(call_staff, call_participant, aws, monkeypatch):
    from common import db
    a = enroll(call_staff)
    b = enroll(call_staff, phone_e164="+17325550143", full_name="Bo Park")
    press(call_participant, a)
    assert call_participant(b, "GET", "/me/emergency")[1]["alerts"] == []
    monkeypatch.setattr(db, "SECRET_ARN", "participant")
    with pytest.raises(Exception, match="permission denied"):  # no UPDATE grant on status
        with db.tx(participant_id=a) as t:
            t.execute("UPDATE trial.emergency_alerts SET status = 'resolved'")


def test_validation(call_staff, call_participant):
    pid = enroll(call_staff)
    assert press(call_participant, pid, {"severity": "meh"})[0] == 400
    assert call_staff("GET", "/staff/alerts", query={"status": "bogus"})[0] == 400
    assert call_staff("POST", "/staff/alerts/not-a-uuid/acknowledge")[0] == 400


def test_chat_emergency_keywords_raise_an_alert_too(call_staff, call_participant):
    pid = enroll(call_staff)
    call_participant(pid, "POST", "/me/chat", {"text": "I have chest pain and can't breathe"})
    a = call_staff("GET", "/staff/alerts")[1]["alerts"][0]
    assert a["source"] == "chat_keyword" and "chest pain" in a["keywords"] and "chest pain" in a["message"]


def test_withdrawn_participant_gets_instructions_but_no_page(call_staff, call_participant, aws):
    pid = enroll(call_staff)
    call_staff("DELETE", f"/staff/participants/{pid}", {"reason": "withdrew consent"})
    status, out = press(call_participant, pid)
    assert status == 200 and out["alert"] is None and "911" in out["reply"]
    assert aws.emails == [] and call_staff("GET", "/staff/alerts")[1]["alerts"] == []


def test_chat_records_token_usage_and_staff_can_see_it(call_staff, call_participant, aws):
    pid = enroll(call_staff)
    call_participant(pid, "POST", "/me/chat", {"text": "When is my next visit?"})
    call_participant(pid, "POST", "/me/chat", {"text": "Thanks"})
    status, out = call_staff("GET", "/staff/usage", query={"days": "7"})
    assert status == 200 and out["totals"]["calls"] == 2
    assert out["totals"]["input_tokens"] > 800 and out["totals"]["output_tokens"] > 0
    assert out["by_participant"][0]["participant_id"] == pid and len(out["by_day"]) == 1
    assert call_staff("GET", "/staff/usage", claims=__import__("conftest").OTHER_SITE_CLAIMS)[1]["totals"]["calls"] == 0
    assert call_staff("GET", "/staff/usage", query={"days": "x"})[0] == 400
