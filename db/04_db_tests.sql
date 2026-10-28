-- Database-level tests: permissions, RLS, append-only triggers, dedupe, versioning.
-- Run after 03_seed.sql, as the master user. Re-runnable, leaves no test rows behind.
--
-- A failing test raises an error starting with "FAIL Txx" (or an unexpected SQL error) and stops the run.
-- Passing tests are recorded in tt_test.results, the last statement lists them (expect 20 rows).

GRANT agent_role, messaging_role, portal_role, participant_role TO CURRENT_USER;  -- lets the master SET ROLE

CREATE SCHEMA IF NOT EXISTS tt_test;
GRANT USAGE ON SCHEMA tt_test TO PUBLIC;
CREATE TABLE IF NOT EXISTS tt_test.results (
  test_id text, note text, at timestamptz NOT NULL DEFAULT clock_timestamp());
GRANT SELECT, INSERT ON tt_test.results TO PUBLIC;
TRUNCATE tt_test.results;

-- Switch to a role with the session context a Lambda would set. NULL role = master.
CREATE OR REPLACE FUNCTION tt_test.become(p_role text, p_participant text DEFAULT '', p_sites text DEFAULT '')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  RESET ROLE;
  PERFORM set_config('app.participant_id', p_participant, true);
  PERFORM set_config('app.site_ids', p_sites, true);
  IF p_role IS NOT NULL THEN
    EXECUTE format('SET LOCAL ROLE %I', p_role);
  END IF;
END $$;

CREATE OR REPLACE FUNCTION tt_test.pass(p_test text, p_note text) RETURNS void LANGUAGE sql AS $$
  INSERT INTO tt_test.results (test_id, note) VALUES (p_test, p_note);
$$;

-- Positive write tests end with RAISE ... ERRCODE 'TTRBK' inside a sub-block so the row is rolled back.

------------------------------------------------------------------------------
-- Agent (agent_role, app.participant_id = P-0142)
------------------------------------------------------------------------------
DO $$ BEGIN
  PERFORM tt_test.become('agent_role', 'P-0142');
  BEGIN
    PERFORM full_name FROM pii.participants;
    RAISE EXCEPTION 'FAIL T01: agent_role read full_name';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    PERFORM phone_e164 FROM pii.participants;
    RAISE EXCEPTION 'FAIL T01: agent_role read phone_e164';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T01', 'agent cannot read name or phone');
END $$;

DO $$
DECLARE n_p int; n_own int; n_other int; n_comm_other int; n_rem_other int;
BEGIN
  PERFORM tt_test.become('agent_role', 'P-0142');
  SELECT count(*) INTO n_p FROM pii.participants;
  SELECT count(*) FILTER (WHERE participant_id = 'P-0142'),
         count(*) FILTER (WHERE participant_id <> 'P-0142')
    INTO n_own, n_other FROM trial.clinical_events;
  SELECT count(*) INTO n_comm_other FROM trial.communications WHERE participant_id IS DISTINCT FROM 'P-0142';
  SELECT count(*) INTO n_rem_other FROM trial.reminders WHERE participant_id <> 'P-0142';
  IF n_p <> 1 OR n_own = 0 OR n_other + n_comm_other + n_rem_other > 0 THEN
    RAISE EXCEPTION 'FAIL T02: participants=% own_events=% other_events=% other_comms=% other_reminders=%',
      n_p, n_own, n_other, n_comm_other, n_rem_other;
  END IF;
  PERFORM tt_test.pass('T02', 'agent sees only its session participant');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become('agent_role', 'P-0142');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text, created_by)
    VALUES ('P-0201', 'SYMPTOM', 'whatsapp', 'injected write', 'agent');
    RAISE EXCEPTION 'FAIL T03: agent wrote an event for another participant';
  EXCEPTION WHEN insufficient_privilege THEN NULL;   -- RLS WITH CHECK violation
  END;
  PERFORM tt_test.pass('T03', 'agent cannot write for another participant');
END $$;

DO $$
DECLARE v uuid;
BEGIN
  PERFORM tt_test.become('agent_role', 'P-0142');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, source_comm_id, verbatim_text,
      pain_score_band, characteristics, created_by)
    VALUES ('P-0142', 'SYMPTOM', 'whatsapp', 'c0000000-0000-4000-8000-000000000001', 'knee sore today',
      '4-6', '{sore}', 'agent')
    RETURNING event_id INTO v;
    IF v IS NULL THEN RAISE EXCEPTION 'FAIL T04: no event_id returned'; END IF;
    RAISE EXCEPTION 'rollback' USING ERRCODE = 'TTRBK';
  EXCEPTION WHEN SQLSTATE 'TTRBK' THEN NULL;
  END;
  PERFORM tt_test.pass('T04', 'agent can log a symptom for its participant (RETURNING works)');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become('agent_role', 'P-0142');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, ae_term, severity_grade, created_by)
    VALUES ('P-0142', 'AE', 'whatsapp', 'Injection site rash', 2, 'agent');
    RAISE EXCEPTION 'FAIL T05: agent wrote staff assessment columns';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    UPDATE trial.reminders SET status = 'paused' WHERE participant_id = 'P-0142';
    RAISE EXCEPTION 'FAIL T05: agent updated a reminder';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T05', 'agent cannot write AE assessment or change reminders');
END $$;

DO $$
DECLARE n int;
BEGIN
  PERFORM tt_test.become('agent_role', '');   -- Lambda forgot set_config
  SELECT (SELECT count(*) FROM pii.participants) + (SELECT count(*) FROM trial.clinical_events)
       + (SELECT count(*) FROM trial.communications) + (SELECT count(*) FROM trial.reminders) INTO n;
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL T06: % rows visible with no participant context', n; END IF;
  PERFORM tt_test.pass('T06', 'agent with no participant context sees nothing');
END $$;

------------------------------------------------------------------------------
-- Messaging Lambdas (messaging_role: router, reminder, parser)
------------------------------------------------------------------------------
DO $$
DECLARE pid text;
BEGIN
  PERFORM tt_test.become('messaging_role');
  SELECT participant_id INTO pid FROM pii.participants WHERE phone_e164 = '+17325550142';
  IF pid IS DISTINCT FROM 'P-0142' THEN RAISE EXCEPTION 'FAIL T07: phone lookup returned %', pid; END IF;
  BEGIN
    PERFORM full_name FROM pii.participants WHERE participant_id = pid;
    RAISE EXCEPTION 'FAIL T07: messaging_role read full_name';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T07', 'router can look up by phone but not read name');
END $$;

DO $$
DECLARE v uuid;
BEGIN
  PERFORM tt_test.become('messaging_role');
  INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, provider_message_id)
  VALUES ('P-0142', 'whatsapp', 'inbound', 'text', 'redelivered copy', 'wamid.SEED001')
  ON CONFLICT (provider_message_id) DO NOTHING
  RETURNING comm_id INTO v;
  IF v IS NOT NULL THEN RAISE EXCEPTION 'FAIL T08: duplicate wamid inserted as %', v; END IF;
  PERFORM tt_test.pass('T08', 'duplicate wamid returns no row (dedupe)');
END $$;

DO $$
DECLARE n int;
BEGIN
  PERFORM tt_test.become('messaging_role');
  BEGIN
    UPDATE trial.communications SET delivery_status = 'read' WHERE provider_message_id = 'wamid.SEED002';
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL T09: status update touched % rows', n; END IF;
    RAISE EXCEPTION 'rollback' USING ERRCODE = 'TTRBK';
  EXCEPTION WHEN SQLSTATE 'TTRBK' THEN NULL;
  END;
  BEGIN
    UPDATE trial.communications SET body_text = 'tampered' WHERE provider_message_id = 'wamid.SEED002';
    RAISE EXCEPTION 'FAIL T09: messaging_role rewrote body_text';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T09', 'router can update delivery_status only');
END $$;

DO $$
DECLARE v_old timestamptz; v_new timestamptz;
BEGIN
  PERFORM tt_test.become('messaging_role');
  SELECT updated_at INTO v_old FROM trial.reminders WHERE reminder_id = 'a0000000-0000-4000-8000-000000000001';
  BEGIN
    UPDATE trial.reminders SET send_count = send_count + 1, last_sent_at = now()
    WHERE reminder_id = 'a0000000-0000-4000-8000-000000000001'
    RETURNING updated_at INTO v_new;
    IF v_new IS NULL OR v_new <= v_old THEN
      RAISE EXCEPTION 'FAIL T10: updated_at not bumped (old %, new %)', v_old, v_new;
    END IF;
    RAISE EXCEPTION 'rollback' USING ERRCODE = 'TTRBK';
  EXCEPTION WHEN SQLSTATE 'TTRBK' THEN NULL;
  END;
  PERFORM tt_test.pass('T10', 'reminder Lambda can update counters; updated_at trigger fires');
END $$;

------------------------------------------------------------------------------
-- Staff portal (portal_role, app.site_ids = RIVERSIDE)
------------------------------------------------------------------------------
DO $$
DECLARE n_lake int; n_river int;
BEGIN
  PERFORM tt_test.become('portal_role', '', 'RIVERSIDE');
  SELECT count(*) FILTER (WHERE site_id = 'LAKEVIEW'), count(*) FILTER (WHERE site_id = 'RIVERSIDE')
    INTO n_lake, n_river FROM pii.participants;
  IF n_lake > 0 OR n_river <> 2 THEN RAISE EXCEPTION 'FAIL T11: lakeview=% riverside=%', n_lake, n_river; END IF;
  SELECT count(*) INTO n_lake FROM trial.clinical_events WHERE participant_id = 'P-0201';
  IF n_lake > 0 THEN RAISE EXCEPTION 'FAIL T11: % LAKEVIEW events visible', n_lake; END IF;
  SELECT count(*) INTO n_lake FROM trial.reminders WHERE participant_id = 'P-0201';
  IF n_lake > 0 THEN RAISE EXCEPTION 'FAIL T11: % LAKEVIEW reminders visible', n_lake; END IF;
  PERFORM tt_test.pass('T11', 'portal sees only its own sites');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become('portal_role', '', 'RIVERSIDE');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, visit_code, created_by)
    VALUES ('P-0201', 'VISIT', 'portal', 'WK08', 'staff:coordinator@riverside-clinic.org');
    RAISE EXCEPTION 'FAIL T12: RIVERSIDE staff wrote a LAKEVIEW event';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    UPDATE trial.clinical_events SET staff_notes = 'edit' WHERE event_id = 'e0000000-0000-4000-8000-000000000001';
    RAISE EXCEPTION 'FAIL T12: portal updated a clinical event';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T12', 'portal cannot write other sites or edit events in place');
END $$;

DO $$
DECLARE v uuid;
BEGIN
  PERFORM tt_test.become('portal_role', '', 'RIVERSIDE');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, status, source, parent_event_id, summary,
      onset_date, ae_term, severity_grade, serious, relatedness, expected, outcome,
      supersedes_event_id, change_reason, created_by)
    VALUES ('P-0142', 'AE', 'acknowledged', 'portal', 'e0000000-0000-4000-8000-000000000003',
      'Itchy rash at injection site, not spreading', '2026-10-19', 'Injection site rash', 1, false,
      'probable', true, 'recovering', 'e0000000-0000-4000-8000-000000000004', 'PI assessment',
      'staff:dr.patel@riverside-clinic.org')
    RETURNING event_id INTO v;
    IF NOT EXISTS (SELECT 1 FROM trial.current_events WHERE event_id = v)
       OR EXISTS (SELECT 1 FROM trial.current_events WHERE event_id = 'e0000000-0000-4000-8000-000000000004') THEN
      RAISE EXCEPTION 'FAIL T13: current_events did not switch to the new AE version';
    END IF;
    RAISE EXCEPTION 'rollback' USING ERRCODE = 'TTRBK';
  EXCEPTION WHEN SQLSTATE 'TTRBK' THEN NULL;
  END;
  PERFORM tt_test.pass('T13', 'PI assessment is a new AE version; current_events shows it');
END $$;

------------------------------------------------------------------------------
-- Participant website (participant_role, app.participant_id = P-0142)
------------------------------------------------------------------------------
DO $$
DECLARE n_ae int; n_visit int;
BEGIN
  PERFORM tt_test.become('participant_role', 'P-0142');
  SELECT count(*) FILTER (WHERE record_type = 'AE'), count(*) FILTER (WHERE record_type = 'VISIT')
    INTO n_ae, n_visit FROM trial.clinical_events;
  IF n_ae > 0 OR n_visit <> 1 THEN RAISE EXCEPTION 'FAIL T14: ae=% visit=%', n_ae, n_visit; END IF;
  BEGIN
    PERFORM staff_notes FROM trial.clinical_events;
    RAISE EXCEPTION 'FAIL T14: participant read staff_notes';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T14', 'participant sees own visits, not AEs or staff columns');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become('participant_role', 'P-0142');
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, parent_event_id, scheduled_at,
      reason_code, verbatim_text, created_by)
    VALUES ('P-0142', 'VISIT_CHANGE', 'portal', 'e0000000-0000-4000-8000-000000000001', '2026-10-30 15:00Z',
      'work', 'Can we do Friday instead?', 'participant:P-0142');
    RAISE EXCEPTION 'rollback' USING ERRCODE = 'TTRBK';
  EXCEPTION WHEN SQLSTATE 'TTRBK' THEN NULL;
  END;
  BEGIN
    INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text, created_by)
    VALUES ('P-0142', 'SYMPTOM', 'portal', 'typed on website', 'participant:P-0142');
    RAISE EXCEPTION 'FAIL T15: participant inserted a SYMPTOM';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T15', 'participant can request a reschedule, nothing else');
END $$;

------------------------------------------------------------------------------
-- Integrity: append-only and versioning (as the table owner, so only triggers stop it)
------------------------------------------------------------------------------
DO $$ BEGIN
  PERFORM tt_test.become(NULL);
  BEGIN
    UPDATE trial.clinical_events SET summary = 'x' WHERE event_id = 'e0000000-0000-4000-8000-000000000001';
    RAISE EXCEPTION 'FAIL T16: owner updated a clinical event';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM trial.clinical_events WHERE event_id = 'e0000000-0000-4000-8000-000000000001';
    RAISE EXCEPTION 'FAIL T16: owner deleted a clinical event';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM trial.reminders WHERE reminder_id = 'a0000000-0000-4000-8000-000000000003';
    RAISE EXCEPTION 'FAIL T16: owner deleted a reminder';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM trial.communications WHERE comm_id = 'c0000000-0000-4000-8000-000000000005';
    RAISE EXCEPTION 'FAIL T16: owner deleted a communication';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  PERFORM tt_test.pass('T16', 'append-only triggers hold even for the owner');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become(NULL);
  BEGIN   -- e0..02 is already superseded by e0..03
    INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text,
      supersedes_event_id, change_reason, created_by)
    VALUES ('P-0142', 'SYMPTOM', 'whatsapp', 'fork', 'e0000000-0000-4000-8000-000000000002', 'fork', 'agent');
    RAISE EXCEPTION 'FAIL T17: a version was superseded twice';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN   -- supersede a P-0142 SYMPTOM with a P-0201 row
    INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text,
      supersedes_event_id, change_reason, created_by)
    VALUES ('P-0201', 'SYMPTOM', 'whatsapp', 'cross', 'e0000000-0000-4000-8000-000000000003', 'cross', 'agent');
    RAISE EXCEPTION 'FAIL T17: superseded across participants';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN   -- correction without a reason
    INSERT INTO trial.clinical_events (participant_id, record_type, source, verbatim_text,
      supersedes_event_id, created_by)
    VALUES ('P-0142', 'SYMPTOM', 'whatsapp', 'no reason', 'e0000000-0000-4000-8000-000000000003', 'agent');
    RAISE EXCEPTION 'FAIL T17: correction without change_reason';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  PERFORM tt_test.pass('T17', 'versions form one chain, same participant/type, reason required');
END $$;

DO $$
DECLARE n int;
BEGIN
  PERFORM tt_test.become('messaging_role');
  SELECT count(*) INTO n FROM trial.current_events
  WHERE event_id IN ('e0000000-0000-4000-8000-000000000002', 'e0000000-0000-4000-8000-000000000003');
  IF n <> 1 OR NOT EXISTS (SELECT 1 FROM trial.current_events
                           WHERE event_id = 'e0000000-0000-4000-8000-000000000003') THEN
    RAISE EXCEPTION 'FAIL T18: current_events returned % versions of the symptom', n;
  END IF;
  PERFORM tt_test.pass('T18', 'current_events returns only the latest version');
END $$;

DO $$ BEGIN
  PERFORM tt_test.become('portal_role', '', 'RIVERSIDE');
  BEGIN
    INSERT INTO trial.reminders (participant_id, reminder_type, label, template_name, schedule_kind,
      timezone, created_by)
    VALUES ('P-0142', 'dose', 'Bad reminder', 'dose_reminder_v2', 'recurring', 'America/Chicago',
      'staff:coordinator@riverside-clinic.org');
    RAISE EXCEPTION 'FAIL T19: recurring reminder accepted without cron_expr';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  PERFORM tt_test.pass('T19', 'reminder schedule CHECK enforced');
END $$;

DO $$
DECLARE missing text;
BEGIN
  PERFORM tt_test.become(NULL);
  SELECT string_agg(column_name, ', ') INTO missing FROM information_schema.columns t
  WHERE t.table_schema = 'trial' AND t.table_name = 'clinical_events'
    AND NOT EXISTS (SELECT 1 FROM information_schema.columns v WHERE v.table_schema = 'trial'
                    AND v.table_name = 'current_events' AND v.column_name = t.column_name);
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'FAIL T20: current_events is missing %; recreate the view after adding columns', missing;
  END IF;
  PERFORM tt_test.pass('T20', 'current_events has every clinical_events column');
END $$;

RESET ROLE;
SELECT test_id, note, at FROM tt_test.results ORDER BY test_id;
