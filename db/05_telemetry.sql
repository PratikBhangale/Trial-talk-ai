-- TrialTalk telemetry: span storage for OpenTelemetry traces. Run after 02_roles_rls.sql, as the master user.
--
-- NO PHI, BY DESIGN. This table holds timing and routing metadata only (span names, ids, durations, status
-- and a small set of allowlisted attributes such as http.route or http.status_code). Nothing that identifies
-- a participant, and no message text, may be written here. The allowlist is enforced by the exporter in
-- lambdas/common/telemetry.py: any attribute not on it is dropped before the INSERT. Because the table is
-- PHI-free it has no row-level security and no participant/site scoping.

------------------------------------------------------------------------------
-- trial.telemetry_spans
------------------------------------------------------------------------------
CREATE TABLE trial.telemetry_spans (
  span_id      text PRIMARY KEY,                      -- 16 hex chars from the tracer
  trace_id     text NOT NULL,                         -- 32 hex chars; groups the spans of one request
  parent_id    text,                                  -- NULL for a root span
  name         text NOT NULL,                         -- 'GET /participants', 'agent.invoke', ...
  service      text NOT NULL,                         -- which Lambda/service emitted it
  kind         text NOT NULL DEFAULT 'INTERNAL',      -- OTel span kind: INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER
  start_ts     timestamptz NOT NULL,
  duration_ms  numeric NOT NULL,
  status       text NOT NULL DEFAULT 'UNSET',         -- OTel status: UNSET, OK, ERROR
  attrs        jsonb NOT NULL DEFAULT '{}'::jsonb,    -- allowlisted attributes only (see header)
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX telemetry_spans_trace_idx ON trial.telemetry_spans (trace_id);
CREATE INDEX telemetry_spans_start_idx ON trial.telemetry_spans (start_ts DESC);
CREATE INDEX telemetry_spans_name_start_idx ON trial.telemetry_spans (name, start_ts DESC);

------------------------------------------------------------------------------
-- Grants. Services write spans; only the staff portal reads them back. No RLS (no PHI).
------------------------------------------------------------------------------
GRANT INSERT ON trial.telemetry_spans TO messaging_role, portal_role;
GRANT SELECT ON trial.telemetry_spans TO portal_role;
