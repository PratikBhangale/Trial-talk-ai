-- TrialTalk schema: 4 tables, 1 view, integrity triggers.
-- Target: Aurora PostgreSQL 16 Serverless v2, database "trialtalk".
-- Run as the cluster master user. Order matters (FKs between tables are added after creation).

-- gen_random_uuid() is built in since PG13, so pgcrypto is not needed.
CREATE SCHEMA IF NOT EXISTS pii;
CREATE SCHEMA IF NOT EXISTS trial;

------------------------------------------------------------------------------
-- 1. pii.participants
------------------------------------------------------------------------------
CREATE TABLE pii.participants (
  participant_id      text PRIMARY KEY,               -- 'P-0142', shared key
  full_name           text NOT NULL,
  email               text,
  phone_e164          text NOT NULL UNIQUE,           -- inbound WhatsApp lookup
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
  status              text NOT NULL CHECK (status IN
                        ('prospect','screening','enrolled','withdrawn','screen_failed')),
  whatsapp_opt_in     boolean NOT NULL DEFAULT false,
  opt_in_at           timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON pii.participants (site_id);

------------------------------------------------------------------------------
-- 2. trial.clinical_events (append-only, corrections are new rows)
------------------------------------------------------------------------------
CREATE TABLE trial.clinical_events (
  -- identity and timeline
  event_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id     text NOT NULL REFERENCES pii.participants(participant_id),
  record_type        text NOT NULL CHECK (record_type IN
                       ('PRESCREEN','CONSENT_Q','VISIT','VISIT_CHANGE','SYMPTOM','AE','DOSE','SIGNATURE')),
  status             text,                            -- per type
  occurred_at        timestamptz NOT NULL DEFAULT now(),
  source             text NOT NULL CHECK (source IN ('whatsapp','email','scheduler','portal')),
  parent_event_id    uuid REFERENCES trial.clinical_events(event_id),  -- AE->symptom, change->visit, signature->record
  source_comm_id     uuid,                            -- FK added after communications

  -- participant-reported (agent fills)
  verbatim_text      text,
  summary            text,
  pain_score_band    text CHECK (pain_score_band IN ('0-3','4-6','7-10')),
  body_location      text,
  onset_date         date,
  characteristics    text[],
  severity_hint      text CHECK (severity_hint IN ('none','mild','moderate','severe','urgent')),
  urgent_keywords    text[],
  reason_code        text,                            -- transport, work, unwell, forgot, side_effects, ran_out, other
  consent_section    text,
  topic              text,                            -- withdrawal, risks, costs, privacy, schedule
  flag_for_follow_up boolean NOT NULL DEFAULT false,
  attachment_s3_key  text,

  -- scheduling
  visit_code         text,                            -- 'SCREEN', 'WK04'
  dose_label         text,                            -- 'evening'
  scheduled_at       timestamptz,
  window_start       date,
  window_end         date,
  response           text CHECK (response IN ('confirmed','taken','skipped','snoozed','no_response')),

  -- answers and instructions
  response_text      text,
  answered_by        text,                            -- knowledge_base | coordinator | pi

  -- staff assessment (AE only, agent has no grant)
  ae_term            text,
  severity_grade     int CHECK (severity_grade BETWEEN 1 AND 5),
  serious            boolean,
  relatedness        text CHECK (relatedness IN ('not_related','unlikely','possible','probable','definite')),
  expected           boolean,
  action_taken       text,
  outcome            text,                            -- recovering, recovered, ongoing, fatal, unknown
  staff_notes        text,

  -- type-specific extras
  details            jsonb NOT NULL DEFAULT '{}',

  -- versioning = audit trail
  supersedes_event_id uuid REFERENCES trial.clinical_events(event_id),
  change_reason      text,
  created_by         text NOT NULL,                   -- agent | reminder-lambda | staff:<email> | participant:<id>
  created_at         timestamptz NOT NULL DEFAULT now(),

  CHECK (record_type NOT IN ('SYMPTOM','CONSENT_Q') OR verbatim_text IS NOT NULL),
  CHECK (supersedes_event_id IS NULL OR change_reason IS NOT NULL)
);
CREATE INDEX ON trial.clinical_events (participant_id, occurred_at DESC);
CREATE INDEX ON trial.clinical_events (record_type, status);
-- Unique (not just indexed): a version can be superseded once, so history stays a straight line
-- and current_events never returns two "latest" rows.
CREATE UNIQUE INDEX clinical_events_supersedes_once
  ON trial.clinical_events (supersedes_event_id) WHERE supersedes_event_id IS NOT NULL;
-- trial.current_events is created at the end of section 4, after the last column is added:
-- a view's e.* is fixed when the view is created.

------------------------------------------------------------------------------
-- 3. trial.communications
------------------------------------------------------------------------------
CREATE TABLE trial.communications (
  comm_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id      text REFERENCES pii.participants(participant_id),  -- NULL for multi-participant digests
  channel             text NOT NULL CHECK (channel IN ('whatsapp','email')),
  direction           text NOT NULL CHECK (direction IN ('inbound','outbound')),
  msg_type            text NOT NULL,    -- text, template, button_reply, list_reply, image, audio, location,
                                        -- report, digest, staff_reply, participant_request
  template_name       text,
  from_addr           text,
  to_addr             text,
  subject             text,             -- carries token: [TT P-0142 AE-7f3a]
  body_text           text,             -- cleaned text or voice-note transcript
  s3_raw_key          text,             -- raw MIME or media
  parsed_action       jsonb,            -- button ID or staff-reply intent
  related_event_id    uuid REFERENCES trial.clinical_events(event_id),
  provider_message_id text UNIQUE,      -- WhatsApp wamid / SES id: dedupe
  delivery_status     text,             -- sent, delivered, read, failed

  -- attachments
  attachments         jsonb NOT NULL DEFAULT '[]',
                      -- [{s3_key, filename, mime_type, size_bytes, sha256, scan_status, shareable}]
  attachment_text     text,

  -- agent usage (on messages the agent produced)
  session_id          text,
  model_id            text,
  input_tokens        int,
  output_tokens       int,
  latency_ms          int,
  tool_calls          int,
  guardrail_action    text,             -- NONE | INTERVENED

  created_at          timestamptz NOT NULL DEFAULT now(),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple',
      coalesce(subject,'') || ' ' || coalesce(body_text,'') || ' ' || coalesce(attachment_text,''))) STORED
);
CREATE INDEX ON trial.communications (participant_id, created_at DESC);
CREATE INDEX ON trial.communications USING gin (tsv);
CREATE INDEX ON trial.communications (related_event_id);

ALTER TABLE trial.clinical_events
  ADD FOREIGN KEY (source_comm_id) REFERENCES trial.communications(comm_id);

------------------------------------------------------------------------------
-- 4. trial.reminders (updated in place, staff edits appended to change_log)
------------------------------------------------------------------------------
CREATE TABLE trial.reminders (
  reminder_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id        text NOT NULL REFERENCES pii.participants(participant_id),
  reminder_type         text NOT NULL CHECK (reminder_type IN ('dose','visit','diary','custom')),
  label                 text NOT NULL,          -- 'Evening dose', 'Week 4 visit, 24h'

  -- what to send
  template_name         text NOT NULL,
  template_language     text NOT NULL DEFAULT 'en',
  template_params       jsonb NOT NULL DEFAULT '{}',
  buttons               text[],

  -- when to send
  schedule_kind         text NOT NULL CHECK (schedule_kind IN ('recurring','one_time')),
  cron_expr             text,                   -- recurring: 'cron(0 20 * * ? *)'
  send_at               timestamptz,            -- one_time
  timezone              text NOT NULL,
  starts_on             date,
  ends_on               date,
  visit_event_id        uuid REFERENCES trial.clinical_events(event_id),

  -- follow-up rules
  follow_up_after_min   int,
  escalate_after_misses int,

  -- state
  status                text NOT NULL DEFAULT 'active' CHECK (status IN
                          ('draft','active','paused','completed','cancelled')),
  scheduler_arn         text,
  next_send_at          timestamptz,
  last_sent_at          timestamptz,
  send_count            int NOT NULL DEFAULT 0,

  -- audit
  change_log            jsonb NOT NULL DEFAULT '[]',   -- [{at, by, field, old, new, reason}]
  created_by            text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_by            text,
  updated_at            timestamptz NOT NULL DEFAULT now(),

  CHECK ((schedule_kind = 'recurring' AND cron_expr IS NOT NULL)
      OR (schedule_kind = 'one_time'  AND send_at   IS NOT NULL))
);
CREATE INDEX ON trial.reminders (participant_id, status);
CREATE INDEX ON trial.reminders (status, next_send_at);

-- Link sends and dose responses back to the reminder
ALTER TABLE trial.communications  ADD COLUMN reminder_id uuid REFERENCES trial.reminders(reminder_id);
ALTER TABLE trial.clinical_events ADD COLUMN reminder_id uuid REFERENCES trial.reminders(reminder_id);

-- Latest version of each clinical record. Created after all clinical_events columns exist.
CREATE VIEW trial.current_events WITH (security_invoker = true) AS
SELECT e.* FROM trial.clinical_events e
WHERE NOT EXISTS (SELECT 1 FROM trial.clinical_events n WHERE n.supersedes_event_id = e.event_id);
-- Router matches a button reply to "latest message with that reminder_id"
CREATE INDEX ON trial.communications (reminder_id, created_at DESC);
CREATE INDEX ON trial.clinical_events (reminder_id);

------------------------------------------------------------------------------
-- Triggers. These hold even for the table owner, unlike grants.
------------------------------------------------------------------------------

-- Reject UPDATE / DELETE / TRUNCATE.
CREATE FUNCTION trial.block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on %.% is not allowed; insert a new row instead',
    TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER clinical_events_append_only
  BEFORE UPDATE OR DELETE ON trial.clinical_events
  FOR EACH ROW EXECUTE FUNCTION trial.block_mutation();
CREATE TRIGGER clinical_events_no_truncate
  BEFORE TRUNCATE ON trial.clinical_events
  FOR EACH STATEMENT EXECUTE FUNCTION trial.block_mutation();
CREATE TRIGGER communications_no_delete
  BEFORE DELETE ON trial.communications
  FOR EACH ROW EXECUTE FUNCTION trial.block_mutation();
CREATE TRIGGER reminders_no_delete
  BEFORE DELETE ON trial.reminders
  FOR EACH ROW EXECUTE FUNCTION trial.block_mutation();

-- A new version must belong to the same participant and record_type as the row it replaces.
CREATE FUNCTION trial.check_supersedes() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  prev_pid  text;
  prev_type text;
BEGIN
  IF NEW.supersedes_event_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT participant_id, record_type INTO prev_pid, prev_type
  FROM trial.clinical_events WHERE event_id = NEW.supersedes_event_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'supersedes_event_id % not found', NEW.supersedes_event_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF prev_pid <> NEW.participant_id OR prev_type <> NEW.record_type THEN
    RAISE EXCEPTION 'event % is % for %; new version is % for %',
      NEW.supersedes_event_id, prev_type, prev_pid, NEW.record_type, NEW.participant_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER clinical_events_check_supersedes
  BEFORE INSERT ON trial.clinical_events
  FOR EACH ROW EXECUTE FUNCTION trial.check_supersedes();

-- Keep updated_at honest without relying on every Lambda to set it.
CREATE FUNCTION trial.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE TRIGGER participants_updated_at
  BEFORE UPDATE ON pii.participants
  FOR EACH ROW EXECUTE FUNCTION trial.set_updated_at();
CREATE TRIGGER reminders_updated_at
  BEFORE UPDATE ON trial.reminders
  FOR EACH ROW EXECUTE FUNCTION trial.set_updated_at();
