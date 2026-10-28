"""db/06_alerts_web.sql: emergency_alerts, the 'web' channel and the telemetry prune grant, as the real logins
(integration; needs TT_PG_BIN)."""

import pytest


def _connect(pg, user):
    import psycopg
    return psycopg.connect(f"host=127.0.0.1 port={pg['port']} dbname=trialtalk user={user}", autocommit=True)


@pytest.fixture
def participant(admin, clean_db):
    admin.execute("INSERT INTO pii.participants (participant_id, full_name, phone_e164, study_id, site_id, status) "
                  "VALUES ('P-A1', 'Alert Test', '+15550000001', 'S1', 'RIVERSIDE', 'enrolled')")
    return "P-A1"


def test_messaging_can_insert_find_and_update_alert(pg, participant):
    """What the router's raise_alert does: look for an open alert, fold a repeat into it, else insert."""
    with _connect(pg, "tt_messaging") as c:
        c.execute("INSERT INTO trial.emergency_alerts (participant_id, severity, source, message, keywords) "
                  "VALUES (%s, 'life_threatening', 'chat_keyword', 'chest pain', %s::text[])",
                  (participant, '{"chest pain"}'))
        alert_id = c.execute("SELECT alert_id FROM trial.emergency_alerts WHERE participant_id = %s "
                             "AND status <> 'resolved' AND last_pressed_at > now() - make_interval(mins => 30) "
                             "ORDER BY created_at DESC LIMIT 1", (participant,)).fetchone()[0]
        c.execute("UPDATE trial.emergency_alerts SET press_count = press_count + 1, last_pressed_at = now(), "
                  "message = %s::text WHERE alert_id = %s::uuid", ("still hurts", alert_id))
        row = c.execute("SELECT press_count, message, status, keywords FROM trial.emergency_alerts "
                        "WHERE alert_id = %s", (alert_id,)).fetchone()
    assert row == (2, "still hurts", "open", ["chest pain"])


def test_messaging_cannot_triage_or_delete_alerts(pg, participant, admin):
    import psycopg
    admin.execute("INSERT INTO trial.emergency_alerts (participant_id, source) VALUES (%s, 'sos_button')",
                  (participant,))
    with _connect(pg, "tt_messaging") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE trial.emergency_alerts SET status = 'resolved'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM trial.emergency_alerts")


def test_alerts_cannot_be_deleted_even_by_owner(admin, participant):
    import psycopg
    admin.execute("INSERT INTO trial.emergency_alerts (participant_id, source) VALUES (%s, 'sos_button')",
                  (participant,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # trial.block_mutation() raises 42501
        admin.execute("DELETE FROM trial.emergency_alerts")


def test_participant_sees_only_own_alerts(pg, participant, admin):
    admin.execute("INSERT INTO pii.participants (participant_id, full_name, phone_e164, study_id, site_id, status) "
                  "VALUES ('P-A2', 'Other', '+15550000002', 'S1', 'RIVERSIDE', 'enrolled')")
    admin.execute("INSERT INTO trial.emergency_alerts (participant_id, source) VALUES ('P-A1', 'sos_button'), "
                  "('P-A2', 'sos_button')")
    with _connect(pg, "tt_participant") as c, c.transaction():
        c.execute("SELECT set_config('app.participant_id', 'P-A1', true)")
        rows = c.execute("SELECT participant_id FROM trial.emergency_alerts").fetchall()
    assert rows == [("P-A1",)]


def test_participant_can_insert_own_web_communication(pg, participant):
    with _connect(pg, "tt_participant") as c, c.transaction():
        c.execute("SELECT set_config('app.participant_id', %s, true)", (participant,))
        c.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, "
                  "session_id, latency_ms, input_tokens, output_tokens, model_id) "
                  "VALUES (%s, 'web', 'inbound', 'text', 'hello', 'sess', 10, 5, 7, 'm')", (participant,))
        rows = c.execute("SELECT channel, body_text FROM trial.communications").fetchall()
    assert rows == [("web", "hello")]


def test_participant_cannot_insert_web_row_for_someone_else(pg, participant):
    import psycopg
    with _connect(pg, "tt_participant") as c, c.transaction():
        c.execute("SELECT set_config('app.participant_id', %s, true)", (participant,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text) "
                      "VALUES ('P-OTHER', 'web', 'inbound', 'text', 'hi')")


def test_channel_check_allows_web_and_rejects_unknown(admin, participant):
    import psycopg
    admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type) "
                  "VALUES (%s, 'web', 'inbound', 'text')", (participant,))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type) "
                      "VALUES (%s, 'sms', 'inbound', 'text')", (participant,))


def test_messaging_can_prune_old_telemetry_spans(pg, clean_db, admin):
    admin.execute("INSERT INTO trial.telemetry_spans (span_id, trace_id, name, service, start_ts, duration_ms) VALUES "
                  "('old', 't', 'n', 's', now() - interval '30 days', 1), ('new', 't', 'n', 's', now(), 1)")
    with _connect(pg, "tt_messaging") as c:
        deleted = c.execute("DELETE FROM trial.telemetry_spans WHERE start_ts < now() - interval '14 days'").rowcount
        left = [r[0] for r in c.execute("SELECT span_id FROM trial.telemetry_spans").fetchall()]
    assert deleted == 1 and left == ["new"]


def test_other_roles_cannot_delete_telemetry_spans(pg):
    import psycopg
    for user in ("tt_portal", "tt_participant"):
        with _connect(pg, user) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("DELETE FROM trial.telemetry_spans")
