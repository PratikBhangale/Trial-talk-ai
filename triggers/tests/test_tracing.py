"""Trace propagation SNS -> ingest -> SQS -> router -> agent -> send, token recording, and telemetry-outage safety."""

import json
import re

import pytest

import telemetry
from conftest import finished, needs_sdk

TP_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")  # flags: sampled, maybe random-id (03)
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
PHONE, TEXT = "+15550001111", "what time do I take my pill"
PARTICIPANT = {"participant_id": "P-001", "preferred_language": "en", "timezone": "America/New_York",
               "whatsapp_opt_in": True, "site_id": "RIVERSIDE", "status": "active"}


def sns_event(*messages):
    entry = {"changes": [{"value": {"messages": list(messages)}}]}
    header = {"messageId": "aws-1", "context": {"MetaPhoneNumberIds": [{"arn": "arn:phone"}]},
              "whatsAppWebhookEntry": json.dumps(entry)}
    return {"Records": [{"Sns": {"Message": json.dumps(header)}}]}


def text_message(wamid="wamid.1", body=TEXT):
    return {"from": PHONE.lstrip("+"), "id": wamid, "type": "text", "text": {"body": body}}


def sqs_event(traceparent=f"00-{TRACE_ID}-{PARENT_ID}-01", n=1):
    records = []
    for i in range(n):
        body = {"wamid": f"wamid.{i}", "phone": PHONE, "type": "text", "text": TEXT, "button_id": None,
                "button_text": None, "media_id": None, "comm_id": f"c-{i}", "participant_id": "P-001"}
        if traceparent is not None:
            body["traceparent"] = traceparent
        records.append({"body": json.dumps(body)})
    return {"Records": records}


def router_db(sql, params):
    if "FROM pii.participants" in sql:
        return [PARTICIPANT]
    return []


def agent_contract(payload):
    """The Task 3 response: reply + usage + the agent's own spans in the caller's trace."""
    m = TP_RE.match(payload.get("traceparent") or "")
    trace_id, parent = (m.group(1), m.group(2)) if m else ("f" * 32, None)
    return {"reply": "Take it at 8am.",
            "usage": {"input_tokens": 120, "output_tokens": 45, "latency_ms": 900, "model": "test-model"},
            "trace": {"trace_id": trace_id, "spans": [
                {"span_id": "a" * 16, "parent_id": parent, "name": "agent.invoke", "start": "2026-10-28T12:00:00.000Z",
                 "duration_ms": 900.0, "status": "OK", "kind": "SERVER",
                 "attributes": {"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 45}},
                {"span_id": "b" * 16, "parent_id": "a" * 16, "name": "tool.search_trial_docs",
                 "start": "2026-10-28T12:00:00.100Z", "duration_ms": 80.5, "status": "OK", "kind": "INTERNAL",
                 "attributes": {"trialtalk.kb.results": 3}}]}}


def outbound_rows(rds):
    return [p for sql, p in rds.statements
            if "INSERT INTO trial.communications" in sql and "'outbound'" in sql and "'whatsapp'" in sql]


def no_phi(span):
    blob = json.dumps(dict(span.attributes or {}), default=str)
    assert PHONE.lstrip("+") not in blob and TEXT not in blob and "P-001" not in blob


# ------------------------------------------------------------------ the five pinned cases

@needs_sdk
def test_ingest_injects_traceparent(aws, ingest):
    aws.rds.respond = lambda sql, p: ([{"participant_id": "P-001"}] if "FROM pii.participants" in sql
                                      else [{"comm_id": "c-1"}] if "RETURNING comm_id" in sql else [])
    assert ingest.handler(sns_event(text_message()), None) == {"ok": True}

    assert len(aws.sqs.sent) == 1
    body = json.loads(aws.sqs.sent[0]["MessageBody"])
    m = TP_RE.match(body["traceparent"])
    assert m, body["traceparent"]
    (span,) = finished(aws, "whatsapp.ingest")
    assert m.group(1) == f"{span.context.trace_id:032x}"
    assert m.group(2) == f"{span.context.span_id:016x}"
    assert span.parent is None
    assert span.kind.name == "CONSUMER"
    assert span.attributes["trialtalk.msg_type"] == "text"
    assert span.attributes["trialtalk.duplicate"] is False
    no_phi(span)


@needs_sdk
def test_router_continues_trace_and_writes_tokens(aws, router):
    aws.rds.respond = router_db
    aws.agent.respond = agent_contract
    assert router.handler(sqs_event(), None) == {"ok": True}

    (route,) = finished(aws, "whatsapp.route")
    (call,) = finished(aws, "agent.call")
    assert f"{route.context.trace_id:032x}" == TRACE_ID
    assert f"{route.parent.span_id:016x}" == PARENT_ID
    assert route.kind.name == "CONSUMER" and call.kind.name == "CLIENT"
    assert call.parent.span_id == route.context.span_id
    assert call.attributes["gen_ai.usage.input_tokens"] == 120
    assert call.attributes["gen_ai.usage.output_tokens"] == 45
    assert call.attributes["gen_ai.request.model"] == "test-model"
    for s in (route, call):
        no_phi(s)

    # The agent got the agent.call span as its parent
    (payload,) = aws.agent.payloads
    assert payload["traceparent"] == f"00-{TRACE_ID}-{call.context.span_id:016x}-01"

    (row,) = outbound_rows(aws.rds)
    assert row["body"] == "Take it at 8am."
    assert (row["in_tok"], row["out_tok"]) == ("120", "45")
    assert aws.social.sent[0]["text"]["body"] == "Take it at 8am."

    agent_rows = [r for r in aws.rds.span_rows() if r["service"] == "trial-agent"]
    assert {r["span_id"] for r in agent_rows} == {"a" * 16, "b" * 16}
    assert {r["trace_id"] for r in agent_rows} == {TRACE_ID}
    # The router's own spans reached the DB sink too, in the same trace
    own = {r["name"]: r for r in aws.rds.span_rows() if r["service"] != "trial-agent"}
    assert own["whatsapp.route"]["trace_id"] == TRACE_ID and own["whatsapp.route"]["kind"] == "CONSUMER"


def test_router_handles_old_agent_response(aws, router):
    aws.rds.respond = router_db
    aws.agent.respond = lambda payload: {"reply": "ok"}
    assert router.handler(sqs_event(), None) == {"ok": True}

    (row,) = outbound_rows(aws.rds)
    assert row["body"] == "ok"
    assert row["in_tok"] is None and row["out_tok"] is None
    assert row["sid"].startswith("trialtalk-wa-P-001-") and row["lat"] is not None
    assert not [r for r in aws.rds.span_rows() if r["service"] == "trial-agent"]
    assert aws.social.sent[0]["text"]["body"] == "ok"


def test_router_survives_telemetry_outage(aws, router):
    aws.rds.respond = router_db
    aws.agent.respond = agent_contract
    aws.rds.fail_batch = True
    assert router.handler(sqs_event(), None) == {"ok": True}

    assert aws.social.sent[0]["text"]["body"] == "Take it at 8am."
    (row,) = outbound_rows(aws.rds)
    assert row["body"] == "Take it at 8am." and row["mid"] == "aws-msg-1"


@needs_sdk
def test_corrupt_traceparent_starts_new_trace(aws, router):
    aws.rds.respond = router_db
    aws.agent.respond = agent_contract
    assert router.handler(sqs_event(traceparent="garbage"), None) == {"ok": True}

    (route,) = finished(aws, "whatsapp.route")
    tid = f"{route.context.trace_id:032x}"
    assert re.fullmatch(r"[0-9a-f]{32}", tid) and tid != "0" * 32 and tid != TRACE_ID
    assert route.parent is None
    assert TP_RE.match(aws.agent.payloads[0]["traceparent"]).group(1) == tid
    assert outbound_rows(aws.rds)[0]["body"] == "Take it at 8am."


# ------------------------------------------------------------------ the rest of the pipeline

@needs_sdk
def test_ingest_marks_duplicates_and_does_not_forward(aws, ingest):
    aws.rds.respond = lambda sql, p: [{"participant_id": "P-001"}] if "FROM pii.participants" in sql else []
    ingest.handler(sns_event(text_message()), None)
    assert aws.sqs.sent == []
    (span,) = finished(aws, "whatsapp.ingest")
    assert span.attributes["trialtalk.duplicate"] is True


def test_inactive_telemetry_leaves_messages_unchanged(aws, ingest, router, monkeypatch):
    """No SDK (today's deployed Lambdas): SQS body and agent payload are exactly what they were before."""
    monkeypatch.setattr(telemetry, "AVAILABLE", False)
    monkeypatch.setattr(telemetry, "_state", {})
    aws.rds.respond = lambda sql, p: ([{"participant_id": "P-001"}] if "FROM pii.participants" in sql
                                      else [{"comm_id": "c-1"}] if "RETURNING comm_id" in sql else [])
    ingest.handler(sns_event(text_message()), None)
    body = json.loads(aws.sqs.sent[0]["MessageBody"])
    assert set(body) == {"wamid", "phone", "type", "text", "button_id", "button_text", "media_id", "comm_id",
                         "participant_id"}

    aws.rds.respond = router_db
    router.handler(sqs_event(traceparent=None), None)
    assert aws.agent.payloads == [{"participant_id": "P-001", "text": TEXT}]


def test_router_flushes_once_per_invocation(aws, router, monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "flush", lambda: calls.append(1))
    aws.rds.respond = router_db
    router.handler(sqs_event(n=3), None)
    assert calls == [1]


def test_router_flushes_even_when_a_record_fails(aws, router, monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "flush", lambda: calls.append(1))

    def boom(sql, p):
        raise RuntimeError("db down")
    aws.rds.respond = boom
    with pytest.raises(RuntimeError):
        router.handler(sqs_event(), None)
    assert calls == [1]


def test_router_ignores_malformed_usage(aws, router):
    """A strange usage block must never turn a good reply into an error or break the communications INSERT."""
    aws.rds.respond = router_db
    aws.agent.respond = lambda payload: {"reply": "fine", "usage": {"input_tokens": "lots", "output_tokens": True},
                                         "trace": "nope"}
    router.handler(sqs_event(), None)
    (row,) = outbound_rows(aws.rds)
    assert row["body"] == "fine" and row["in_tok"] is None and row["out_tok"] is None


@needs_sdk
def test_legacy_handler_traces_and_persists_agent_spans(aws, legacy):
    aws.rds.respond = lambda sql, p: [{"participant_id": "P-001"}] if "FROM pii.participants" in sql else []
    aws.agent.respond = agent_contract
    assert legacy.handler(sns_event(text_message()), None) == {"ok": True}

    (root,) = finished(aws, "whatsapp.legacy")
    (call,) = finished(aws, "agent.call")
    assert root.parent is None and call.parent.span_id == root.context.span_id
    assert call.kind.name == "CLIENT" and call.attributes["gen_ai.usage.input_tokens"] == 120
    no_phi(root)
    assert aws.social.sent[0]["text"]["body"] == "Take it at 8am."
    tid = f"{root.context.trace_id:032x}"
    assert TP_RE.match(aws.agent.payloads[0]["traceparent"]).group(1) == tid
    agent_rows = [r for r in aws.rds.span_rows() if r["service"] == "trial-agent"]
    assert {r["trace_id"] for r in agent_rows} == {tid} and len(agent_rows) == 2


def test_legacy_handler_survives_telemetry_outage(aws, legacy):
    aws.rds.respond = lambda sql, p: [{"participant_id": "P-001"}] if "FROM pii.participants" in sql else []
    aws.agent.respond = agent_contract
    aws.rds.fail_batch = True
    assert legacy.handler(sns_event(text_message()), None) == {"ok": True}
    assert aws.social.sent[0]["text"]["body"] == "Take it at 8am."


@needs_sdk
def test_reminder_send_span(aws, reminder, monkeypatch):
    class Pages:
        def paginate(self, **kw):
            return [{"templates": [{"templateName": "dose_v1", "templateLanguage": "en", "templateStatus": "APPROVED"}]}]
    monkeypatch.setattr(aws.social, "get_paginator", lambda name: Pages(), raising=False)

    def respond(sql, p):
        if "FROM trial.reminders r JOIN" in sql:
            return [{"reminder_type": "dose", "label": "Morning pill", "status": "active", "schedule_kind": "daily",
                     "template_name": "dose_v1", "template_language": "en", "template_params": '{"1": "morning"}',
                     "buttons": "Taken|Skipped", "ends_on": None, "follow_up_after_min": None,
                     "participant_id": "P-001", "phone_e164": PHONE, "whatsapp_opt_in": True,
                     "timezone": "America/New_York"}]
        if "RETURNING comm_id" in sql:
            return [{"comm_id": "c-9"}]
        if "RETURNING event_id" in sql:
            return [{"event_id": "e-9"}]
        if "RETURNING send_count" in sql:
            return [{"send_count": 1, "status": "active"}]
        return []
    aws.rds.respond = respond
    result = reminder.handler({"reminder_id": "11111111-1111-1111-1111-111111111111"}, None)
    assert result["result"] == "sent" and result["via"] == "template"

    (span,) = finished(aws, "reminder.send")
    assert span.parent is None
    assert span.attributes["trialtalk.msg_type"] == "template"
    assert span.attributes["trialtalk.reminder.type"] == "dose"
    no_phi(span)


@needs_sdk
def test_reconcile_run_span(aws, reconcile, monkeypatch):
    class Scheduler:
        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda self, **kw: [{"Schedules": [{"Name": "rem-gone"}]}]})()
    monkeypatch.setattr(reconcile, "scheduler", Scheduler())
    aws.rds.respond = lambda sql, p: [{"reminder_id": "new", "schedule_kind": "daily", "cron_expr": "cron(0 8 * * ? *)",
                                       "send_at": None, "timezone": "UTC", "starts_on": None, "ends_on": None}]
    result = reconcile.handler({}, None)
    assert result["created"] == ["new"] and result["deleted"] == ["gone"]

    (span,) = finished(aws, "reconcile.run")
    assert span.attributes["trialtalk.reconcile.created"] == 1
    assert span.attributes["trialtalk.reconcile.deleted"] == 1
    assert span.attributes["trialtalk.reconcile.dry_run"] is True


def test_legacy_handler_without_telemetry_module(aws, legacy, monkeypatch):
    """Deployed as a lone handler.py (no telemetry.py in the zip): behaves exactly as before."""
    monkeypatch.setattr(legacy, "telemetry", None)
    aws.rds.respond = lambda sql, p: [{"participant_id": "P-001"}] if "FROM pii.participants" in sql else []
    aws.agent.respond = agent_contract
    assert legacy.handler(sns_event(text_message()), None) == {"ok": True}
    assert aws.agent.payloads == [{"participant_id": "P-001", "text": TEXT}]
    assert aws.social.sent[0]["text"]["body"] == "Take it at 8am."
    assert legacy._agent_traces == [] and aws.rds.batches == []
