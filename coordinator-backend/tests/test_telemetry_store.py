"""db/05_telemetry.sql: trial.telemetry_spans grants, as the real logins (integration; needs TT_PG_BIN)."""

import json
from datetime import datetime, timezone

INSERT = ("INSERT INTO trial.telemetry_spans (span_id, trace_id, parent_id, name, service, kind, start_ts, "
          "duration_ms, status, attrs) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)")


def _connect(pg, user):
    import psycopg
    return psycopg.connect(f"host=127.0.0.1 port={pg['port']} dbname=trialtalk user={user}", autocommit=True)


def test_portal_can_insert_and_select_span(pg):
    start = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    with _connect(pg, "tt_portal") as c:
        c.execute(INSERT, ("s-portal-1", "t-1", "s-root", "GET /x", "staff-api", "SERVER", start, 12.5, "OK",
                           json.dumps({"http.route": "/x"})))
        row = c.execute("SELECT span_id, trace_id, parent_id, name, service, kind, start_ts, duration_ms, status, "
                        "attrs, created_at FROM trial.telemetry_spans WHERE span_id = 's-portal-1'").fetchone()
    assert row[:6] == ("s-portal-1", "t-1", "s-root", "GET /x", "staff-api", "SERVER")
    assert row[6] == start
    assert float(row[7]) == 12.5
    assert row[8] == "OK"
    assert row[9] == {"http.route": "/x"}
    assert row[10] is not None


def test_defaults_apply(pg):
    with _connect(pg, "tt_portal") as c:
        c.execute("INSERT INTO trial.telemetry_spans (span_id, trace_id, name, service, start_ts, duration_ms) "
                  "VALUES ('s-def-1', 't-2', 'n', 'svc', now(), 1)")
        kind, status, attrs, parent = c.execute(
            "SELECT kind, status, attrs, parent_id FROM trial.telemetry_spans WHERE span_id = 's-def-1'").fetchone()
    assert (kind, status, attrs, parent) == ("INTERNAL", "UNSET", {}, None)


def test_messaging_insert_is_idempotent_on_conflict(pg):
    """The exporter uses ON CONFLICT (span_id) DO NOTHING, which needs SELECT on the arbiter column."""
    sql = INSERT + " ON CONFLICT (span_id) DO NOTHING"
    args = ("s-msg-1", "t-3", None, "reminder", "reminder", "INTERNAL", datetime.now(timezone.utc), 3, "UNSET", "{}")
    with _connect(pg, "tt_messaging") as c:
        c.execute(sql, args)
        c.execute(sql, args)
        assert c.execute("SELECT count(*) FROM trial.telemetry_spans WHERE span_id = 's-msg-1'").fetchone()[0] == 1


def test_participant_can_insert_span_with_conflict_clause(pg):
    sql = INSERT + " ON CONFLICT (span_id) DO NOTHING"
    args = ("s-part-1", "t-4", None, "POST /chat", "participant-api", "SERVER", datetime.now(timezone.utc), 5,
            "OK", json.dumps({"http.route": "/chat"}))
    with _connect(pg, "tt_participant") as c:
        c.execute(sql, args)
        c.execute(sql, args)
        assert c.execute("SELECT count(*) FROM trial.telemetry_spans WHERE span_id = 's-part-1'").fetchone()[0] == 1
