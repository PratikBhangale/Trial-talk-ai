"""Test setup for the triggers Lambdas. Fully offline: boto3 (and botocore, if absent) are faked before any handler
is imported, and each test wires the handlers' module-level clients to fresh fakes.

The Lambdas are deployed flat (handler.py + lambdas/common/*.py in one zip), so lambdas/common goes on sys.path and
each handler.py is loaded under its own module name. Run from triggers/: python -m pytest tests/ -v
"""

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

TRIGGERS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRIGGERS / "lambdas" / "common"))

for k, v in {"DB_CLUSTER_ARN": "arn:cluster", "DB_SECRET_ARN": "arn:secret", "DB_NAME": "trialtalk",
             "INBOUND_QUEUE_URL": "https://sqs.example/inbound.fifo", "FORWARD_TO_ROUTER": "1",
             "AGENT_RUNTIME_ARN": "arn:runtime", "RUNTIME_ARN": "arn:runtime",
             "ORIGINATION_PHONE_NUMBER_ID": "phone-id", "WABA_ID": "waba"}.items():
    os.environ[k] = v
os.environ.pop("WHATSAPP_STUB", None)
os.environ.pop("TELEMETRY_DB_SINK", None)
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)


class _Unwired:
    """What boto3.client() returns at import time. Tests replace it; touching it unwired is a test bug."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        raise AssertionError(f"boto3 client {self.name!r} used without a fake (.{attr})")


sys.modules["boto3"] = types.SimpleNamespace(client=lambda name, **kw: _Unwired(name))
try:
    import botocore.exceptions  # noqa: F401
except ImportError:
    class ClientError(Exception):
        def __init__(self, error_response=None, operation_name=""):
            super().__init__(operation_name)
            self.response = error_response or {"Error": {}}

    _bc = types.ModuleType("botocore")
    _bc.exceptions = types.ModuleType("botocore.exceptions")
    _bc.exceptions.ClientError = ClientError
    sys.modules["botocore"], sys.modules["botocore.exceptions"] = _bc, _bc.exceptions

import db  # noqa: E402
import schedules  # noqa: E402
import telemetry  # noqa: E402
import wa  # noqa: E402

SDK = telemetry.AVAILABLE
needs_sdk = pytest.mark.skipif(not SDK, reason="OpenTelemetry SDK not installed")


def load(name: str, path: Path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


# ------------------------------------------------------------------ fakes

class FakeRds:
    """RDS Data API. `respond(sql, params) -> rows` answers execute_statement; batch calls (span inserts) recorded."""

    def __init__(self, respond=None):
        self.respond = respond or (lambda sql, params: [])
        self.statements = []   # (sql, {name: python value or None})
        self.batches = []
        self.fail_batch = False
        self.exceptions = types.SimpleNamespace(DatabaseResumingException=type("Resuming", (Exception,), {}))

    def begin_transaction(self, **kw):
        return {"transactionId": "tx-1"}

    def commit_transaction(self, **kw):
        return {}

    rollback_transaction = commit_transaction

    def execute_statement(self, sql, parameters=(), **kw):
        params = {p["name"]: None if p["value"].get("isNull") else p["value"].get("stringValue")
                  for p in parameters}
        self.statements.append((sql, params))
        return {"formattedRecords": json.dumps(self.respond(sql, params))}

    def batch_execute_statement(self, **kw):
        if self.fail_batch:
            raise RuntimeError("Data API unavailable")
        self.batches.append(kw)
        return {}

    def span_rows(self):
        """Every span row written to trial.telemetry_spans, as {column: value}."""
        out = []
        for call in self.batches:
            assert "INSERT INTO trial.telemetry_spans" in call["sql"]
            for ps in call["parameterSets"]:
                out.append({p["name"]: next(iter(p["value"].values())) if not p["value"].get("isNull") else None
                            for p in ps})
        return out


class FakeAgent:
    """AgentCore runtime. `respond(payload) -> response body dict`."""

    def __init__(self, respond=None):
        self.respond = respond or (lambda payload: {"reply": "ok"})
        self.payloads = []

    def invoke_agent_runtime(self, **kw):
        payload = json.loads(kw["payload"])
        self.payloads.append(payload)
        return {"response": io.BytesIO(json.dumps(self.respond(payload)).encode())}


class FakeSocial:
    def __init__(self):
        self.sent = []

    def send_whatsapp_message(self, **kw):
        self.sent.append(json.loads(kw["message"]))
        return {"messageId": f"aws-msg-{len(self.sent)}"}


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "sqs-1"}


@pytest.fixture
def aws(monkeypatch):
    """Fresh fakes behind every client the handlers and telemetry use; telemetry re-initialised per test."""
    fakes = types.SimpleNamespace(rds=FakeRds(), agent=FakeAgent(), social=FakeSocial(), sqs=FakeSqs(), mem=None)
    monkeypatch.setattr(db, "rds", fakes.rds)
    monkeypatch.setattr(wa, "social", fakes.social)
    monkeypatch.setattr(telemetry, "_rds", fakes.rds)
    if SDK:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        telemetry.init(force=True)
        fakes.mem = InMemorySpanExporter()
        telemetry._state["tracer_provider"].add_span_processor(SimpleSpanProcessor(fakes.mem))
    yield fakes


def finished(fakes, name=None):
    spans = list(fakes.mem.get_finished_spans()) if fakes.mem else []
    return [s for s in spans if name is None or s.name == name]


@pytest.fixture
def ingest(aws, monkeypatch):
    mod = load("tt_ingest", TRIGGERS / "lambdas" / "ingest" / "handler.py")
    monkeypatch.setattr(mod, "sqs", aws.sqs)
    return mod


@pytest.fixture
def router(aws, monkeypatch):
    mod = load("tt_router", TRIGGERS / "lambdas" / "router" / "handler.py")
    monkeypatch.setattr(mod, "agentcore", aws.agent)
    return mod


@pytest.fixture
def reminder(aws):
    return load("tt_reminder", TRIGGERS / "lambdas" / "reminder" / "handler.py")


@pytest.fixture
def reconcile(aws):
    return load("tt_reconcile", TRIGGERS / "lambdas" / "reconcile" / "handler.py")


@pytest.fixture
def legacy(aws, monkeypatch):
    mod = load("tt_legacy_whatsapp", TRIGGERS / "whatsapp" / "handler.py")
    monkeypatch.setattr(mod, "rds", aws.rds)
    monkeypatch.setattr(mod, "agentcore", aws.agent)
    monkeypatch.setattr(mod, "social", aws.social)
    return mod
