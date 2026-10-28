-- TrialTalk emergency alerts and the 'web' channel. Run after 05_telemetry.sql, as the master user.
--
-- Adds what the participant website and the WhatsApp router need on top of 01 and 02:
--   1. a 'web' channel for communications rows written by the website (chat, requests, staff replies)
--   2. trial.emergency_alerts, one row per emergency (SOS button, or emergency keywords in chat/WhatsApp)
--   3. a delete grant on telemetry_spans for the nightly retention prune
-- Grants and row-level security for the new objects live in this file, next to what they protect, so the
-- whole feature applies and reads as one unit. They follow the patterns in 02_roles_rls.sql.
--
-- IDEMPOTENT: safe on a fresh database and on Aurora, which already has the alerts table, the web channel and
-- own_web_messages from earlier manual changes. CREATE ... IF NOT EXISTS, DROP ... IF EXISTS before every policy
-- and trigger, and GRANTs are repeatable. It also SUPERSEDES db/05_alerts_router_write.sql (applied to Aurora
-- manually on Oct 28, since deleted): the same messaging_role INSERT and UPDATE grants, with the same
-- resolved-row guard on UPDATE, now live in section 2.

------------------------------------------------------------------------------
-- 1. The 'web' channel
-- 01_schema.sql created the inline CHECK, which Postgres named communications_channel_check.
------------------------------------------------------------------------------
ALTER TABLE trial.communications DROP CONSTRAINT IF EXISTS communications_channel_check;
ALTER TABLE trial.communications ADD CONSTRAINT communications_channel_check
  CHECK (channel IN ('whatsapp','email','web'));

-- Participants read their own web rows: chat history, their requests, and staff replies
-- (msg_type 'request_reply', linked to the request through parsed_action.reply_to_comm_id).
GRANT SELECT (comm_id, participant_id, channel, direction, msg_type, subject, body_text, parsed_action, created_at)
  ON trial.communications TO participant_role;
DROP POLICY IF EXISTS own_web_messages ON trial.communications;
CREATE POLICY own_web_messages ON trial.communications FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true) AND channel = 'web');

-- Participants log their own web chat, with the agent's usage on its replies.
-- (02's own_requests policy already allows their participant_request rows.)
GRANT INSERT (session_id, latency_ms, input_tokens, output_tokens, model_id) ON trial.communications TO participant_role;
DROP POLICY IF EXISTS own_web_chat ON trial.communications;
CREATE POLICY own_web_chat ON trial.communications FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true) AND channel = 'web' AND msg_type = 'text');

------------------------------------------------------------------------------
-- 2. trial.emergency_alerts
-- Alerts need an acknowledge / resolve lifecycle that append-only clinical_events cannot give them, and
-- participant_role can neither insert nor read AE rows. Repeat presses fold into the open alert (press_count).
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trial.emergency_alerts (
  alert_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  participant_id   text NOT NULL REFERENCES pii.participants(participant_id),
  severity         text NOT NULL DEFAULT 'life_threatening' CHECK (severity IN ('life_threatening','urgent','concern')),
  source           text NOT NULL CHECK (source IN ('sos_button','chat_keyword')),
  message          text,                                    -- what the participant wrote, if anything
  keywords         text[],                                  -- emergency keywords that matched (chat_keyword)
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
  press_count      int NOT NULL DEFAULT 1,                  -- repeat presses fold into the open alert
  last_pressed_at  timestamptz NOT NULL DEFAULT now(),
  acknowledged_by  text,                                    -- staff:<email>
  acknowledged_at  timestamptz,
  resolved_by      text,
  resolved_at      timestamptz,
  resolution_note  text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS emergency_alerts_status_created_at_idx ON trial.emergency_alerts (status, created_at DESC);
CREATE INDEX IF NOT EXISTS emergency_alerts_participant_id_created_at_idx
  ON trial.emergency_alerts (participant_id, created_at DESC);

-- Never deleted (resolve it instead). Same trigger function as the other append-only tables.
DROP TRIGGER IF EXISTS emergency_alerts_no_delete ON trial.emergency_alerts;
CREATE TRIGGER emergency_alerts_no_delete
  BEFORE DELETE ON trial.emergency_alerts
  FOR EACH ROW EXECUTE FUNCTION trial.block_mutation();

-- Grants
-- portal_role: staff see and triage alerts at their sites (insert is for staff-raised alerts).
GRANT SELECT, INSERT ON trial.emergency_alerts TO portal_role;
GRANT UPDATE (status, acknowledged_by, acknowledged_at, resolved_by, resolved_at, resolution_note)
  ON trial.emergency_alerts TO portal_role;
-- participant_role: raise and re-press their own alert (SOS button).
GRANT SELECT ON trial.emergency_alerts TO participant_role;
GRANT INSERT (participant_id, severity, source, message, keywords) ON trial.emergency_alerts TO participant_role;
GRANT UPDATE (press_count, last_pressed_at, message) ON trial.emergency_alerts TO participant_role;
-- messaging_role: the router raises an alert on emergency keywords in WhatsApp. It looks for the participant's
-- open alert (SELECT), folds a repeat into it (UPDATE press_count, last_pressed_at, message) or inserts a new one.
GRANT SELECT ON trial.emergency_alerts TO messaging_role;
REVOKE INSERT ON trial.emergency_alerts FROM messaging_role;   -- drop any table-level INSERT from earlier manual grants
GRANT INSERT (participant_id, severity, source, message, keywords) ON trial.emergency_alerts TO messaging_role;
GRANT UPDATE (press_count, last_pressed_at, message) ON trial.emergency_alerts TO messaging_role;

-- Row-level security (once enabled, a role with no matching policy sees nothing)
ALTER TABLE trial.emergency_alerts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS site_scope ON trial.emergency_alerts;
CREATE POLICY site_scope ON trial.emergency_alerts FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));   -- inherits the site filter
DROP POLICY IF EXISTS own_alerts ON trial.emergency_alerts;
CREATE POLICY own_alerts ON trial.emergency_alerts FOR ALL TO participant_role
  USING (participant_id = current_setting('app.participant_id', true))
  WITH CHECK (participant_id = current_setting('app.participant_id', true));
-- messaging_role (the router): read all, insert, and update only while the alert is unresolved, so it can never
-- touch a resolved alert. Acknowledge and resolve stay with portal_role. The old names are dropped too:
-- messaging_all was the first version of the SELECT policy, messaging_insert and messaging_update came from
-- db/05_alerts_router_write.sql.
DROP POLICY IF EXISTS messaging_all ON trial.emergency_alerts;
DROP POLICY IF EXISTS messaging_select ON trial.emergency_alerts;
DROP POLICY IF EXISTS messaging_insert ON trial.emergency_alerts;
DROP POLICY IF EXISTS messaging_update ON trial.emergency_alerts;
CREATE POLICY messaging_select ON trial.emergency_alerts FOR SELECT TO messaging_role USING (true);
CREATE POLICY messaging_insert ON trial.emergency_alerts FOR INSERT TO messaging_role WITH CHECK (true);
CREATE POLICY messaging_update ON trial.emergency_alerts FOR UPDATE TO messaging_role
  USING (status <> 'resolved') WITH CHECK (status <> 'resolved');

------------------------------------------------------------------------------
-- 3. Telemetry retention
-- 05_telemetry.sql gave the services INSERT and SELECT only.
------------------------------------------------------------------------------
GRANT DELETE ON trial.telemetry_spans TO messaging_role;   -- nightly retention prune (tt-reconcile)
