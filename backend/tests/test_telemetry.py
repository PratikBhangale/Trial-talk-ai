"""OpenTelemetry: request spans, child spans, metrics, and that nothing identifying leaks into them."""

import json

import pytest

pytest.importorskip("opentelemetry.sdk")

from common import telemetry  # noqa: E402
from common.http import Router  # noqa: E402


@pytest.fixture
def router():
    telemetry.init(force=True)
    r = Router()

    @r.route("GET", "/things/{tid}")
    def thing(event, claims, tid):
        with telemetry.span("db SELECT", **{"db.system": "postgresql"}):
            pass
        telemetry.count("trialtalk.alerts.raised", severity="urgent", source="sos_button")
        return 200, {"id": tid}

    @r.route("GET", "/boom")
    def boom(event, claims):
        raise RuntimeError("secret detail")

    return r


def call(router, method, path, headers=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": headers or {}}
    return router.dispatch(event, lambda e: {})


def test_request_span_has_child_and_route_template(router):
    assert call(router, "GET", "/things/P-1001")["statusCode"] == 200
    snap = telemetry.snapshot()
    assert snap["enabled"]
    by_name = {s["name"]: s for s in snap["spans"]}
    root, child = by_name["GET /things/{tid}"], by_name["db SELECT"]
    assert child["parent_id"] == root["span_id"] and child["trace_id"] == root["trace_id"]
    assert root["attributes"]["http.response.status_code"] == 200
    # The concrete id must not appear anywhere in telemetry.
    assert "P-1001" not in json.dumps(snap)


def test_metrics_and_counters(router):
    call(router, "GET", "/things/a")
    call(router, "GET", "/things/b")
    metrics = {m["name"]: m for m in telemetry.snapshot()["metrics"]}
    point = metrics["http.server.request.duration"]["points"][0]
    assert point["count"] == 2 and point["attributes"]["http.route"] == "/things/{tid}"
    counter = metrics["trialtalk.alerts.raised"]["points"][0]
    assert counter["value"] == 2 and counter["attributes"] == {"severity": "urgent", "source": "sos_button"}


def test_errors_are_flagged_and_unmatched_routes_are_bucketed(router):
    assert call(router, "GET", "/boom")["statusCode"] == 500
    assert call(router, "GET", "/nope/123")["statusCode"] == 404
    spans = {s["name"]: s for s in telemetry.snapshot()["spans"]}
    assert spans["GET /boom"]["status"] == "ERROR"
    assert "GET unmatched" in spans  # not "GET /nope/123"


def test_continues_incoming_trace(router):
    tid = "0af7651916cd43dd8448eb211c80319c"
    call(router, "GET", "/things/x", {"traceparent": f"00-{tid}-b7ad6b7169203331-01"})
    assert any(s["trace_id"] == tid for s in telemetry.snapshot()["spans"])
