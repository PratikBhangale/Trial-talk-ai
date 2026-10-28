"""tt-ingest: SNS whatsapp_topic -> this Lambda.

- Inbound message: look up the participant by phone, log one trial.communications row
  (deduped on the WhatsApp message id), then queue it on the FIFO queue (group = phone)
  so tt-router handles each person's messages in order.
- Status event (sent / delivered / read / failed): update delivery_status on the outbound row.

The topic is a standard SNS topic, which can't feed a FIFO queue directly; this Lambda is the bridge.
"""
import json
import os
from contextlib import contextmanager

import boto3

import telemetry
from db import Tx
from wa import message_fields, parse_sns

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["INBOUND_QUEUE_URL"]
FORWARD = os.environ.get("FORWARD_TO_ROUTER") == "1"

# Statuses can arrive out of order; never move backwards (read -> delivered).
STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


def handle_status(s, aws_id):
    rank = STATUS_RANK.get(s.get("status"))
    if not rank:
        return
    with Tx() as tx:
        n = tx.q("""UPDATE trial.communications SET delivery_status = :s
                    WHERE provider_message_id IN (:aws, :wamid)
                      AND COALESCE(CASE delivery_status WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2
                                     WHEN 'read' THEN 3 WHEN 'failed' THEN 4 END, 0) < :r::int
                    RETURNING comm_id""",
                 s=s["status"], aws=aws_id, wamid=s.get("id"), r=rank)
    print(f"status {s['status']} for {aws_id}: {len(n)} row(s) updated")
    if s["status"] == "failed":
        print(f"delivery failed: {json.dumps(s.get('errors'))}")


def handle_message(m):
    with _span("whatsapp.ingest", "CONSUMER") as span:
        f = message_fields(m)
        span.set_attribute("trialtalk.msg_type", f["type"])
        with Tx() as tx:
            p = tx.one("SELECT participant_id FROM pii.participants WHERE phone_e164 = :ph", ph=f["phone"])
            pid = p["participant_id"] if p else None
            parsed = {"button_id": f["button_id"], "button_text": f["button_text"]} if f["button_id"] or f["button_text"] else None
            attachments = [{"media_id": f["media_id"], "scan_status": "not_fetched"}] if f["media_id"] else []
            row = tx.one("""
                INSERT INTO trial.communications (participant_id, channel, direction, msg_type, from_addr,
                  body_text, parsed_action, provider_message_id, attachments)
                VALUES (:pid, 'whatsapp', 'inbound', :t, :ph, :body, :pa::jsonb, :wamid, :att::jsonb)
                ON CONFLICT (provider_message_id) DO NOTHING
                RETURNING comm_id""",
                pid=pid, t=f["type"], ph=f["phone"], body=f["text"], pa=parsed, wamid=f["wamid"], att=attachments)
        span.set_attribute("trialtalk.duplicate", not row)
        if not row:
            print(f"duplicate {f['wamid']}, skipped")
            return
        if FORWARD:
            body = {**f, "comm_id": row["comm_id"], "participant_id": pid}
            tp = telemetry.traceparent()   # tt-router continues this trace
            if tp:
                body["traceparent"] = tp
            sqs.send_message(QueueUrl=QUEUE_URL, MessageGroupId=f["phone"], MessageDeduplicationId=f["wamid"],
                             MessageBody=json.dumps(body))
        print(f"logged {f['type']} {row['comm_id']} for {pid or 'unknown sender'}, forwarded={FORWARD}")


@contextmanager
def _span(name, kind, **attrs):
    """telemetry.span() with an OTel span kind (telemetry.span() takes none, and that module is kept
    byte-identical with coordinator-backend's copy)."""
    tracer = telemetry._state.get("tracer") if telemetry.init() else None
    if tracer is None:
        with telemetry.span(name, **attrs) as s:   # no-op without the SDK
            yield s
        return
    from opentelemetry.trace import SpanKind
    with tracer.start_as_current_span(name, kind=SpanKind[kind], attributes=attrs or None,
                                      record_exception=True, set_status_on_exception=True) as s:
        yield s


def handler(event, context):
    try:
        for kind, item, aws_id in parse_sns(event):
            if kind == "status":
                handle_status(item, aws_id)
            else:
                handle_message(item)
        return {"ok": True}
    finally:
        telemetry.flush()
