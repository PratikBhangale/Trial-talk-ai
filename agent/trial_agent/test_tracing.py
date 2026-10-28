"""Offline tests for per-message tracing. No AWS: the Strands Agent and its AWS-backed pieces are stubbed.

Run from agent/trial_agent/:  python -m pytest test_tracing.py -v
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# core/store read these at import; dummy values keep the import offline
os.environ.setdefault("MEMORY_ID", "test-memory")
os.environ.setdefault("DB_CLUSTER_ARN", "arn:aws:rds:us-east-1:000000000000:cluster:test")
os.environ.setdefault("DB_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).parent))

import pytest

import app
import core
import tools
import tracing

TRACE_ID = "ab" * 16
REMOTE_PARENT = "cd" * 8
TRACEPARENT = f"00-{TRACE_ID}-{REMOTE_PARENT}-01"
SPAN_KEYS = {"span_id", "parent_id", "name", "start", "duration_ms", "status", "kind", "attributes"}


class _Result:
    def __init__(self, text="hi", usage=None):
        self.metrics = SimpleNamespace(accumulated_usage=usage if usage is not None
                                       else {"inputTokens": 120, "outputTokens": 45, "totalTokens": 165})
        self._text = text

    def __str__(self):
        return self._text


def _stub_agent(monkeypatch, behaviour):
    """Replace the Strands Agent (and the AWS-backed pieces build_agent constructs) with offline fakes."""
    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, text):
            return behaviour(text)

    monkeypatch.setattr(core, "Agent", FakeAgent)
    monkeypatch.setattr(core, "BedrockModel", lambda **kw: None)
    monkeypatch.setattr(core, "AgentCoreMemorySessionManager", lambda *a, **kw: None)


def _by_name(trace, name):
    return [s for s in trace["spans"] if s["name"] == name]


def test_collect_produces_parented_spans():
    with tracing.collect(None) as col:
        with tracing.span("outer", kind="SERVER", **{"trialtalk.x": 1}):
            with tracing.span("middle"):
                with tracing.span("inner"):
                    pass
    trace = col.result()

    spans = {s["name"]: s for s in trace["spans"]}
    assert set(spans) == {"outer", "middle", "inner"}
    for s in spans.values():
        assert set(s) == SPAN_KEYS
        assert s["status"] == "OK"
        assert isinstance(s["duration_ms"], float)
        assert s["start"].endswith("Z") and "T" in s["start"]
    assert len(trace["trace_id"]) == 32
    assert spans["outer"]["parent_id"] is None
    assert spans["outer"]["kind"] == "SERVER"
    assert spans["outer"]["attributes"] == {"trialtalk.x": 1}
    assert spans["middle"]["parent_id"] == spans["outer"]["span_id"]
    assert spans["inner"]["parent_id"] == spans["middle"]["span_id"]
    assert spans["inner"]["kind"] == "INTERNAL"


def test_collect_isolates_messages_and_ignores_spans_outside():
    with tracing.span("stray"):  # no collector active: must be a harmless no-op
        pass
    with tracing.collect(None) as a:
        with tracing.span("a"):
            pass
    with tracing.collect(None) as b:
        with tracing.span("b"):
            pass
    assert [s["name"] for s in a.result()["spans"]] == ["a"]
    assert [s["name"] for s in b.result()["spans"]] == ["b"]
    assert a.result()["trace_id"] != b.result()["trace_id"]


def test_traceparent_continues_remote_trace():
    with tracing.collect(TRACEPARENT) as col:
        with tracing.span("agent.invoke", kind="SERVER"):
            pass
    trace = col.result()
    assert trace["trace_id"] == TRACE_ID
    assert trace["spans"][0]["parent_id"] == REMOTE_PARENT


def test_invalid_traceparent_starts_new_trace():
    with tracing.collect("garbage") as col:
        with tracing.span("agent.invoke"):
            pass
    trace = col.result()
    assert len(trace["trace_id"]) == 32 and trace["trace_id"] != TRACE_ID
    assert trace["spans"][0]["parent_id"] is None


def test_handle_message_returns_reply_usage_trace(monkeypatch):
    _stub_agent(monkeypatch, lambda text: _Result("  hi  "))

    reply, usage, trace = core.handle_message("P-0201", "when is my next dose?", traceparent=TRACEPARENT)

    assert reply == "hi"
    assert usage["input_tokens"] == 120 and usage["output_tokens"] == 45
    assert isinstance(usage["latency_ms"], int) and usage["latency_ms"] >= 0
    assert usage["model"] == "global.anthropic.claude-sonnet-4-6"
    assert trace["trace_id"] == TRACE_ID
    [root] = _by_name(trace, "agent.invoke")
    assert root["kind"] == "SERVER" and root["status"] == "OK"
    assert root["parent_id"] == REMOTE_PARENT
    attrs = root["attributes"]
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 45
    assert attrs["gen_ai.request.model"] == "global.anthropic.claude-sonnet-4-6"
    assert attrs["trialtalk.config_hash"] == core.CONFIG_HASH and len(core.CONFIG_HASH) == 12
    # No PHI: neither the participant id nor the message text appears in any attribute
    for s in trace["spans"]:
        flat = repr(s["attributes"])
        assert "P-0201" not in flat and "next dose" not in flat


def test_app_invoke_response_contract(monkeypatch):
    _stub_agent(monkeypatch, lambda text: _Result("hello"))
    resp = app.invoke({"participant_id": "P-0201", "text": "hi", "traceparent": TRACEPARENT})
    assert resp["reply"] == "hello"
    assert set(resp["usage"]) == {"input_tokens", "output_tokens", "latency_ms", "model"}
    assert resp["trace"]["trace_id"] == TRACE_ID
    assert _by_name(resp["trace"], "agent.invoke")


def test_missing_usage_falls_back_to_zeros(monkeypatch, capsys):
    class NoMetrics:
        def __str__(self):
            return "ok"

    _stub_agent(monkeypatch, lambda text: NoMetrics())
    reply, usage, _ = core.handle_message("P-0201", "hi")
    assert reply == "ok"
    assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0


def test_invoke_error_still_returns_trace(monkeypatch):
    def boom(text):
        with tracing.span("tool.search_trial_docs"):
            pass
        raise RuntimeError("model throttled")

    _stub_agent(monkeypatch, boom)
    resp = app.invoke({"participant_id": "P-0201", "text": "hi", "traceparent": TRACEPARENT})

    assert "Sorry" in resp["reply"]
    assert resp["trace"]["trace_id"] == TRACE_ID
    [root] = _by_name(resp["trace"], "agent.invoke")
    assert root["status"] == "ERROR"
    assert _by_name(resp["trace"], "tool.search_trial_docs")[0]["parent_id"] == root["span_id"]


def test_tracing_failure_never_breaks_reply(monkeypatch):
    class BrokenTracer:
        def start_span(self, *a, **kw):
            raise RuntimeError("tracer exploded")

    monkeypatch.setattr(tracing, "_tracer", BrokenTracer())
    _stub_agent(monkeypatch, lambda text: _Result("still here"))
    resp = app.invoke({"participant_id": "P-0201", "text": "hi"})
    assert resp["reply"] == "still here"
    assert resp["usage"]["input_tokens"] == 120
    assert "trace" in resp


def test_search_trial_docs_span_has_kb_attrs(monkeypatch):
    class FakeKB:
        def retrieve(self, **kw):
            return {"retrievalResults": [
                {"content": {"text": "Take the dose with food."}, "score": 0.81,
                 "location": {"s3Location": {"uri": "s3://b/protocol.pdf"}}},
                {"content": {"text": "Visit 3 is week 4."}, "score": 0.42},
            ]}

    monkeypatch.setattr(tools, "kb", FakeKB())
    monkeypatch.setattr(tools, "KNOWLEDGE_BASE_ID", "KB123")
    with tracing.collect(None) as col:
        out = tools.search_trial_docs(question="can I take my dose with food")
    assert "Take the dose with food." in out
    [s] = _by_name(col.result(), "tool.search_trial_docs")
    assert s["attributes"] == {"trialtalk.kb.results": 2, "trialtalk.kb.top_score": 0.81}


def test_escalate_span_has_severity_only(monkeypatch):
    sent = []
    monkeypatch.setattr(tools.store, "get_profile", lambda pid: {"site_id": "S1", "study_id": "T1"})
    monkeypatch.setattr(tools, "ses", SimpleNamespace(send_email=lambda **kw: sent.append(kw)))
    ctx = SimpleNamespace(agent=SimpleNamespace(state={"participant_id": "P-0201"}))
    with tracing.collect(None) as col:
        tools.escalate_to_coordinator(severity="routine", summary="mild headache", tool_context=ctx)
    assert sent
    [s] = _by_name(col.result(), "tool.escalate_to_coordinator")
    assert s["attributes"] == {"trialtalk.escalation.severity": "routine"}
    assert s["status"] == "OK"
