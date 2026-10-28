"""Per-message tracing for the agent. Spans are collected in memory and returned to the caller,
who persists them (the agent itself never writes telemetry anywhere).

    with tracing.collect(payload.get("traceparent")) as col:
        with tracing.span("agent.invoke", kind="SERVER", **{"gen_ai.request.model": model}):
            ...
    col.result()  # {"trace_id": str, "spans": [SpanDict, ...]}

SpanDict = {span_id, parent_id, name, start (ISO-8601 ms, UTC), duration_ms, status, kind, attributes}.
Tracing must never break or delay a reply: every tracing failure is swallowed and printed once.
No PHI in attributes: callers pass only counts, scores, ids of config, never message text or participant data.
"""

import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind, Status, StatusCode, format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

MAX_SPANS = 200  # per message; a runaway tool loop can't grow the response without bound

_current: ContextVar["Collector | None"] = ContextVar("trialtalk_collector", default=None)
_owners: dict[int, "Collector"] = {}  # open span_id -> the collector that will receive it
_owners_lock = threading.Lock()
_warned: set[str] = set()


def _warn(what: str, err: Exception) -> None:
    if what not in _warned:
        _warned.add(what)
        print(f"tracing: {what} failed, continuing without it: {type(err).__name__}: {err}")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_dict(s) -> dict:
    start, end = s.start_time or 0, s.end_time or s.start_time or 0
    return {
        "span_id": format_span_id(s.context.span_id),
        "parent_id": format_span_id(s.parent.span_id) if s.parent is not None else None,
        "name": s.name,
        "start": _iso(start),
        "duration_ms": round((end - start) / 1e6, 3),
        "status": s.status.status_code.name,
        "kind": s.kind.name,
        "attributes": dict(s.attributes or {}),
    }


class Collector:
    """Receives the finished spans of one message."""

    def __init__(self, trace_id: str = ""):
        self.trace_id = trace_id
        self._spans: deque = deque(maxlen=MAX_SPANS)
        self._lock = threading.Lock()

    def _add(self, span_dict: dict) -> None:
        with self._lock:
            self._spans.append(span_dict)

    def result(self) -> dict:
        with self._lock:
            spans = sorted(self._spans, key=lambda s: s["start"])
        return {"trace_id": self.trace_id, "spans": spans}


class _CollectorProcessor(SpanProcessor):
    """Routes each finished span to the collector that was current when it started (works across threads,
    since Strands copies contextvars into its worker threads)."""

    def on_start(self, span, parent_context=None) -> None:
        try:
            col = _current.get()
            if col is None:
                return
            if not col.trace_id:
                col.trace_id = format_trace_id(span.context.trace_id)
            with _owners_lock:
                _owners[span.context.span_id] = col
        except Exception as e:
            _warn("span start", e)

    def on_end(self, span) -> None:
        try:
            with _owners_lock:
                col = _owners.pop(span.context.span_id, None)
            if col is not None:
                col._add(_to_dict(span))
        except Exception as e:
            _warn("span export", e)


# Our own provider, deliberately NOT installed globally: Strands/AgentCore may own the global one.
# ALWAYS_ON so an unsampled upstream traceparent never silently drops the agent's spans.
_provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
_provider.add_span_processor(_CollectorProcessor())
_tracer = _provider.get_tracer("trialtalk.agent")


def _remote_context(traceparent):
    """OTel context continuing `traceparent`, or None if it is missing or invalid (new trace)."""
    if not isinstance(traceparent, str) or not traceparent:
        return None
    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent}, context=otel_context.Context())
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


def trace_id_of(traceparent) -> str:
    """The trace id inside a valid traceparent, else ''."""
    try:
        ctx = _remote_context(traceparent)
        return format_trace_id(trace.get_current_span(ctx).get_span_context().trace_id) if ctx else ""
    except Exception:
        return ""


def empty_trace(traceparent=None) -> dict:
    return {"trace_id": trace_id_of(traceparent), "spans": []}


@contextmanager
def collect(traceparent: str | None = None):
    """Collect every span started inside this block (in any thread that inherits its context)."""
    col = Collector()
    col_token = _current.set(col)
    ctx_token = None
    try:
        remote = _remote_context(traceparent)
        if remote is not None:
            col.trace_id = trace_id_of(traceparent)
        # No (valid) traceparent: start from an empty context so we never join an unrelated ambient trace
        ctx_token = otel_context.attach(remote if remote is not None else otel_context.Context())
    except Exception as e:
        _warn("traceparent", e)
    try:
        yield col
    finally:
        try:
            if ctx_token is not None:
                otel_context.detach(ctx_token)
            _current.reset(col_token)
        except Exception as e:
            _warn("collector cleanup", e)


@contextmanager
def span(name: str, kind: str = "INTERNAL", **attrs):
    """A child of the current span. Yields the span (a no-op span if tracing is broken).
    Ends OK, or ERROR if the body raises (exception re-raised; its message is never recorded)."""
    s, token = trace.INVALID_SPAN, None
    try:
        s = _tracer.start_span(name, kind=SpanKind[kind],
                               attributes={k: v for k, v in attrs.items() if v is not None})
        token = otel_context.attach(trace.set_span_in_context(s))
    except Exception as e:
        _warn("span", e)
    try:
        yield s
    except BaseException as exc:
        _finish(s, token, StatusCode.ERROR, type(exc).__name__)
        raise
    else:
        _finish(s, token, StatusCode.OK, None)


def _finish(s, token, code, description) -> None:
    try:
        status = getattr(s, "status", None)  # SDK spans expose it; the no-op span doesn't
        if status is not None and status.status_code is StatusCode.ERROR:
            code, description = StatusCode.ERROR, status.description  # body already marked it failed
        s.set_status(Status(code, description))
    except Exception as e:
        _warn("span status", e)
    try:
        if token is not None:
            otel_context.detach(token)
    except Exception as e:
        _warn("context detach", e)
    try:
        s.end()
    except Exception as e:
        _warn("span end", e)


def set_attrs(s, **attrs) -> None:
    """Set attributes on a span without ever raising."""
    try:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
    except Exception as e:
        _warn("set attribute", e)


def mark_error(s, description: str) -> None:
    """Mark a span failed when the code handled the error itself (e.g. a tool returning a fallback)."""
    try:
        s.set_status(Status(StatusCode.ERROR, description))
    except Exception as e:
        _warn("set status", e)
