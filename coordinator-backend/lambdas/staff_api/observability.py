"""Observability endpoints for the staff portal: request/latency/token series, traces, drift, estimated cost.

Spans come from trial.telemetry_spans (written by every service; no PHI, so no site scoping there). Token totals
come from trial.communications, which is site-scoped by row-level security like the rest of the staff API.
The responses are aggregate numbers and span metadata only, never participant data.
"""

import json
import re
import time
from datetime import datetime, timezone

from common import db
from common.auth import Staff, require
from common.http import HttpError, Router, query_params

router = Router()

# Sonnet-class public list prices, USD per million tokens. Every cost in the API is an estimate from this constant.
PRICING = {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0}

MAX_HOURS = 168
TOKEN_DAYS = 14
REQUEST_BUCKET_S = 900     # 15 minutes
LATENCY_BUCKET_S = 3600    # 1 hour
CURRENT_S = 24 * 3600      # drift: current = last 24 h ...
BASELINE_S = 8 * 24 * 3600  # ... baseline = 24 h to 8 d ago
TRACE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ISO = "YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\""

# key, label, bad_direction (the direction that is a regression; "none" = informational only).
DRIFT_METRICS = [
    ("tokens_in_per_reply", "Input tokens per reply", "none"),
    ("tokens_out_per_reply", "Output tokens per reply", "none"),
    ("agent_p95_ms", "Agent p95 latency (ms)", "up"),
    ("reply_length_chars", "Reply length (chars)", "none"),
    ("kb_search_rate", "KB search rate", "down"),
    ("escalation_rate", "Escalation rate", "up"),
]


def est_cost(tokens_in, tokens_out) -> float:
    """Estimated USD for a token count, 4 decimals."""
    return round(((tokens_in or 0) * PRICING["input_usd_per_mtok"]
                  + (tokens_out or 0) * PRICING["output_usd_per_mtok"]) / 1_000_000, 4)


def _num(v):
    return None if v is None else float(v)


def drift_metrics(current: dict, baseline: dict) -> list[dict]:
    """The six drift cards. change_pct is None when either side is missing or the baseline is zero."""
    out = []
    for key, label, bad in DRIFT_METRICS:
        cur, base = _num(current.get(key)), _num(baseline.get(key))
        change = round((cur - base) / base * 100, 1) if cur is not None and base else None
        out.append({"key": key, "label": label, "current": cur, "baseline": base, "change_pct": change,
                    "bad_direction": bad})
    return out


# ------------------------------------------------------------------ helpers

def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _int_param(event: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = query_params(event).get(name)
    try:
        return min(max(int(raw), lo), hi) if raw not in (None, "") else default
    except ValueError:
        raise HttpError(400, f"{name} must be a number")


def _r1(v):
    return None if v is None else round(float(v), 1)


def _buckets(rows: list[dict], since: int, now: int, step: int, empty: dict) -> list[dict]:
    """One entry per `step` bucket from `since` to `now`, zero-filled where there were no spans."""
    by_bucket = {int(r["b"]): r for r in rows}
    return [{"t": _iso(b), **{k: (by_bucket[b][k] if b in by_bucket else v) for k, v in empty.items()}}
            for b in range(since // step * step, now // step * step + 1, step)]


# ------------------------------------------------------------------ roots
#
# A trace's root is its earliest stored span (start_ts, then span_id) among the spans that have no stored parent:
# parent_id IS NULL, or no row of the same trace has span_id = parent_id (the dev server keeps request spans in
# memory while the agent's spans are stored; in AWS a flush can miss the root). One root per trace, so nothing is
# counted twice. A request failed when ANY span of its trace is ERROR, the traces list's rule too: a WhatsApp root
# ends OK before routing, and an agent failure marks only agent.call / agent.invoke.

_ROOTS = (
    "SELECT DISTINCT ON (c.trace_id) c.* FROM trial.telemetry_spans c "
    "WHERE {scope}(c.parent_id IS NULL OR NOT EXISTS (SELECT 1 FROM trial.telemetry_spans p "
    "      WHERE p.trace_id = c.trace_id AND p.span_id = c.parent_id)) "
    "ORDER BY c.trace_id, c.start_ts, c.span_id")

# Roots that started at or after :since, each with its trace's error flag. Only traces with a span in the window are
# looked at; a trace whose root started before the window stays out even if later spans fall inside it.
WINDOW_ROOTS = (
    "WITH roots AS (SELECT r.trace_id, r.start_ts, r.duration_ms, "
    "  EXISTS (SELECT 1 FROM trial.telemetry_spans e WHERE e.trace_id = r.trace_id AND e.status = 'ERROR') "
    "    AS failed "
    "FROM (" + _ROOTS.format(scope="c.trace_id IN (SELECT trace_id FROM trial.telemetry_spans "
                                   "WHERE start_ts >= to_timestamp(:since::bigint)) AND ") + ") r "
    "WHERE r.start_ts >= to_timestamp(:since::bigint)) ")

ALL_ROOTS = "WITH roots AS (" + _ROOTS.format(scope="") + ") "


# ------------------------------------------------------------------ queries

def _summary(t: db.Tx, since: int) -> dict:
    row = t.one(
        WINDOW_ROOTS + "SELECT count(*) AS requests, count(*) FILTER (WHERE failed) AS errors, "
        "  count(DISTINCT trace_id) AS traces, "
        "  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95 FROM roots",
        since=since)
    tokens = t.one("SELECT coalesce(sum(input_tokens), 0) AS tin, coalesce(sum(output_tokens), 0) AS tout "
                   "FROM trial.communications WHERE input_tokens IS NOT NULL "
                   "AND created_at >= to_timestamp(:since::bigint)", since=since)
    requests, errors = int(row["requests"]), int(row["errors"])
    tin, tout = int(tokens["tin"]), int(tokens["tout"])
    return {"requests": requests, "errors": errors, "error_rate": round(errors / requests, 4) if requests else 0.0,
            "p50_ms": _r1(row["p50"]), "p95_ms": _r1(row["p95"]), "tokens_in": tin, "tokens_out": tout,
            "est_cost_usd": est_cost(tin, tout), "traces": int(row["traces"])}


def _request_series(t: db.Tx, since: int, now: int) -> list[dict]:
    rows = t.query(
        WINDOW_ROOTS + "SELECT (floor(extract(epoch FROM start_ts) / :step::int) * :step::int)::bigint AS b, "
        "  count(*) AS count, count(*) FILTER (WHERE failed) AS errors FROM roots GROUP BY 1",
        step=REQUEST_BUCKET_S, since=since)
    return _buckets(rows, since, now, REQUEST_BUCKET_S, {"count": 0, "errors": 0})


def _latency_series(t: db.Tx, since: int, now: int) -> list[dict]:
    rows = t.query(
        WINDOW_ROOTS + "SELECT (floor(extract(epoch FROM start_ts) / :step::int) * :step::int)::bigint AS b, "
        "  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95 FROM roots GROUP BY 1",
        step=LATENCY_BUCKET_S, since=since)
    for r in rows:
        r["p50"], r["p95"] = _r1(r["p50"]), _r1(r["p95"])
    return _buckets(rows, since, now, LATENCY_BUCKET_S, {"p50": None, "p95": None})


def _token_series(t: db.Tx, now: int) -> list[dict]:
    today = datetime.fromtimestamp(now, timezone.utc).date()
    days = [(today.fromordinal(today.toordinal() - i)) for i in range(TOKEN_DAYS - 1, -1, -1)]
    start = int(datetime(days[0].year, days[0].month, days[0].day, tzinfo=timezone.utc).timestamp())
    rows = t.query(
        "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, sum(input_tokens) AS tin, "
        "  sum(output_tokens) AS tout FROM trial.communications WHERE input_tokens IS NOT NULL "
        "AND created_at >= to_timestamp(:start::bigint) GROUP BY 1", start=start)
    by_day = {r["day"]: r for r in rows}
    out = []
    for d in days:
        key = d.isoformat()
        tin = int(by_day[key]["tin"] or 0) if key in by_day else 0
        tout = int(by_day[key]["tout"] or 0) if key in by_day else 0
        out.append({"day": key, "input_tokens": tin, "output_tokens": tout, "est_cost_usd": est_cost(tin, tout)})
    return out


def _window_metrics(t: db.Tx, lo: int, hi: int) -> dict:
    """The six drift measures over spans/communications in [lo, hi). A rate is None when there were no agent traces."""
    agent = t.one(
        "WITH a AS (SELECT trace_id, duration_ms, "
        "  EXISTS (SELECT 1 FROM trial.telemetry_spans x WHERE x.trace_id = s.trace_id "
        "          AND x.name = 'tool.search_trial_docs') AS kb, "
        "  EXISTS (SELECT 1 FROM trial.telemetry_spans x WHERE x.trace_id = s.trace_id "
        "          AND x.name = 'tool.escalate_to_coordinator') AS esc "
        "  FROM trial.telemetry_spans s WHERE s.name = 'agent.invoke' "
        "  AND s.start_ts >= to_timestamp(:lo::bigint) AND s.start_ts < to_timestamp(:hi::bigint)) "
        "SELECT count(DISTINCT trace_id) AS traces, count(DISTINCT trace_id) FILTER (WHERE kb) AS kb, "
        "  count(DISTINCT trace_id) FILTER (WHERE esc) AS esc, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95 FROM a", lo=lo, hi=hi)
    replies = t.one(
        "SELECT avg(input_tokens) AS tin, avg(output_tokens) AS tout, avg(length(body_text)) AS chars "
        "FROM trial.communications WHERE input_tokens IS NOT NULL "
        "AND created_at >= to_timestamp(:lo::bigint) AND created_at < to_timestamp(:hi::bigint)", lo=lo, hi=hi)
    traces = int(agent["traces"])
    return {"tokens_in_per_reply": _r1(replies["tin"]), "tokens_out_per_reply": _r1(replies["tout"]),
            "agent_p95_ms": _r1(agent["p95"]), "reply_length_chars": _r1(replies["chars"]),
            "kb_search_rate": round(int(agent["kb"]) / traces, 4) if traces else None,
            "escalation_rate": round(int(agent["esc"]) / traces, 4) if traces else None}


def _config_changes(t: db.Tx, since: int) -> list[dict]:
    rows = t.query(
        "SELECT extract(epoch FROM at)::float8 AS at, model, config_hash FROM ("
        "  SELECT start_ts AS at, attrs->>'gen_ai.request.model' AS model, "
        "    attrs->>'trialtalk.config_hash' AS config_hash, "
        "    lag(attrs->>'trialtalk.config_hash') OVER (ORDER BY start_ts) AS prev "
        "  FROM trial.telemetry_spans WHERE name = 'agent.invoke' AND start_ts >= to_timestamp(:since::bigint) "
        "  AND attrs->>'trialtalk.config_hash' IS NOT NULL) x "
        "WHERE prev IS NOT NULL AND prev <> config_hash ORDER BY at", since=since)
    return [{"at": _iso(float(r["at"])), "model": r["model"], "config_hash": r["config_hash"]} for r in rows]


# ------------------------------------------------------------------ routes

@router.route("GET", "/staff/observability")
def observability(event, staff: Staff):
    """Dashboard payload: KPIs, request/latency/token series and behavior drift. Empty tables give zeros."""
    require(staff, "ops:read")
    hours = _int_param(event, "hours", 24, 1, MAX_HOURS)
    now = int(time.time())
    since = now - hours * 3600
    with db.tx(site_ids=staff.site_ids) as t:
        summary = _summary(t, since)
        series = {"requests": _request_series(t, since, now), "tokens": _token_series(t, now),
                  "latency": _latency_series(t, since, now)}
        drift = {"metrics": drift_metrics(_window_metrics(t, now - CURRENT_S, now + 1),
                                          _window_metrics(t, now - BASELINE_S, now - CURRENT_S)),
                 "config_changes": _config_changes(t, now - TOKEN_DAYS * 86400)}
    return 200, {"summary": summary, "series": series, "drift": drift}


@router.route("GET", "/staff/observability/traces")
def traces(event, staff: Staff):
    """Most recent traces, one row per trace (its root, see ALL_ROOTS), newest first."""
    require(staff, "ops:read")
    limit = _int_param(event, "limit", 50, 1, 200)
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(
            ALL_ROOTS + f"SELECT r.trace_id, to_char(r.start_ts AT TIME ZONE 'UTC', '{ISO}') AS started_at, "
            "  r.name AS root_name, r.service, r.duration_ms, agg.span_count, agg.tokens, "
            "  CASE WHEN agg.has_error THEN 'ERROR' ELSE r.status END AS status "
            "FROM (SELECT * FROM roots ORDER BY start_ts DESC, span_id LIMIT :limit::int) r "
            "JOIN LATERAL (SELECT count(*) AS span_count, bool_or(w.status = 'ERROR') AS has_error, "
            # Tokens: agent.invoke spans only (the router's agent.call span repeats the same numbers); a trace with
            # no token-bearing agent.invoke falls back to its agent.call spans.
            "  CASE WHEN count(w.tok) FILTER (WHERE w.name = 'agent.invoke') > 0 "
            "       THEN sum(w.tok) FILTER (WHERE w.name = 'agent.invoke') "
            "       ELSE sum(w.tok) FILTER (WHERE w.name = 'agent.call') END AS tokens "
            "  FROM (SELECT s.name, s.status, "
            "    CASE WHEN s.attrs->>'gen_ai.usage.input_tokens' ~ '^[0-9]+$' "
            "           OR s.attrs->>'gen_ai.usage.output_tokens' ~ '^[0-9]+$' "
            "    THEN (CASE WHEN s.attrs->>'gen_ai.usage.input_tokens' ~ '^[0-9]+$' "
            "          THEN (s.attrs->>'gen_ai.usage.input_tokens')::bigint ELSE 0 END "
            "        + CASE WHEN s.attrs->>'gen_ai.usage.output_tokens' ~ '^[0-9]+$' "
            "          THEN (s.attrs->>'gen_ai.usage.output_tokens')::bigint ELSE 0 END) END AS tok "
            "    FROM trial.telemetry_spans s WHERE s.trace_id = r.trace_id) w) agg ON true "
            "ORDER BY r.start_ts DESC, r.span_id", limit=limit)
    for r in rows:
        r["duration_ms"] = _num(r["duration_ms"])
        r["span_count"] = int(r["span_count"])
        r["tokens"] = None if r["tokens"] is None else int(r["tokens"])
    return 200, {"traces": rows}


@router.route("GET", "/staff/observability/traces/{trace_id}")
def trace(event, staff: Staff, trace_id: str):
    """Every span of one trace, start time ascending (the waterfall). Unknown ids give an empty list."""
    require(staff, "ops:read")
    if not TRACE_ID.fullmatch(trace_id):
        raise HttpError(400, "Invalid trace id")
    with db.tx(site_ids=staff.site_ids) as t:
        rows = t.query(
            f"SELECT s.span_id, s.trace_id, s.parent_id, s.name, s.service, s.kind, "
            f"  to_char(s.start_ts AT TIME ZONE 'UTC', '{ISO}') AS start_ts, s.duration_ms, s.status, s.attrs, "
            f"  to_char(s.created_at AT TIME ZONE 'UTC', '{ISO}') AS created_at "
            "FROM trial.telemetry_spans s WHERE s.trace_id = :tid ORDER BY s.start_ts, s.span_id LIMIT 1000",
            tid=trace_id)
    for r in rows:
        r["duration_ms"] = _num(r["duration_ms"])
        if isinstance(r["attrs"], str):  # the Data API hands jsonb back as text
            try:
                r["attrs"] = json.loads(r["attrs"])
            except ValueError:
                r["attrs"] = {}
    return 200, {"spans": rows}
