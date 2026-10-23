-- TrialTalk roles, grants and row-level security. Run after 01_schema.sql, as the master user.
--
-- Group roles (NOLOGIN) carry the privileges. Each Lambda connects through the RDS Data API
-- with its own LOGIN user, whose Secrets Manager secret only that Lambda can read.
-- The master user owns the tables and bypasses RLS, no Lambda should ever use it.
--
-- Session context the Lambda must set inside each Data API transaction:
--   agent_role, participant_role : SELECT set_config('app.participant_id', :pid, true)
--   portal_role                  : SELECT set_config('app.site_ids', 'RIVERSIDE,LAKEVIEW', true)
-- If unset, those roles see zero rows (fail closed).

------------------------------------------------------------------------------
-- Roles
------------------------------------------------------------------------------
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['agent_role','messaging_role','portal_role','participant_role'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

-- Login users for the Data API. Replace the passwords, then store each as a
-- Secrets Manager secret: {"username": "tt_agent", "password": "..."}.
CREATE ROLE tt_agent       LOGIN PASSWORD 'CHANGE_ME_agent'       IN ROLE agent_role;       -- agent tool Lambdas
CREATE ROLE tt_messaging   LOGIN PASSWORD 'CHANGE_ME_messaging'   IN ROLE messaging_role;   -- router, reminder, email parser
CREATE ROLE tt_portal      LOGIN PASSWORD 'CHANGE_ME_portal'      IN ROLE portal_role;      -- staff API Lambda
CREATE ROLE tt_participant LOGIN PASSWORD 'CHANGE_ME_participant' IN ROLE participant_role; -- participant website API

GRANT USAGE ON SCHEMA pii, trial TO agent_role, messaging_role, portal_role, participant_role;

------------------------------------------------------------------------------
-- Agent: no names, emails, phones or addresses, no staff assessment columns
------------------------------------------------------------------------------
GRANT SELECT (participant_id, preferred_language, timezone, status, site_id, study_id)
  ON pii.participants TO agent_role;
GRANT SELECT ON trial.clinical_events, trial.current_events, trial.communications TO agent_role;
GRANT INSERT (participant_id, record_type, status, occurred_at, source, parent_event_id, source_comm_id,
  verbatim_text, summary, pain_score_band, body_location, onset_date, characteristics, severity_hint,
  urgent_keywords, reason_code, consent_section, topic, flag_for_follow_up, attachment_s3_key,
  visit_code, dose_label, scheduled_at, window_start, window_end, response, response_text, answered_by,
  details, reminder_id, supersedes_event_id, change_reason, created_by)
  ON trial.clinical_events TO agent_role;
GRANT SELECT ON trial.reminders TO agent_role;   -- read-only: agent can explain reminders, not change them
GRANT INSERT ON trial.communications TO agent_role;

------------------------------------------------------------------------------
-- Router, reminder, parser Lambdas
------------------------------------------------------------------------------
GRANT SELECT (participant_id, phone_e164, preferred_language, timezone, whatsapp_opt_in, site_id, status)
  ON pii.participants TO messaging_role;
-- SELECT is needed (not in the original plan) for: INSERT ... RETURNING comm_id,
-- UPDATE ... WHERE provider_message_id = ..., and finding the DOSE row a "Taken" tap supersedes.
GRANT SELECT, INSERT ON trial.clinical_events, trial.communications TO messaging_role;
GRANT SELECT ON trial.current_events TO messaging_role;
GRANT UPDATE (delivery_status, attachments, attachment_text) ON trial.communications TO messaging_role;
GRANT SELECT ON trial.reminders TO messaging_role;
GRANT UPDATE (next_send_at, last_sent_at, send_count, status) ON trial.reminders TO messaging_role;
-- STOP / BAJA handling turns messaging off in code
GRANT UPDATE (whatsapp_opt_in) ON pii.participants TO messaging_role;

------------------------------------------------------------------------------
-- Portal (RLS narrows to the user's sites)
------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON pii.participants TO portal_role;
GRANT SELECT, INSERT ON trial.clinical_events, trial.communications TO portal_role;
GRANT SELECT ON trial.current_events TO portal_role;
GRANT SELECT, INSERT, UPDATE ON trial.reminders TO portal_role;

------------------------------------------------------------------------------
-- Participant website: own data only, no staff columns
------------------------------------------------------------------------------
GRANT SELECT ON pii.participants TO participant_role;
GRANT UPDATE (email, address_line1, address_line2, city, state, postal_code, country,
  preferred_language, timezone, updated_at) ON pii.participants TO participant_role;
-- parent_event_id and supersedes_event_id added so the site can show the latest version of each record
GRANT SELECT (event_id, participant_id, record_type, status, occurred_at, verbatim_text, pain_score_band, body_location,
  onset_date, visit_code, scheduled_at, window_start, window_end, response, response_text,
  parent_event_id, supersedes_event_id)
  ON trial.clinical_events TO participant_role;
GRANT INSERT (participant_id, record_type, source, parent_event_id, scheduled_at, reason_code,
  verbatim_text, details, created_by) ON trial.clinical_events TO participant_role;
GRANT SELECT (reminder_id, participant_id, label, reminder_type, cron_expr, send_at, timezone, starts_on, ends_on,
  status, next_send_at) ON trial.reminders TO participant_role;
GRANT INSERT (participant_id, channel, direction, msg_type, subject, body_text)
  ON trial.communications TO participant_role;

-- Nobody is granted UPDATE/DELETE on clinical_events or DELETE elsewhere, the triggers in
-- 01_schema.sql also block these for the owner.
REVOKE UPDATE, DELETE ON trial.clinical_events FROM PUBLIC;
REVOKE DELETE ON trial.communications, trial.reminders FROM PUBLIC;

------------------------------------------------------------------------------
-- Row-level security
-- Once RLS is enabled, a role with no matching policy sees nothing, so agent_role and
-- messaging_role need their own policies (missing from the original plan).
------------------------------------------------------------------------------
ALTER TABLE pii.participants     ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.clinical_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.communications ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial.reminders      ENABLE ROW LEVEL SECURITY;

-- Portal: user's sites only
CREATE POLICY site_scope ON pii.participants FOR ALL TO portal_role
  USING (site_id = ANY (string_to_array(current_setting('app.site_ids', true), ',')));
CREATE POLICY site_scope ON trial.clinical_events FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));  -- inherits site filter
CREATE POLICY site_scope ON trial.communications FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));  -- digests (NULL participant) hidden
CREATE POLICY site_scope ON trial.reminders FOR ALL TO portal_role
  USING (participant_id IN (SELECT participant_id FROM pii.participants));

-- Agent: the session's participant only (router passes participant_id, the tool Lambda sets it)
CREATE POLICY session_participant ON pii.participants FOR SELECT TO agent_role
  USING (participant_id = current_setting('app.participant_id', true));
CREATE POLICY session_participant ON trial.clinical_events FOR ALL TO agent_role
  USING (participant_id = current_setting('app.participant_id', true));
CREATE POLICY session_participant ON trial.communications FOR ALL TO agent_role
  USING (participant_id = current_setting('app.participant_id', true));
CREATE POLICY session_participant ON trial.reminders FOR SELECT TO agent_role
  USING (participant_id = current_setting('app.participant_id', true));

-- Messaging Lambdas: look up by phone before the participant is known, so all rows,
-- column grants above keep them away from names and addresses.
CREATE POLICY messaging_all ON pii.participants     FOR ALL TO messaging_role USING (true) WITH CHECK (true);
CREATE POLICY messaging_all ON trial.clinical_events FOR ALL TO messaging_role USING (true) WITH CHECK (true);
CREATE POLICY messaging_all ON trial.communications FOR ALL TO messaging_role USING (true) WITH CHECK (true);
CREATE POLICY messaging_all ON trial.reminders      FOR ALL TO messaging_role USING (true) WITH CHECK (true);

-- Participants: own rows only (app.participant_id set from the Cognito token)
CREATE POLICY own_record ON pii.participants FOR ALL TO participant_role
  USING (participant_id = current_setting('app.participant_id', true));
CREATE POLICY own_history ON trial.clinical_events FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true)
         AND record_type IN ('VISIT','VISIT_CHANGE','SYMPTOM','DOSE','CONSENT_Q'));
CREATE POLICY own_reschedule ON trial.clinical_events FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true) AND record_type = 'VISIT_CHANGE');
CREATE POLICY own_reminders ON trial.reminders FOR SELECT TO participant_role
  USING (participant_id = current_setting('app.participant_id', true));
CREATE POLICY own_requests ON trial.communications FOR INSERT TO participant_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true) AND msg_type = 'participant_request');
