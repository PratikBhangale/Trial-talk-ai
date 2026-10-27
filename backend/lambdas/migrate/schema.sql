-- TrialTalk schema (build plan, "Data model" + "Authentication and RBAC").
-- Applied by the migrate Lambda on every deploy, so every statement is idempotent.
-- Four tables only; signatures, attachments, audit history and token usage live inside them.
-- (Plus trial.emergency_alerts below: alerts need an acknowledge / resolve lifecycle that append-only
-- clinical_events cannot give them.)
--
-- Additions beyond the plan (kept minimal):
--   * channel 'web' on trial.communications for the participant website chat and requests
--   * participant_role may read its own web chat rows and the template_params of its reminders
--   * messaging_role may SELECT communications/clinical_events (router matches replies to reminders)
--   * RLS policies for agent_role / messaging_role (the plan enables RLS but only lists portal/participant policies)
--   * pii.participant_seq generates 'P-1001', 'P-1002', ...

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS pgaudit;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pgaudit not loaded (check shared_preload_libraries): %', SQLERRM;
END $$;

CREATE SCHEMA IF NOT EXISTS pii;
CREATE SCHEMA IF NOT EXISTS trial;

CREATE SEQUENCE IF NOT EXISTS pii.participant_seq START 1001;

-- ---------------------------------------------------------------- 1. pii.participants
CREATE TABLE IF NOT EXISTS pii.participants (
  participant_id      text PRIMARY KEY DEFAULT ('P-' || lpad(nextval('pii.participant_seq')::text, 4, '0')),
  full_name           text NOT NULL,
  email               text,
  phone_e164          text NOT NULL UNIQUE CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  date_of_birth       date,
  address_line1       text,
  address_line2       text,
  city                text,
  state               text,
  postal_code         text,
  country             text DEFAULT 'US',
  travel_radius_miles int,
  preferred_language  text NOT NULL DEFAULT 'en',
  timezone            text NOT NULL DEFAULT 'America/New_York',
  study_id            text NOT NULL,
  site_id             text NOT NULL,
  status              text NOT NULL CHECK (status IN ('prospect','screening','enrolled','withdrawn','screen_failed')),
  whatsapp_opt_in     boolean NOT NULL DEFAULT false,
  opt_in_at           timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS participants_site_idx ON pii.participants (site_id, status);

-- ---------------------------------------------------------------- 2. trial.clinical_events (append-only)
CREATE TABLE IF NOT EXISTS trial.clinical_events (
  event_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id      text NOT NULL REFERENCES pii.participants(participant_id),
  record_type         text NOT NULL CHECK (record_type IN
                        ('PRESCREEN','CONSENT_Q','VISIT','VISIT_CHANGE','SYMPTOM','AE','DOSE','SIGNATURE')),
  status              text,
  occurred_at         timestamptz NOT NULL DEFAULT now(),
  source              text NOT NULL CHECK (source IN ('whatsapp','email','scheduler','portal')),
  parent_event_id     uuid REFERENCES trial.clinical_events(event_id),
  source_comm_id      uuid,
  verbatim_text       text,
  summary             text,
  pain_score_band     text CHECK (pain_score_band IN ('0-3','4-6','7-10')),
  body_location       text,
  onset_date          date,
  characteristics     text[],
  severity_hint       text CHECK (severity_hint IN ('none','mild','moderate','severe','urgent')),
  urgent_keywords     text[],
  reason_code         text,
  consent_section     text,
  topic               text,
  flag_for_follow_up  boolean NOT NULL DEFAULT false,
  attachment_s3_key   text,
  visit_code          text,
  dose_label          text,
  scheduled_at        timestamptz,
  window_start        date,
  window_end          date,
  response            text CHECK (response IN ('confirmed','taken','skipped','snoozed','no_response')),
  response_text       text,
  answered_by         text,
  ae_term             text,
  severity_grade      int CHECK (severity_grade BETWEEN 1 AND 5),
  serious             boolean,
  relatedness         text CHECK (relatedness IN ('not_related','unlikely','possible','probable','definite')),
  expected            boolean,
  action_taken        text,
  outcome             text,
  staff_notes         text,
  details             jsonb NOT NULL DEFAULT '{}',
  supersedes_event_id uuid REFERENCES trial.clinical_events(event_id),
  change_reason       text,
  created_by          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (record_type NOT IN ('SYMPTOM','CONSENT_Q') OR verbatim_text IS NOT NULL),
  CHECK (supersedes_event_id IS NULL OR change_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS clinical_events_pid_idx ON trial.clinical_events (participant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS clinical_events_type_idx ON trial.clinical_events (record_type, status);
CREATE INDEX IF NOT EXISTS clinical_events_supersedes_idx ON trial.clinical_events (supersedes_event_id);

CREATE OR REPLACE VIEW trial.current_events WITH (security_invoker = true) AS
SELECT e.* FROM trial.clinical_events e
WHERE NOT EXISTS (SELECT 1 FROM trial.clinical_events n WHERE n.supersedes_event_id = e.event_id);

-- ---------------------------------------------------------------- 3. trial.communications
CREATE TABLE IF NOT EXISTS trial.communications (
  comm_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id      text REFERENCES pii.participants(participant_id),
  channel             text NOT NULL CHECK (channel IN ('whatsapp','email','web')),
  direction           text NOT NULL CHECK (direction IN ('inbound','outbound')),
  msg_type            text NOT NULL,
  template_name       text,
  from_addr           text,
  to_addr             text,
  subject             text,
  body_text           text,
  s3_raw_key          text,
  parsed_action       jsonb,
  related_event_id    uuid REFERENCES trial.clinical_events(event_id),
  provider_message_id text UNIQUE,
  delivery_status     text,
  attachments         jsonb NOT NULL DEFAULT '[]',
  attachment_text     text,
  session_id          text,
  model_id            text,
  input_tokens        int,
  output_tokens       int,
  latency_ms          int,
  tool_calls          int,
  guardrail_action    text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple',
      coalesce(subject,'') || ' ' || coalesce(body_text,'') || ' ' || coalesce(attachment_text,''))) STORED
);
CREATE INDEX IF NOT EXISTS communications_pid_idx ON trial.communications (participant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS communications_tsv_idx ON trial.communications USING gin (tsv);

DO $$ BEGIN
  ALTER TABLE trial.clinical_events
    ADD CONSTRAINT clinical_events_source_comm_fk FOREIGN KEY (source_comm_id) REFERENCES trial.communications(comm_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------- 4. trial.reminders
CREATE TABLE IF NOT EXISTS trial.reminders (
  reminder_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id        text NOT NULL REFERENCES pii.participants(participant_id),
  reminder_type         text NOT NULL CHECK (reminder_type IN ('dose','visit','diary','custom')),
  label                 text NOT NULL,
  template_name         text NOT NULL,
  template_language     text NOT NULL DEFAULT 'en',
  template_params       jsonb NOT NULL DEFAULT '{}',
  buttons               text[],
  schedule_kind         text NOT NULL CHECK (schedule_kind IN ('recurring','one_time')),
  cron_expr             text,
  send_at               timestamptz,
  timezone              text NOT NULL,
  starts_on             date,
  ends_on               date,
  visit_event_id        uuid REFERENCES trial.clinical_events(event_id),
  follow_up_after_min   int,
  escalate_after_misses int,
  status                text NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','paused','completed','cancelled')),
  scheduler_arn         text,
  next_send_at          timestamptz,
  last_sent_at          timestamptz,
  send_count            int NOT NULL DEFAULT 0,
  change_log            jsonb NOT NULL DEFAULT '[]',
  created_by            text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_by            text,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK ((schedule_kind = 'recurring' AND cron_expr IS NOT NULL)
      OR (schedule_kind = 'one_time' AND send_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS reminders_pid_idx ON trial.reminders (participant_id, status);
CREATE INDEX IF NOT EXISTS reminders_next_idx ON trial.reminders (status, next_send_at);

ALTER TABLE trial.communications ADD COLUMN IF NOT EXISTS reminder_id uuid REFERENCES trial.reminders(reminder_id);
-- A staff reply to a participant request (msg_type 'staff_reply') points at the request it answers.
ALTER TABLE trial.communications ADD COLUMN IF NOT EXISTS reply_to_comm_id uuid REFERENCES trial.communications(comm_id);
CREATE INDEX IF NOT EXISTS communications_reply_idx ON trial.communications (reply_to_comm_id);
ALTER TABLE trial.clinical_events ADD COLUMN IF NOT EXISTS reminder_id uuid REFERENCES trial.reminders(reminder_id);

-- ---------------------------------------------------------------- 5. trial.emergency_alerts
-- One row per emergency: the participant's SOS button, or Layer 0 keywords in the web chat. Staff acknowledge and
-- resolve them; repeat presses while one is open are folded into it (press_count) so staff see one alert, not fifty.
CREATE TABLE IF NOT EXISTS trial.emergency_alerts (
  alert_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id   text NOT NULL REFERENCES pii.participants(participant_id),
  severity         text NOT NULL DEFAULT 'life_threatening' CHECK (severity IN ('life_threatening','urgent','concern')),
  source           text NOT NULL CHECK (source IN ('sos_button','chat_keyword')),
  message          text,
  keywords         text[],
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
  press_count      int NOT NULL DEFAULT 1,
  last_pressed_at  timestamptz NOT NULL DEFAULT now(),
  acknowledged_by  text,
  acknowledged_at  timestamptz,
  resolved_by      text,
  resolved_at      timestamptz,
  resolution_note  text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS emergency_alerts_status_idx ON trial.emergency_alerts (status, created_at DESC);
CREATE INDEX IF NOT EXISTS emergency_alerts_pid_idx ON trial.emergency_alerts (participant_id, created_at DESC);

-- ---------------------------------------------------------------- roles
DO $$ BEGIN CREATE ROLE agent_role NOLOGIN;       EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE messaging_role NOLOGIN;   EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE portal_role NOLOGIN;      EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE participant_role NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Start from nothing on every run, then grant exactly what the plan lists.
REVOKE ALL ON ALL TABLES IN SCHEMA pii, trial FROM PUBLIC, agent_role, messaging_role, portal_role, participant_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA pii, trial FROM PUBLIC, agent_role, messaging_role, portal_role, participant_role;
GRANT USAGE ON SCHEMA pii, trial TO agent_role, messaging_role, portal_role, participant_role;

-- Agent: no names, emails, phones or addresses; no staff assessment columns
GRANT SELECT (participant_id, preferred_language, timezone, status, site_id, study_id) ON pii.participants TO agent_role;
GRANT SELECT ON trial.clinical_events, trial.current_events, trial.communications TO agent_role;
GRANT INSERT (participant_id, record_type, status, occurred_at, source, parent_event_id, source_comm_id,
  verbatim_text, summary, pain_score_band, body_location, onset_date, characteristics, severity_hint,
  urgent_keywords, reason_code, consent_section, topic, flag_for_follow_up, attachment_s3_key,
  visit_code, dose_label, scheduled_at, window_start, window_end, response, response_text, answered_by,
  details, reminder_id, supersedes_event_id, change_reason, created_by) ON trial.clinical_events TO agent_role;
GRANT SELECT ON trial.reminders TO agent_role;
GRANT INSERT ON trial.communications TO agent_role;

-- Router, reminder, parser Lambdas
GRANT SELECT (participant_id, phone_e164, preferred_language, timezone, whatsapp_opt_in, status, site_id)
  ON pii.participants TO messaging_role;
GRANT SELECT, INSERT ON trial.clinical_events, trial.communications TO messaging_role;
GRANT SELECT ON trial.current_events TO messaging_role;
GRANT UPDATE (delivery_status, attachments, attachment_text) ON trial.communications TO messaging_role;
GRANT SELECT ON trial.reminders TO messaging_role;
GRANT UPDATE (next_send_at, last_sent_at, send_count, status) ON trial.reminders TO messaging_role;

-- Portal (row-level security narrows to the user's sites)
GRANT SELECT, INSERT, UPDATE ON pii.participants TO portal_role;
GRANT USAGE ON SEQUENCE pii.participant_seq TO portal_role;
GRANT SELECT, INSERT ON trial.clinical_events, trial.current_events, trial.communications TO portal_role;
GRANT SELECT, INSERT, UPDATE ON trial.reminders TO portal_role;

-- Participant website: own data only; no staff columns
GRANT SELECT ON pii.participants TO participant_role;
GRANT UPDATE (email, address_line1, address_line2, city, state, postal_code, country,
  preferred_language, timezone, updated_at) ON pii.participants TO participant_role;
GRANT SELECT (event_id, record_type, status, occurred_at, verbatim_text, pain_score_band, body_location,
  onset_date, visit_code, dose_label, scheduled_at, window_start, window_end, response, response_text, supersedes_event_id)
  ON trial.clinical_events TO participant_role;
GRANT SELECT (event_id, record_type, status, occurred_at, verbatim_text, pain_score_band, body_location,
  onset_date, visit_code, dose_label, scheduled_at, window_start, window_end, response, response_text)
  ON trial.current_events TO participant_role;
GRANT INSERT (participant_id, record_type, source, parent_event_id, scheduled_at, reason_code,
  verbatim_text, details, created_by) ON trial.clinical_events TO participant_role;
GRANT SELECT (reminder_id, participant_id, label, reminder_type, template_params, cron_expr, send_at, timezone,
  starts_on, ends_on, status, next_send_at) ON trial.reminders TO participant_role;
GRANT SELECT (comm_id, participant_id, channel, direction, msg_type, subject, body_text, created_at, reply_to_comm_id)
  ON trial.communications TO participant_role;
GRANT INSERT (participant_id, channel, direction, msg_type, subject, body_text, session_id, latency_ms,
  input_tokens, output_tokens, model_id) ON trial.communications TO participant_role;

-- Emergency alerts. Participants raise and re-press their own; staff triage; the agent/router can only read.
GRANT SELECT, INSERT ON trial.emergency_alerts TO portal_role, messaging_role;
GRANT UPDATE (status, acknowledged_by, acknowledged_at, resolved_by, resolved_at, resolution_note)
  ON trial.emergency_alerts TO portal_role;
GRANT SELECT ON trial.emergency_alerts TO participant_role;
GRANT INSERT (participant_id, severity, source, message, keywords) ON trial.emergency_alerts TO participant_role;
GRANT UPDATE (press_count, last_pressed_at, message) ON trial.emergency_alerts TO participant_role;

-- ---------------------------------------------------------------- row-level security
ALTER TABLE pii.participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.clinical_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.communications ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.emergency_alerts ENABLE ROW LEVEL SECURITY;

-- Staff: their sites only
DROP POLICY IF EXISTS site_scope ON pii.participants;
CREATE POLICY site_scope ON pii.participants FOR ALL TO portal_role
  USING (site_id = ANY (string_to_array(current_setting('app.site_ids', true), ',')));
DROP POLICY IF EXISTS site_scope ON trial.clinical_events;
CREATE POLICY site_scope ON trial.clinical_events FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));
DROP POLICY IF EXISTS site_scope ON trial.communications;
CREATE POLICY site_scope ON trial.communications FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));
DROP POLICY IF EXISTS site_scope ON trial.reminders;
CREATE POLICY site_scope ON trial.reminders FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));
DROP POLICY IF EXISTS site_scope ON trial.emergency_alerts;
CREATE POLICY site_scope ON trial.emergency_alerts FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));

-- Participants: own rows only (app.participant_id is set from the Cognito token)
DROP POLICY IF EXISTS own_record ON pii.participants;
CREATE POLICY own_record ON pii.participants FOR ALL TO participant_role
  USING (participant_id = current_setting('app.participant_id', true));
DROP POLICY IF EXISTS own_history ON trial.clinical_events;
CREATE POLICY own_history ON trial.clinical_events FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true)
         AND record_type IN ('VISIT','VISIT_CHANGE','SYMPTOM','DOSE','CONSENT_Q'));
DROP POLICY IF EXISTS own_reschedule ON trial.clinical_events;
CREATE POLICY own_reschedule ON trial.clinical_events FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true) AND record_type = 'VISIT_CHANGE');
DROP POLICY IF EXISTS own_reminders ON trial.reminders;
CREATE POLICY own_reminders ON trial.reminders FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true));
DROP POLICY IF EXISTS own_requests ON trial.communications;
CREATE POLICY own_requests ON trial.communications FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true)
              AND channel = 'web' AND msg_type IN ('participant_request','text'));
DROP POLICY IF EXISTS own_web_messages ON trial.communications;
CREATE POLICY own_web_messages ON trial.communications FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true) AND channel = 'web');
DROP POLICY IF EXISTS own_alerts ON trial.emergency_alerts;
CREATE POLICY own_alerts ON trial.emergency_alerts FOR ALL TO participant_role
  USING (participant_id = current_setting('app.participant_id', true))
  WITH CHECK (participant_id = current_setting('app.participant_id', true));

-- Service roles (router, reminder, parser Lambdas; agent). Their tools are bound to one participant in code.
DROP POLICY IF EXISTS service_all ON pii.participants;
CREATE POLICY service_all ON pii.participants FOR SELECT TO messaging_role, agent_role USING (true);
DROP POLICY IF EXISTS service_all ON trial.clinical_events;
CREATE POLICY service_all ON trial.clinical_events FOR ALL TO messaging_role, agent_role USING (true);
DROP POLICY IF EXISTS service_all ON trial.communications;
CREATE POLICY service_all ON trial.communications FOR ALL TO messaging_role, agent_role USING (true);
DROP POLICY IF EXISTS service_all ON trial.emergency_alerts;
CREATE POLICY service_all ON trial.emergency_alerts FOR SELECT TO messaging_role USING (true);
DROP POLICY IF EXISTS service_all ON trial.reminders;
CREATE POLICY service_all ON trial.reminders FOR ALL TO messaging_role, agent_role USING (true);

-- Clinical records are append-only for everyone; reminders and messages are cancelled, never deleted
REVOKE UPDATE, DELETE, TRUNCATE ON trial.clinical_events FROM PUBLIC, agent_role, messaging_role, portal_role, participant_role;
REVOKE DELETE, TRUNCATE ON trial.communications, trial.reminders, trial.emergency_alerts FROM PUBLIC, agent_role, messaging_role, portal_role, participant_role;
REVOKE DELETE, TRUNCATE ON pii.participants FROM PUBLIC, agent_role, messaging_role, portal_role, participant_role;
