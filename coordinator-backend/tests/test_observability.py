"""Staff observability API: pure helpers (no DB) and the three endpoints end to end (needs TT_PG_BIN; see conftest)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import STAFF_CLAIMS

# ------------------------------------------------------------------ pure helpers


def test_est_cost_math():
    from staff_api import observability as obs
    assert obs.PRICING == {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0}
    assert obs.est_cost(1_000_000, 1_000_000) == 18.0
    assert obs.est_cost(0, 0) == 0.0
    assert obs.est_cost(None, None) == 0.0
    assert obs.est_cost(1234, 567) == round((1234 * 3 + 567 * 15) / 1e6, 4) == 0.0122


def test_drift_change_pct_and_zero_baseline():
    from staff_api import observability as obs
    metrics = {m["key"]: m for m in obs.drift_metrics(
        {"tokens_in_per_reply": 150.0, "tokens_out_per_reply": 10.0, "agent_p95_ms": None},
        {"tokens_in_per_reply": 100.0, "tokens_out_per_reply": 0, "agent_p95_ms": 500.0})}
    assert len(metrics) == 6
    assert metrics["tokens_in_per_reply"]["change_pct"] == 50.0
    assert metrics["tokens_in_per_reply"]["current"] == 150.0 and metrics["tokens_in_per_reply"]["baseline"] == 100.0
    assert metrics["tokens_out_per_reply"]["change_pct"] is None   # baseline 0
    assert metrics["agent_p95_ms"]["change_pct"] is None            # current missing
    assert metrics["reply_length_chars"] == {                        # absent from both dicts
        "key": "reply_length_chars", "label": metrics["reply_length_chars"]["label"], "current": None,
        "baseline": None, "change_pct": None, "bad_direction": "none"}
    down = obs.drift_metrics({"kb_search_rate": 0.2}, {"kb_search_rate": 0.4})
    assert [m["change_pct"] for m in down if m["key"] == "kb_search_rate"] == [-50.0]


def test_drift_bad_direction_labels():
    from staff_api import observability as obs
    rows = obs.drift_metrics({}, {})
    assert [m["key"] for m in rows] == ["tokens_in_per_reply", "tokens_out_per_reply", "agent_p95_ms",
                                        "reply_length_chars", "kb_search_rate", "escalation_rate"]
    by_key = {m["key"]: m for m in rows}
    assert by_key["agent_p95_ms"]["bad_direction"] == "up"
    assert by_key["kb_search_rate"]["bad_direction"] == "down"
    assert by_key["escalation_rate"]["bad_direction"] == "up"
    assert by_key["tokens_in_per_reply"]["bad_direction"] == "none"
    assert all(m["bad_direction"] in ("up", "down", "none") and m["label"] for m in rows)


def test_pid_from_session_strict_formats():
    from staff_api.observability import pid_from_session as pid
    # the router: "trialtalk-wa-<pid>-<local day>" right-padded with "0" to exactly 33 characters
    assert pid("trialtalk-wa-P-0142-2026-10-28".ljust(33, "0")) == "P-0142"
    assert pid("trialtalk-wa-P-0142-2026-10-20000") == "P-0142"     # a day ending in 0, then the padding
    assert pid("trialtalk-wa-P-10001-2026-10-28".ljust(33, "0")) == "P-10001"
    assert pid("trialtalk-wa-P-1234567-2026-10-28") == "P-1234567"  # already 33: no padding
    # the participant site's web chat: "web-<pid>-<local day>", stored unpadded
    assert pid("web-P-0142-2026-10-28") == "P-0142"
    for bad in (None, 42, "", "P-0142", "synthetic-0123456789ab",
                "trialtalk-wa-demo-0123456789ab-2026-10-28",            # demo users name no participant
                "trialtalk-wa-P-0142-2026-10-28",                       # router sessions are always padded to 33
                "trialtalk-wa-P-0142-2026-10-280000",                   # padded past 33
                "trialtalk-wa-P-1234567-2026-10-280",                   # padded although already 33
                "web-P-0142-2026-10-28000",                             # web sessions are never padded
                "web-P-0142-2026-13-01", "web-P-0142-2026-02-30",       # not a calendar day
                "web-p-0142-2026-10-28", "WEB-P-0142-2026-10-28", "web-P-01a2-2026-10-28", "web-P--2026-10-28",
                "web-P-0142-26-10-28", " web-P-0142-2026-10-28", "web-P-0142-2026-10-28\n",
                "web-P-0142-2026-10-28-x", "web-P-0142-2026-10-28' OR 1=1", "whatsapp-session-P-0142".ljust(33, "0"),
                "web-P-0142-2026-10-28" + "0" * 60):
        assert pid(bad) is None, bad


# ------------------------------------------------------------------ endpoints

INSERT_SPAN = ("INSERT INTO trial.telemetry_spans (span_id, trace_id, parent_id, name, service, kind, start_ts, "
               "duration_ms, status, attrs) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)")
T_OK, T_ERR = "a" * 32, "b" * 32
OPS = {"email": "someone@riverside.example.org", "cognito:groups": "[participant]", "custom:site_ids": "RIVERSIDE"}


def _span(admin, span_id, trace, parent, name, ago_min, dur, status="OK", attrs=None, service="svc"):
    start = datetime.now(timezone.utc) - timedelta(minutes=ago_min)
    admin.execute(INSERT_SPAN, (span_id, trace, parent, name, service, "INTERNAL", start, dur, status,
                                json.dumps(attrs or {})))


@pytest.fixture
def spans(admin):
    admin.execute("TRUNCATE trial.telemetry_spans")
    yield admin
    admin.execute("TRUNCATE trial.telemetry_spans")


def _seed(admin):
    _span(admin, "s-root-ok", T_OK, None, "whatsapp.route", 30, 100.0, service="router")
    _span(admin, "s-agent", T_OK, "s-root-ok", "agent.invoke", 30, 80.0, service="agent",
          attrs={"gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 200,
                 "gen_ai.request.model": "model-a", "trialtalk.config_hash": "h1"})
    _span(admin, "s-tool", T_OK, "s-agent", "tool.search_trial_docs", 30, 20.0, service="agent")
    _span(admin, "s-root-err", T_ERR, None, "whatsapp.route", 20, 300.0, status="ERROR", service="router")


def _comms(call_staff, admin):
    status, body = call_staff("POST", "/staff/participants", {
        "full_name": "Maria Lopez", "phone_e164": "+17325550142", "study_id": "KOA-301", "site_id": "RIVERSIDE"})
    assert status == 201, body
    pid = body["participant"]["participant_id"]
    for tin, tout in ((1000, 200), (500, 100)):
        admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, "
                      "model_id, input_tokens, output_tokens) VALUES (%s, 'whatsapp', 'outbound', 'text', 'hello there', "
                      "'model-a', %s, %s)", (pid, tin, tout))
    return pid


def test_observability_shapes_and_math(call_staff, spans):
    from staff_api import observability as obs
    _seed(spans)
    _comms(call_staff, spans)
    status, body = call_staff("GET", "/staff/observability", query={"hours": "24"})
    assert status == 200, body
    s = body["summary"]
    assert s["requests"] == 2 and s["errors"] == 1 and s["error_rate"] == 0.5 and s["traces"] == 2
    assert s["p50_ms"] is not None and s["p95_ms"] is not None and s["p95_ms"] >= s["p50_ms"]
    assert (s["tokens_in"], s["tokens_out"]) == (1500, 300)
    assert s["est_cost_usd"] == obs.est_cost(1500, 300) == 0.009

    req = body["series"]["requests"]
    assert len(req) in (96, 97) and sum(b["count"] for b in req) == 2 and sum(b["errors"] for b in req) == 1
    assert set(req[0]) == {"t", "count", "errors"} and req[0]["t"].endswith("Z")
    lat = body["series"]["latency"]
    assert len(lat) in (24, 25) and set(lat[0]) == {"t", "p50", "p95"}
    assert any(b["p95"] is not None for b in lat)
    tokens = body["series"]["tokens"]
    assert len(tokens) == 14 and tokens[-1]["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert tokens[-1]["input_tokens"] == 1500 and tokens[-1]["output_tokens"] == 300
    assert tokens[-1]["est_cost_usd"] == 0.009 and tokens[0]["input_tokens"] == 0

    drift = body["drift"]
    assert [m["key"] for m in drift["metrics"]] == [m["key"] for m in obs.drift_metrics({}, {})]
    cur = {m["key"]: m["current"] for m in drift["metrics"]}
    assert cur["tokens_in_per_reply"] == 750.0 and cur["tokens_out_per_reply"] == 150.0
    assert cur["agent_p95_ms"] == 80.0 and cur["kb_search_rate"] == 1.0 and cur["escalation_rate"] == 0.0
    assert cur["reply_length_chars"] == 11.0
    assert all(m["baseline"] is None and m["change_pct"] is None for m in drift["metrics"])
    assert drift["config_changes"] == []


def test_config_changes_from_hash_lag(call_staff, spans):
    for i, (h, m) in enumerate((("h1", "model-a"), ("h1", "model-a"), ("h2", "model-b"))):
        _span(spans, f"s-a{i}", f"{i:032d}", None, "agent.invoke", 90 - i * 10, 50.0,
              attrs={"trialtalk.config_hash": h, "gen_ai.request.model": m})
    status, body = call_staff("GET", "/staff/observability")
    assert status == 200
    changes = body["drift"]["config_changes"]
    assert len(changes) == 1 and changes[0]["config_hash"] == "h2" and changes[0]["model"] == "model-b"
    assert changes[0]["at"].endswith("Z")


def test_drift_baseline_window(call_staff, spans):
    # one agent trace 3 days ago (baseline), one now (current); only the baseline one has a KB search
    _span(spans, "b-agent", "c" * 32, None, "agent.invoke", 3 * 24 * 60, 400.0)
    _span(spans, "b-tool", "c" * 32, "b-agent", "tool.search_trial_docs", 3 * 24 * 60, 10.0)
    _span(spans, "c-agent", "d" * 32, None, "agent.invoke", 5, 200.0)
    status, body = call_staff("GET", "/staff/observability")
    m = {x["key"]: x for x in body["drift"]["metrics"]}
    assert m["agent_p95_ms"]["current"] == 200.0 and m["agent_p95_ms"]["baseline"] == 400.0
    assert m["agent_p95_ms"]["change_pct"] == -50.0
    assert m["kb_search_rate"]["current"] == 0.0 and m["kb_search_rate"]["baseline"] == 1.0
    assert m["kb_search_rate"]["change_pct"] == -100.0


def test_traces_list_and_waterfall_roundtrip(call_staff, spans):
    _seed(spans)
    status, body = call_staff("GET", "/staff/observability/traces", query={"limit": "50"})
    assert status == 200, body
    traces = {t["trace_id"]: t for t in body["traces"]}
    assert set(traces) == {T_OK, T_ERR}
    assert [t["trace_id"] for t in body["traces"]] == [T_ERR, T_OK]  # newest first
    ok, err = traces[T_OK], traces[T_ERR]
    assert set(ok) == {"trace_id", "started_at", "root_name", "service", "duration_ms", "span_count", "tokens",
                       "status"}
    assert (ok["root_name"], ok["service"], ok["duration_ms"], ok["span_count"], ok["tokens"], ok["status"]) == \
           ("whatsapp.route", "router", 100.0, 3, 1200, "OK")
    assert err["status"] == "ERROR" and err["tokens"] is None and err["span_count"] == 1

    status, body = call_staff("GET", f"/staff/observability/traces/{T_OK}")
    assert status == 200
    rows = body["spans"]
    assert [r["span_id"] for r in rows] == ["s-root-ok", "s-agent", "s-tool"]
    assert [r["start_ts"] for r in rows] == sorted(r["start_ts"] for r in rows)
    agent = next(r for r in rows if r["name"] == "agent.invoke")
    assert agent["parent_id"] == "s-root-ok" and agent["attrs"]["gen_ai.usage.input_tokens"] == 1000
    assert {"span_id", "trace_id", "parent_id", "name", "service", "kind", "start_ts", "duration_ms", "status",
            "attrs"} <= set(agent)

    assert call_staff("GET", "/staff/observability/traces", query={"limit": "1"})[1]["traces"][0]["trace_id"] == T_ERR
    assert call_staff("GET", "/staff/observability/traces", query={"limit": "x"})[0] == 400
    assert call_staff("GET", "/staff/observability/traces/bad id!")[0] == 400
    assert call_staff("GET", "/staff/observability", query={"hours": "x"})[0] == 400


def test_trace_tokens_not_double_counted(call_staff, spans):
    both, only_call = "e" * 32, "f" * 32
    usage = {"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 45}
    _span(spans, "w-root", both, None, "whatsapp.route", 10, 500.0, service="router")
    _span(spans, "w-call", both, "w-root", "agent.call", 10, 400.0, attrs=usage, service="router")
    _span(spans, "w-invoke", both, "w-call", "agent.invoke", 10, 380.0, attrs=usage, service="agent")
    _span(spans, "o-root", only_call, None, "whatsapp.route", 5, 500.0, service="router")
    _span(spans, "o-call", only_call, "o-root", "agent.call", 5, 400.0, attrs=usage, service="router")
    traces = {t["trace_id"]: t for t in call_staff("GET", "/staff/observability/traces")[1]["traces"]}
    assert traces[both]["tokens"] == 165 and traces[both]["span_count"] == 3
    assert traces[only_call]["tokens"] == 165


def _bucket_iso(ago_min, step):
    """The request-series bucket label a span started `ago_min` minutes ago lands in (as _span stores it)."""
    start = datetime.now(timezone.utc) - timedelta(minutes=ago_min)
    b = int(start.timestamp()) // step * step
    return datetime.fromtimestamp(b, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), start


def test_error_in_a_child_span_fails_the_request(call_staff, spans):
    from staff_api import observability as obs
    _seed(spans)  # T_ERR: the error is on the root itself; T_OK: no error anywhere
    child_err = "9" * 32  # root OK (WhatsApp ingest ends before routing), agent.invoke ERROR
    label, start = _bucket_iso(180, obs.REQUEST_BUCKET_S)
    spans.execute(INSERT_SPAN, ("c-root", child_err, None, "whatsapp.ingest", "ingest", "SERVER", start, 50.0, "OK",
                                "{}"))
    spans.execute(INSERT_SPAN, ("c-invoke", child_err, "c-root", "agent.invoke", "agent", "INTERNAL", start, 40.0,
                                "ERROR", "{}"))
    status, body = call_staff("GET", "/staff/observability", query={"hours": "24"})
    assert status == 200, body
    s = body["summary"]
    assert (s["requests"], s["errors"], s["traces"]) == (3, 2, 3) and s["error_rate"] == round(2 / 3, 4)
    req = {b["t"]: b for b in body["series"]["requests"]}
    assert (req[label]["count"], req[label]["errors"]) == (1, 1)
    assert sum(b["count"] for b in req.values()) == 3 and sum(b["errors"] for b in req.values()) == 2
    traces = {t["trace_id"]: t for t in call_staff("GET", "/staff/observability/traces")[1]["traces"]}
    assert traces[child_err]["status"] == "ERROR" and traces[T_ERR]["status"] == "ERROR"
    assert traces[T_OK]["status"] == "OK"


def test_trace_with_only_an_orphaned_span_is_listed(call_staff, spans):
    # Dev-server web chat: POST /me/chat and agent.call stay in memory, the agent's own spans are stored.
    orphan = "7" * 32
    _span(spans, "g-invoke", orphan, "never-stored", "agent.invoke", 8, 600.0, service="trial-agent",
          attrs={"gen_ai.usage.input_tokens": 300, "gen_ai.usage.output_tokens": 40})
    _span(spans, "g-tool", orphan, "g-invoke", "tool.search_trial_docs", 8, 30.0, service="trial-agent")
    status, body = call_staff("GET", "/staff/observability/traces")
    assert status == 200, body
    (row,) = body["traces"]
    assert (row["trace_id"], row["root_name"], row["service"], row["duration_ms"], row["span_count"],
            row["tokens"], row["status"]) == (orphan, "agent.invoke", "trial-agent", 600.0, 2, 340, "OK")
    s = call_staff("GET", "/staff/observability")[1]["summary"]
    assert (s["requests"], s["traces"], s["errors"], s["p50_ms"]) == (1, 1, 0, 600.0)


def test_real_root_plus_orphaned_child_counts_once(call_staff, spans):
    both = "6" * 32
    _span(spans, "h-root", both, None, "whatsapp.route", 12, 900.0, service="router")
    # its parent (an agent.call that was never flushed) is missing, so it is a root candidate too
    _span(spans, "h-invoke", both, "h-lost-call", "agent.invoke", 12 - 1 / 60, 700.0, status="ERROR",
          service="trial-agent", attrs={"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5})
    traces = call_staff("GET", "/staff/observability/traces")[1]["traces"]
    assert [(t["trace_id"], t["root_name"], t["span_count"], t["tokens"], t["status"]) for t in traces] == \
           [(both, "whatsapp.route", 2, 15, "ERROR")]
    body = call_staff("GET", "/staff/observability")[1]
    s = body["summary"]
    assert (s["requests"], s["traces"], s["errors"], s["p50_ms"]) == (1, 1, 1, 900.0)
    assert sum(b["count"] for b in body["series"]["requests"]) == 1
    assert sum(b["errors"] for b in body["series"]["requests"]) == 1


def test_empty_db_returns_zeros_not_500(call_staff, spans):
    status, body = call_staff("GET", "/staff/observability")
    assert status == 200, body
    assert body["summary"] == {"requests": 0, "errors": 0, "error_rate": 0.0, "p50_ms": None, "p95_ms": None,
                               "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0, "traces": 0}
    assert sum(b["count"] for b in body["series"]["requests"]) == 0
    assert all(b["p50"] is None and b["p95"] is None for b in body["series"]["latency"])
    assert len(body["series"]["tokens"]) == 14 and all(d["input_tokens"] == 0 for d in body["series"]["tokens"])
    assert all(m["current"] is None and m["change_pct"] is None for m in body["drift"]["metrics"])
    assert body["drift"]["config_changes"] == []
    assert call_staff("GET", "/staff/observability/traces") == (200, {"traces": [], "hours": 24, "status": "all"})
    assert call_staff("GET", "/staff/observability/traces", query={"status": "errors"})[1]["traces"] == []
    assert call_staff("GET", f"/staff/observability/traces/{T_OK}") == (200, {"spans": []})


def test_usage_has_est_cost(call_staff, spans):
    _comms(call_staff, spans)
    status, body = call_staff("GET", "/staff/usage")
    assert status == 200
    assert body["totals"]["est_cost_usd"] == 0.009
    assert body["by_day"][0]["est_cost_usd"] == 0.009


def test_requires_ops_read(call_staff, spans):
    for path in ("/staff/observability", "/staff/observability/traces", f"/staff/observability/traces/{T_OK}"):
        assert call_staff("GET", path, claims=OPS)[0] == 403
    # a staff role that lacks ops:read is refused too
    from common import auth
    saved = auth.PERMISSIONS["coordinator"]
    auth.PERMISSIONS["coordinator"] = saved - {"ops:read"}
    try:
        for path in ("/staff/observability", "/staff/observability/traces", f"/staff/observability/traces/{T_OK}"):
            assert call_staff("GET", path, claims=STAFF_CLAIMS)[0] == 403
    finally:
        auth.PERMISSIONS["coordinator"] = saved


# ------------------------------------------------------------------ drill-down: errors filter, time bound, participant


def test_traces_errors_filter(call_staff, spans):
    _seed(spans)
    child_err = "9" * 32  # root OK, the failure is on a child span: still an error trace
    _span(spans, "c-root", child_err, None, "whatsapp.ingest", 40, 50.0, service="ingest")
    _span(spans, "c-invoke", child_err, "c-root", "agent.invoke", 40, 40.0, status="ERROR", service="agent")
    status, body = call_staff("GET", "/staff/observability/traces", query={"status": "errors"})
    assert status == 200, body
    assert body["status"] == "errors" and body["hours"] == 24
    assert [t["trace_id"] for t in body["traces"]] == [T_ERR, child_err]  # newest first, OK traces filtered out
    assert all(t["status"] == "ERROR" for t in body["traces"])
    everything = call_staff("GET", "/staff/observability/traces", query={"status": "all"})[1]["traces"]
    assert {t["trace_id"] for t in everything} == {T_OK, T_ERR, child_err}
    # the limit applies after the filter: the newest error trace, not the newest trace
    assert [t["trace_id"] for t in call_staff("GET", "/staff/observability/traces",
                                              query={"status": "errors", "limit": "1"})[1]["traces"]] == [T_ERR]
    # the list and the error KPI count the same traces
    assert call_staff("GET", "/staff/observability")[1]["summary"]["errors"] == len(body["traces"])
    assert call_staff("GET", "/staff/observability/traces", query={"status": "failed"})[0] == 400


def test_traces_time_bound(call_staff, spans):
    _seed(spans)
    old, old_err = "1" * 32, "2" * 32
    _span(spans, "o-root", old, None, "whatsapp.route", 30 * 60, 100.0, service="router")          # 30 h ago
    _span(spans, "oe-root", old_err, None, "whatsapp.route", 100 * 60, 100.0, status="ERROR")       # 100 h ago
    _span(spans, "ancient", "3" * 32, None, "whatsapp.route", 200 * 60, 100.0, service="router")    # 200 h ago

    def ids(**q):
        status, body = call_staff("GET", "/staff/observability/traces", query=q or None)
        assert status == 200, body
        return body["hours"], {t["trace_id"] for t in body["traces"]}

    assert ids() == (24, {T_OK, T_ERR})                                    # default: the last 24 hours
    assert ids(hours="48") == (48, {T_OK, T_ERR, old})
    assert ids(hours="999") == (168, {T_OK, T_ERR, old, old_err})          # clamped to 7 days
    assert ids(hours="0") == (1, {T_OK, T_ERR})                            # clamped to 1 hour
    assert ids(hours="168", status="errors") == (168, {T_ERR, old_err})
    assert call_staff("GET", "/staff/observability/traces", query={"hours": "x"})[0] == 400


def test_trace_root_before_window_stays_out(call_staff, spans):
    # A trace whose root started before the window is not listed even if a later span falls inside it (the KPIs'
    # rule), and the one-root-per-trace rule still holds for the traces that are listed.
    straddle = "4" * 32
    _span(spans, "s-root", straddle, None, "whatsapp.route", 61, 100.0, service="router")
    _span(spans, "s-late", straddle, "s-root", "agent.invoke", 59, 100.0, service="agent")
    assert call_staff("GET", "/staff/observability/traces", query={"hours": "1"})[1]["traces"] == []
    (row,) = call_staff("GET", "/staff/observability/traces", query={"hours": "2"})[1]["traces"]
    assert (row["trace_id"], row["root_name"], row["span_count"]) == (straddle, "whatsapp.route", 2)


def _enroll(call_staff, name, phone, site="RIVERSIDE"):
    status, body = call_staff("POST", "/staff/participants", {
        "full_name": name, "phone_e164": phone, "study_id": "KOA-301", "site_id": site})
    assert status == 201, body
    return body["participant"]["participant_id"]


def _reply_row(admin, pid, session, at, tin=None, tout=None, direction="outbound"):
    admin.execute("INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, "
                  "session_id, input_tokens, output_tokens, created_at) "
                  "VALUES (%s, 'whatsapp', %s, 'text', 'reply', %s, %s, %s, %s)",
                  (pid, direction, session, tin, tout, at))


def _agent_trace(admin, trace, ago_min, tin=120, tout=45, prefix="t", root="whatsapp.route", status="OK", usage=None):
    """A routing trace: a 6 s root span with an agent.invoke inside it. Returns the root's start. root=None stores only
    the agent's own spans (the dev server's web chat); usage replaces the agent.invoke token attributes."""
    start = datetime.now(timezone.utc) - timedelta(minutes=ago_min)
    if root:
        admin.execute(INSERT_SPAN, (f"{prefix}-root", trace, None, root, "router", "CONSUMER", start, 6000.0, status,
                                    "{}"))
    attrs = usage if usage is not None else {"gen_ai.usage.input_tokens": tin, "gen_ai.usage.output_tokens": tout}
    admin.execute(INSERT_SPAN, (f"{prefix}-invoke", trace, f"{prefix}-root" if root else "never-stored", "agent.invoke",
                                "trial-agent", "SERVER", start + timedelta(milliseconds=200), 5000.0, status,
                                json.dumps(attrs)))
    return start


def _web_session(pid, at):
    return f"web-{pid}-{at:%Y-%m-%d}"


def _wa_session(pid, at):
    return f"trialtalk-wa-{pid}-{at:%Y-%m-%d}".ljust(33, "0")


def test_trace_participant_resolved_inside_window(call_staff, spans):
    pid = _enroll(call_staff, "Maria Lopez", "+17325550142")
    linked, outside, demo = "a1" * 16, "b2" * 16, "c3" * 16
    start = _agent_trace(spans, linked, 10, prefix="l")
    _reply_row(spans, pid, _wa_session(pid, start), start + timedelta(seconds=5.5), 120, 45)
    # the participant's own inbound (no tokens) in the same window does not change the answer
    _reply_row(spans, pid, _wa_session(pid, start), start + timedelta(seconds=0.1), direction="inbound")
    status, body = call_staff("GET", f"/staff/observability/traces/{linked}")
    assert status == 200, body
    assert body["participant_id"] == pid and len(body["spans"]) == 2

    # a reply logged well after the trace ended is not a match
    start = _agent_trace(spans, outside, 30, prefix="o")
    _reply_row(spans, pid, _wa_session(pid, start), start + timedelta(seconds=40), 120, 45)
    status, body = call_staff("GET", f"/staff/observability/traces/{outside}")
    assert status == 200 and body["spans"] and "participant_id" not in body

    # a session that names no participant (demo user), or names another participant than the row's, is not a match
    start = _agent_trace(spans, demo, 50, prefix="d")
    _reply_row(spans, pid, "trialtalk-wa-demo-0123456789ab-" + f"{start:%Y-%m-%d}", start + timedelta(seconds=3),
               120, 45)
    _reply_row(spans, pid, _wa_session("P-9999", start), start + timedelta(seconds=3), 120, 45)
    assert "participant_id" not in call_staff("GET", f"/staff/observability/traces/{demo}")[1]


def test_trace_participant_needs_matching_tokens_and_one_participant(call_staff, spans):
    maria = _enroll(call_staff, "Maria Lopez", "+17325550142")
    sam = _enroll(call_staff, "Sam Okafor", "+17325550143")
    both, tokens = "d4" * 16, "e5" * 16
    # two participants' replies inside one trace window with the same token counts: ambiguous, so no link
    start = _agent_trace(spans, both, 10, prefix="b")
    _reply_row(spans, maria, _wa_session(maria, start), start + timedelta(seconds=5), 120, 45)
    _reply_row(spans, sam, _wa_session(sam, start), start + timedelta(seconds=5.2), 120, 45)
    assert "participant_id" not in call_staff("GET", f"/staff/observability/traces/{both}")[1]
    # the trace's token counts pick the right reply when another participant's falls in the window too
    start = _agent_trace(spans, tokens, 30, tin=700, tout=90, prefix="t")
    _reply_row(spans, maria, _wa_session(maria, start), start + timedelta(seconds=5), 120, 45)
    _reply_row(spans, sam, _wa_session(sam, start), start + timedelta(seconds=5.2), 700, 90)
    assert call_staff("GET", f"/staff/observability/traces/{tokens}")[1]["participant_id"] == sam


def test_trace_participant_is_site_scoped(call_staff, spans):
    from conftest import OTHER_SITE_CLAIMS
    pid = _enroll(call_staff, "Maria Lopez", "+17325550142")   # a RIVERSIDE participant
    trace = "f6" * 16
    start = _agent_trace(spans, trace, 10)
    _reply_row(spans, pid, _wa_session(pid, start), start + timedelta(seconds=5), 120, 45)
    assert call_staff("GET", f"/staff/observability/traces/{trace}")[1]["participant_id"] == pid
    status, body = call_staff("GET", f"/staff/observability/traces/{trace}", claims=OTHER_SITE_CLAIMS)
    assert status == 200, body
    assert len(body["spans"]) == 2 and "participant_id" not in body   # spans are PHI-free; the link is not


def test_failed_whatsapp_trace_never_links_a_bystander(call_staff, spans):
    # Review finding: Maria's agent call fails (no tokens anywhere in the trace) and the router logs her fallback
    # reply with session_id NULL; Sam's unrelated, successful reply lands inside the 30 s window. Must not be Sam.
    maria = _enroll(call_staff, "Maria Lopez", "+17325550142")
    sam = _enroll(call_staff, "Sam Okafor", "+17325550143")
    tr = "ab" * 16
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    spans.execute(INSERT_SPAN, ("r", tr, None, "whatsapp.route", "router", "CONSUMER", start, 30000.0, "ERROR", "{}"))
    spans.execute(INSERT_SPAN, ("i", tr, "r", "agent.invoke", "trial-agent", "SERVER",
                                start + timedelta(milliseconds=200), 29000.0, "ERROR", "{}"))
    _reply_row(spans, maria, None, start + timedelta(seconds=30))
    _reply_row(spans, sam, _wa_session(sam, start), start + timedelta(seconds=12), 300, 60)
    # Sam's inbound in the window (a session id, no tokens) is not a reply either
    _reply_row(spans, sam, _wa_session(sam, start), start + timedelta(seconds=11), direction="inbound")
    body = call_staff("GET", f"/staff/observability/traces/{tr}")[1]
    assert body["spans"] and "participant_id" not in body


def test_failed_web_trace_links_its_own_untokened_reply(call_staff, spans):
    # A web chat whose agent call failed: the fallback reply carries the web session and no tokens, so it is the
    # one candidate; a bystander's successful reply in the window (tokens) is not.
    maria = _enroll(call_staff, "Maria Lopez", "+17325550142")
    sam = _enroll(call_staff, "Sam Okafor", "+17325550143")
    tr = "ac" * 16
    start = _agent_trace(spans, tr, 5, root=None, status="ERROR", usage={})
    _reply_row(spans, maria, _web_session(maria, start), start + timedelta(seconds=5.5))
    _reply_row(spans, sam, _web_session(sam, start), start + timedelta(seconds=3), 300, 60)
    assert call_staff("GET", f"/staff/observability/traces/{tr}")[1]["participant_id"] == maria


def test_trace_participant_channel_and_token_pair_rules(call_staff, spans):
    maria = _enroll(call_staff, "Maria Lopez", "+17325550142")
    # A WhatsApp trace never matches a web session (and the reverse), even with equal tokens.
    wa, web = "ad" * 16, "ae" * 16
    start = _agent_trace(spans, wa, 10, prefix="w")
    _reply_row(spans, maria, _web_session(maria, start), start + timedelta(seconds=5), 120, 45)
    assert "participant_id" not in call_staff("GET", f"/staff/observability/traces/{wa}")[1]
    start = _agent_trace(spans, web, 20, root=None, prefix="x")
    _reply_row(spans, maria, _wa_session(maria, start), start + timedelta(seconds=4), 120, 45)
    assert "participant_id" not in call_staff("GET", f"/staff/observability/traces/{web}")[1]
    # Only an input count on the trace: the pair must still match, so a reply with an output count does not.
    lone = "af" * 16
    start = _agent_trace(spans, lone, 30, prefix="l", usage={"gen_ai.usage.input_tokens": 120})
    _reply_row(spans, maria, _wa_session(maria, start), start + timedelta(seconds=5), 120, 45)
    assert "participant_id" not in call_staff("GET", f"/staff/observability/traces/{lone}")[1]
    # No agent.invoke tokens stored, but the router's agent.call has them: those are the trace's usage.
    call = "b0" * 16
    start = _agent_trace(spans, call, 40, prefix="c", usage={})
    spans.execute(INSERT_SPAN, ("c-call", call, "c-root", "agent.call", "router", "CLIENT",
                                start + timedelta(milliseconds=100), 5200.0, "OK",
                                json.dumps({"gen_ai.usage.input_tokens": 500, "gen_ai.usage.output_tokens": 70})))
    _reply_row(spans, maria, _wa_session(maria, start), start + timedelta(seconds=5.5), 500, 70)
    assert call_staff("GET", f"/staff/observability/traces/{call}")[1]["participant_id"] == maria
