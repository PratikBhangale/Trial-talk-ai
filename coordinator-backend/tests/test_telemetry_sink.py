"""Durable span sink: DB exporter, attribute allowlist, W3C propagation helpers."""

import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")

from common import telemetry  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class FakeRds:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def batch_execute_statement(self, **kw):
        if self.fail:
            raise RuntimeError("rds down")
        self.calls.append(kw)
        return {}


@pytest.fixture
def rds(monkeypatch):
    fake = FakeRds()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name, **kw: fake))
    monkeypatch.setenv("DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("DB_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("DB_NAME", "trialtalk")
    monkeypatch.delenv("TELEMETRY_DB_SINK", raising=False)
    monkeypatch.setattr(telemetry, "_rds", None)
    telemetry.init(force=True)
    yield fake
    monkeypatch.delenv("DB_CLUSTER_ARN")
    monkeypatch.delenv("DB_SECRET_ARN")
    telemetry.init(force=True)


def rows(fake):
    """Every parameter set sent, as {name: parameter} dicts (typeHint and all)."""
    out = []
    for call in fake.calls:
        assert "INSERT INTO trial.telemetry_spans" in call["sql"]
        assert "ON CONFLICT (span_id) DO NOTHING" in call["sql"]
        for ps in call["parameterSets"]:
            out.append({p["name"]: p for p in ps})
    return out


def value(p):
    return next(iter(p["value"].values()))


def test_spans_flushed_to_db(rds):
    with telemetry.span("db SELECT", **{"db.system": "postgresql", "gen_ai.usage.input_tokens": 12}) as s:
        trace_id = f"{s.get_span_context().trace_id:032x}"
    telemetry.flush()
    assert len(rds.calls) == 1
    assert rds.calls[0]["resourceArn"] == "arn:cluster" and rds.calls[0]["secretArn"] == "arn:secret"
    assert rds.calls[0]["database"] == "trialtalk"
    (row,) = rows(rds)
    assert value(row["name"]) == "db SELECT"
    assert value(row["trace_id"]) == trace_id
    assert "doubleValue" in row["duration_ms"]["value"]
    assert row["attrs"]["typeHint"] == "JSON"
    assert json.loads(value(row["attrs"]))["db.system"] == "postgresql"
    assert row["start_ts"]["typeHint"] == "TIMESTAMP"
    assert len(value(row["start_ts"])) == len("2026-01-01 00:00:00.000")


def test_allowlist_drops_phi_and_long_values():
    out = telemetry._filter_attrs({
        "body_text": "hello", "phone": "+15555550100", "gen_ai.usage.input_tokens": 7,
        "http.route": "x" * 300, "http.request.method": "GET", "trialtalk.note": "y" * 200,
    })
    assert out == {"gen_ai.usage.input_tokens": 7, "http.request.method": "GET", "trialtalk.note": "y" * 200}


def test_db_failure_never_raises(rds):
    rds.fail = True
    with telemetry.span("work"):
        pass
    telemetry.flush()
    telemetry.persist_spans([{"span_id": "a" * 16, "name": "n", "start": "2026-01-01T00:00:00+00:00",
                              "duration_ms": 1, "status": "OK", "kind": "INTERNAL", "attributes": {}}],
                            "trial-agent", "b" * 32)


def test_persist_spans_inserts_agent_format(rds):
    spans = [
        {"span_id": "1" * 16, "parent_id": None, "name": "invoke_agent", "start": "2026-05-01T10:00:00.123+00:00",
         "duration_ms": 850.5, "status": "OK", "kind": "SERVER",
         "attributes": {"gen_ai.request.model": "m", "patient_name": "Jane", "faas.name": "f"}},
        {"span_id": "2" * 16, "parent_id": "1" * 16, "name": "tool", "start": "2026-05-01T10:00:00.200Z",
         "duration_ms": 20, "status": "ERROR", "kind": "INTERNAL", "attributes": {}},
    ]
    telemetry.persist_spans(spans, "trial-agent", "c" * 32)
    got = rows(rds)
    assert len(got) == 2
    assert {value(r["service"]) for r in got} == {"trial-agent"}
    assert {value(r["trace_id"]) for r in got} == {"c" * 32}
    first, second = got
    assert value(first["name"]) == "invoke_agent" and value(first["start_ts"]) == "2026-05-01 10:00:00.123"
    assert first["parent_id"]["value"].get("isNull") is True
    assert value(second["parent_id"]) == "1" * 16 and value(second["status"]) == "ERROR"
    assert value(second["start_ts"]) == "2026-05-01 10:00:00.200"
    assert json.loads(value(first["attrs"])) == {"gen_ai.request.model": "m", "faas.name": "f"}


def test_persist_spans_noop_without_db_env(monkeypatch):
    monkeypatch.delenv("DB_CLUSTER_ARN", raising=False)
    telemetry.persist_spans([{"span_id": "1" * 16, "name": "x"}], "svc", "d" * 32)  # must not raise


def test_db_sink_can_be_disabled(rds, monkeypatch):
    monkeypatch.setenv("TELEMETRY_DB_SINK", "0")
    telemetry.init(force=True)
    with telemetry.span("work"):
        pass
    telemetry.flush()
    assert rds.calls == []


def test_traceparent_roundtrip():
    telemetry.init(force=True)
    assert telemetry.traceparent() is None  # no active span
    with telemetry.span("outer") as s:
        tp = telemetry.traceparent()
        sc = s.get_span_context()
    version, trace_id, span_id, flags = tp.split("-")
    assert (version, trace_id, span_id) == ("00", f"{sc.trace_id:032x}", f"{sc.span_id:016x}")
    ctx = telemetry.context_from(tp)
    assert ctx is not None
    from opentelemetry import trace
    assert trace.get_current_span(ctx).get_span_context().trace_id == sc.trace_id
    assert telemetry.context_from("garbage") is None
    assert telemetry.context_from(None) is None


def test_telemetry_copies_identical():
    a = (ROOT / "coordinator-backend/lambdas/common/telemetry.py").read_bytes()
    b = (ROOT / "triggers/lambdas/common/telemetry.py").read_bytes()
    assert a == b
