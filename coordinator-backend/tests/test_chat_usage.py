"""Web chat records the agent's token usage and persists its trace; the dev server's synthetic telemetry seed.

Integration tests (need TT_PG_BIN; see conftest). The agent is FakeAgentCore, which returns the same contract as
the real agent: {"reply", "usage": {...}, "trace": {"trace_id", "spans": [...]}}.
"""

import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

from test_api_integration import enroll

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devserver"))


@pytest.fixture
def span_sink(aws, monkeypatch):
    """Route telemetry.persist_spans (its own Data API client, as in the Lambda) to the local database."""
    from common import telemetry
    monkeypatch.setenv("DB_CLUSTER_ARN", "arn:local-cluster")
    monkeypatch.setenv("DB_SECRET_ARN", "participant")  # the login the deployed participant_api writes spans as
    monkeypatch.delenv("TELEMETRY_DB_SINK", raising=False)  # persist_spans is skipped when it is "0"
    monkeypatch.setattr(telemetry, "_rds", aws.rds)
    return aws


@pytest.fixture
def clean_spans(admin):
    admin.execute("TRUNCATE trial.telemetry_spans")
    yield admin
    admin.execute("TRUNCATE trial.telemetry_spans")


def outbound(admin, pid):
    return admin.execute("SELECT input_tokens, output_tokens, model_id, latency_ms, body_text FROM "
                         "trial.communications WHERE participant_id = %s AND direction = 'outbound'", (pid,)).fetchall()


def test_chat_records_usage_and_spans(call_participant, call_staff, span_sink, clean_spans):
    aws, admin = span_sink, clean_spans
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "When is my next visit?"})
    assert status == 200 and body == {"reply": "echo: When is my next visit?", "emergency": False}

    sent = aws.agent.responses[-1]
    (row,) = outbound(admin, pid)
    assert (row[0], row[1]) == (sent["usage"]["input_tokens"], sent["usage"]["output_tokens"])
    assert 300 <= row[0] <= 900 and 60 <= row[1] <= 250
    assert row[2] == sent["usage"]["model"] and row[3] is not None

    trace_id = sent["trace"]["trace_id"]
    spans = admin.execute("SELECT span_id, service, name FROM trial.telemetry_spans WHERE trace_id = %s",
                          (trace_id,)).fetchall()
    assert len(spans) == len(sent["trace"]["spans"]) >= 2
    assert {s[1] for s in spans} == {"trial-agent"}
    assert "agent.invoke" in {s[2] for s in spans} and "tool.search_trial_docs" in {s[2] for s in spans}


def test_db_sink_flag_off_stores_no_agent_spans_but_chat_works(call_participant, call_staff, span_sink, clean_spans,
                                                                monkeypatch):
    monkeypatch.setenv("TELEMETRY_DB_SINK", "0")
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and body["reply"] == "echo: hello"
    assert clean_spans.execute("SELECT count(*) FROM trial.telemetry_spans").fetchone()[0] == 0
    assert len(outbound(clean_spans, pid)) == 1


def test_chat_passes_traceparent_to_the_agent(call_participant, call_staff, span_sink, monkeypatch):
    from common import telemetry
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    monkeypatch.setattr(telemetry, "traceparent", lambda: tp)
    pid = enroll(call_staff)
    call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert span_sink.agent.calls[-1]["traceparent"] == tp
    # the fake continues the caller's trace, as the real agent does
    assert span_sink.agent.responses[-1]["trace"]["trace_id"] == "a" * 32


def test_error_response_without_usage_or_trace(call_participant, call_staff, span_sink, clean_spans):
    aws, admin = span_sink, clean_spans
    aws.agent.next = {"reply": "Sorry, something went wrong.", "trace": {"trace_id": "", "spans": []}}
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and body["reply"] == "Sorry, something went wrong."
    (row,) = outbound(admin, pid)
    assert row[0] is None and row[1] is None and row[2] is None
    assert admin.execute("SELECT count(*) FROM trial.telemetry_spans").fetchone()[0] == 0


def test_old_style_reply_only_response(call_participant, call_staff, span_sink, clean_spans):
    aws, admin = span_sink, clean_spans
    aws.agent.next = {"reply": "plain"}
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and body["reply"] == "plain"
    assert outbound(admin, pid)[0][:2] == (None, None)
    assert admin.execute("SELECT count(*) FROM trial.telemetry_spans").fetchone()[0] == 0


def test_garbage_usage_and_trace_never_break_the_reply(call_participant, call_staff, span_sink, clean_spans):
    aws, admin = span_sink, clean_spans
    aws.agent.next = {"reply": "ok", "usage": {"input_tokens": "lots", "output_tokens": -5}, "trace": "nope"}
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and body["reply"] == "ok"
    assert outbound(admin, pid)[0][:2] == (None, None)


def test_telemetry_failure_does_not_change_the_reply(call_participant, call_staff, span_sink, monkeypatch):
    from common import telemetry

    def boom(*a, **k):
        raise RuntimeError("telemetry down")
    monkeypatch.setattr(telemetry, "persist_spans", boom)
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and body == {"reply": "echo: hello", "emergency": False}


def test_agent_failure_still_replies_with_no_tokens(call_participant, call_staff, span_sink, clean_spans):
    aws, admin = span_sink, clean_spans

    def fail(**kw):
        raise RuntimeError("agent down")
    aws.agent.invoke_agent_runtime = fail
    pid = enroll(call_staff)
    status, body = call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    assert status == 200 and "something went wrong" in body["reply"]
    assert outbound(admin, pid)[0][:2] == (None, None)


# ------------------------------------------------------------------ synthetic seed

def seeded(pg, admin, call_staff):
    """seed_telemetry() against the test database (needs at least one participant for the token rows)."""
    import server
    enroll(call_staff)
    server.pg = types.SimpleNamespace(admin_dsn=pg["admin_dsn"])
    server.seed_telemetry(days=7)
    return server


def test_seed_telemetry_shapes_and_is_marked_synthetic(pg, call_staff, aws, admin, clean_spans):
    seeded(pg, admin, call_staff)
    rows = admin.execute("SELECT trace_id, span_id, parent_id, name, service, kind, status, attrs, start_ts, duration_ms "
                         "FROM trial.telemetry_spans").fetchall()
    assert len(rows) > 200
    assert all(r[7].get("trialtalk.synthetic") is True for r in rows)
    names = {r[3] for r in rows}
    assert {"whatsapp.ingest", "whatsapp.route", "agent.call", "agent.invoke", "tool.search_trial_docs"} <= names
    assert any(r[6] == "ERROR" for r in rows) and any(r[6] == "OK" for r in rows)
    assert len({r[8].date() for r in rows}) >= 7
    assert all(float(r[9]) > 0 for r in rows)

    # Trace structure: one root per trace, whatsapp.ingest -> whatsapp.route -> agent.call -> agent.invoke -> tools
    by_id = {r[1]: r for r in rows}
    roots = [r for r in rows if r[2] is None]
    assert {r[3] for r in roots} == {"whatsapp.ingest"}
    assert all(r[2] in by_id for r in rows if r[2] is not None)
    for r in rows:
        if r[3] == "agent.invoke":
            assert by_id[r[2]][3] == "agent.call" and by_id[by_id[r[2]][2]][3] == "whatsapp.route"
        if r[3].startswith("tool."):
            assert by_id[r[2]][3] == "agent.invoke"
    invokes = [r for r in rows if r[3] == "agent.invoke" and r[6] == "OK"]
    assert all(1 <= r[7]["gen_ai.usage.input_tokens"] and r[7]["gen_ai.usage.output_tokens"] >= 1 for r in invokes)
    assert len({r[7]["trialtalk.config_hash"] for r in invokes}) == 2  # a config change midway

    # No PHI: attribute keys are all in the telemetry allowlist namespaces and values are short scalars
    for r in rows:
        assert all(isinstance(v, (bool, int, float, str)) and len(str(v)) <= 80 for v in r[7].values())
        assert not re.search(r"\+?\d{10}|P-\d{4}", json.dumps(r[7]))

    comms = admin.execute("SELECT input_tokens, output_tokens, created_at FROM trial.communications "
                          "WHERE session_id LIKE 'synthetic-%%' AND input_tokens IS NOT NULL").fetchall()
    assert len(comms) >= len(invokes) * 0.9 and len({c[2].date() for c in comms}) >= 7


def test_seed_always_has_recent_errors_including_an_agent_failure(pg, call_staff, aws, admin, clean_spans):
    """The last 24 h hold >= 2 error traces (a root ERROR, so the Error-rate tile is clickable, and agent failures)."""
    seeded(pg, admin, call_staff)
    recent = "start_ts >= now() - interval '24 hours'"
    assert admin.execute(f"SELECT count(*) FROM trial.telemetry_spans WHERE parent_id IS NULL AND status = 'ERROR' "
                         f"AND {recent}").fetchone()[0] >= 1
    assert admin.execute(f"SELECT count(DISTINCT trace_id) FROM trial.telemetry_spans WHERE status = 'ERROR' "
                         f"AND {recent}").fetchone()[0] >= 2
    failed = admin.execute(f"SELECT trace_id, attrs FROM trial.telemetry_spans WHERE name = 'agent.invoke' "
                           f"AND status = 'ERROR' AND {recent}").fetchall()
    assert failed
    for _, attrs in failed:  # an agent failure: ERROR, no tokens, still synthetic
        assert attrs["trialtalk.synthetic"] is True
        assert "gen_ai.usage.input_tokens" not in attrs and "gen_ai.usage.output_tokens" not in attrs
    for (tid, _) in failed:
        names = dict(admin.execute("SELECT name, status FROM trial.telemetry_spans WHERE trace_id = %s", (tid,)))
        assert names["whatsapp.route"] == "ERROR" and names["agent.call"] == "ERROR"
    # Its reply row: no session, no tokens (the shape a failed WhatsApp agent call leaves behind)
    rows = admin.execute("SELECT session_id, input_tokens, output_tokens FROM trial.communications "
                         "WHERE body_text LIKE '[demo data]%' AND session_id IS NULL AND created_at >= now() - interval '24 hours'"
                         ).fetchall()
    assert rows and all(r == (None, None, None) for r in rows)


def test_seed_telemetry_is_rerunnable(pg, call_staff, aws, admin, clean_spans):
    server = seeded(pg, admin, call_staff)
    server.seed_telemetry(days=7)
    n = admin.execute("SELECT count(DISTINCT trace_id) FROM trial.telemetry_spans").fetchone()[0]
    n_spans = admin.execute("SELECT count(*) FROM trial.telemetry_spans").fetchone()[0]
    comms = admin.execute("SELECT count(*) FROM trial.communications WHERE body_text LIKE '[demo data]%'").fetchone()[0]
    server.seed_telemetry(days=7)
    assert admin.execute("SELECT count(*) FROM trial.communications WHERE body_text LIKE '[demo data]%'").fetchone()[0] \
        <= comms * 1.2
    # synthetic spans are replaced, not doubled
    assert admin.execute("SELECT count(*) FROM trial.telemetry_spans").fetchone()[0] <= n_spans * 1.2
    assert n > 50


def test_seeded_data_renders_in_observability(pg, call_staff, aws, admin, clean_spans):
    seeded(pg, admin, call_staff)
    status, body = call_staff("GET", "/staff/observability", query={"hours": "168"})
    assert status == 200, body
    s = body["summary"]
    assert s["requests"] > 100 and s["tokens_in"] > 0 and s["tokens_out"] > 0 and s["errors"] > 0
    assert len(body["drift"]["config_changes"]) >= 1
    status, body = call_staff("GET", "/staff/observability/traces", query={"limit": "5"})
    assert status == 200 and len(body["traces"]) == 5


def test_devserver_init_wrapper_touches_the_flag_only_when_building(monkeypatch):
    """The flag is process-wide: idempotent init() calls (every request, on other threads) must not set it."""
    import server
    from common import telemetry
    monkeypatch.delenv("TELEMETRY_DB_SINK", raising=False)
    seen = []
    wrapped = server._init_without_request_sink(lambda **kw: seen.append(os.environ.get("TELEMETRY_DB_SINK")))
    assert server._init_without_request_sink(wrapped) is wrapped  # no double wrapping
    monkeypatch.setattr(telemetry, "_state", {})
    wrapped()                                   # first build: flag "0" during, gone after
    monkeypatch.setattr(telemetry, "_state", {"built": True})
    wrapped()                                   # idempotent call: flag untouched
    wrapped(force=True)                         # rebuild (Reset data): flag "0" during
    assert seen == ["0", None, "0"]
    assert "TELEMETRY_DB_SINK" not in os.environ
