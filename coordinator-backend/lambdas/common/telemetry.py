# Copy of coordinator-backend/lambdas/common/telemetry.py — keep in sync (test_telemetry_sink.py enforces)
"""OpenTelemetry for the Lambdas: one span per request, child spans for database and outbound calls, request metrics.

Uses the real OpenTelemetry SDK when it is installed (the dev server's venv, or a Lambda layer) and does nothing
when it is not, so the handlers never depend on it. Three sinks:

  * an in-process ring buffer + metric reader, read by GET /staff/observability (the staff portal widget);
  * an OTLP/HTTP exporter, added when OTEL_EXPORTER_OTLP_ENDPOINT is set and the OTLP package is importable;
  * a durable sink into trial.telemetry_spans (RDS Data API), added when DB_CLUSTER_ARN and DB_SECRET_ARN are set
    and TELEMETRY_DB_SINK is not "0". Call flush() at the end of every Lambda handler.

The in-process window is per Lambda instance. Durable, fleet-wide data is what the DB sink and OTLP are for.
Telemetry never fails or delays a request: every exporter/persist path swallows its exceptions.

Nothing identifying goes into telemetry: route templates not URLs, SQL verbs not statements, no participant ids,
no bodies. Keep it that way when adding spans.
"""

import json
import math
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    from opentelemetry import trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    AVAILABLE = True
except ImportError:  # SDK not installed: every function below becomes a no-op
    AVAILABLE = False

MAX_SPANS = 400

_lock = threading.Lock()
_state: dict = {}

ALLOWED_ATTR_PREFIXES = ("http.", "db.", "messaging.", "gen_ai.", "trialtalk.", "faas.", "rpc.")
MAX_ATTR_STRING = 200
_BATCH = 100  # parameter sets per Data API call
_INSERT_SQL = (
    "INSERT INTO trial.telemetry_spans (span_id, trace_id, parent_id, name, service, kind, start_ts, duration_ms, "
    "status, attrs) VALUES (:span_id, :trace_id, :parent_id, :name, :service, :kind, :start_ts, :duration_ms, "
    ":status, :attrs) ON CONFLICT (span_id) DO NOTHING")

_rds = None
_warned = False


def _warn(what: str, exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"telemetry: {what} failed ({type(exc).__name__}: {exc}); continuing without it")


def _ok_value(v) -> bool:
    if isinstance(v, str):
        return len(v) <= MAX_ATTR_STRING
    if isinstance(v, float):
        return math.isfinite(v)  # NaN/Infinity make json.dumps emit JSON the Data API rejects (and poison the batch)
    return isinstance(v, (bool, int))


def _filter_attrs(attrs: dict) -> dict:
    """Keep only allowlisted keys with short scalar values. Anything else (bodies, phones, names) is dropped."""
    out = {}
    for k, v in (attrs or {}).items():
        if not isinstance(k, str) or not k.startswith(ALLOWED_ATTR_PREFIXES):
            continue
        if isinstance(v, (list, tuple)):
            if all(_ok_value(i) for i in v):
                out[k] = list(v)
        elif _ok_value(v):
            out[k] = v
    return out


def _db_configured() -> bool:
    return bool(os.environ.get("DB_CLUSTER_ARN") and os.environ.get("DB_SECRET_ARN"))


def _rds_client():
    global _rds
    if _rds is None:
        import boto3
        try:  # short timeouts, one attempt: a slow or resuming Aurora must not hold the request
            from botocore.config import Config
            cfg = {"config": Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1})}
        except ImportError:
            cfg = {}
        _rds = boto3.client("rds-data", **cfg)
    return _rds


def _ts(iso) -> str:
    """ISO-8601 (or epoch seconds) to the Data API's 'YYYY-MM-DD HH:MM:SS.mmm' in UTC."""
    if isinstance(iso, (int, float)):
        dt = datetime.fromtimestamp(iso, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _str(name, v):
    return {"name": name, "value": {"isNull": True} if v is None else {"stringValue": str(v)}}


def _param_set(row: dict) -> list:
    return [
        _str("span_id", row["span_id"]), _str("trace_id", row["trace_id"]), _str("parent_id", row.get("parent_id")),
        _str("name", row["name"]), _str("service", row["service"]), _str("kind", row.get("kind") or "INTERNAL"),
        {"name": "start_ts", "typeHint": "TIMESTAMP", "value": {"stringValue": _ts(row["start"])}},
        {"name": "duration_ms", "value": {"doubleValue": float(row.get("duration_ms") or 0)}},
        _str("status", row.get("status") or "UNSET"),
        {"name": "attrs", "typeHint": "JSON",
         "value": {"stringValue": json.dumps(_filter_attrs(row.get("attributes") or {}), default=str, allow_nan=False)}},
    ]


def _insert(rows: list[dict]) -> None:
    """Write rows (each: span_id, trace_id, parent_id, name, service, kind, start, duration_ms, status, attributes)."""
    if not rows or not _db_configured():
        return
    try:
        client = _rds_client()
        for i in range(0, len(rows), _BATCH):
            sets = []
            for row in rows[i:i + _BATCH]:
                try:
                    sets.append(_param_set(row))
                except Exception as exc:  # one malformed span must not drop its neighbours
                    _warn("building a span row", exc)
            if sets:
                client.batch_execute_statement(
                    resourceArn=os.environ["DB_CLUSTER_ARN"], secretArn=os.environ["DB_SECRET_ARN"],
                    database=os.environ.get("DB_NAME", "trialtalk"), sql=_INSERT_SQL, parameterSets=sets)
    except Exception as exc:
        _warn("writing spans to the database", exc)


def persist_spans(spans: list[dict], service: str, trace_id: str) -> None:
    """Insert pre-built span dicts (the agent's own format) through the same filtered path. Never raises."""
    try:
        _insert([{**s, "service": service, "trace_id": trace_id} for s in spans or []])
    except Exception as exc:
        _warn("persist_spans", exc)


class _NullSpan:
    def set_attribute(self, *a, **kw):
        pass

    def set_status(self, *a, **kw):
        pass

    def record_exception(self, *a, **kw):
        pass


if AVAILABLE:
    class _RingExporter(SpanExporter):
        """Keeps the most recent finished spans as plain dicts."""

        def __init__(self):
            self.spans: deque = deque(maxlen=MAX_SPANS)

        def export(self, spans):
            for s in spans:
                ctx, parent = s.get_span_context(), s.parent
                self.spans.append({
                    "trace_id": f"{ctx.trace_id:032x}",
                    "span_id": f"{ctx.span_id:016x}",
                    "parent_id": f"{parent.span_id:016x}" if parent else None,
                    "name": s.name,
                    "start": datetime.fromtimestamp(s.start_time / 1e9, timezone.utc).isoformat(timespec="milliseconds"),
                    "duration_ms": round((s.end_time - s.start_time) / 1e6, 2),
                    "status": s.status.status_code.name,
                    "attributes": dict(s.attributes or {}),
                    "events": [e.name for e in s.events],
                })
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass


    class _DbExporter(SpanExporter):
        """Durable sink: finished spans go to trial.telemetry_spans. Failures are swallowed."""

        def export(self, spans):
            try:
                rows = []
                for s in spans:
                    ctx, parent = s.get_span_context(), s.parent
                    rows.append({
                        "span_id": f"{ctx.span_id:016x}", "trace_id": f"{ctx.trace_id:032x}",
                        "parent_id": f"{parent.span_id:016x}" if parent else None, "name": s.name,
                        "service": (s.resource.attributes.get("service.name") if s.resource else None) or "trialtalk",
                        "kind": s.kind.name, "start": s.start_time / 1e9,
                        "duration_ms": round((s.end_time - s.start_time) / 1e6, 2),
                        "status": s.status.status_code.name, "attributes": dict(s.attributes or {}),
                    })
                _insert(rows)
            except Exception as exc:
                _warn("DB exporter", exc)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True


def init(*, force: bool = False) -> bool:
    """Idempotent. Returns True when telemetry is active."""
    if not AVAILABLE:
        return False
    with _lock:
        if _state and not force:
            return True
        old = _state.get("tracer_provider")
        if old is not None:
            try:
                old.shutdown()  # stop a previous BatchSpanProcessor thread
            except Exception:
                pass
        service = os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "trialtalk"
        resource = Resource.create({"service.name": service, "deployment.environment":
                                    os.environ.get("DEPLOY_ENV", "dev")})
        ring = _RingExporter()
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(SimpleSpanProcessor(ring))
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        otlp = _otlp_exporter()
        if otlp:
            tracer_provider.add_span_processor(SimpleSpanProcessor(otlp))
        if _db_configured() and os.environ.get("TELEMETRY_DB_SINK") != "0":
            tracer_provider.add_span_processor(BatchSpanProcessor(_DbExporter()))
        meter = meter_provider.get_meter("trialtalk")
        _state.clear()
        _state.update(
            ring=ring, reader=reader, service=service, otlp=bool(otlp), tracer=tracer_provider.get_tracer("trialtalk"),
            tracer_provider=tracer_provider, counters={},
            request_duration=meter.create_histogram(
                "http.server.request.duration", unit="s", description="HTTP server request duration"),
            meter=meter,
        )
        return True


def _otlp_exporter():
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        print("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp-proto-http is not installed")
        return None
    return OTLPSpanExporter()  # endpoint and headers come from the standard OTEL_* environment variables


def enabled() -> bool:
    return bool(_state)


def flush() -> None:
    """Force-flush every span processor. Call at the end of each Lambda handler. Never raises."""
    if not AVAILABLE or not _state:
        return
    try:
        _state["tracer_provider"].force_flush(2000)  # telemetry loses data before it slows a reply
    except Exception as exc:
        _warn("flush", exc)


def traceparent() -> str | None:
    """W3C traceparent header value for the current span, or None outside a span."""
    if not init():
        return None
    try:
        if not trace.get_current_span().get_span_context().is_valid:
            return None
        carrier: dict = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get("traceparent")
    except Exception:
        return None


def context_from(tp: str | None):
    """OTel context continuing the trace in a traceparent string; None (start a new trace) if absent or invalid."""
    if not tp or not AVAILABLE:
        return None
    try:
        ctx = TraceContextTextMapPropagator().extract(carrier={"traceparent": tp})
        return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None
    except Exception:
        return None


@contextmanager
def span(name: str, **attributes):
    """Child span (or a root one outside a request). Exceptions are recorded and re-raised."""
    if not init():
        yield _NullSpan()
        return
    with _state["tracer"].start_as_current_span(name, attributes=attributes or None,
                                                record_exception=True, set_status_on_exception=True) as s:
        yield s


@contextmanager
def request_span(method: str, route: str, headers: dict | None = None):
    """Server span for one API call. Continues the caller's trace if a W3C traceparent header came in."""
    if not init():
        yield _NullSpan()
        return
    ctx = TraceContextTextMapPropagator().extract(carrier={k.lower(): v for k, v in (headers or {}).items()})
    started = time.monotonic()
    with _state["tracer"].start_as_current_span(
            f"{method} {route}", context=ctx, kind=trace.SpanKind.SERVER,
            attributes={"http.request.method": method, "http.route": route}) as s:
        try:
            yield s
        finally:
            status = s.attributes.get("http.response.status_code") if hasattr(s, "attributes") else None
            _state["request_duration"].record(time.monotonic() - started, {
                "http.request.method": method, "http.route": route,
                "http.response.status_code": status if status is not None else 0})


def finish_request(s, status: int) -> None:
    s.set_attribute("http.response.status_code", status)
    if status >= 500:
        s.set_status(trace.Status(trace.StatusCode.ERROR, f"HTTP {status}"))


def count(name: str, n: int = 1, **attributes) -> None:
    """Increment a monotonic counter, e.g. count('trialtalk.alerts.raised', severity='urgent')."""
    if not init():
        return
    counters = _state["counters"]
    if name not in counters:
        counters[name] = _state["meter"].create_counter(name)
    counters[name].add(n, attributes)


def _metrics() -> list[dict]:
    data = _state["reader"].get_metrics_data()
    out = []
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                points = []
                for p in m.data.data_points:
                    attrs = {k: v for k, v in (p.attributes or {}).items()}
                    if hasattr(p, "bucket_counts"):
                        points.append({"attributes": attrs, "count": p.count, "sum": p.sum, "min": p.min, "max": p.max,
                                       "bounds": list(p.explicit_bounds), "buckets": list(p.bucket_counts)})
                    else:
                        points.append({"attributes": attrs, "value": p.value})
                out.append({"name": m.name, "unit": m.unit, "description": m.description,
                            "kind": "histogram" if points and "buckets" in points[0] else "counter",
                            "points": points})
    return out


def snapshot(limit: int = 150) -> dict:
    if not init():
        return {"enabled": False, "reason": "OpenTelemetry SDK is not installed in this environment",
                "spans": [], "metrics": []}
    spans = list(_state["ring"].spans)[-limit:]
    return {"enabled": True, "service": _state["service"], "otlp_export": _state["otlp"],
            "spans": spans, "metrics": _metrics()}


def reset() -> None:
    """Dev server 'Reset data': start with an empty window."""
    if AVAILABLE and _state:
        init(force=True)
