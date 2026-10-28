"""tt-router: SQS FIFO (one message group per phone) -> this Lambda. Build plan flow A.

1. Layer 0, in code, before any model: STOP/BAJA opt-out, START opt-in, emergency keywords.
2. Dose buttons from reminders (Taken / Skipped / Remind me in 30 min) are recorded here.
3. Everything else goes to the agent on AgentCore Runtime; its reply is sent and logged.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

import schedules
import telemetry
import wa
from db import Tx

agentcore = boto3.client("bedrock-agentcore")
ses = boto3.client("sesv2")
RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
COORDINATOR_EMAIL = os.environ.get("COORDINATOR_EMAIL")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
DEMO_PARTICIPANT_ID = os.environ.get("DEMO_PARTICIPANT_ID", "")   # empty = demo mode off

STOP_WORDS = {"stop", "baja", "unsubscribe", "cancelar"}
START_WORDS = {"start", "alta", "subscribe"}
EMERGENCY = [
    "chest pain", "can't breathe", "cant breathe", "cannot breathe", "trouble breathing",
    "difficulty breathing", "short of breath", "face swelling", "lip swelling", "lips swelling",
    "throat swelling", "tongue swelling", "fainted", "passed out", "unconscious", "seizure",
    "suicide", "kill myself", "hospital",
    "dolor de pecho", "no puedo respirar", "me desmayé", "me desmaye", "convulsión", "suicidio",
]

TEXT = {
    "en": {
        "unknown": "This number is for participants enrolled in a Continuum AI study. Please contact your study site for help.",
        "stopped": "You won't get any more messages from this number. Reply START to turn them back on.",
        "started": "Welcome back. Messages from your study team are on again.",
        "emergency": "If this is an emergency, call 911 (or your local emergency number) now. I'm also alerting your study team.",
        "taken": "Thanks, I've recorded your {label} dose as taken.",
        "snoozed": "OK, I'll remind you again in 30 minutes.",
        "media": "Sorry, I can only read text messages for now. Please type your message.",
        "error": "Sorry, something went wrong on my side. If this is urgent, please contact your study site or emergency services directly.",
    },
    "es": {
        "unknown": "Este número es para participantes de un estudio de Continuum AI. Comuníquese con su centro de estudio.",
        "stopped": "No recibirá más mensajes de este número. Responda ALTA para volver a activarlos.",
        "started": "Bienvenido de nuevo. Los mensajes de su equipo de estudio están activados.",
        "emergency": "Si es una emergencia, llame al 911 ahora. También estoy avisando a su equipo de estudio.",
        "taken": "Gracias, registré su dosis de la {label} como tomada.",
        "snoozed": "De acuerdo, se lo recordaré de nuevo en 30 minutos.",
        "media": "Lo siento, por ahora solo puedo leer mensajes de texto. Por favor, escriba su mensaje.",
        "error": "Lo siento, algo salió mal. Si es urgente, comuníquese con su centro de estudio o con emergencias.",
    },
}


def t(lang, key, **kw):
    return TEXT.get(lang, TEXT["en"])[key].format(**kw)


def reply(m, pid, lang, text, **usage):
    """Send a WhatsApp text and log the outbound row (also when sending fails)."""
    status, msg_id = "sent", None
    try:
        msg_id = wa.send_text(m["phone"], text)
    except ClientError as e:
        status = "failed"
        print(f"send failed: {e.response['Error']}")
    with Tx() as tx:
        tx.q("""INSERT INTO trial.communications (participant_id, channel, direction, msg_type, to_addr,
                  body_text, provider_message_id, delivery_status, session_id, latency_ms, input_tokens, output_tokens)
                VALUES (:pid, 'whatsapp', 'outbound', 'text', :to, :body, :mid, :st, :sid, :lat::int,
                  :in_tok::int, :out_tok::int)""",
             pid=pid, to=m["phone"], body=text, mid=msg_id, st=status,
             sid=usage.get("session_id"), lat=usage.get("latency_ms"),
             in_tok=usage.get("input_tokens"), out_tok=usage.get("output_tokens"))


# ---------- Layer 0 ----------

def normalize(text):
    return re.sub(r"\s+", " ", (text or "").lower().replace("’", "'")).strip()


REPRESS_WINDOW_MIN = 30   # same window as the website SOS: repeats join the open alert instead of a new one


def raise_alert(pid, text, words):
    """Show the emergency in the staff portal's alerts widget. Never raises: a failure here must not stop the
    participant's emergency reply or the coordinator email."""
    try:
        with Tx() as tx:
            open_alert = tx.one(
                "SELECT alert_id FROM trial.emergency_alerts WHERE participant_id = :pid AND status <> 'resolved' "
                "  AND last_pressed_at > now() - make_interval(mins => :mins::int) ORDER BY created_at DESC LIMIT 1",
                pid=pid, mins=REPRESS_WINDOW_MIN)
            if open_alert:
                tx.q("UPDATE trial.emergency_alerts SET press_count = press_count + 1, last_pressed_at = now(), "
                     "message = :msg::text WHERE alert_id = :id::uuid", id=open_alert["alert_id"], msg=text)
            else:
                tx.q("INSERT INTO trial.emergency_alerts (participant_id, severity, source, message, keywords) "
                     "VALUES (:pid, 'life_threatening', 'chat_keyword', :msg::text, :kw::text[])",
                     pid=pid, msg=text, kw="{" + ",".join(f'"{w}"' for w in words) + "}")
    except Exception as e:
        print(f"emergency alert not recorded for {pid}: {e}")


def emergency(m, p, words):
    """Log an urgent SYMPTOM, tell the participant to call emergency services, email the coordinator."""
    with Tx() as tx:
        ev = tx.one("""
            INSERT INTO trial.clinical_events (participant_id, record_type, status, source, source_comm_id,
              verbatim_text, severity_hint, urgent_keywords, flag_for_follow_up, created_by)
            VALUES (:pid, 'SYMPTOM', 'escalated', 'whatsapp', :cid::uuid, :txt, 'urgent', :kw::text[], true, 'router')
            RETURNING event_id""",
            pid=p["participant_id"], cid=m["comm_id"], txt=m["text"],
            kw="{" + ",".join(f'"{w}"' for w in words) + "}")
    if p["status"] != "withdrawn":   # like the website SOS: withdrawn people get the instruction, the team isn't paged
        raise_alert(p["participant_id"], m["text"], words)
    reply(m, p["participant_id"], p["preferred_language"], t(p["preferred_language"], "emergency"))
    if COORDINATOR_EMAIL and SENDER_EMAIL:
        subject = f"[TT {p['participant_id']} SYM-{ev['event_id'][:4]}] URGENT: emergency keywords on WhatsApp"
        body = (f"Participant {p['participant_id']} (site {p['site_id']}) sent a message with emergency keywords: "
                f"{', '.join(words)}.\n\nTheir words: \"{m['text']}\"\n\n"
                f"They were told to call emergency services. Please contact them now.\nEvent: {ev['event_id']}")
        try:
            r = ses.send_email(FromEmailAddress=SENDER_EMAIL, Destination={"ToAddresses": [COORDINATOR_EMAIL]},
                               Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}})
            with Tx() as tx:
                tx.q("""INSERT INTO trial.communications (participant_id, channel, direction, msg_type, from_addr,
                          to_addr, subject, body_text, related_event_id, provider_message_id, delivery_status)
                        VALUES (:pid, 'email', 'outbound', 'report', :fr, :to, :sub, :body, :ev::uuid, :mid, 'sent')""",
                     pid=p["participant_id"], fr=SENDER_EMAIL, to=COORDINATOR_EMAIL, sub=subject, body=body,
                     ev=ev["event_id"], mid=r["MessageId"])
        except ClientError as e:
            print(f"coordinator email failed: {e.response['Error']}")


# ---------- dose buttons ----------

def open_dose(tx, pid, reminder_id):
    return tx.one("""
        SELECT event_id, dose_label, reminder_id FROM trial.current_events
        WHERE participant_id = :pid AND record_type = 'DOSE' AND response = 'no_response'
          AND (:rid::uuid IS NULL OR reminder_id = :rid::uuid)
          AND scheduled_at > now() - interval '24 hours'
        ORDER BY scheduled_at DESC LIMIT 1""", pid=pid, rid=reminder_id)


def record_dose(tx, dose, response, comm_id, why):
    """New version of the DOSE row. Returns False if someone already recorded it (version chain is taken)."""
    try:
        tx.q("""INSERT INTO trial.clinical_events (participant_id, record_type, source, source_comm_id, reminder_id,
                  dose_label, scheduled_at, response, details, supersedes_event_id, change_reason, created_by)
                SELECT participant_id, 'DOSE', 'whatsapp', :cid::uuid, reminder_id, dose_label, scheduled_at,
                  :resp, details, event_id, :why, 'router'
                FROM trial.clinical_events WHERE event_id = :eid::uuid""",
             cid=comm_id, resp=response, why=why, eid=dose["event_id"])
        return True
    except ClientError as e:
        if "clinical_events_supersedes_once" in str(e):
            return False
        raise


def dose_button(m, p):
    """Returns the text to pass to the agent, or None if handled here."""
    action, _, rid = (m.get("button_id") or "").partition(":")
    action = action.upper() or normalize(m["text"]).upper()
    action = {"TAKEN": "TAKEN", "SKIPPED": "SKIPPED", "SNOOZE": "SNOOZE", "REMIND ME IN 30 MIN": "SNOOZE"}.get(action)
    if not action:
        return m["text"]
    pid, lang = p["participant_id"], p["preferred_language"]
    with Tx() as tx:
        dose = open_dose(tx, pid, rid or None)
        if not dose:
            return m["text"]             # nothing open to update; let the agent answer
        label = dose["dose_label"] or "scheduled"
        if action == "TAKEN":
            record_dose(tx, dose, "taken", m["comm_id"], "participant tapped Taken")
        elif action == "SKIPPED":
            record_dose(tx, dose, "skipped", m["comm_id"], "participant tapped Skipped")
        else:
            record_dose(tx, dose, "snoozed", m["comm_id"], "participant asked to be reminded in 30 min")
    if action == "TAKEN":
        reply(m, pid, lang, t(lang, "taken", label=label))
        return None
    if action == "SNOOZE":
        rid = dose["reminder_id"]
        schedules.once_in(f"snooze-{rid}-{schedules.stamp()}", 30, {"reminder_id": rid, "mode": "snooze"})
        reply(m, pid, lang, t(lang, "snoozed"))
        return None
    return f"I skipped my {label} dose."      # agent explains what to do and asks why


# ---------- agent ----------

def ask_agent(p, text, conversation_key=None):
    """conversation_key is set only for demo users (never by the sender): it gives them private agent memory."""
    local_day = datetime.now(ZoneInfo(p["timezone"])).strftime("%Y-%m-%d")
    owner = conversation_key or p["participant_id"]
    session = f"trialtalk-wa-{owner}-{local_day}".ljust(33, "0")   # API needs 33+ chars
    payload = {"participant_id": p["participant_id"], "text": text}
    if conversation_key:
        payload["conversation_key"] = conversation_key
    start = time.time()
    with telemetry.span("agent.call", kind="CLIENT") as span:
        tp = telemetry.traceparent()   # the agent's spans join this trace, under agent.call
        if tp:
            payload["traceparent"] = tp
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=session,
            payload=json.dumps(payload).encode())
        body = json.loads(resp["response"].read())
        tokens = agent_usage(body, span)
    return body.get("reply"), {"session_id": session, "latency_ms": int((time.time() - start) * 1000), **tokens}


# ---------- telemetry (never changes a reply: every failure here is swallowed) ----------

_agent_traces = []   # traces the agent returned; persisted after the replies are sent, not before


def agent_usage(body, span):
    """Token counts from the agent's response (absent on old agents and error replies), recorded on the
    agent.call span. The agent's own spans are queued for persist_agent_traces()."""
    tokens = {}
    try:
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        for key in ("input_tokens", "output_tokens"):
            v = usage.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                tokens[key] = v
                span.set_attribute(f"gen_ai.usage.{key}", v)
        if isinstance(usage.get("model"), str):
            span.set_attribute("gen_ai.request.model", usage["model"])
        trace = body.get("trace")
        if isinstance(trace, dict) and trace.get("trace_id") and trace.get("spans"):
            _agent_traces.append(trace)
    except Exception as e:
        print(f"agent usage not recorded: {type(e).__name__}")
    return tokens


def persist_agent_traces():
    while _agent_traces:
        trace = _agent_traces.pop(0)
        telemetry.persist_spans(trace["spans"], service="trial-agent", trace_id=trace["trace_id"])


def demo_chat(m):
    """Unknown number while demo mode is on: chat with the agent as the shared synthetic participant, with private
    memory per tester (hashed number, never stored raw). No Layer 0 checks or clinical writes, as before the router."""
    key = "demo-" + hashlib.sha256(m["phone"].lstrip("+").encode()).hexdigest()[:12]
    print(f"Demo user {key}")
    demo = {"participant_id": DEMO_PARTICIPANT_ID, "timezone": "America/New_York"}
    try:
        answer, usage = ask_agent(demo, m["text"].strip()[:1000], key)
    except Exception as e:
        print(f"agent call failed for demo user {key}: {e}")
        answer, usage = None, {}
    reply(m, None, "en", answer or t("en", "error"), **usage)


def handler(event, context):
    try:
        for record in event["Records"]:
            m = json.loads(record["body"])
            with telemetry.span("whatsapp.route", kind="CONSUMER", context_carrier=m.get("traceparent"),
                                **{"trialtalk.msg_type": m.get("type")}):
                pid = m.get("participant_id")
                if not pid:
                    if DEMO_PARTICIPANT_ID and m.get("type") == "text" and m.get("text"):
                        demo_chat(m)
                    else:
                        reply(m, None, "en", t("en", "unknown"))
                    continue
                with Tx() as tx:
                    p = tx.one("""SELECT participant_id, preferred_language, timezone, whatsapp_opt_in, site_id, status
                                  FROM pii.participants WHERE participant_id = :pid""", pid=pid)
                lang, text = p["preferred_language"], normalize(m.get("text"))

                # Layer 0: opt-out / opt-in
                if text in STOP_WORDS:
                    with Tx() as tx:
                        tx.q("UPDATE pii.participants SET whatsapp_opt_in = false WHERE participant_id = :pid", pid=pid)
                    reply(m, pid, lang, t(lang, "stopped"))
                    continue
                if text in START_WORDS:
                    with Tx() as tx:
                        tx.q("UPDATE pii.participants SET whatsapp_opt_in = true WHERE participant_id = :pid", pid=pid)
                    reply(m, pid, lang, t(lang, "started"))
                    continue
                if not p["whatsapp_opt_in"]:
                    print(f"{pid} has opted out; not replying")
                    continue

                if not m.get("text"):
                    reply(m, pid, lang, t(lang, "media"))
                    continue

                # Layer 0: emergency keywords. Alert first, then the agent still answers.
                hits = [w for w in EMERGENCY if w in text]
                if hits:
                    emergency(m, p, hits)

                agent_text = dose_button(m, p) if m["type"] in ("button", "button_reply") else m["text"]
                if agent_text is None:
                    continue
                try:
                    answer, usage = ask_agent(p, agent_text)
                except Exception as e:
                    print(f"agent call failed for {pid}: {e}")
                    answer, usage = None, {}
                reply(m, pid, lang, answer or t(lang, "error"), **usage)
        return {"ok": True}
    finally:
        persist_agent_traces()
        telemetry.flush()
