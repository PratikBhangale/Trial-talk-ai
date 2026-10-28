"""db/seed_demo_clinic.py: the production demo-clinic seeder.

Unit tests check the generated rows (pure functions, no database): archetype mix, CHECK-constraint values, the
not-engaging window, alerts, and the teardown SQL. The integration tests (TT_PG_BIN) run the real seeder through
FakeRdsData as the master user against the db/ schema with 03_seed.sql's KOA-301 rows loaded, then reseed and
tear down, checking KOA-301 is never touched and the no-delete triggers end up enabled again.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import local_env  # noqa: F401  (puts db/ on sys.path)
import seed_demo_clinic as sdc

NOW = datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def plans():
    return {s: sdc.plan_study(s, NOW) for s in sdc.DEMO_STUDIES}


def _all(rows, *keys):
    return [r for k in keys for r in rows[k]]


def _ts(v):
    return datetime.fromisoformat(v)


# ------------------------------------------------------------------ unit

def test_demo_studies_never_include_the_live_study():
    assert sdc.LIVE_STUDY == "KOA-301" and sdc.LIVE_STUDY not in sdc.DEMO_STUDIES
    assert len(sdc.DEMO_STUDIES) == 4
    with pytest.raises(ValueError):
        sdc.plan_study("KOA-301", NOW)


def test_studies_sql_has_every_demo_study():
    sql = (local_env.DB_SQL / "07_studies.sql").read_text()
    for s in (sdc.LIVE_STUDY, *sdc.DEMO_STUDIES):
        assert f"('{s}'," in sql


def test_plan_is_deterministic(plans):
    again = sdc.plan_study("HTN-205", NOW)
    assert json.dumps(again[0], sort_keys=True) == json.dumps(plans["HTN-205"][0], sort_keys=True)
    other_seed = sdc.plan_study("HTN-205", NOW, seed=1)[0]
    assert other_seed["comms_pre"] != plans["HTN-205"][0]["comms_pre"]


def test_archetype_mix_per_study(plans):
    mixes = []
    for study, (rows, arch) in plans.items():
        n = len(rows["participants"])
        assert 14 <= n <= 16
        mix = {a: sum(v == a for v in arch.values()) for a in sdc.ARCHETYPES}
        assert mix == sdc.ARCHETYPE_MIX[study]
        assert 0.4 <= mix["adherent"] / n <= 0.6
        assert 0.1 <= mix["low_adherence"] / n <= 0.3
        assert 0.1 <= mix["not_engaging"] / n <= 0.2
        assert 0.1 <= mix["flagged"] / n <= 0.25
        mixes.append(tuple(mix.values()))
    assert len(set(mixes)) > 1, "the trials should not all look the same"


def test_participants_are_valid_and_unique(plans):
    people = [p for rows, _ in plans.values() for p in rows["participants"]]
    assert len({p["participant_id"] for p in people}) == len(people)
    assert len({p["phone_e164"] for p in people}) == len(people)
    langs = set()
    for p in people:
        assert set(p) == set(sdc.COLUMNS["pii.participants"])
        assert p["study_id"] in sdc.DEMO_STUDIES
        assert p["site_id"] in sdc.SITES
        assert p["status"] == "enrolled" and p["whatsapp_opt_in"] is True
        assert p["phone_e164"].startswith("+1555") and len(p["phone_e164"]) == 12
        assert not p["participant_id"][2:].isdigit(), "must not join the portal's P-<n> id sequence"
        langs.add(p["preferred_language"])
    assert langs == {"en", "es"}
    assert {p["site_id"] for p in people} == set(sdc.SITES)


def test_rows_respect_check_constraints(plans):
    for rows, _ in plans.values():
        for e in _all(rows, "events", "events_v2"):
            assert set(e) == set(sdc.COLUMNS["trial.clinical_events"])
            assert e["record_type"] in sdc.CHECKS["record_type"]
            assert e["source"] in sdc.CHECKS["event_source"]
            assert e["pain_score_band"] in sdc.CHECKS["pain_score_band"]
            assert e["severity_hint"] in sdc.CHECKS["severity_hint"]
            assert e["response"] in sdc.CHECKS["response"]
            assert e["created_by"] and isinstance(e["details"], dict)
            if e["record_type"] == "SYMPTOM":
                assert e["verbatim_text"]
            if e["supersedes_event_id"]:
                assert e["change_reason"]
        for c in _all(rows, "comms_pre", "comms_post"):
            assert set(c) == set(sdc.COLUMNS["trial.communications"])
            assert c["channel"] in sdc.CHECKS["channel"] and c["direction"] in sdc.CHECKS["direction"]
            assert c["attachments"] == []
        for a in rows["alerts"]:
            assert set(a) == set(sdc.COLUMNS["trial.emergency_alerts"])
            assert a["severity"] in sdc.CHECKS["alert_severity"]
            assert a["source"] in sdc.CHECKS["alert_source"]
            assert a["status"] in sdc.CHECKS["alert_status"]


def test_ids_are_unique_and_references_point_backwards(plans):
    """Every FK target is inserted in an earlier step (or earlier in the same step for none of them)."""
    all_comm_ids, all_event_ids, mids = set(), set(), []
    for rows, _ in plans.values():
        pids = {p["participant_id"] for p in rows["participants"]}
        pre = {c["comm_id"] for c in rows["comms_pre"]}
        ev1 = {e["event_id"]: e for e in rows["events"]}
        ev2 = {e["event_id"]: e for e in rows["events_v2"]}
        for c in rows["comms_pre"]:
            assert c["related_event_id"] is None and c["participant_id"] in pids
        for e in rows["events"]:
            assert e["source_comm_id"] in pre and e["supersedes_event_id"] is None
        superseded = [e["supersedes_event_id"] for e in rows["events_v2"]]
        assert len(superseded) == len(set(superseded)), "a version can be superseded once"
        for e in rows["events_v2"]:
            old = ev1[e["supersedes_event_id"]]
            assert (old["participant_id"], old["record_type"]) == (e["participant_id"], "DOSE")
            assert e["source_comm_id"] in pre
        for c in rows["comms_post"]:
            assert c["related_event_id"] in ev1 or c["related_event_id"] in ev2
        for a in rows["alerts"]:
            assert a["participant_id"] in pids
        comm_ids = pre | {c["comm_id"] for c in rows["comms_post"]}
        assert not comm_ids & all_comm_ids and not (set(ev1) | set(ev2)) & all_event_ids
        all_comm_ids |= comm_ids
        all_event_ids |= set(ev1) | set(ev2)
        mids += [c["provider_message_id"] for c in _all(rows, "comms_pre", "comms_post")]
    assert len(mids) == len(set(mids))


def test_timestamps_span_three_weeks_in_the_past(plans):
    earliest = NOW - timedelta(days=sdc.HISTORY_DAYS, hours=1)
    stamps = []
    for rows, _ in plans.values():
        for r in _all(rows, "comms_pre", "comms_post", "events", "events_v2", "alerts"):
            for k in ("created_at", "occurred_at", "acknowledged_at", "resolved_at", "last_pressed_at"):
                if r.get(k):
                    t = _ts(r[k])
                    assert earliest <= t <= NOW, (k, r[k])
                    stamps.append(t)
    assert (NOW - min(stamps)).days >= 20
    # weighted to daytime (local): most participant messages between 07:00 and 22:00 Chicago time
    inbound = [_ts(c["created_at"]).astimezone(sdc.TZ).hour for rows, _ in plans.values()
               for c in _all(rows, "comms_pre") if c["direction"] == "inbound"]
    assert sum(7 <= h <= 22 for h in inbound) / len(inbound) > 0.9


def test_adherence_follows_archetype(plans):
    rates = {a: [] for a in sdc.ARCHETYPES}
    for rows, arch in plans.values():
        final = {e["event_id"]: e for e in rows["events"] if e["record_type"] == "DOSE"}
        for v2 in rows["events_v2"]:
            final[v2["supersedes_event_id"]] = v2
        for pid, a in arch.items():
            mine = [e for e in final.values() if e["participant_id"] == pid]
            assert mine
            rates[a].append(sum(e["response"] == "taken" for e in mine) / len(mine))
    avg = {a: sum(v) / len(v) for a, v in rates.items()}
    assert avg["adherent"] > 0.85
    assert 0.4 < avg["low_adherence"] < 0.7
    assert avg["not_engaging"] < 0.35
    assert avg["adherent"] > avg["flagged"] > avg["low_adherence"]


def test_not_engaging_means_no_inbound_whatsapp_in_14_days(plans):
    cutoff = NOW - timedelta(days=sdc.NOT_ENGAGING_DAYS)
    for rows, arch in plans.values():
        recent = {c["participant_id"] for c in _all(rows, "comms_pre", "comms_post")
                  if c["channel"] == "whatsapp" and c["direction"] == "inbound" and _ts(c["created_at"]) > cutoff}
        silent = {pid for pid, a in arch.items() if a == "not_engaging"}
        assert not recent & silent
        assert recent == set(arch) - silent, "everyone else wrote in the last 14 days"
        for pid in silent:   # they did engage once, before going quiet
            assert any(c["participant_id"] == pid and c["direction"] == "inbound" for c in rows["comms_pre"])


def test_symptoms_mild_vs_flagged(plans):
    for study, (rows, arch) in plans.items():
        sym = [e for e in rows["events"] if e["record_type"] == "SYMPTOM"]
        urgent = [e for e in sym if e["severity_hint"] == "urgent"]
        mild = [e for e in sym if e["severity_hint"] != "urgent"]
        assert mild and urgent
        for e in mild:
            assert e["status"] == "logged" and not e["flag_for_follow_up"] and e["created_by"] == "agent"
        for e in urgent:
            assert arch[e["participant_id"]] == "flagged"
            assert e["status"] == "escalated" and e["flag_for_follow_up"] and e["urgent_keywords"]
        assert {e["participant_id"] for e in urgent} == {p for p, a in arch.items() if a == "flagged"}
        # every symptom has the participant's message and a reply linked to it
        replies = {c["related_event_id"] for c in rows["comms_post"]}
        assert all(e["event_id"] in replies for e in sym)


def test_agent_replies_carry_usage(plans):
    replies = [c for rows, _ in plans.values() for c in _all(rows, "comms_pre", "comms_post") if c["model_id"]]
    assert len(replies) > 100
    for c in replies:
        assert c["direction"] == "outbound" and c["channel"] == "whatsapp" and c["msg_type"] == "text"
        assert c["model_id"] == sdc.MODEL_ID and c["session_id"].startswith("synthetic-")
        assert c["input_tokens"] > 0 and c["output_tokens"] > 0 and c["latency_ms"] > 0


def test_alerts_lifecycle(plans):
    for study, (rows, arch) in plans.items():
        alerts = rows["alerts"]
        flagged = {p for p, a in arch.items() if a == "flagged"}
        assert {a["participant_id"] for a in alerts} == flagged
        by_status = {s: [a for a in alerts if a["status"] == s] for s in sdc.CHECKS["alert_status"]}
        assert 1 <= len(by_status["open"]) <= 2 and len(by_status["open"]) == sdc.OPEN_ALERTS[study]
        assert by_status["resolved"], "some alerts are resolved"
        for a in alerts:
            created = _ts(a["created_at"])
            if a["status"] == "open":
                assert a["acknowledged_at"] is None and a["resolved_at"] is None
                assert NOW - created < timedelta(days=2)
            else:
                assert a["acknowledged_by"].startswith("staff:") and _ts(a["acknowledged_at"]) >= created
            if a["status"] == "resolved":
                assert _ts(a["resolved_at"]) >= _ts(a["acknowledged_at"]) and a["resolution_note"]
            if a["source"] == "chat_keyword":
                assert a["keywords"] and a["severity"] == "life_threatening"


def test_urgent_texts_trip_the_router_keywords():
    for msgs in sdc.URGENT.values():
        for en, es in msgs:
            assert sdc.emergency_keywords(en) and sdc.emergency_keywords(es)


def test_chunks_fit_and_round_trip(plans):
    rows = plans["AST-302"][0]["comms_pre"]
    parts = sdc.chunks(rows, max_chars=20000)
    assert len(parts) > 1 and all(len(p) <= 20000 for p in parts)
    assert [r for p in parts for r in json.loads(p)] == json.loads(json.dumps(rows))


def test_teardown_sql_is_scoped_and_restores_triggers():
    stmts = sdc.teardown_statements()
    sqls = [s for s, _ in stmts]
    n = len(sdc.NO_DELETE_TRIGGERS)
    # lock timeout first, then the fingerprint guard, both before any trigger is touched
    assert sqls[0] == "SET LOCAL lock_timeout = '5s'"
    assert sqls[1] is sdc.TEARDOWN_GUARD and stmts[1][1] == {"studies": "{HTN-205,DM2-410,MIG-118,AST-302}"}
    body = sqls[2:]
    assert [s.split()[3] for s in body[:n]] == ["DISABLE"] * n
    assert [s.split()[3] for s in body[-n:]] == ["ENABLE"] * n
    assert [s.replace("DISABLE", "ENABLE") for s in body[:n]] == body[-n:]
    assert {g for _, g in sdc.NO_DELETE_TRIGGERS} == {"clinical_events_append_only", "communications_no_delete",
                                                      "reminders_no_delete", "emergency_alerts_no_delete"}
    delete_sql, params = stmts[2 + n]
    assert delete_sql is sdc.TEARDOWN_DELETE and len(stmts) == 3 + 2 * n
    assert params == {"studies": "{HTN-205,DM2-410,MIG-118,AST-302}"}
    assert "KOA" not in delete_sql and "KOA" not in params["studies"]
    # every DELETE (participants included) is scoped by study AND the seeder's fingerprint
    assert delete_sql.count("DELETE FROM") == 5
    assert delete_sql.count("study_id = ANY(:studies::text[])") == 5
    assert delete_sql.count(sdc.FINGERPRINT) == 5
    assert "phone_e164 LIKE '+1555%'" in sdc.FINGERPRINT
    assert "participant_id ~ '^P-(HTN|DM2|MIG|AST)-[0-9]{2}$'" in sdc.FINGERPRINT
    assert f"NOT ({sdc.FINGERPRINT})" in sdc.TEARDOWN_GUARD


def test_fingerprint_matches_every_seeded_participant_only(plans):
    import re
    phone, pid = re.compile(r"^\+1555"), re.compile(r"^P-(HTN|DM2|MIG|AST)-[0-9]{2}$")
    for rows, _ in plans.values():
        for p in rows["participants"]:
            assert phone.match(p["phone_e164"]) and pid.match(p["participant_id"])
    for real in ("P-0142", "P-1001", "P-HTN-001", "HTN-01"):
        assert not pid.match(real)


def test_failed_commit_is_not_masked_by_rollback(capsys):
    """Commit fails (e.g. the server committed but the response was lost) and rollback then fails too: the caller
    must see the commit error, not the rollback one."""

    class Rds:
        def begin_transaction(self, **kw):
            return {"transactionId": "t1"}

        def execute_statement(self, sql, **kw):
            if "trial.studies" in sql:
                return {"formattedRecords": json.dumps([{"study_id": s} for s in sdc.DEMO_STUDIES])}
            if "INSERT INTO pii.participants" in sql:
                rows = json.loads(kw["parameters"][0]["value"]["stringValue"])
                return {"formattedRecords": json.dumps([{"participant_id": r["participant_id"]} for r in rows])}
            return {"formattedRecords": "[]"}

        def commit_transaction(self, **kw):
            raise RuntimeError("commit response lost")

        def rollback_transaction(self, **kw):
            raise RuntimeError("transaction not found")

    with pytest.raises(RuntimeError, match="commit response lost"):
        sdc.main(["--cluster-arn", "a", "--secret-arn", "s"], rds=Rds(), now=NOW)
    assert "rollback failed too" in capsys.readouterr().err


def test_dry_run_prints_counts_without_aws(monkeypatch, capsys):
    import boto3
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: pytest.fail("dry run must not touch AWS"), raising=False)
    totals = sdc.main(["--dry-run"], now=NOW)
    out = capsys.readouterr().out
    for s in sdc.DEMO_STUDIES:
        assert f"==> {s}" in out
    assert "KOA-301" not in out
    expected = {k: sum(sdc.counts(sdc.plan_study(s, NOW)[0])[k] for s in sdc.DEMO_STUDIES)
                for k in sdc.counts(sdc.plan_study("HTN-205", NOW)[0])}
    assert totals == expected and totals["participants"] == 60
    for k in ("participants", "communications", "clinical_events", "emergency_alerts"):
        assert f"   {k:<18}{totals[k]:>6}" in out

    sdc.main(["--dry-run", "--teardown"], now=NOW)
    out = capsys.readouterr().out
    assert "DISABLE TRIGGER clinical_events_append_only" in out and "ENABLE TRIGGER emergency_alerts_no_delete" in out


def test_real_run_requires_arns(capsys):
    with pytest.raises(SystemExit):
        sdc.main([], now=NOW)


# ------------------------------------------------------------------ integration (local PostgreSQL)

def _table_counts(admin, where):
    q = {
        "participants": f"SELECT count(*) FROM pii.participants WHERE study_id {where}",
        "communications": "SELECT count(*) FROM trial.communications c JOIN pii.participants p USING (participant_id) "
                          f"WHERE p.study_id {where}",
        "clinical_events": "SELECT count(*) FROM trial.clinical_events e JOIN pii.participants p "
                           f"USING (participant_id) WHERE p.study_id {where}",
        "emergency_alerts": "SELECT count(*) FROM trial.emergency_alerts a JOIN pii.participants p "
                            f"USING (participant_id) WHERE p.study_id {where}",
    }
    return {k: admin.execute(sql).fetchone()[0] for k, sql in q.items()}


@pytest.fixture
def clinic(pg, clean_db, admin):
    from local_env import FakeRdsData, SEED_FILE, run_sql_file, wipe
    run_sql_file(admin, SEED_FILE)                 # KOA-301's rows stand in for the live data
    rds = FakeRdsData(pg["port"])
    args = ["--cluster-arn", "arn:local", "--secret-arn", "admin"]
    now = datetime.now(timezone.utc)   # fixed for the whole test, so expected counts can't straddle a boundary

    def run(*extra):
        return sdc.main(args + list(extra), rds=rds, now=now)
    run.now = now
    yield run
    wipe(admin)


def test_seed_reseed_and_teardown_against_postgres(clinic, admin, capsys):
    koa_before = _table_counts(admin, "= 'KOA-301'")
    assert koa_before["participants"] == 3
    now = clinic.now

    result = clinic()
    expected = {s: sdc.counts(sdc.plan_study(s, now)[0]) for s in sdc.DEMO_STUDIES}
    for s in sdc.DEMO_STUDIES:
        got = _table_counts(admin, f"= '{s}'")
        assert got == {k: expected[s][k] for k in got} == {k: result[s][k] for k in got}
    assert _table_counts(admin, "= 'KOA-301'") == koa_before

    # the KPIs read what the archetypes meant: not engaging = no inbound WhatsApp in 14 days
    silent = admin.execute(
        "SELECT p.study_id, count(*) FROM pii.participants p WHERE p.study_id <> 'KOA-301' AND NOT EXISTS ("
        "  SELECT 1 FROM trial.communications c WHERE c.participant_id = p.participant_id AND c.channel = 'whatsapp'"
        "    AND c.direction = 'inbound' AND c.created_at > now() - interval '14 days') GROUP BY 1").fetchall()
    assert dict(silent) == {s: sdc.ARCHETYPE_MIX[s]["not_engaging"] for s in sdc.DEMO_STUDIES}
    # current_events hides the superseded DOSE versions: one current row per reminder
    current = dict(admin.execute(
        "SELECT e.response, count(*) FROM trial.current_events e JOIN pii.participants p USING (participant_id) "
        "WHERE p.study_id <> 'KOA-301' AND e.record_type = 'DOSE' GROUP BY 1").fetchall())
    reminders = sum(sum(e["record_type"] == "DOSE" for e in sdc.plan_study(s, now)[0]["events"])
                    for s in sdc.DEMO_STUDIES)
    assert sum(current.values()) == reminders
    assert current["taken"] > current["skipped"] > 0 and current["no_response"] > 0
    open_alerts = admin.execute("SELECT count(*) FROM trial.emergency_alerts WHERE status = 'open'").fetchone()[0]
    assert open_alerts == sum(sdc.OPEN_ALERTS.values())
    studies = admin.execute("SELECT study_id FROM trial.studies ORDER BY 1").fetchall()
    assert {r[0] for r in studies} == {"KOA-301", *sdc.DEMO_STUDIES}

    # re-running is a no-op
    before = _table_counts(admin, "<> 'KOA-301'")
    assert clinic() == {s: "skipped" for s in sdc.DEMO_STUDIES}
    assert _table_counts(admin, "<> 'KOA-301'") == before

    # teardown removes the demo studies only, and puts the triggers back
    capsys.readouterr()
    deleted = clinic("--teardown")
    assert int(deleted["participants"]) == before["participants"]
    assert int(deleted["communications"]) == before["communications"]
    assert _table_counts(admin, "<> 'KOA-301'") == dict.fromkeys(before, 0)
    assert _table_counts(admin, "= 'KOA-301'") == koa_before
    enabled = admin.execute(
        "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname = ANY(%s)",
        ([g for _, g in sdc.NO_DELETE_TRIGGERS],)).fetchall()
    assert sorted(enabled) == sorted((g, "O") for _, g in sdc.NO_DELETE_TRIGGERS)
    with pytest.raises(Exception, match="not allowed"):
        admin.execute("DELETE FROM trial.communications")

    # and the studies can be seeded again after a teardown
    assert all(v != "skipped" for v in clinic().values())


def test_teardown_failure_leaves_triggers_on(clinic, admin, monkeypatch):
    clinic()
    monkeypatch.setattr(sdc, "TEARDOWN_DELETE", "SELECT 1/0")
    with pytest.raises(Exception):
        clinic("--teardown")
    enabled = admin.execute("SELECT DISTINCT tgenabled FROM pg_trigger WHERE tgname = ANY(%s)",
                            ([g for _, g in sdc.NO_DELETE_TRIGGERS],)).fetchall()
    assert enabled == [("O",)]
    planned = sdc.counts(sdc.plan_study("HTN-205", clinic.now)[0])["participants"]
    assert _table_counts(admin, "= 'HTN-205'")["participants"] == planned


def test_teardown_aborts_when_a_demo_study_holds_a_real_participant(clinic, admin, capsys, monkeypatch):
    """Staff can enroll a real person into a demo study (study_id is free text): teardown must list them and stop,
    deleting nothing and leaving the triggers on."""
    clinic()
    admin.execute("INSERT INTO pii.participants (participant_id, full_name, phone_e164, study_id, site_id, status) "
                  "VALUES ('P-1001', 'Real Person', '+13125559999', 'HTN-205', 'LAKEVIEW', 'enrolled'), "
                  "       ('P-HTN-77X', 'Odd Id', '+15552059999', 'MIG-118', 'RIVERSIDE', 'screening')")
    admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text) "
                  "VALUES ('P-1001', 'whatsapp', 'inbound', 'text', 'hello')")
    before = _table_counts(admin, "<> 'KOA-301'")
    capsys.readouterr()
    with pytest.raises(SystemExit, match="ABORTED, nothing deleted") as err:
        clinic("--teardown")
    assert "P-1001 (HTN-205)" in str(err.value) and "P-HTN-77X (MIG-118)" in str(err.value)
    assert _table_counts(admin, "<> 'KOA-301'") == before
    enabled = admin.execute("SELECT DISTINCT tgenabled FROM pg_trigger WHERE tgname = ANY(%s)",
                            ([g for _, g in sdc.NO_DELETE_TRIGGERS],)).fetchall()
    assert enabled == [("O",)]
    assert "DISABLE TRIGGER" not in capsys.readouterr().out, "the guard runs before any trigger is touched"

    # even with the guard bypassed, the DELETE itself spares anyone without the fingerprint
    monkeypatch.setattr(sdc, "TEARDOWN_GUARD", sdc.TEARDOWN_GUARD + " LIMIT 0")
    deleted = clinic("--teardown")
    assert int(deleted["participants"]) == before["participants"] - 2
    left = admin.execute("SELECT participant_id FROM pii.participants WHERE study_id <> 'KOA-301' ORDER BY 1")
    assert left.fetchall() == [("P-1001",), ("P-HTN-77X",)]
    assert admin.execute("SELECT count(*) FROM trial.communications WHERE participant_id = 'P-1001'").fetchone()[0] == 1


def test_seed_refuses_without_studies_table_rows(clinic, admin):
    admin.execute("DELETE FROM trial.studies WHERE study_id = 'MIG-118'")
    try:
        with pytest.raises(SystemExit, match="07_studies.sql"):
            clinic()
        assert _table_counts(admin, "<> 'KOA-301'")["participants"] == 0
    finally:
        from local_env import run_sql_file
        run_sql_file(admin, local_env.DB_SQL / "07_studies.sql")
