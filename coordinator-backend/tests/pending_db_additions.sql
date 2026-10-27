-- What the backend needs on top of db/01_schema.sql and db/02_roles_rls.sql, for the participant website features
-- db/ has no room for yet: web chat, seeing staff replies to requests, and emergency alerts (the SOS button).
--
-- Applied only by the local test environment (tests/local_env.py), so the test portals keep those features.
-- It is NOT applied to Aurora. These widen participant_role, so they are a proposal for the db/ owners;
-- delete this file to see the test portals with db/ exactly as it is.

------------------------------------------------------------------------------
-- 1. A channel for rows that come from the website (chat, requests, staff replies to requests)
------------------------------------------------------------------------------
ALTER TABLE trial.communications DROP CONSTRAINT communications_channel_check;
ALTER TABLE trial.communications ADD CONSTRAINT communications_channel_check
  CHECK (channel IN ('whatsapp','email','web'));

------------------------------------------------------------------------------
-- 2. Participants read their own web rows: chat history, their requests, and staff replies
--    (msg_type 'request_reply', linked to the request through parsed_action.reply_to_comm_id)
------------------------------------------------------------------------------
GRANT SELECT (comm_id, participant_id, channel, direction, msg_type, subject, body_text, parsed_action, created_at)
  ON trial.communications TO participant_role;
CREATE POLICY own_web_messages ON trial.communications FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true) AND channel = 'web');

------------------------------------------------------------------------------
-- 3. Participants log their own web chat, with the agent's usage on its replies
--    (db/'s own_requests policy already allows their participant_request rows)
------------------------------------------------------------------------------
GRANT INSERT (session_id, latency_ms, input_tokens, output_tokens, model_id) ON trial.communications TO participant_role;
CREATE POLICY own_web_chat ON trial.communications FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true) AND channel = 'web' AND msg_type = 'text');

------------------------------------------------------------------------------
-- 4. trial.emergency_alerts: one row per emergency (SOS button, or emergency keywords in web chat).
--    Alerts need an acknowledge / resolve lifecycle that append-only clinical_events cannot give them, and
--    participant_role can neither insert nor read AE rows. Repeat presses fold into the open alert (press_count).
------------------------------------------------------------------------------
CREATE TABLE trial.emergency_alerts (
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
CREATE INDEX ON trial.emergency_alerts (status, created_at DESC);
CREATE INDEX ON trial.emergency_alerts (participant_id, created_at DESC);

CREATE TRIGGER emergency_alerts_no_delete
  BEFORE DELETE ON trial.emergency_alerts
  FOR EACH ROW EXECUTE FUNCTION trial.block_mutation();

-- Participants raise and re-press their own; staff triage; the router and reminder Lambdas can only read.
GRANT SELECT, INSERT ON trial.emergency_alerts TO portal_role;
GRANT UPDATE (status, acknowledged_by, acknowledged_at, resolved_by, resolved_at, resolution_note)
  ON trial.emergency_alerts TO portal_role;
GRANT SELECT ON trial.emergency_alerts TO messaging_role, participant_role;
GRANT INSERT (participant_id, severity, source, message, keywords) ON trial.emergency_alerts TO participant_role;
GRANT UPDATE (press_count, last_pressed_at, message) ON trial.emergency_alerts TO participant_role;

ALTER TABLE trial.emergency_alerts ENABLE ROW LEVEL SECURITY;
CREATE POLICY site_scope ON trial.emergency_alerts FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));
CREATE POLICY own_alerts ON trial.emergency_alerts FOR ALL TO participant_role
  USING (participant_id = current_setting('app.participant_id', true))
  WITH CHECK (participant_id = current_setting('app.participant_id', true));
CREATE POLICY messaging_all ON trial.emergency_alerts FOR SELECT TO messaging_role USING (true);
