"""OpenTelemetry for the Lambdas: one span per request, child spans for database and outbound calls, request metrics.

Uses the real OpenTelemetry SDK when it is installed (the dev server's venv, or a Lambda layer) and does nothing
when it is not, so the handlers never depend on it. Two sinks:

  * an in-process ring buffer + metric reader, read by GET /staff/observability (the staff portal widget);
  * an OTLP/HTTP exporter, added when OTEL_EXPORTER_OTLP_ENDPOINT is set and the OTLP package is importable.

The in-process window is per Lambda instance. Durable, fleet-wide data is what the OTLP exporter is for.

Nothing identifying goes into telemetry: route templates not URLs, SQL verbs not statements, no participant ids,
no bodies. Keep it that way when adding spans.
"""

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
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    AVAILABLE = True
except ImportError:  # SDK not installed: every function below becomes a no-op
    AVAILABLE = False

MAX_SPANS = 400

_lock = threading.Lock()
_state: dict = {}


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


def init(*, force: bool = False) -> bool:
    """Idempotent. Returns True when telemetry is active."""
    if not AVAILABLE:
        return False
    with _lock:
        if _state and not force:
            return True
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
