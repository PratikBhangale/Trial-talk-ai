-- Drops everything 01-04 created, so the scripts can be re-run from scratch. Run as the master user.
-- Destroys all data in pii and trial.

DROP SCHEMA IF EXISTS tt_test CASCADE;
DROP SCHEMA IF EXISTS trial CASCADE;
DROP SCHEMA IF EXISTS pii CASCADE;

DROP ROLE IF EXISTS tt_agent, tt_messaging, tt_portal, tt_participant;
DROP ROLE IF EXISTS agent_role, messaging_role, portal_role, participant_role;
