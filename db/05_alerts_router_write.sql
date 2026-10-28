-- Lets tt-router (messaging_role) raise an emergency alert when a WhatsApp message contains emergency keywords,
-- so it shows in the staff portal's Emergency alerts widget. trial.emergency_alerts itself already exists.
-- messaging_role could only read it; this adds INSERT and a repeat-press UPDATE, nothing else. It cannot
-- acknowledge or resolve alerts (that stays with portal_role) and cannot touch a resolved alert.
-- Run once, as the master user:
--   python db/run_sql.py --cluster-arn <cluster ARN> --secret-arn <master secret ARN> db/05_alerts_router_write.sql

GRANT INSERT (participant_id, severity, source, message, keywords) ON trial.emergency_alerts TO messaging_role;
GRANT UPDATE (press_count, last_pressed_at, message) ON trial.emergency_alerts TO messaging_role;

CREATE POLICY messaging_insert ON trial.emergency_alerts FOR INSERT TO messaging_role WITH CHECK (true);
CREATE POLICY messaging_update ON trial.emergency_alerts FOR UPDATE TO messaging_role
  USING (status <> 'resolved') WITH CHECK (status <> 'resolved');
