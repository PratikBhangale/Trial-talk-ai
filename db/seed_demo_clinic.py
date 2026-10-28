"""Fill the demo clinic: four synthetic trials, ~15 participants each, three weeks of history.

KOA-301 is the LIVE study and is never read, changed or deleted here. This script only touches participants of
DEMO_STUDIES (HTN-205, DM2-410, MIG-118, AST-302), whose reference rows come from 07_studies.sql (run that first).

Each participant gets an ARCHETYPE that drives their history over the last HISTORY_DAYS days:
  adherent       engaged on WhatsApp, takes ~93% of doses
  low_adherence  engaged (answers, asks questions) but takes ~55% of doses
  not_engaging   answered for the first week, then went silent: NO inbound WhatsApp in the last 14 days
  flagged        engaged, plus an emergency-keyword symptom (urgent SYMPTOM, coordinator email, emergency alert)
The mix varies a little per study (ARCHETYPE_MIX) so the trials look different.

Rows written, all INSERT only (clinical_events and communications are append-only):
  pii.participants        status 'enrolled', fake +1555 phones, the two existing sites
  trial.communications    dose reminder templates, button replies, participant texts, agent replies (with
                          input/output tokens, model_id and session_id 'synthetic-...'), router replies, emails
  trial.clinical_events   DOSE rows as the reminder Lambda writes them (no_response), then a superseding version
                          when the participant tapped Taken / Skipped; SYMPTOM rows (mild ones logged by the agent,
                          urgent ones escalated by the router with flag_for_follow_up)
  trial.emergency_alerts  the flagged participants' alerts: most resolved, a few still open

Usage (as the cluster MASTER user, like run_sql.py):
  python3 seed_demo_clinic.py --dry-run                     # row counts per study and table; no AWS calls
  python3 seed_demo_clinic.py --cluster-arn <arn> --secret-arn <master secret arn> [--profile p]
  python3 seed_demo_clinic.py --cluster-arn <arn> --secret-arn <master secret arn> --teardown

Idempotent: participants are inserted ON CONFLICT DO NOTHING, and a study that already has participants is skipped
entirely (no history is generated twice). Each study is seeded in its own transaction.

--teardown deletes the seeder's own rows only. Run it OFF-HOURS: it briefly locks four tables live traffic writes to.
A demo participant is one in DEMO_STUDIES that also carries the seeder's FINGERPRINT (+1555 phone and a
P-HTN-01 style id). Staff can enroll a real person into a demo study (study_id is free text), so before anything
is deleted the script lists demo-study participants WITHOUT the fingerprint and aborts if there are any; the DELETE
itself also requires the fingerprint. The append-only triggers block DELETE even for the owner, so inside ONE
transaction it runs, as the master user:
  SET LOCAL lock_timeout = '5s';     -- never queue behind (or block) live writes for long
  <the fingerprint guard: abort, listing the ids, if a demo study holds anyone else>
  ALTER TABLE trial.clinical_events  DISABLE TRIGGER clinical_events_append_only;
  ALTER TABLE trial.communications   DISABLE TRIGGER communications_no_delete;
  ALTER TABLE trial.reminders        DISABLE TRIGGER reminders_no_delete;
  ALTER TABLE trial.emergency_alerts DISABLE TRIGGER emergency_alerts_no_delete;
  <one DELETE statement, child tables first, scoped to study_id IN DEMO_STUDIES AND the fingerprint>
  ALTER TABLE ... ENABLE TRIGGER ... (same four)
Because it is one transaction, a failure rolls the DISABLEs back too: the triggers are never left off, and the
ALTERs' table locks mean no other session can delete anything while they are off. communications and
clinical_events reference each other (source_comm_id / related_event_id), so the deletes are one statement with
data-modifying CTEs: foreign keys are checked at the end of the statement, after every child row is gone.
"""
import argparse
import json
import random
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

LIVE_STUDY = "KOA-301"                                   # never touched
DEMO_STUDIES = ("HTN-205", "DM2-410", "MIG-118", "AST-302")
assert LIVE_STUDY not in DEMO_STUDIES

SITES = ("RIVERSIDE", "LAKEVIEW")                        # db/03_seed.sql
SITE_INFO = {
    "RIVERSIDE": {"city": "Springfield", "zips": ["62701", "62702", "62703", "62704", "62707"],
                  "coordinator": "coordinator@riverside-clinic.org"},
    "LAKEVIEW": {"city": "Chicago", "zips": ["60611", "60613", "60614", "60618", "60640"],
                 "coordinator": "coordinator@lakeview-health.org"},
}
TIMEZONE = "America/Chicago"
TZ = ZoneInfo(TIMEZONE)
MODEL_ID = "global.anthropic.claude-sonnet-4-6"          # agent/trial_agent/root_agent.yaml
ALERT_SENDER = "alerts@trialtalk.example.org"
HISTORY_DAYS = 21
NOT_ENGAGING_DAYS = 14                                    # the "% not engaging" KPI window
ARCHETYPES = ("adherent", "low_adherence", "not_engaging", "flagged")

# Participants per archetype, per study (roughly 50/20/15/15, varied so the trials look different).
ARCHETYPE_MIX = {
    "HTN-205": {"adherent": 8, "low_adherence": 3, "not_engaging": 2, "flagged": 2},   # 15
    "DM2-410": {"adherent": 7, "low_adherence": 4, "not_engaging": 3, "flagged": 2},   # 16
    "MIG-118": {"adherent": 7, "low_adherence": 2, "not_engaging": 2, "flagged": 3},   # 14
    "AST-302": {"adherent": 8, "low_adherence": 3, "not_engaging": 2, "flagged": 2},   # 15
}
OPEN_ALERTS = {"HTN-205": 1, "DM2-410": 1, "MIG-118": 2, "AST-302": 1}   # the rest are resolved
ACKNOWLEDGED_ALERTS = {"AST-302": 1}                                     # acknowledged, not yet resolved
# (p_taken, p_skipped); the rest of the doses get no reply. not_engaging applies to their first week only.
ADHERENCE = {"adherent": (0.93, 0.03), "low_adherence": (0.55, 0.15), "not_engaging": (0.62, 0.06),
             "flagged": (0.82, 0.06)}

# Per-study protocol details: id prefix, phone block, dose slots (label, local hour, minute), age range.
STUDY_PROFILE = {
    "HTN-205": {"code": "HTN", "phone_block": "205", "doses": [("morning", 8, 0)], "ages": (45, 76)},
    "DM2-410": {"code": "DM2", "phone_block": "410", "doses": [("morning", 7, 30)], "ages": (40, 72)},
    "MIG-118": {"code": "MIG", "phone_block": "118", "doses": [("evening", 21, 0)], "ages": (22, 56)},
    "AST-302": {"code": "AST", "phone_block": "302", "doses": [("morning", 8, 0), ("evening", 20, 0)],
                "ages": (18, 66)},
}

# Allowed values (db/01_schema.sql and 06_alerts_web.sql CHECKs), used by the tests.
CHECKS = {
    "record_type": {"PRESCREEN", "CONSENT_Q", "VISIT", "VISIT_CHANGE", "SYMPTOM", "AE", "DOSE", "SIGNATURE"},
    "event_source": {"whatsapp", "email", "scheduler", "portal"},
    "pain_score_band": {"0-3", "4-6", "7-10", None},
    "severity_hint": {"none", "mild", "moderate", "severe", "urgent", None},
    "response": {"confirmed", "taken", "skipped", "snoozed", "no_response", None},
    "channel": {"whatsapp", "email", "web"},
    "direction": {"inbound", "outbound"},
    "participant_status": {"prospect", "screening", "enrolled", "withdrawn", "screen_failed"},
    "alert_severity": {"life_threatening", "urgent", "concern"},
    "alert_source": {"sos_button", "chat_keyword"},
    "alert_status": {"open", "acknowledged", "resolved"},
}

# Columns written per table. Every row carries exactly these keys, so NOT NULL columns with defaults
# (details, attachments, flag_for_follow_up, press_count) are always given a value.
COLUMNS = {
    "pii.participants": [
        "participant_id", "full_name", "email", "phone_e164", "date_of_birth", "address_line1", "city", "state",
        "postal_code", "country", "travel_radius_miles", "preferred_language", "timezone", "study_id", "site_id",
        "status", "whatsapp_opt_in", "opt_in_at", "created_at", "updated_at"],
    "trial.communications": [
        "comm_id", "participant_id", "channel", "direction", "msg_type", "template_name", "from_addr", "to_addr",
        "subject", "body_text", "parsed_action", "related_event_id", "provider_message_id", "delivery_status",
        "attachments", "session_id", "model_id", "input_tokens", "output_tokens", "latency_ms", "tool_calls",
        "guardrail_action", "created_at"],
    "trial.clinical_events": [
        "event_id", "participant_id", "record_type", "status", "occurred_at", "source", "source_comm_id",
        "verbatim_text", "summary", "pain_score_band", "body_location", "onset_date", "characteristics",
        "severity_hint", "urgent_keywords", "reason_code", "flag_for_follow_up", "dose_label", "scheduled_at",
        "response", "details", "supersedes_event_id", "change_reason", "created_by", "created_at"],
    "trial.emergency_alerts": [
        "alert_id", "participant_id", "severity", "source", "message", "keywords", "status", "press_count",
        "last_pressed_at", "acknowledged_by", "acknowledged_at", "resolved_by", "resolved_at", "resolution_note",
        "created_at"],
}
# Insert order (foreign keys): participants; messages that reference no event; events (they point at those
# messages); superseding DOSE versions (they point at the first version); messages that reference an event;
# alerts. Each step is a (table, plan key) pair.
STEPS = [
    ("pii.participants", "participants"),
    ("trial.communications", "comms_pre"),
    ("trial.clinical_events", "events"),
    ("trial.clinical_events", "events_v2"),
    ("trial.communications", "comms_post"),
    ("trial.emergency_alerts", "alerts"),
]

# ------------------------------------------------------------------ names and text banks

FIRST_EN = ["James", "Linda", "Robert", "Patricia", "Michael", "Jennifer", "William", "Barbara", "David", "Susan",
            "Richard", "Karen", "Joseph", "Nancy", "Thomas", "Lisa", "Charles", "Betty", "Daniel", "Sandra",
            "Anthony", "Ashley", "Mark", "Donna", "Kevin", "Michelle", "Brian", "Carol", "Darnell", "Keisha",
            "Wei", "Mei", "Raj", "Priya", "Tomasz", "Agnieszka", "Kwame", "Ama", "Olivia", "Ethan", "Grace",
            "Marcus", "Hannah", "Terrence", "Denise", "Gregory"]
LAST_EN = ["Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson", "Taylor", "Thomas",
           "Moore", "Martin", "Jackson", "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson", "Walker",
           "Young", "Allen", "King", "Wright", "Scott", "Nguyen", "Patel", "Kowalski", "Mensah", "Washington",
           "Brooks", "Sullivan", "O'Brien", "Murphy", "Chen", "Okafor", "Schmidt", "Bennett"]
FIRST_ES = ["María", "José", "Carmen", "Luis", "Rosa", "Jorge", "Ana", "Carlos", "Guadalupe", "Miguel", "Elena",
            "Francisco", "Lucía", "Javier", "Sofía", "Ricardo", "Alejandra", "Raúl"]
LAST_ES = ["García", "Hernández", "Martínez", "López", "González", "Rodríguez", "Pérez", "Sánchez", "Ramírez",
           "Torres", "Flores", "Rivera", "Morales", "Castillo", "Vargas", "Reyes"]
STREETS = ["Oak Ave", "Maple St", "Cedar Ln", "Washington Blvd", "Lincoln Ave", "Park Pl", "Elm St", "Grand Ave",
           "Lake St", "Prairie Rd", "Walnut St", "Clark St", "Ridge Ave", "Monroe St", "Willow Dr"]

# Participant questions with the agent's answers, per study: (question_en, question_es, answer_en, answer_es).
QUESTIONS_COMMON = [
    ("When is my next clinic visit?", "¿Cuándo es mi próxima visita a la clínica?",
     "Your next visit is in your schedule on the participant website, and you'll get a reminder 24 hours before. "
     "If you need to change it, reply here or use the Reschedule button.",
     "Su próxima visita aparece en su calendario en el sitio web de participantes y recibirá un recordatorio 24 horas "
     "antes. Si necesita cambiarla, responda aquí o use el botón Reprogramar."),
    ("I'm travelling next week, should I bring my study medication?",
     "Voy a viajar la próxima semana, ¿llevo mi medicamento del estudio?",
     "Yes, please bring enough for the whole trip in its original packaging, and keep taking it at your usual time "
     "(adjusted to local time). Let your study team know your travel dates.",
     "Sí, lleve suficiente para todo el viaje en su empaque original y siga tomándolo a su hora habitual (ajustada a la "
     "hora local). Avise a su equipo de estudio las fechas del viaje."),
    ("Will I get paid for the visit last week?", "¿Me van a pagar la visita de la semana pasada?",
     "Visit stipends are processed by the site within about two weeks of each visit. I've noted your question for "
     "the coordinator so they can confirm the status.",
     "Los pagos por visita se procesan en unas dos semanas después de cada visita. Anoté su pregunta para el "
     "coordinador para que le confirme el estado."),
]
QUESTIONS = {
    "HTN-205": [
        ("Should I take my blood pressure before or after the pill?",
         "¿Me tomo la presión antes o después de la pastilla?",
         "Take your home blood pressure reading before your morning dose, after sitting quietly for 5 minutes, and "
         "log both readings in the diary.",
         "Tómese la presión antes de la dosis de la mañana, después de estar sentado 5 minutos tranquilo, y anote las "
         "dos lecturas en el diario."),
        ("My reading this morning was 138/86, is that ok?", "Hoy en la mañana tuve 138/86, ¿está bien?",
         "Thanks for logging it. 138/86 is within the range your study team expects right now. Keep taking your dose "
         "as usual; if you see readings above 180/120 or feel unwell, contact the site right away.",
         "Gracias por anotarla. 138/86 está dentro del rango que espera su equipo. Siga con su dosis como siempre; si "
         "ve lecturas por encima de 180/120 o se siente mal, llame al centro de inmediato."),
        ("Can I keep drinking coffee during the study?", "¿Puedo seguir tomando café durante el estudio?",
         "Moderate coffee is fine, but avoid caffeine in the 30 minutes before you measure your blood pressure.",
         "El café con moderación está bien, pero evite la cafeína 30 minutos antes de medirse la presión."),
    ],
    "DM2-410": [
        ("Do I take the tablet with breakfast?", "¿Me tomo la pastilla con el desayuno?",
         "Yes, take it with breakfast together with your metformin, at about the same time each day.",
         "Sí, tómela con el desayuno junto con la metformina, más o menos a la misma hora cada día."),
        ("My sugar was 182 this morning before eating", "Mi azúcar salió en 182 en la mañana en ayunas",
         "Thanks for logging it. A single fasting reading of 182 isn't an emergency, but please keep logging daily; "
         "your study team reviews the trend. If you get readings above 300 or under 70, contact the site.",
         "Gracias por anotarlo. Una lectura de 182 en ayunas no es una emergencia, pero siga anotando a diario; su "
         "equipo revisa la tendencia. Si tiene lecturas arriba de 300 o abajo de 70, llame al centro."),
        ("Can I still take my vitamin D?", "¿Puedo seguir tomando vitamina D?",
         "Vitamin D is on the allowed list for this study. Please mention any new supplement at your next visit.",
         "La vitamina D está permitida en este estudio. Mencione cualquier suplemento nuevo en su próxima visita."),
    ],
    "MIG-118": [
        ("Can I still use my rescue medication for a migraine?", "¿Puedo usar mi medicina de rescate para la migraña?",
         "Yes, your usual rescue medication is allowed. Please log each time you take it in the headache diary.",
         "Sí, su medicina de rescate habitual está permitida. Anote cada vez que la tome en el diario de cefaleas."),
        ("Does the study pill have to be at night?", "¿La pastilla del estudio tiene que ser en la noche?",
         "The protocol asks for an evening dose, around the same time each night. Taking it a little earlier or later "
         "is fine.",
         "El protocolo pide una dosis por la noche, más o menos a la misma hora. Un poco antes o después está bien."),
        ("I had 3 migraine days this week, should I log them all?",
         "Tuve 3 días de migraña esta semana, ¿los anoto todos?",
         "Yes, please log every migraine day, even short ones. That count is the main thing the study measures.",
         "Sí, anote cada día de migraña, aunque sea corto. Ese conteo es lo principal que mide el estudio."),
    ],
    "AST-302": [
        ("Should I rinse my mouth after the inhaler?", "¿Me enjuago la boca después del inhalador?",
         "Yes, rinse with water and spit after each dose. It lowers the chance of a sore throat or thrush.",
         "Sí, enjuáguese con agua y escupa después de cada dosis. Reduce la probabilidad de dolor de garganta."),
        ("Can I keep using my blue rescue inhaler?", "¿Puedo seguir usando mi inhalador azul de rescate?",
         "Yes, keep your rescue inhaler with you and use it as needed. Please log each use in the diary.",
         "Sí, lleve su inhalador de rescate y úselo cuando lo necesite. Anote cada uso en el diario."),
        ("My peak flow was a bit lower today", "Mi flujo máximo salió un poco más bajo hoy",
         "Thanks for logging it. One lower reading is common; keep measuring morning and evening. If it drops below "
         "your yellow zone or you get short of breath, contact the site.",
         "Gracias por anotarlo. Una lectura baja es común; siga midiendo mañana y noche. Si baja de su zona amarilla o "
         "le falta el aire, llame al centro."),
    ],
}

# Mild symptoms the agent logs (status 'logged', not flagged).
SYMPTOMS = {
    "HTN-205": [
        dict(en="Felt a bit dizzy when I stood up this morning", es="Me sentí un poco mareado al pararme esta mañana",
             summary="Brief dizziness on standing", location=None, band=None, sev="mild",
             chars=["on_standing", "brief"]),
        dict(en="My ankles look a little swollen tonight", es="Los tobillos se me ven un poco hinchados hoy",
             summary="Mild ankle swelling in the evening", location="ankles", band="0-3", sev="mild",
             chars=["swelling", "evening"]),
        dict(en="Dull headache since lunch", es="Dolor de cabeza leve desde el almuerzo",
             summary="Mild headache", location="head", band="0-3", sev="mild", chars=["dull"]),
    ],
    "DM2-410": [
        dict(en="Some nausea after breakfast today", es="Tuve náuseas después del desayuno hoy",
             summary="Mild nausea after the morning dose", location="stomach", band="0-3", sev="mild",
             chars=["after_meal"]),
        dict(en="Stomach upset and loose stools since yesterday", es="Malestar de estómago y diarrea desde ayer",
             summary="GI upset with loose stools", location="stomach", band="4-6", sev="moderate",
             chars=["diarrhea", "cramping"]),
        dict(en="Felt shaky before lunch, sugar was 74", es="Me sentí tembloroso antes de comer, el azúcar en 74",
             summary="Shakiness with low-normal glucose (74)", location=None, band=None, sev="mild",
             chars=["shaky", "low_glucose"]),
    ],
    "MIG-118": [
        dict(en="Migraine started around 3pm, took my rescue pill", es="Empezó la migraña a las 3, tomé mi pastilla de rescate",
             summary="Migraine day, rescue medication taken", location="head", band="4-6", sev="moderate",
             chars=["throbbing", "rescue_med_taken"]),
        dict(en="Feeling tired and a little foggy this week", es="Me siento cansada y un poco confundida esta semana",
             summary="Fatigue and mild brain fog", location=None, band=None, sev="mild", chars=["fatigue"]),
        dict(en="Tingling in my fingers on and off", es="Hormigueo en los dedos de vez en cuando",
             summary="Intermittent finger tingling", location="hands", band="0-3", sev="mild",
             chars=["tingling", "intermittent"]),
    ],
    "AST-302": [
        dict(en="Throat is a bit sore and hoarse", es="Tengo la garganta un poco irritada y ronca",
             summary="Sore, hoarse throat", location="throat", band="0-3", sev="mild", chars=["hoarse"]),
        dict(en="Used my rescue inhaler twice last night, a bit wheezy", es="Usé el inhalador de rescate dos veces anoche",
             summary="Night-time wheeze, rescue inhaler used twice", location="chest", band="4-6", sev="moderate",
             chars=["wheeze", "night", "rescue_inhaler"]),
        dict(en="Coughing more than usual this morning", es="Estoy tosiendo más de lo normal esta mañana",
             summary="Increased morning cough", location="chest", band="0-3", sev="mild", chars=["cough"]),
    ],
}
SYMPTOM_REPLY = (
    "Thanks for telling me. I've logged this for your study team. If it gets worse, or you notice anything new, "
    "message me or call the site. If it's an emergency, call 911.",
    "Gracias por avisarme. Lo registré para su equipo de estudio. Si empeora o nota algo nuevo, escríbame o llame "
    "al centro. Si es una emergencia, llame al 911.")

# Urgent messages per study: (en, es). Each contains one of the router's emergency keywords.
URGENT = {
    "HTN-205": [("I have chest pain and my BP reading is 192/118", "Tengo dolor de pecho y la presión en 192/118"),
                ("I fainted in the kitchen a few minutes ago", "Me desmayé en la cocina hace unos minutos")],
    "DM2-410": [("I passed out and my sugar was 48", "Me desmaye y el azúcar estaba en 48"),
                ("Chest pain and sweating after my walk", "Dolor de pecho y sudor después de caminar")],
    "MIG-118": [("Worst headache of my life and I fainted", "El peor dolor de cabeza de mi vida y me desmayé"),
                ("My lips swelling after the pill and trouble breathing", "Se me hinchan los labios, no puedo respirar")],
    "AST-302": [("I can't breathe, the rescue inhaler isn't working", "No puedo respirar, el inhalador no funciona"),
                ("Really short of breath and lips look blue", "Me falta el aire, no puedo respirar bien")],
}
# triggers/lambdas/router/handler.py EMERGENCY: the keywords recorded on the urgent SYMPTOM and the alert.
EMERGENCY_KEYWORDS = [
    "chest pain", "can't breathe", "cant breathe", "cannot breathe", "trouble breathing",
    "difficulty breathing", "short of breath", "face swelling", "lip swelling", "lips swelling",
    "throat swelling", "tongue swelling", "fainted", "passed out", "unconscious", "seizure",
    "suicide", "kill myself", "hospital",
    "dolor de pecho", "no puedo respirar", "me desmayé", "me desmaye", "convulsión", "suicidio",
]


def emergency_keywords(text):
    low = text.lower()
    return [k for k in EMERGENCY_KEYWORDS if k in low]


EMERGENCY_REPLY = (
    "If this is an emergency, call 911 (or your local emergency number) now. I'm also alerting your study team.",
    "Si es una emergencia, llame al 911 ahora. También estoy avisando a su equipo de estudio.")
SOS_MESSAGES = ["Feeling very dizzy and my heart is racing", "Pressed SOS, please call me", None]
RESOLUTION_NOTES = [
    "Called participant; went to the ER, evaluated and discharged same day. AE report filed.",
    "Spoke with participant within 15 minutes; symptoms resolved, PI reviewed. Follow-up visit booked.",
    "Participant seen at urgent care; no hospital admission. PI assessed as possibly related, AE recorded.",
    "Reached participant by phone; false alarm (pressed SOS by mistake), feeling fine.",
]

SKIP_REASONS = [  # (participant reply en, es, reason_code, agent reply en, es)
    ("Forgot, I was out all day", "Se me olvidó, estuve fuera todo el día", "forgot",
     "No problem. Don't double up: just take the next dose at the usual time. Want me to move your reminder later "
     "on busy days?",
     "No hay problema. No tome doble: tome la siguiente dosis a la hora de siempre. ¿Quiere que le mueva el "
     "recordatorio en días ocupados?"),
    ("I felt sick to my stomach so I skipped it", "Me sentía mal del estómago y no la tomé", "side_effects",
     "Thanks for letting me know. I've noted it for your study team. If the nausea continues tomorrow, tell me and "
     "the coordinator will call you.",
     "Gracias por avisarme. Lo anoté para su equipo. Si las náuseas siguen mañana, dígame y el coordinador le "
     "llamará."),
    ("I ran out, need a refill", "Se me acabó, necesito más", "ran_out",
     "Thanks. I've flagged that you need more study medication; the site will contact you to arrange pickup.",
     "Gracias. Avisé que necesita más medicamento del estudio; el centro le contactará para recogerlo."),
    ("Was working a double shift", "Estaba trabajando doble turno", "work",
     "Understood. Take your next dose at the usual time. If your shifts change often, we can adjust the reminder.",
     "Entendido. Tome la siguiente dosis a la hora de siempre. Si sus turnos cambian seguido, podemos ajustar el "
     "recordatorio."),
]
TAKEN_REPLY = ("Thanks, I've recorded your {label} dose as taken.", "Gracias, registré su dosis de la {label} como tomada.")
LABEL_ES = {"morning": "mañana", "evening": "noche"}


# ------------------------------------------------------------------ plan (pure, no I/O)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _uuid(rng):
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _daytime(rng, day, now, lo=7, hi=22):
    """A random time on local date `day`, weighted towards morning and evening, never after `now`."""
    hours = list(range(lo, hi))
    weights = [3 if h in (7, 8, 9, 18, 19, 20) else 2 if 10 <= h <= 17 else 1 for h in hours]
    h = rng.choices(hours, weights)[0]
    dt = datetime.combine(day, dtime(h, rng.randint(0, 59), rng.randint(0, 59)), TZ).astimezone(timezone.utc)
    return min(dt, now - timedelta(minutes=rng.randint(5, 50)))


def _empty(table):
    return dict.fromkeys(COLUMNS[table])


class _Builder:
    """Collects the rows of one study, in insert steps."""

    def __init__(self, study, now, rng):
        self.study, self.now, self.rng = study, now, rng
        self.rows = {key: [] for _, key in STEPS}
        self.wamid = 0

    def _mid(self, prefix="wamid.SYNTH"):
        self.wamid += 1
        return f"{prefix}-{self.study}-{self.rng.getrandbits(48):012x}-{self.wamid:05d}"

    def comm(self, step, p, at, direction, msg_type, body, channel="whatsapp", **extra):
        row = _empty("trial.communications")
        row.update(comm_id=_uuid(self.rng), participant_id=p["participant_id"], channel=channel, direction=direction,
                   msg_type=msg_type, body_text=body, created_at=iso(at), attachments=[],
                   provider_message_id=self._mid() if channel == "whatsapp" else self._mid("ses.SYNTH"))
        if channel == "whatsapp":
            if direction == "inbound":
                row["from_addr"] = p["phone_e164"]
            else:
                row["to_addr"] = p["phone_e164"]
                row["delivery_status"] = "read" if p["_engaged_at"](at) else "delivered"
        row.update(extra)
        self.rows[step].append(row)
        return row

    def agent_reply(self, step, p, at, body, related_event_id=None):
        r = self.rng
        lat = r.randint(1800, 7200)
        return self.comm(step, p, at + timedelta(milliseconds=lat), "outbound", "text", body,
                         related_event_id=related_event_id,
                         session_id=f"synthetic-{p['participant_id']}-{at.astimezone(TZ):%Y%m%d}",
                         model_id=MODEL_ID, input_tokens=r.randint(1100, 2900), output_tokens=r.randint(60, 290),
                         latency_ms=lat, tool_calls=r.choice([0, 1, 1, 2, 3]), guardrail_action="NONE")

    def event(self, step, p, record_type, at, created_by, source, **extra):
        row = _empty("trial.clinical_events")
        row.update(event_id=_uuid(self.rng), participant_id=p["participant_id"], record_type=record_type,
                   occurred_at=iso(at), created_at=iso(at + timedelta(seconds=self.rng.randint(1, 20))),
                   source=source, created_by=created_by, flag_for_follow_up=False, details={})
        row.update(extra)
        self.rows[step].append(row)
        return row


def _participants(study, now, rng):
    prof, mix = STUDY_PROFILE[study], ARCHETYPE_MIX[study]
    archetypes = [a for a in ARCHETYPES for _ in range(mix[a])]
    rng.shuffle(archetypes)
    used, out = set(), []
    for i, arche in enumerate(archetypes, 1):
        lang = "es" if rng.random() < 0.25 else "en"
        while True:
            first, last = ((rng.choice(FIRST_ES), rng.choice(LAST_ES)) if lang == "es"
                           else (rng.choice(FIRST_EN), rng.choice(LAST_EN)))
            if (first, last) not in used:
                used.add((first, last))
                break
        site = SITES[0] if i % 5 in (1, 2, 4) else SITES[1]          # ~60/40 RIVERSIDE/LAKEVIEW
        info = SITE_INFO[site]
        lo, hi = prof["ages"]
        dob = now.date() - timedelta(days=rng.randint(lo * 365, hi * 365))
        enrolled = now - timedelta(days=rng.randint(HISTORY_DAYS + 2, HISTORY_DAYS + 30), hours=rng.randint(0, 8))
        email_local = (first + "." + last[0]).lower()
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("'", "")):
            email_local = email_local.replace(a, b)
        out.append({
            "participant_id": f"P-{prof['code']}-{i:02d}", "full_name": f"{first} {last}",
            "email": f"{email_local}{i}@example.com", "phone_e164": f"+1555{prof['phone_block']}{i:04d}",
            "date_of_birth": dob.isoformat(), "address_line1": f"{rng.randint(10, 4999)} {rng.choice(STREETS)}",
            "city": info["city"], "state": "IL", "postal_code": rng.choice(info["zips"]), "country": "US",
            "travel_radius_miles": rng.choice([10, 15, 20, 25, 30]), "preferred_language": lang,
            "timezone": TIMEZONE, "study_id": study, "site_id": site, "status": "enrolled", "whatsapp_opt_in": True,
            "opt_in_at": iso(enrolled + timedelta(hours=1)), "created_at": iso(enrolled), "updated_at": iso(enrolled),
            "_archetype": arche,
        })
    return out


def plan_study(study, now, seed=0):
    """(rows by insert step, {participant_id: archetype}) for one demo study.
    Pure: the same (study, now, seed) gives the same rows."""
    if study not in DEMO_STUDIES:
        raise ValueError(f"{study} is not a demo study")
    rng = random.Random(f"{seed}:{study}")
    b = _Builder(study, now, rng)
    prof = STUDY_PROFILE[study]
    silent_from = now - timedelta(days=NOT_ENGAGING_DAYS)
    start = now - timedelta(days=HISTORY_DAYS)
    days = [(start + timedelta(days=d)).astimezone(TZ).date() for d in range(HISTORY_DAYS + 1)]
    flagged_episodes = []                                       # (participant, urgent message, tiebreak)
    archetype_of = {}

    for p in _participants(study, now, rng):
        arche = p.pop("_archetype")
        archetype_of[p["participant_id"]] = arche
        b.rows["participants"].append(p)
        p = dict(p, _archetype=arche)
        # not_engaging people only talk before the silent window (with a day's margin)
        p["_engaged_at"] = (lambda at, a=arche: a != "not_engaging" or at < silent_from - timedelta(days=1))
        lang = p["preferred_language"]
        L = 0 if lang == "en" else 1
        p_taken, p_skip = ADHERENCE[arche]
        p_taken = min(0.99, max(0.05, p_taken + rng.uniform(-0.04, 0.04)))
        last_inbound = None

        # --- doses
        for day in days:
            for label, hh, mm in prof["doses"]:
                sched = datetime.combine(day, dtime(hh, mm), TZ).astimezone(timezone.utc)
                if sched < start or sched > now - timedelta(minutes=10):
                    continue
                reminder = b.comm("comms_pre", p, sched, "outbound", "template", None,
                                  template_name="dose_reminder_v2", parsed_action=None)
                e1 = b.event("events", p, "DOSE", sched, "reminder-lambda", "scheduler",
                             source_comm_id=reminder["comm_id"], dose_label=label, scheduled_at=iso(sched),
                             response="no_response",
                             details={"reminder_sent_at": iso(sched), "snooze": False, "synthetic": True})
                talks = p["_engaged_at"](sched)
                roll = rng.random()
                outcome = ("taken" if roll < p_taken else "skipped" if roll < p_taken + p_skip else "no_response")
                if not talks or outcome == "no_response":
                    continue
                reply_at = sched + timedelta(minutes=min(240, int(rng.lognormvariate(2.6, 0.9))) + 1,
                                             seconds=rng.randint(0, 59))
                if reply_at > now - timedelta(minutes=2):
                    continue
                button = "Taken" if outcome == "taken" else "Skipped"
                inbound = b.comm("comms_pre", p, reply_at, "inbound", "button_reply", button,
                                 parsed_action={"button_id": button.upper()})
                last_inbound = reply_at
                v2 = dict(e1, event_id=_uuid(rng), source="whatsapp", source_comm_id=inbound["comm_id"],
                          response=outcome, supersedes_event_id=e1["event_id"], created_by="router",
                          change_reason=f"participant tapped {button}", occurred_at=iso(reply_at),
                          created_at=iso(reply_at + timedelta(seconds=1)))
                b.rows["events_v2"].append(v2)
                if outcome == "taken":
                    lbl = label if L == 0 else LABEL_ES[label]
                    b.comm("comms_pre", p, reply_at + timedelta(seconds=2), "outbound", "text",
                           TAKEN_REPLY[L].format(label=lbl))
                else:
                    why = rng.choice(SKIP_REASONS)
                    v2["reason_code"] = why[2]
                    b.agent_reply("comms_pre", p, reply_at, "Thanks for telling me. Can I ask why you skipped it?"
                                  if L == 0 else "Gracias por avisarme. ¿Me dice por qué no la tomó?")
                    said = reply_at + timedelta(minutes=rng.randint(1, 25))
                    if said < now - timedelta(minutes=2):
                        b.comm("comms_pre", p, said, "inbound", "text", why[L])
                        b.agent_reply("comms_post", p, said, why[3 + L], related_event_id=v2["event_id"])
                        last_inbound = said

        # --- questions and mild symptoms (only while the participant still talks)
        talk_days = [d for d in days[1:-1] if p["_engaged_at"](datetime.combine(d, dtime(22, 0), TZ))]
        n_q = {"adherent": rng.randint(0, 3), "low_adherence": rng.randint(1, 3), "not_engaging": rng.randint(0, 1),
               "flagged": rng.randint(1, 3)}[arche]
        bank = QUESTIONS[study] + QUESTIONS_COMMON
        for q in rng.sample(bank, min(n_q, len(bank))):
            if not talk_days:
                break
            at = _daytime(rng, rng.choice(talk_days), now)
            b.comm("comms_pre", p, at, "inbound", "text", q[L])
            b.agent_reply("comms_pre", p, at, q[2 + L])
            last_inbound = max(last_inbound or at, at)
        n_s = {"adherent": rng.randint(0, 1), "low_adherence": rng.randint(0, 2), "not_engaging": 0,
               "flagged": rng.randint(1, 2)}[arche]
        for s in rng.sample(SYMPTOMS[study], n_s):
            at = _daytime(rng, rng.choice(talk_days), now)
            inbound = b.comm("comms_pre", p, at, "inbound", "text", s[("en", "es")[L]])
            ev = b.event("events", p, "SYMPTOM", at + timedelta(seconds=rng.randint(5, 30)), "agent", "whatsapp",
                         status="logged", source_comm_id=inbound["comm_id"], verbatim_text=inbound["body_text"],
                         summary=s["summary"], pain_score_band=s["band"], body_location=s["location"],
                         onset_date=(at.astimezone(TZ).date() - timedelta(days=rng.randint(0, 1))).isoformat(),
                         characteristics=s["chars"], severity_hint=s["sev"], details={"synthetic": True})
            b.agent_reply("comms_post", p, at, SYMPTOM_REPLY[L], related_event_id=ev["event_id"])
            last_inbound = max(last_inbound or at, at)

        # engaged people must have written in the last 14 days (the KPI counts them as engaging)
        if arche != "not_engaging" and (last_inbound is None or last_inbound < silent_from):
            q = rng.choice(bank)
            at = _daytime(rng, days[-2], now)
            b.comm("comms_pre", p, at, "inbound", "text", q[L])
            b.agent_reply("comms_pre", p, at, q[2 + L])

        # --- the flagged participant's emergency (router path: urgent SYMPTOM, reply, coordinator email)
        if arche == "flagged":
            msg = rng.choice(URGENT[study])
            flagged_episodes.append((p, msg, rng.random()))

    # Most recent episodes stay open (or acknowledged); the rest are resolved.
    rng.shuffle(flagged_episodes)
    n_open, n_ack = OPEN_ALERTS[study], ACKNOWLEDGED_ALERTS.get(study, 0)
    for k, (p, msg, _) in enumerate(flagged_episodes):
        state = "open" if k < n_open else "acknowledged" if k < n_open + n_ack else "resolved"
        hours_ago = {"open": rng.uniform(2, 30), "acknowledged": rng.uniform(4, 40)}.get(state, rng.uniform(48, 400))
        at = min(_daytime(rng, (now - timedelta(hours=hours_ago)).astimezone(TZ).date(), now),
                 now - timedelta(hours=1 if state != "resolved" else 40))
        _emergency(b, p, msg, at, state)
        if rng.random() < 0.5:                       # an earlier website SOS press, long resolved
            sos_at = _daytime(rng, (now - timedelta(days=rng.randint(9, 19))).astimezone(TZ).date(), now)
            _alert(b, p, sos_at, "resolved", severity=rng.choice(["urgent", "concern"]), source="sos_button",
                   message=rng.choice(SOS_MESSAGES), keywords=None)
    return b.rows, archetype_of


def _emergency(b, p, msg, at, state):
    L = 0 if p["preferred_language"] == "en" else 1
    text = msg[L]
    kws = emergency_keywords(text)
    inbound = b.comm("comms_pre", p, at, "inbound", "text", text)
    ev = b.event("events", p, "SYMPTOM", at + timedelta(seconds=2), "router", "whatsapp", status="escalated",
                 source_comm_id=inbound["comm_id"], verbatim_text=text, severity_hint="urgent",
                 urgent_keywords=kws, flag_for_follow_up=True, details={"synthetic": True})
    b.comm("comms_pre", p, at + timedelta(seconds=3), "outbound", "text", EMERGENCY_REPLY[L])
    coordinator = SITE_INFO[p["site_id"]]["coordinator"]
    b.comm("comms_post", p, at + timedelta(seconds=4), "outbound", "report", channel="email",
           body=(f"Participant {p['participant_id']} (site {p['site_id']}) sent a message with emergency keywords: "
                 f"{', '.join(kws)}.\n\nTheir words: \"{text}\"\n\nThey were told to call emergency services. "
                 f"Please contact them now.\nEvent: {ev['event_id']}"),
           from_addr=ALERT_SENDER, to_addr=coordinator, delivery_status="sent", related_event_id=ev["event_id"],
           subject=f"[TT {p['participant_id']} SYM-{ev['event_id'][:4]}] URGENT: emergency keywords on WhatsApp")
    _alert(b, p, at + timedelta(seconds=3), state, severity="life_threatening", source="chat_keyword",
           message=text, keywords=kws)


def _alert(b, p, at, state, severity, source, message, keywords):
    r = b.rng
    coordinator = "staff:" + SITE_INFO[p["site_id"]]["coordinator"]
    row = _empty("trial.emergency_alerts")
    presses = 1 if r.random() < 0.8 else 2
    row.update(alert_id=_uuid(r), participant_id=p["participant_id"], severity=severity, source=source,
               message=message, keywords=keywords, status=state, press_count=presses,
               last_pressed_at=iso(at + timedelta(minutes=0 if presses == 1 else r.randint(1, 20))),
               created_at=iso(at))
    if state in ("acknowledged", "resolved"):
        ack = min(at + timedelta(minutes=r.randint(4, 40)), b.now - timedelta(minutes=5))
        row.update(acknowledged_by=coordinator, acknowledged_at=iso(ack))
        if state == "resolved":
            res = min(ack + timedelta(hours=r.uniform(0.5, 20)), b.now - timedelta(minutes=1))
            row.update(resolved_by=coordinator, resolved_at=iso(res), resolution_note=r.choice(RESOLUTION_NOTES))
    b.rows["alerts"].append(row)


def plan_all(now, seed=0):
    """{study: rows_by_step} for every demo study."""
    return {s: plan_study(s, now, seed)[0] for s in DEMO_STUDIES}


def counts(rows):
    """Row counts per table for one study's plan, with the useful splits."""
    comms = rows["comms_pre"] + rows["comms_post"]
    events = rows["events"] + rows["events_v2"]
    return {
        "participants": len(rows["participants"]),
        "communications": len(comms),
        "  inbound": sum(c["direction"] == "inbound" for c in comms),
        "  agent replies": sum(c["model_id"] is not None for c in comms),
        "clinical_events": len(events),
        "  DOSE": sum(e["record_type"] == "DOSE" for e in events),
        "  SYMPTOM": sum(e["record_type"] == "SYMPTOM" for e in events),
        "emergency_alerts": len(rows["alerts"]),
        "  open": sum(a["status"] == "open" for a in rows["alerts"]),
    }


# ------------------------------------------------------------------ SQL

def insert_sql(table):
    cols = ", ".join(COLUMNS[table])
    conflict = " ON CONFLICT DO NOTHING RETURNING participant_id" if table == "pii.participants" else ""
    return (f"INSERT INTO {table} ({cols}) SELECT {cols} "
            f"FROM jsonb_populate_recordset(NULL::{table}, :rows::jsonb){conflict}")


def chunks(rows, max_chars=30000):
    """Split rows into JSON arrays small enough for one Data API parameter."""
    out, cur, size = [], [], 2
    for r in rows:
        n = len(json.dumps(r, ensure_ascii=False)) + 1
        if cur and size + n > max_chars:
            out.append(json.dumps(cur, ensure_ascii=False))
            cur, size = [], 2
        cur.append(r)
        size += n
    if cur:
        out.append(json.dumps(cur, ensure_ascii=False))
    return out


def pg_array(values):
    return "{" + ",".join(values) + "}"


NO_DELETE_TRIGGERS = [
    ("trial.clinical_events", "clinical_events_append_only"),
    ("trial.communications", "communications_no_delete"),
    ("trial.reminders", "reminders_no_delete"),
    ("trial.emergency_alerts", "emergency_alerts_no_delete"),
]
# The seeder's fingerprint: every participant it writes matches this, and teardown deletes nobody else.
FINGERPRINT = "phone_e164 LIKE '+1555%' AND participant_id ~ '^P-(HTN|DM2|MIG|AST)-[0-9]{2}$'"
_DEMO = f"(SELECT participant_id FROM pii.participants WHERE study_id = ANY(:studies::text[]) AND {FINGERPRINT})"
# Demo-study participants the seeder did not write (a real enrollment into a demo study): teardown aborts on any.
TEARDOWN_GUARD = ("SELECT participant_id, phone_e164, study_id FROM pii.participants "
                  f"WHERE study_id = ANY(:studies::text[]) AND NOT ({FINGERPRINT}) ORDER BY participant_id")
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
# One statement: communications and clinical_events reference each other, and foreign keys are checked at the end
# of the statement, so every child row is gone by then. Child tables first, participants last.
TEARDOWN_DELETE = f"""
WITH a AS (DELETE FROM trial.emergency_alerts WHERE participant_id IN {_DEMO} RETURNING 1),
     c AS (DELETE FROM trial.communications   WHERE participant_id IN {_DEMO} RETURNING 1),
     e AS (DELETE FROM trial.clinical_events  WHERE participant_id IN {_DEMO} RETURNING 1),
     r AS (DELETE FROM trial.reminders        WHERE participant_id IN {_DEMO} RETURNING 1),
     p AS (DELETE FROM pii.participants       WHERE participant_id IN {_DEMO} RETURNING 1)
SELECT (SELECT count(*) FROM a) AS emergency_alerts, (SELECT count(*) FROM c) AS communications,
       (SELECT count(*) FROM e) AS clinical_events, (SELECT count(*) FROM r) AS reminders,
       (SELECT count(*) FROM p) AS participants"""


def teardown_statements():
    """Every statement --teardown runs, in order, in one transaction: (sql, params). TEARDOWN_GUARD is a check:
    teardown() aborts if it returns any row."""
    studies = pg_array(DEMO_STUDIES)
    stmts = [(LOCK_TIMEOUT, {}), (TEARDOWN_GUARD, {"studies": studies})]
    stmts += [(f"ALTER TABLE {t} DISABLE TRIGGER {g}", {}) for t, g in NO_DELETE_TRIGGERS]
    stmts.append((TEARDOWN_DELETE, {"studies": studies}))
    stmts += [(f"ALTER TABLE {t} ENABLE TRIGGER {g}", {}) for t, g in NO_DELETE_TRIGGERS]
    return stmts


# ------------------------------------------------------------------ Data API

class DataApi:
    def __init__(self, rds, cluster_arn, secret_arn, database):
        self.rds = rds
        self.db = dict(resourceArn=cluster_arn, secretArn=secret_arn, database=database)

    def _call(self, fn, **kw):
        resuming = getattr(getattr(self.rds, "exceptions", None), "DatabaseResumingException", None)
        for _ in range(8):
            try:
                return fn(**kw)
            except Exception as e:
                if isinstance(resuming, type) and isinstance(e, resuming):
                    print("   cluster is resuming, retrying in 10s...")
                    time.sleep(10)
                    continue
                raise
        raise RuntimeError("cluster did not resume")

    def begin(self):
        return self._call(self.rds.begin_transaction, **self.db)["transactionId"]

    def commit(self, tx):
        self._call(self.rds.commit_transaction, resourceArn=self.db["resourceArn"], secretArn=self.db["secretArn"],
                   transactionId=tx)

    def rollback(self, tx):
        self.rds.rollback_transaction(resourceArn=self.db["resourceArn"], secretArn=self.db["secretArn"],
                                      transactionId=tx)

    def rollback_quietly(self, tx):
        """Roll back after a failure without masking it (e.g. the commit failed after the server committed)."""
        try:
            self.rollback(tx)
        except Exception as e:
            print(f"   rollback failed too (the original error follows): {e}", file=sys.stderr)

    def q(self, sql, tx=None, **params):
        kw = dict(self.db, sql=sql, formatRecordsAs="JSON",
                  parameters=[{"name": k, "value": {"stringValue": str(v)}} for k, v in params.items()])
        if tx:
            kw["transactionId"] = tx
        r = self._call(self.rds.execute_statement, **kw)
        return json.loads(r.get("formattedRecords") or "[]")


def seed(api, now, seed_value=0, out=print):
    """Insert every demo study that has no participants yet. Returns {study: counts or 'skipped'}."""
    have = {r["study_id"] for r in api.q("SELECT study_id FROM trial.studies WHERE study_id = ANY(:s::text[])",
                                         s=pg_array(DEMO_STUDIES))}
    missing = [s for s in DEMO_STUDIES if s not in have]
    if missing:
        raise SystemExit(f"trial.studies has no row for {', '.join(missing)}: run db/07_studies.sql first")
    existing = {r["study_id"]: int(r["n"]) for r in api.q(
        "SELECT study_id, count(*) AS n FROM pii.participants WHERE study_id = ANY(:s::text[]) GROUP BY 1",
        s=pg_array(DEMO_STUDIES))}
    sites = {r["site_id"] for r in api.q("SELECT DISTINCT site_id FROM pii.participants")}
    if sites and not set(SITES) <= sites:
        out(f"warning: sites {sorted(set(SITES) - sites)} have no participants yet in this database")
    result = {}
    for study in DEMO_STUDIES:
        if existing.get(study):
            out(f"==> {study}: {existing[study]} participants already present, skipped (run --teardown to reseed)")
            result[study] = "skipped"
            continue
        rows = plan_study(study, now, seed_value)[0]
        ids = [p["participant_id"] for p in rows["participants"]]
        phones = [p["phone_e164"] for p in rows["participants"]]
        clash = api.q("SELECT participant_id, phone_e164 FROM pii.participants "
                      "WHERE participant_id = ANY(:i::text[]) OR phone_e164 = ANY(:p::text[])",
                      i=pg_array(ids), p=pg_array(phones))
        if clash:
            raise SystemExit(f"{study}: ids/phones already used by other participants: {clash}")
        tx = api.begin()
        try:
            for table, key in STEPS:
                for payload in chunks(rows[key]):
                    got = api.q(insert_sql(table), tx=tx, rows=payload)
                    if table == "pii.participants" and len(got) != len(json.loads(payload)):
                        raise RuntimeError(f"{study}: {len(got)} of {len(json.loads(payload))} participants inserted")
            api.commit(tx)
        except BaseException:
            api.rollback_quietly(tx)
            raise
        c = counts(rows)
        out(f"==> {study}: " + ", ".join(f"{k.strip()} {v}" for k, v in c.items()))
        result[study] = c
    return result


def teardown(api, out=print):
    tx = api.begin()
    try:
        deleted = None
        for sql, params in teardown_statements():
            out(f"   {' '.join(sql.split())[:110]}")
            rows = api.q(sql, tx=tx, **params)
            if sql is TEARDOWN_GUARD:
                if rows:
                    raise SystemExit(
                        f"ABORTED, nothing deleted: {len(rows)} participant(s) in the demo studies were not written by "
                        "this seeder (no +1555 phone / P-<STUDY>-NN id), so they may be real people: "
                        + ", ".join(f"{r['participant_id']} ({r['study_id']})" for r in rows)
                        + ". Move them out of the demo studies (or remove them by hand) first.")
                continue
            if rows:
                deleted = rows[0]
        api.commit(tx)
    except BaseException:
        api.rollback_quietly(tx)
        raise
    out("deleted: " + ", ".join(f"{k} {v}" for k, v in deleted.items()))
    return deleted


def dry_run(now, seed_value=0, teardown_mode=False, out=print):
    if teardown_mode:
        out("--teardown would run, in one transaction, as the master user:")
        for sql, params in teardown_statements():
            out(" ".join(sql.split()) + (f"   -- {params}" if params else "") + ";")
        return None
    plans = plan_all(now, seed_value)
    out(f"dry run (now = {iso(now)}): rows per study, nothing written")
    totals = {}
    for study in DEMO_STUDIES:
        arch = plan_study(study, now, seed_value)[1]
        mix = {a: sum(v == a for v in arch.values()) for a in ARCHETYPES}
        c = counts(plans[study])
        out(f"==> {study}  archetypes {mix}")
        for k, v in c.items():
            out(f"   {k:<18}{v:>6}")
            totals[k] = totals.get(k, 0) + v
    out("==> total")
    for k, v in totals.items():
        out(f"   {k:<18}{v:>6}")
    return totals


def main(argv=None, rds=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cluster-arn")
    ap.add_argument("--secret-arn", help="the cluster MASTER secret (DDL for --teardown, inserts for seeding)")
    ap.add_argument("--database", default="trialtalk")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--teardown", action="store_true", help="delete the demo studies' data (never KOA-301)")
    ap.add_argument("--dry-run", action="store_true", help="print row counts (or the teardown SQL); no AWS calls")
    ap.add_argument("--seed", type=int, default=0, help="random seed for the synthetic history")
    args = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)

    if args.dry_run:
        return dry_run(now, args.seed, args.teardown)
    if not (args.cluster_arn and args.secret_arn):
        ap.error("--cluster-arn and --secret-arn are required (or use --dry-run)")
    if rds is None:
        import boto3
        rds = boto3.Session(profile_name=args.profile, region_name=args.region).client("rds-data")
    api = DataApi(rds, args.cluster_arn, args.secret_arn, args.database)
    return teardown(api) if args.teardown else seed(api, now, args.seed)


if __name__ == "__main__":
    main()
    sys.exit(0)
