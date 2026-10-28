-- TrialTalk studies: makes trials first-class reference data. Run after 06_alerts_web.sql, as the master user.
--
-- Until now a study was only the free-text pii.participants.study_id ('KOA-301'). This file adds
-- trial.studies, one row per trial the clinic runs, so the portal can list trials, label them and break
-- KPIs down per study.
--
-- NO PHI, BY DESIGN. A study row is protocol metadata (id, name, phase, condition, description), the same for
-- every viewer. It is plain reference data: no row-level security, and every service role may read it.
-- The reference rows live in this file (section 2) so a local bootstrap gets them too.
--
-- IDEMPOTENT: CREATE TABLE IF NOT EXISTS, repeatable GRANTs, and INSERT ... ON CONFLICT DO UPDATE for the rows.

------------------------------------------------------------------------------
-- 1. trial.studies
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trial.studies (
  study_id     text PRIMARY KEY,                       -- 'KOA-301', matches pii.participants.study_id
  name         text NOT NULL,                          -- display name
  phase        text,                                   -- 'Phase 2', 'Phase 3', ...
  condition    text,                                   -- indication under study
  description  text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Reference data, no PHI: every service role reads it, none writes it (the master user owns the rows).
GRANT SELECT ON trial.studies TO portal_role, participant_role, agent_role, messaging_role;

------------------------------------------------------------------------------
-- 2. Reference rows
-- KOA-301 is the LIVE study (real participants on Aurora). The other four are the demo clinic's trials,
-- populated by db/seed_demo_clinic.py.
------------------------------------------------------------------------------
INSERT INTO trial.studies (study_id, name, phase, condition, description) VALUES
  ('KOA-301', 'KNEE-RELIEF: Oral Therapy for Knee Osteoarthritis Pain', 'Phase 3', 'Knee osteoarthritis',
   'Randomized, double-blind, placebo-controlled study of a once-daily oral therapy for moderate-to-severe knee '
   'osteoarthritis pain over 12 weeks, with daily dose check-ins and symptom reporting over WhatsApp.'),
  ('HTN-205', 'PRESSURE-LOW: Once-Daily Dual Therapy in Uncontrolled Hypertension', 'Phase 2', 'Essential hypertension',
   'Dose-ranging study of a once-daily dual-mechanism tablet in adults whose blood pressure stays above target '
   'on one agent; daily home BP readings and dizziness/oedema monitoring.'),
  ('DM2-410', 'GLYCO-STEADY: Once-Daily Oral Add-On in Type 2 Diabetes', 'Phase 3', 'Type 2 diabetes mellitus',
   'Randomized, active-comparator study of a once-daily oral tablet added to metformin over 26 weeks; dose '
   'reminders, glucose logging and gastrointestinal side-effect reporting.'),
  ('MIG-118', 'CALM-HEAD: Preventive Oral Therapy for Episodic Migraine', 'Phase 2', 'Episodic migraine',
   'Placebo-controlled study of a daily preventive tablet in adults with 4 to 14 migraine days a month; '
   'headache diary check-ins and rescue-medication tracking.'),
  ('AST-302', 'BREATHE-WELL: Add-On Inhaler for Moderate-to-Severe Asthma', 'Phase 3', 'Persistent asthma',
   'Randomized, double-blind study of a twice-daily add-on maintenance inhaler over 24 weeks; inhaler adherence, '
   'symptom scores and exacerbation reporting.')
ON CONFLICT (study_id) DO UPDATE SET
  name        = EXCLUDED.name,
  phase       = EXCLUDED.phase,
  condition   = EXCLUDED.condition,
  description = EXCLUDED.description;
