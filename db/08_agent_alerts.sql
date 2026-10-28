-- 08: the agent's triage escalations raise dashboard alerts, not just a coordinator email.
-- escalate_to_coordinator (agent/trial_agent/tools.py) inserts one trial.emergency_alerts row per escalation
-- with source 'agent_triage', as the tt_agent login, scoped by RLS to the participant it is talking to.
-- Idempotent; safe to re-run. Canonical state lives in db/06_alerts_web.sql; this file is the live delta.

ALTER TABLE trial.emergency_alerts DROP CONSTRAINT IF EXISTS emergency_alerts_source_check;
ALTER TABLE trial.emergency_alerts ADD CONSTRAINT emergency_alerts_source_check
  CHECK (source IN ('sos_button','chat_keyword','agent_triage'));

-- agent_role: insert only, and only as the participant of its own session. No SELECT: the agent never
-- reads other alerts, and the no-delete trigger plus missing UPDATE grant keep rows append-only to it.
GRANT INSERT (participant_id, severity, source, message) ON trial.emergency_alerts TO agent_role;
DROP POLICY IF EXISTS agent_insert ON trial.emergency_alerts;
CREATE POLICY agent_insert ON trial.emergency_alerts FOR INSERT TO agent_role
  WITH CHECK (participant_id = current_setting('app.participant_id', true));
