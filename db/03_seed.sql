-- Synthetic test data. Run after 02_roles_rls.sql, as the master user.
-- Fixed IDs so tests and Lambda test events can reference them:
--   events     e0000000-0000-4000-8000-0000000000NN
--   comms      c0000000-0000-4000-8000-0000000000NN
--   reminders  a0000000-0000-4000-8000-0000000000NN
-- Two sites so RLS can be tested: RIVERSIDE (P-0142, P-0143) and LAKEVIEW (P-0201).

INSERT INTO pii.participants (participant_id, full_name, email, phone_e164, date_of_birth,
  address_line1, city, state, postal_code, travel_radius_miles, preferred_language, timezone,
  study_id, site_id, status, whatsapp_opt_in, opt_in_at)
VALUES
  ('P-0142','Maria Lopez','maria.l@example.com','+17325550142','1968-04-12',
   '22 Oak Ave','Springfield','IL','62704',25,'es','America/Chicago',
   'KOA-301','RIVERSIDE','enrolled',true,'2026-09-02 14:10Z'),
  ('P-0143','Sam Okafor','sam.o@example.com','+17325550143','1975-11-30',
   '9 Elm St','Springfield','IL','62701',15,'en','America/Chicago',
   'KOA-301','RIVERSIDE','prospect',true,'2026-10-18 09:00Z'),
  ('P-0201','Lee Chen','lee.c@example.com','+13125550201','1959-02-07',
   '400 Lake Shore Dr','Chicago','IL','60611',10,'en','America/Chicago',
   'KOA-301','LAKEVIEW','enrolled',true,'2026-09-10 16:00Z');

-- Inbound WhatsApp message that produced the symptom
INSERT INTO trial.communications (comm_id, participant_id, channel, direction, msg_type, from_addr,
  body_text, provider_message_id, created_at)
VALUES ('c0000000-0000-4000-8000-000000000001','P-0142','whatsapp','inbound','text','+17325550142',
  '4-6. also a rash near the injection spot since yesterday','wamid.SEED001','2026-10-20 13:02Z');

INSERT INTO trial.clinical_events (event_id, participant_id, record_type, status, source, visit_code,
  scheduled_at, window_start, window_end, details, created_by)
VALUES ('e0000000-0000-4000-8000-000000000001','P-0142','VISIT','scheduled','portal','WK04',
  '2026-10-29 15:00Z','2026-10-27','2026-10-31','{"location":"Riverside Clinic, Room 2"}',
  'staff:coordinator@riverside-clinic.org');

-- Symptom, then a second version with the photo details, then the AE that escalates it
INSERT INTO trial.clinical_events (event_id, participant_id, record_type, status, source, source_comm_id,
  verbatim_text, summary, pain_score_band, body_location, onset_date, severity_hint, created_by,
  occurred_at, created_at)
VALUES ('e0000000-0000-4000-8000-000000000002','P-0142','SYMPTOM','logged','whatsapp',
  'c0000000-0000-4000-8000-000000000001',
  'also a rash near the injection spot since yesterday','Rash at injection site','4-6','injection site',
  '2026-10-19','mild','agent','2026-10-20 13:02Z','2026-10-20 13:02:30Z');

INSERT INTO trial.clinical_events (event_id, participant_id, record_type, status, source, verbatim_text,
  summary, pain_score_band, body_location, onset_date, characteristics, severity_hint, attachment_s3_key,
  supersedes_event_id, change_reason, created_by, occurred_at, created_at)
VALUES ('e0000000-0000-4000-8000-000000000003','P-0142','SYMPTOM','escalated','whatsapp',
  'also a rash near the injection spot since yesterday','Itchy rash at injection site, not spreading',
  '4-6','injection site','2026-10-19','{itchy,not_spreading}','mild','s3://tt-media/P-0142/2026-10-20/rash.jpg',
  'e0000000-0000-4000-8000-000000000002','participant added details','agent',
  '2026-10-20 13:02Z','2026-10-20 13:05Z');

INSERT INTO trial.clinical_events (event_id, participant_id, record_type, status, source, parent_event_id,
  summary, onset_date, severity_hint, details, created_by, occurred_at, created_at)
VALUES ('e0000000-0000-4000-8000-000000000004','P-0142','AE','reported','whatsapp',
  'e0000000-0000-4000-8000-000000000003','Itchy rash at injection site, not spreading','2026-10-19','mild',
  '{"reported_to_pi_at":"2026-10-20T13:06:00Z"}','agent','2026-10-20 13:06Z','2026-10-20 13:06Z');

-- Reminders: recurring evening dose, one-time 24h visit reminder
INSERT INTO trial.reminders (reminder_id, participant_id, reminder_type, label, template_name,
  template_language, template_params, buttons, schedule_kind, cron_expr, timezone, starts_on, ends_on,
  follow_up_after_min, escalate_after_misses, next_send_at, created_by)
VALUES ('a0000000-0000-4000-8000-000000000001','P-0142','dose','Evening dose','dose_reminder_v2','es',
  '{"1":"evening"}','{Taken,Skipped,"Remind me in 30 min"}','recurring','cron(0 20 * * ? *)',
  'America/Chicago','2026-09-03','2027-03-03',120,3,now() + interval '1 day',
  'staff:coordinator@riverside-clinic.org');

INSERT INTO trial.reminders (reminder_id, participant_id, reminder_type, label, template_name,
  template_language, template_params, buttons, schedule_kind, send_at, timezone, visit_event_id,
  next_send_at, created_by)
VALUES ('a0000000-0000-4000-8000-000000000002','P-0142','visit','Week 4 visit, 24h','visit_reminder_24h','es',
  '{"1":"Oct 29","2":"10:00 AM","3":"Riverside Clinic"}','{Confirm,Reschedule}','one_time',
  '2026-10-28 15:00Z','America/Chicago','e0000000-0000-4000-8000-000000000001','2026-10-28 15:00Z',
  'staff:coordinator@riverside-clinic.org');

INSERT INTO trial.reminders (reminder_id, participant_id, reminder_type, label, template_name,
  template_params, buttons, schedule_kind, cron_expr, timezone, starts_on, ends_on, created_by)
VALUES ('a0000000-0000-4000-8000-000000000003','P-0201','dose','Morning dose','dose_reminder_v2',
  '{"1":"morning"}','{Taken,Skipped,"Remind me in 30 min"}','recurring','cron(0 8 * * ? *)',
  'America/Chicago','2026-09-11','2027-03-11','staff:coordinator@lakeview-health.org');

-- Dose history, relative to now() so the "3+ missed in 7 days" query returns P-0142
INSERT INTO trial.clinical_events (event_id, participant_id, record_type, source, reminder_id, dose_label,
  scheduled_at, response, details, created_by)
VALUES
  ('e0000000-0000-4000-8000-000000000005','P-0142','DOSE','scheduler','a0000000-0000-4000-8000-000000000001',
   'evening', now() - interval '4 days', 'taken', '{}', 'reminder-lambda'),
  ('e0000000-0000-4000-8000-000000000006','P-0142','DOSE','scheduler','a0000000-0000-4000-8000-000000000001',
   'evening', now() - interval '3 days', 'no_response', '{}', 'reminder-lambda'),
  ('e0000000-0000-4000-8000-000000000007','P-0142','DOSE','scheduler','a0000000-0000-4000-8000-000000000001',
   'evening', now() - interval '2 days', 'no_response', '{}', 'reminder-lambda'),
  ('e0000000-0000-4000-8000-000000000008','P-0142','DOSE','scheduler','a0000000-0000-4000-8000-000000000001',
   'evening', now() - interval '1 day', 'no_response', '{}', 'reminder-lambda');

-- LAKEVIEW participant's visit (must stay invisible to RIVERSIDE staff and to P-0142's agent)
INSERT INTO trial.clinical_events (event_id, participant_id, record_type, status, source, visit_code,
  scheduled_at, window_start, window_end, created_by)
VALUES ('e0000000-0000-4000-8000-000000000009','P-0201','VISIT','scheduled','portal','WK04',
  '2026-10-30 14:00Z','2026-10-28','2026-11-01','staff:coordinator@lakeview-health.org');

-- Messages around the AE loop and reminders
INSERT INTO trial.communications (comm_id, participant_id, channel, direction, msg_type, to_addr, body_text,
  provider_message_id, delivery_status, session_id, model_id, input_tokens, output_tokens, latency_ms,
  tool_calls, guardrail_action, created_at)
VALUES ('c0000000-0000-4000-8000-000000000002','P-0142','whatsapp','outbound','text','+17325550142',
  'Gracias, Maria. He enviado esto al equipo del Dr. Patel.','wamid.SEED002','sent',
  'wa-P-0142-2026-10-20','anthropic.claude-sonnet',4210,118,2350,3,'NONE','2026-10-20 13:06:10Z');

INSERT INTO trial.communications (comm_id, participant_id, channel, direction, msg_type, from_addr, to_addr,
  subject, body_text, related_event_id, provider_message_id, created_at)
VALUES ('c0000000-0000-4000-8000-000000000003','P-0142','email','outbound','report',
  'reports@trialtalk.dev','dr.patel@riverside-clinic.org','[TT P-0142 AE-7f3a] Possible adverse event',
  'Participant P-0142 reports an itchy rash at the injection site since 2026-10-19.',
  'e0000000-0000-4000-8000-000000000004','ses-out-0001','2026-10-20 13:06:20Z');

INSERT INTO trial.communications (comm_id, participant_id, channel, direction, msg_type, from_addr, subject,
  body_text, parsed_action, related_event_id, s3_raw_key, provider_message_id, created_at)
VALUES ('c0000000-0000-4000-8000-000000000004','P-0142','email','inbound','staff_reply',
  'dr.patel@riverside-clinic.org','Re: [TT P-0142 AE-7f3a] Possible adverse event',
  'Apply hydrocortisone, come in Thursday.',
  '{"intent":"relay_and_book","instruction":"Apply hydrocortisone cream","booking":"2026-10-22"}',
  'e0000000-0000-4000-8000-000000000004','s3://tt-mail/in/ses-77c2.eml','ses-77c2','2026-10-20 15:40Z');

INSERT INTO trial.communications (comm_id, participant_id, channel, direction, msg_type, template_name,
  to_addr, reminder_id, provider_message_id, delivery_status, created_at)
VALUES ('c0000000-0000-4000-8000-000000000005','P-0142','whatsapp','outbound','template','dose_reminder_v2',
  '+17325550142','a0000000-0000-4000-8000-000000000001','wamid.SEED005','delivered', now() - interval '1 day');
