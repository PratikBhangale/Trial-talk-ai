"""WhatsApp Lambda: receives messages from SNS, asks the agent on AgentCore, replies on WhatsApp.
Reminder button taps are handled deterministically (no agent)."""

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import boto3

try:
    import telemetry   # triggers/lambdas/common/telemetry.py, when bundled into this zip; tracing is off without it
except ImportError:
    telemetry = None

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
DEMO_PARTICIPANT_ID = os.environ.get("DEMO_PARTICIPANT_ID", "")   # empty = demo mode off
MEMORY_ID = os.environ.get("MEMORY_ID", "")                       # optional: notes for the agent
DB = dict(
    resourceArn=os.environ["DB_CLUSTER_ARN"],
    secretArn=os.environ["DB_SECRET_ARN"],      # tt_messaging login: can look up participants by phone
    database=os.environ.get("DB_NAME", "trialtalk"),
)

rds = boto3.client("rds-data")
agentcore = boto3.client("bedrock-agentcore")
social = boto3.client("socialmessaging")

UNKNOWN_SENDER_REPLY = (
    "This number is for participants enrolled in a Continuum AI study. "
    "Please contact your study site for help."
)
NOT_TEXT_REPLY = "Sorry, I can only read text messages for now. Please type your message."
ERROR_REPLY = (
    "Sorry, something went wrong on my side. "
    "If this is urgent, please contact your study site or emergency services directly."
)


# ---------- 1. unpack the SNS event ----------

def incoming_messages(event):
    """Yield (origination_id, sender, message) for each incoming WhatsApp message."""
    for record in event.get("Records", []):
        header = json.loads(record["Sns"]["Message"])
        origination_id = header["context"]["MetaPhoneNumberIds"][0]["arn"]  # our WhatsApp number
        entry = json.loads(header["whatsAppWebhookEntry"])
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):  # status updates have none
                yield origination_id, message["from"], message


# ---------- 2. who is this? (Aurora, by phone number) ----------

def _sql(sql: str, **params) -> list[dict]:
    """Run one statement as tt_messaging. Retries while the database wakes up from idle."""
    for attempt in range(3):
        try:
            r = rds.execute_statement(
                **DB, sql=sql, formatRecordsAs="JSON",
                parameters=[{"name": k, "value": {"stringValue": str(v)}} for k, v in params.items()],
            )
            return json.loads(r.get("formattedRecords") or "[]")
        except rds.exceptions.DatabaseResumingException:
            time.sleep(10)  # ~15s to wake
    raise RuntimeError("Database did not wake up in time")


def find_participant(phone: str) -> str | None:
    """WhatsApp gives digits only (15859107690); Aurora stores +15859107690."""
    rows = _sql("""SELECT participant_id FROM pii.participants
                   WHERE phone_e164 = :phone AND status NOT IN ('withdrawn', 'screen_failed')""",
                phone=f"+{phone}")
    return rows[0]["participant_id"] if rows else None


# ---------- 3. reminder button taps (deterministic, no agent) ----------

BUTTON_RESPONSES = {
    "Taken": ("taken", "Thanks! Your {label} is recorded as taken."),
    "Skipped": ("skipped", "Thanks for letting us know. Your {label} is recorded as skipped. "
                           "If you have questions about a missed dose, just ask me here."),
}


def handle_button(pid: str, payload: str) -> str | None:
    """Record a dose response from a reminder button. Payload format: rem:<reminder_id>:<button>."""
    parts = payload.split(":", 2)
    if len(parts) != 3 or parts[0] != "rem" or parts[2] not in BUTTON_RESPONSES:
        return None
    reminder_id, button = parts[1], parts[2]
    response, reply = BUTTON_RESPONSES[button]

    # The reminder must belong to the participant who tapped it
    rows = _sql("SELECT label FROM trial.reminders WHERE reminder_id = :rid::uuid AND participant_id = :pid",
                rid=reminder_id, pid=pid)
    if not rows:
        print(f"Button for a reminder that isn't {pid}'s: {reminder_id}")
        return None
    label = rows[0]["label"].lower()

    # Append-only: supersede the latest unanswered DOSE record for this reminder
    updated = _sql("""
        INSERT INTO trial.clinical_events (participant_id, record_type, status, source, dose_label,
          scheduled_at, response, reminder_id, supersedes_event_id, change_reason, details, created_by)
        SELECT participant_id, 'DOSE', :response, 'whatsapp', dose_label, scheduled_at, :response,
          reminder_id, event_id, 'Participant replied with WhatsApp button',
          jsonb_build_object('button', :button), :created_by
        FROM trial.current_events
        WHERE reminder_id = :rid::uuid AND participant_id = :pid
          AND record_type = 'DOSE' AND response = 'no_response'
        ORDER BY occurred_at DESC LIMIT 1
        RETURNING event_id""",
        response=response, button=button, created_by=f"participant:{pid}", rid=reminder_id, pid=pid)

    # No unanswered reminder found (tapped twice, or a test): record a fresh DOSE event instead
    if not updated:
        _sql("""
            INSERT INTO trial.clinical_events (participant_id, record_type, status, source, dose_label,
              response, reminder_id, details, created_by)
            VALUES (:pid, 'DOSE', :response, 'whatsapp', :label, :response, :rid::uuid,
              jsonb_build_object('button', :button), :created_by)""",
            pid=pid, response=response, label=label, rid=reminder_id, button=button,
            created_by=f"participant:{pid}")

    print(f"Dose response for {pid}: {response}")
    note_for_agent(pid, f"Participant tapped '{button}' for their {label} reminder.")
    return reply.format(label=label)


def note_for_agent(pid: str, text: str) -> None:
    """Leave a note in the agent's memory so it knows what happened outside the conversation."""
    if not MEMORY_ID:
        return
    try:
        agentcore.create_event(
            memoryId=MEMORY_ID,
            actorId=pid,
            sessionId=f"participant-{pid}",
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{"conversational": {"role": "ASSISTANT", "content": {"text": text}}}],
        )
    except Exception as e:
        print(f"Memory note failed: {e}")


# ---------- 4. ask the agent ----------

def ask_agent(pid: str, text: str, conversation_key: str | None = None) -> str:
    session_owner = conversation_key or pid
    payload = {"participant_id": pid, "text": text}
    if conversation_key:
        payload["conversation_key"] = conversation_key
    with _span("agent.call", "CLIENT") as span:
        tp = telemetry.traceparent() if telemetry else None   # the agent's spans join this trace
        if tp:
            payload["traceparent"] = tp
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=f"whatsapp-session-{session_owner}".ljust(33, "0"),  # must be 33+ characters
            payload=json.dumps(payload).encode(),
        )
        body = json.loads(resp["response"].read())
        record_agent_usage(body, span)
    return body.get("reply") or ERROR_REPLY


# ---------- telemetry (never changes a reply: every failure here is swallowed) ----------

_agent_traces = []   # traces the agent returned; persisted after the replies are sent, not before


def record_agent_usage(body, span) -> None:
    """Token counts from the agent's response (absent on old agents and error replies) onto the agent.call span;
    the agent's own spans are queued for persist_agent_traces()."""
    try:
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        for key in ("input_tokens", "output_tokens"):
            v = usage.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                span.set_attribute(f"gen_ai.usage.{key}", v)
        if isinstance(usage.get("model"), str):
            span.set_attribute("gen_ai.request.model", usage["model"])
        trace = body.get("trace")
        if telemetry and isinstance(trace, dict) and trace.get("trace_id") and trace.get("spans"):
            _agent_traces.append(trace)
    except Exception as e:
        print(f"Agent usage not recorded: {type(e).__name__}")


def persist_agent_traces() -> None:
    while _agent_traces:
        trace = _agent_traces.pop(0)
        telemetry.persist_spans(trace["spans"], service="trial-agent", trace_id=trace["trace_id"])


class _NoSpan:
    def set_attribute(self, *a, **kw):
        pass


@contextmanager
def _span(name, kind, **attrs):
    """A telemetry span with an OTel span kind (telemetry.span() takes none, and that module is kept
    byte-identical with coordinator-backend's copy). A no-op when telemetry or its SDK is missing."""
    attrs = {k: v for k, v in attrs.items() if v is not None}
    tracer = telemetry._state.get("tracer") if telemetry and telemetry.init() else None
    if tracer is None:
        yield _NoSpan()
        return
    from opentelemetry.trace import SpanKind
    with tracer.start_as_current_span(name, kind=SpanKind[kind], attributes=attrs or None,
                                      record_exception=True, set_status_on_exception=True) as s:
        yield s


# ---------- 5. reply on WhatsApp ----------

def send_whatsapp(origination_id: str, to: str, text: str) -> None:
    message = {
        "messaging_product": "whatsapp",
        "to": f"+{to}",
        "type": "text",
        "text": {"body": text[:4096]},
    }
    social.send_whatsapp_message(
        originationPhoneNumberId=origination_id,
        message=json.dumps(message).encode(),
        metaApiVersion="v20.0",
    )


# ---------- the Lambda entry point ----------

def handler(event, context):
    try:
        for origination_id, sender, message in incoming_messages(event):
            with _span("whatsapp.legacy", "CONSUMER", **{"trialtalk.msg_type": message.get("type")}):
                pid = find_participant(sender)
                conversation_key = None

                if not pid:
                    if not DEMO_PARTICIPANT_ID:
                        print(f"Message from unknown number ending {sender[-4:]}")
                        send_whatsapp(origination_id, sender, UNKNOWN_SENDER_REPLY)
                        continue
                    # Demo mode: shared synthetic profile, private memory per tester (hashed number, never stored raw)
                    pid = DEMO_PARTICIPANT_ID
                    conversation_key = "demo-" + hashlib.sha256(sender.encode()).hexdigest()[:12]
                    print(f"Demo user {conversation_key}")

                # Reminder button taps: recorded directly, no agent.
                # Template buttons arrive as "button"; interactive-message buttons arrive as "interactive".
                button_payload = None
                if message.get("type") == "button":
                    button_payload = message.get("button", {}).get("payload", "")
                elif message.get("type") == "interactive" and message.get("interactive", {}).get("type") == "button_reply":
                    button_payload = message["interactive"]["button_reply"].get("id", "")

                if button_payload is not None:
                    reply = handle_button(pid, button_payload)
                    send_whatsapp(origination_id, sender, reply or "Thanks, your response was received.")
                    continue

                if message.get("type") != "text":
                    send_whatsapp(origination_id, sender, NOT_TEXT_REPLY)
                    continue

                text = message["text"]["body"].strip()[:1000]
                try:
                    reply = ask_agent(pid, text, conversation_key)
                except Exception as e:
                    print(f"Agent call failed for {pid}: {e}")
                    reply = ERROR_REPLY

                send_whatsapp(origination_id, sender, reply)

        return {"ok": True}
    finally:
        persist_agent_traces()
        if telemetry:
            telemetry.flush()