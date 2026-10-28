from datetime import datetime, timezone

import pytest

from auth_triggers import participant_auth, staff_pre_token
from common import auth, safety, scheduling, whatsapp
from common.http import HttpError
from migrate.handler import split_sql


# ------------------------------------------------------------------ scheduling

def test_build_cron_daily_and_weekdays():
    assert scheduling.build_cron("20:00") == "cron(0 20 * * ? *)"
    assert scheduling.build_cron("8:05", ["thu", "MON"]) == "cron(5 8 ? * MON,THU *)"
    with pytest.raises(ValueError):
        scheduling.build_cron("25:00")
    with pytest.raises(ValueError):
        scheduling.build_cron("20:00", ["XYZ"])


def test_next_send_at_daily_uses_participant_time_zone():
    r = {"schedule_kind": "recurring", "cron_expr": "cron(0 20 * * ? *)", "timezone": "America/Chicago"}
    after = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # 07:00 in Chicago
    assert scheduling.next_send_at(r, after) == datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)  # 21:00 Chicago, already past today's dose
    assert scheduling.next_send_at(r, after) == datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)


def test_next_send_at_respects_days_start_and_end():
    r = {"schedule_kind": "recurring", "cron_expr": "cron(0 9 ? * MON *)", "timezone": "UTC",
         "starts_on": "2026-11-01", "ends_on": "2026-11-30"}
    after = datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert scheduling.next_send_at(r, after) == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    r["ends_on"] = "2026-11-01"
    assert scheduling.next_send_at(r, after) is None


def test_one_time_schedule_request_uses_local_time_and_deletes_after():
    r = {"reminder_id": "abc", "schedule_kind": "one_time", "send_at": "2026-11-01T14:00:00+00:00",
         "timezone": "America/New_York", "status": "active"}
    req = scheduling._request(r, enabled=True)
    assert req["ScheduleExpression"] == "at(2026-11-01T09:00:00)"
    assert req["ScheduleExpressionTimezone"] == "America/New_York"
    assert req["ActionAfterCompletion"] == "DELETE"
    assert req["Name"] == "rem-abc"


# ------------------------------------------------------------------ auth

def _event(claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_staff_claims_parse_flattened_groups_and_sites():
    s = auth.staff_claims(_event({"email": "pi@x.org", "cognito:groups": "[PI coordinator]",
                                  "custom:site_ids": "RIVERSIDE,LAKESIDE"}))
    assert s.role == "PI" and s.site_ids == ["RIVERSIDE", "LAKESIDE"]


def test_staff_claims_reject_non_staff_and_siteless():
    with pytest.raises(HttpError):
        auth.staff_claims(_event({"email": "x", "cognito:groups": "[sponsor]", "custom:site_ids": "A"}))
    with pytest.raises(HttpError):
        auth.staff_claims(_event({"email": "x", "cognito:groups": "[coordinator]"}))


def test_require_checks_action_site_and_study():
    s = auth.Staff(email="c@x.org", role="coordinator", site_ids=["RIVERSIDE"], study_ids=["KOA-301"])
    auth.require(s, "reminder:write", site_id="RIVERSIDE", study_id="KOA-301")
    for kwargs in ({"site_id": "LAKESIDE"}, {"study_id": "OTHER"}):
        with pytest.raises(HttpError):
            auth.require(s, "reminder:write", **kwargs)
    with pytest.raises(HttpError):
        auth.require(s, "ae:sign")


def test_participant_claims_come_from_token_only():
    assert auth.participant_claims(_event({"custom:participant_id": "P-0142"})).participant_id == "P-0142"
    with pytest.raises(HttpError):
        auth.participant_claims(_event({}))


def test_pre_token_adds_role_and_sites():
    event = {"request": {"userAttributes": {"custom:site_ids": "RIVERSIDE"},
                         "groupConfiguration": {"groupsToOverride": ["PI"]}}, "response": {}}
    claims = staff_pre_token.handler(event, None)["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert claims == {"role": "PI", "site_ids": "RIVERSIDE"}


# ------------------------------------------------------------------ participant passwordless login

def test_custom_auth_flow(monkeypatch):
    sent = []
    monkeypatch.setattr(whatsapp, "send_template", lambda *a, **k: sent.append((a, k)) or "wamid")

    define = lambda session: participant_auth.define_challenge({"request": {"session": session}, "response": {}}, None)
    assert define([])["response"]["challengeName"] == "CUSTOM_CHALLENGE"

    created = participant_auth.create_challenge(
        {"request": {"session": [], "userAttributes": {"phone_number": "+17325550142"}}, "response": {}}, None)
    code = created["response"]["privateChallengeParameters"]["code"]
    assert len(code) == 6 and len(sent) == 1 and sent[0][0][3] == [code]

    # A wrong answer keeps the same code and sends nothing new.
    retry = participant_auth.create_challenge({"request": {
        "session": [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": False,
                     "challengeMetadata": f"CODE-{code}"}],
        "userAttributes": {"phone_number": "+17325550142"}}, "response": {}}, None)
    assert retry["response"]["privateChallengeParameters"]["code"] == code and len(sent) == 1

    verify = lambda answer: participant_auth.verify_challenge(
        {"request": {"privateChallengeParameters": {"code": code}, "challengeAnswer": answer},
         "response": {}}, None)["response"]["answerCorrect"]
    assert verify(code) and not verify("000000" if code != "000000" else "111111")

    ok = define([{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": True}])["response"]
    assert ok["issueTokens"] is True
    failed = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": False}] * 3
    assert define(failed)["response"]["failAuthentication"] is True


# ------------------------------------------------------------------ Layer 0 and misc

@pytest.mark.parametrize("text", ["I can't breathe", "CHEST PAIN since this morning", "no puedo respirar",
                                  "Tengo dolor en el pecho", "my lips swelling", "me desmayé"])
def test_emergency_keywords_match(text):
    assert safety.emergency_keywords(text)


@pytest.mark.parametrize("text", ["I took my dose", "breathing fine, mild headache", "¿Cuándo es mi visita?"])
def test_emergency_keywords_no_false_alarm(text):
    assert not safety.emergency_keywords(text)


def test_stop_words():
    assert safety.is_stop(" STOP ") and safety.is_stop("Baja") and not safety.is_stop("stop the reminder at 8")


def test_ordered_template_params():
    assert whatsapp.ordered_params({"2": "with food", "1": "evening", "note": "x"}) == ["evening", "with food"]


def test_split_sql_keeps_dollar_blocks_and_quotes():
    sql = "CREATE TABLE a (x text DEFAULT 'a;b'); -- comment; here\nDO $$ BEGIN PERFORM 1; END $$;\nSELECT 2"
    assert split_sql(sql) == ["CREATE TABLE a (x text DEFAULT 'a;b')", "DO $$ BEGIN PERFORM 1; END $$", "SELECT 2"]


# ------------------------------------------------------------------ keeping personal data out of logs and side stores

def test_unhandled_errors_log_no_row_data(capsys):
    from common.http import log_error
    try:
        raise RuntimeError('ERROR: new row for relation "participants" violates check constraint "participants_status_check"'
                           "; Detail: Failing row contains (P-0142, Maria Lopez, +17325550142, 1968-04-12); SQLState: 23514")
    except RuntimeError as e:
        log_error(e)
    out = capsys.readouterr().out
    assert "sqlstate=23514 constraint=participants_status_check" in out
    assert not any(s in out for s in ("Maria", "+17325550142", "1968"))


def test_legacy_mirror_copies_no_name(monkeypatch):
    from common import legacy_mirror
    items = []
    monkeypatch.setattr(legacy_mirror, "LEGACY_TABLE", "t")
    monkeypatch.setattr(legacy_mirror, "_tbl", lambda: type("T", (), {"put_item": lambda self, Item: items.append(Item)})())
    legacy_mirror.sync({"participant_id": "P-0142", "full_name": "Maria Lopez", "phone_e164": "+17325550142",
                        "status": "enrolled", "study_id": "KOA-301", "created_at": "2026-09-02"}, [])
    assert items and "Maria" not in str(items)
