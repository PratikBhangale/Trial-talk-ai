"""Local test UI: staff and participant portals over the real Lambda handlers. No AWS needed.

    TT_PG_BIN=<folder with initdb/pg_ctl> python coordinator-backend/devserver/server.py [--port 8787] [--real-agent]
                                                                                       [--seed-telemetry]

Starts a throwaway PostgreSQL built from db/ (plus tests/pending_db_additions.sql), loads db/03_seed.sql, the
same synthetic data as the deployed database, adds synthetic staff, and serves:

    /staff, /participant            one page per portal (ui/*.html), as they will be deployed separately
    /api/staff/...  -> staff_api.handler        signed in as a staff-pool user (coordinator / PI); this includes
                                                emergency alerts, staff accounts and the Observability dashboard
    /api/me...      -> participant_api.handler  signed in as a participant-pool user

"Sign-in" is the X-Dev-Identity header (<portal>:<username>) in place of Cognito + the JWT authorizer.
The server builds the token claims from the fake user pools the way Cognito would, including the
real staff pre-token trigger, so what the staff "Team" panel changes (role, sites, disabled) is what the
next request gets. Database calls run as each Lambda's own login, so RLS and grants are real.
The Observability dashboard reads trial.telemetry_spans: --seed-telemetry data and the agent's spans from web
chat. Request spans (`pip install opentelemetry-sdk`) are not stored unless TELEMETRY_DB_SINK=1, so the
dashboard's own polling doesn't bury those traces.
Scheduler, WhatsApp, SES and the agent are fakes (tests/local_env.py), unless --real-agent, which
sends web chat to the deployed AgentCore runtime with your local AWS credentials.
--seed-telemetry adds about 7 days of synthetic traces (trial.telemetry_spans) and token-bearing messages so the
staff Observability dashboard has something to show; every synthetic span carries "trialtalk.synthetic": true, and
it is re-created after "Reset data".
Synthetic data only. Everything is deleted when the server stops.
"""

import argparse
import io
import json
import os
import random
import sys
import traceback
import threading
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

HERE = Path(__file__).resolve().parent
UI = HERE.parents[1] / "coordinator-frontend"
sys.path.insert(0, str(HERE.parent / "tests"))

from local_env import (FakeAgentCore, FakeCognito, FakeRdsData, FakeScheduler,  # noqa: E402
                       FakeWhatsApp, LocalPostgres, SEED_FILE, run_sql_file, wipe)

from common import db, scheduling, telemetry, whatsapp  # noqa: E402
from auth_triggers import staff_pre_token  # noqa: E402
import participant_api.handler as participant  # noqa: E402
import reminder.handler as reminder  # noqa: E402
import staff_api.accounts as accounts  # noqa: E402
import staff_api.handler as staff  # noqa: E402

PAGES = {"/": "index.html", "/staff": "staff.html", "/participant": "participant.html",
         "/ui/common.css": "common.css", "/ui/common.js": "common.js",
         "/ui/widgets.js": "widgets.js", "/ui/orb.js": "orb.js", "/ui/charts.js": "charts.js"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8"}

# The first staff user is created in the Cognito console (the plan leaves that to the console); every other
# account is invited from the staff portal's Team panel (/staff/accounts), signed in as that first user. It is a PI
# at every site: nobody can grant more than they hold, so the first user must hold everything the others will get.
BOOTSTRAP_STAFF = {"email": "lead@example.org", "name": "Jamie Fox", "role": "PI",
                   "site_ids": ["RIVERSIDE", "LAKEVIEW"], "study_ids": ["KOA-301"]}
# The staff that db/03_seed.sql's records name (created_by, the AE report email), at its two sites.
SEED_STAFF = [
    {"email": "coordinator@riverside-clinic.org", "name": "Riley Chen", "role": "coordinator",
     "site_ids": ["RIVERSIDE"], "study_ids": ["KOA-301"]},
    {"email": "dr.patel@riverside-clinic.org", "name": "Dr. Patel", "role": "PI",
     "site_ids": ["RIVERSIDE"], "study_ids": ["KOA-301"]},
    {"email": "coordinator@lakeview-health.org", "name": "Morgan Diaz", "role": "coordinator",
     "site_ids": ["LAKEVIEW"], "study_ids": ["KOA-301"]},
]

fakes = types.SimpleNamespace()
# Requests are served on threads (browsers open idle speculative connections that would otherwise stall a
# single-threaded server for seconds), but the Lambda code and the fakes share state: db.SECRET_ARN is switched per
# request. So everything that runs a handler or touches the fakes holds this lock; static files do not.
LOCK = threading.RLock()
pg: LocalPostgres | None = None
SEED_TELEMETRY = False  # --seed-telemetry: seed() also adds the synthetic observability data


class AuthError(Exception):
    pass


class EchoAgent(FakeAgentCore):
    def reply_for(self, body):
        return (f"(local fake agent) You said: \"{body['text']}\". Start the server with --real-agent "
                "to talk to the deployed trial_agent.")


def _init_without_request_sink(real_init):
    """telemetry.init that builds the tracer with TELEMETRY_DB_SINK=0 (no per-request DB exporter) and then removes
    the flag again, so persist_spans keeps writing. The flag is process-wide, so it is touched only when a build
    really happens (first call or force=True: startup and Reset data, which holds LOCK like every chat request),
    never on the idempotent calls every request makes from other threads."""
    if getattr(real_init, "_no_request_sink", False):
        return real_init

    def init(**kw):
        if telemetry._state and not kw.get("force"):
            return real_init(**kw)
        os.environ["TELEMETRY_DB_SINK"] = "0"
        try:
            return real_init(**kw)
        finally:
            os.environ.pop("TELEMETRY_DB_SINK", None)
    init._no_request_sink = True
    return init


def wire(real_agent: bool) -> None:
    """Point every Lambda module at the fakes, the way tests/conftest.py does with monkeypatch."""
    fakes.rds, fakes.scheduler = FakeRdsData(pg.port), FakeScheduler()
    fakes.participant_pool, fakes.staff_pool = FakeCognito(), FakeCognito()
    fakes.whatsapp, fakes.agent, fakes.emails = FakeWhatsApp(), EchoAgent(), []
    db.client = lambda: fakes.rds
    scheduling.client = lambda: fakes.scheduler
    whatsapp.client = lambda: fakes.whatsapp
    staff.cognito = lambda: fakes.participant_pool  # staff_api creates participant logins
    staff.PARTICIPANT_POOL_ID = "local-participant-pool"
    staff.lambda_client = lambda: types.SimpleNamespace(invoke=_invoke_reminder)
    accounts.cognito = lambda: fakes.staff_pool
    accounts.STAFF_POOL_ID = "local-staff-pool"
    # Web chat stores the agent's returned spans through telemetry.persist_spans (its own Data API client), so point
    # that client at the local database as the participant login, like the deployed Lambda. The per-request span
    # sink, which would let the staff dashboard's own polling bury the demo traces, stays off unless the caller sets
    # TELEMETRY_DB_SINK=1. It is turned off only while the tracer is built (see _init_without_request_sink), never
    # in the environment: persist_spans also honors TELEMETRY_DB_SINK=0 and would stop storing the agent's spans.
    os.environ.setdefault("DB_CLUSTER_ARN", "local-cluster")
    os.environ.setdefault("DB_SECRET_ARN", "participant")
    telemetry._rds = fakes.rds
    if os.environ.get("TELEMETRY_DB_SINK") is None:
        telemetry.init = _init_without_request_sink(telemetry.init)  # reset() re-inits through this too
    telemetry.init()
    participant.ses = lambda: types.SimpleNamespace(send_email=lambda **kw: fakes.emails.append(kw))
    participant.ALERT_FROM = "alerts@example.org"
    participant.ALERT_TO = {"RIVERSIDE": ["coordinator@riverside-clinic.org"],
                            "LAKEVIEW": ["coordinator@lakeview-health.org"]}
    if real_agent:
        cdk = json.loads((HERE.parent / "infra" / "cdk.json").read_text())["context"]
        participant.AGENT_RUNTIME_ARN = cdk["agentRuntimeArn"]
        os.environ.setdefault("AWS_REGION", cdk.get("region", "us-east-2"))
    else:
        participant.agentcore = lambda: fakes.agent


def _invoke_reminder(FunctionName, InvocationType, Payload):
    return {"Payload": io.BytesIO(json.dumps(run_reminder(json.loads(Payload))).encode())}


def run_reminder(event: dict) -> dict:
    previous = db.SECRET_ARN
    db.SECRET_ARN = "messaging"
    try:
        return reminder.handler(event, None)
    finally:
        db.SECRET_ARN = previous


# ------------------------------------------------------------------ "Cognito sign-in": user pool -> claims

def _pool_user(pool: FakeCognito, username: str) -> tuple[dict, list[str]]:
    if username not in pool.users:
        raise AuthError("No such user")
    if username in pool.disabled:
        raise AuthError("User is disabled")
    return pool.users[username], sorted(pool.groups.get(username, set()))


def staff_token(username: str) -> dict:
    """ID token claims for the staff portal client: cognito:groups plus what the pre-token trigger adds."""
    attrs, groups = _pool_user(fakes.staff_pool, username)
    event = {"request": {"userAttributes": attrs, "groupConfiguration": {"groupsToOverride": groups}},
             "response": {}}
    added = staff_pre_token.handler(event, None)["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    return {"sub": username, "cognito:username": username, "email": attrs.get("email"),
            "cognito:groups": f"[{' '.join(groups)}]", **{k: v for k, v in attrs.items() if k.startswith("custom:")},
            **added}


def participant_token(username: str) -> dict:
    attrs, _ = _pool_user(fakes.participant_pool, username)
    return {"sub": username, "cognito:username": username, **attrs}


def call_handler(module, login: str | None, claims: dict, method: str, path: str, body: bytes | None,
                 query: dict) -> tuple[int, dict]:
    if login:
        db.SECRET_ARN = login
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "body": body.decode() if body else None,
        "queryStringParameters": query or None,
    }
    resp = module.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"]) if resp.get("body") else {}


def admin_query(sql: str, *args) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(pg.admin_dsn, autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def seed() -> None:
    import psycopg
    with psycopg.connect(pg.admin_dsn, autocommit=True) as c:
        wipe(c)
        run_sql_file(c, SEED_FILE)
    for f in (fakes.scheduler.schedules, fakes.emails, fakes.whatsapp.sent, fakes.agent.calls):
        f.clear()
    fakes.participant_pool.__init__()
    fakes.staff_pool.__init__()

    pool, boot = fakes.staff_pool, BOOTSTRAP_STAFF
    pool.admin_create_user(UserPoolId="", Username=boot["email"], UserAttributes=[
        {"Name": "email", "Value": boot["email"]}, {"Name": "name", "Value": boot["name"]},
        {"Name": "custom:site_ids", "Value": ",".join(boot["site_ids"])},
        {"Name": "custom:study_ids", "Value": ",".join(boot["study_ids"])}])
    pool.admin_set_user_password(Username=boot["email"])
    pool.admin_add_user_to_group(UserPoolId="", Username=boot["email"], GroupName=boot["role"])
    lead = staff_token(boot["email"])
    for user in SEED_STAFF:
        status, out = call_handler(staff, None, lead, "POST", "/staff/accounts", json.dumps(user).encode(), {})
        assert status == 201, out
        pool.admin_set_user_password(Username=user["email"])  # as if they finished first sign-in

    # What staff_api did for the seeded rows when they were created: website logins and reminder schedules.
    participants = admin_query("SELECT participant_id, phone_e164 FROM pii.participants ORDER BY participant_id")
    for p in participants:
        staff._create_login(p)
    for r in admin_query("SELECT reminder_id::text AS reminder_id, schedule_kind, cron_expr, send_at, timezone,"
                         "  starts_on, ends_on, status FROM trial.reminders WHERE status = 'active'"):
        scheduling.create(r)
    if SEED_TELEMETRY:
        seed_telemetry()
    telemetry.reset()  # the window should start with the first thing you do, not the seeding
    print(f"Seeded {1 + len(SEED_STAFF)} staff users, participants "
          f"{', '.join(p['participant_id'] for p in participants)} (db/03_seed.sql)")


# ------------------------------------------------------------------ synthetic telemetry (--seed-telemetry)

SYNTH = {"trialtalk.synthetic": True}  # in EVERY synthetic span's attributes; how the seed finds its own rows
SYNTH_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
SYNTH_HASH_BEFORE, SYNTH_HASH_AFTER = "3f9a1c2b7d10", "8e4d6a05b1c7"
# A generic, PHI-free reply body for the token-bearing messages; real text never goes near telemetry.
SYNTH_REPLY = ("[demo data] Thanks for letting us know. Your study team has this on record and will follow up at "
               "your next visit. If anything changes or you feel worse, contact your study site. ")
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 1, 2, 4, 6, 8, 8, 7, 7, 7, 7, 7, 8, 9, 9, 8, 6, 4, 2, 1]  # traffic by UTC hour


def _synth_traces(days: int, now: datetime, rng: random.Random) -> tuple[list[tuple], list[dict]]:
    """(span rows, message rows): whatsapp.ingest -> whatsapp.route -> agent.call -> agent.invoke -> tool.* traces.

    About 2% of requests fail at ingest, 4% fail in the agent (ERROR on route/agent.call/agent.invoke) and 8% are
    answered by Layer 0 without the agent. Half way through, the agent's config hash changes: prompts get longer
    (more input tokens), and the knowledge base is searched less, so the dashboard's change marker has a story.
    """
    change_at = now - timedelta(days=days * 0.45)
    spans, msgs = [], []

    def sid():
        return f"{rng.getrandbits(64):016x}"

    def add(tid, span_id, parent, name, service, kind, start, dur, status, attrs):
        spans.append((span_id, tid, parent, name, service, kind, start, round(dur, 2), status,
                      json.dumps({**attrs, **SYNTH})))

    for d in range(days + 1):  # calendar days touching the last `days` x 24 h; the two end days are partial
        day0 = (now - timedelta(days=days - d)).replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(rng.randint(35, 65)):
            hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
            t0 = day0 + timedelta(hours=hour, minutes=rng.randint(0, 59), seconds=rng.random() * 60)
            if not now - timedelta(days=days) <= t0 <= now - timedelta(minutes=1):
                continue
            after = t0 >= change_at
            tid, ingest, route = f"{rng.getrandbits(128):032x}", sid(), sid()
            roll = rng.random()
            ing_ms = rng.uniform(15, 90)
            if roll < 0.02:  # signature / payload rejected at ingest
                add(tid, ingest, None, "whatsapp.ingest", "tt-ingest", "SERVER", t0, ing_ms, "ERROR",
                    {"messaging.system": "whatsapp", "http.response.status_code": 400})
                continue
            add(tid, ingest, None, "whatsapp.ingest", "tt-ingest", "SERVER", t0, ing_ms, "OK",
                {"messaging.system": "whatsapp", "http.response.status_code": 200})
            t_route = t0 + timedelta(milliseconds=ing_ms + rng.uniform(40, 400))  # time on the queue
            if roll < 0.10:  # Layer 0: STOP / START / button taps never reach the model
                add(tid, route, ingest, "whatsapp.route", "tt-router", "CONSUMER", t_route, rng.uniform(40, 200),
                    "OK", {"trialtalk.msg_type": "button", "messaging.operation": "process"})
                continue

            failed = roll < 0.14
            call, invoke = sid(), sid()
            if failed:
                inv_ms = rng.choice([rng.uniform(300, 5000), 29000.0])
            else:
                inv_ms = min(max(rng.lognormvariate(7.7, 0.45), 900), 12000) * (1.15 if after else 1.0)
            t_call = t_route + timedelta(milliseconds=rng.uniform(15, 60))
            call_ms = inv_ms + rng.uniform(30, 120)
            status = "ERROR" if failed else "OK"
            add(tid, route, ingest, "whatsapp.route", "tt-router", "CONSUMER", t_route, call_ms + rng.uniform(60, 220),
                status, {"trialtalk.msg_type": "text", "messaging.operation": "process"})
            add(tid, call, route, "agent.call", "tt-router", "CLIENT", t_call, call_ms, status,
                {"rpc.system": "aws-api", "rpc.service": "bedrock-agentcore"})
            t_inv = t_call + timedelta(milliseconds=rng.uniform(10, 60))
            tin = int(min(max(rng.gauss(650, 150), 300), 1100) * (1.25 if after else 1.0))
            tout = int(min(max(rng.gauss(140, 45), 40), 300))
            inv_attrs = {"gen_ai.request.model": SYNTH_MODEL,
                         "trialtalk.config_hash": SYNTH_HASH_AFTER if after else SYNTH_HASH_BEFORE}
            if not failed:
                inv_attrs.update({"gen_ai.usage.input_tokens": tin, "gen_ai.usage.output_tokens": tout})
            add(tid, invoke, call, "agent.invoke", "trial-agent", "SERVER", t_inv, inv_ms, status, inv_attrs)

            tools = 0
            if not failed:
                if rng.random() < (0.45 if after else 0.7):  # the knowledge base is searched less after the change
                    for k in range(rng.randint(1, 2)):
                        add(tid, sid(), invoke, "tool.search_trial_docs", "trial-agent", "INTERNAL",
                            t_inv + timedelta(milliseconds=80 + 400 * k), rng.uniform(60, 350), "OK",
                            {"trialtalk.kb.results": rng.randint(1, 5),
                             "trialtalk.kb.top_score": round(rng.uniform(0.3, 0.95), 3)})
                        tools += 1
                if rng.random() < 0.03:
                    add(tid, sid(), invoke, "tool.escalate_to_coordinator", "trial-agent", "INTERNAL",
                        t_inv + timedelta(milliseconds=inv_ms * 0.8), rng.uniform(80, 250), "OK",
                        {"trialtalk.escalation.severity": "urgent"})
                    tools += 1
                body = (SYNTH_REPLY * 20)[:int(tout * rng.uniform(3.2, 4.4))]
                msgs.append({"created_at": t_route + timedelta(milliseconds=call_ms), "tin": tin, "tout": tout,
                             "latency_ms": int(call_ms), "tools": tools, "body": body, "session": f"synthetic-{tid[:12]}"})
    return spans, msgs


def seed_telemetry(days: int = 7) -> None:
    """Replace the synthetic observability data: ~`days` days of traces in trial.telemetry_spans and, for each agent
    reply, a token-bearing outbound message in trial.communications (the staff API sums tokens from there).

    Every span has attrs "trialtalk.synthetic": true; messages are marked by a session_id of "synthetic-...". No names,
    phones or message text. Run it as the superuser, like seed(); messages go to the participants that exist.
    """
    import psycopg
    spans, msgs = _synth_traces(days, datetime.now(timezone.utc), random.Random(2026))
    people = [r["participant_id"] for r in admin_query("SELECT participant_id FROM pii.participants ORDER BY 1")]
    rng = random.Random(7)
    with psycopg.connect(pg.admin_dsn) as c:
        c.execute("""DELETE FROM trial.telemetry_spans WHERE attrs @> '{"trialtalk.synthetic": true}'""")
        with c.transaction():  # communications is append-only; local throwaway database only, as in wipe()
            c.execute("ALTER TABLE trial.communications DISABLE TRIGGER communications_no_delete")
            c.execute("DELETE FROM trial.communications WHERE session_id LIKE 'synthetic-%%'")
            c.execute("ALTER TABLE trial.communications ENABLE TRIGGER communications_no_delete")
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO trial.telemetry_spans (span_id, trace_id, parent_id, name, service, kind, start_ts, "
                "duration_ms, status, attrs) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (span_id) DO NOTHING", spans)
            if people:
                cur.executemany(
                    "INSERT INTO trial.communications (participant_id, channel, direction, msg_type, body_text, "
                    "session_id, model_id, input_tokens, output_tokens, latency_ms, tool_calls, created_at) "
                    "VALUES (%s, 'whatsapp', 'outbound', 'text', %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(rng.choice(people), m["body"], m["session"], SYNTH_MODEL, m["tin"], m["tout"], m["latency_ms"],
                      m["tools"], m["created_at"]) for m in msgs])
    print(f"Seeded {len({s[1] for s in spans})} synthetic traces ({len(spans)} spans) over {days} days"
          + (f" and {len(msgs)} token-bearing messages" if people else " (no participants: no messages)"))


def identities() -> dict:
    """Who each portal's dev sign-in can pick. Mirrors who Cognito would let through, plus locked-out accounts."""
    sp, pp = fakes.staff_pool, fakes.participant_pool

    def staff_row(u):
        groups = sorted(sp.groups.get(u, set()))
        a = sp.users[u]
        return {"username": u, "name": a.get("name"), "groups": groups, "site_ids": a.get("custom:site_ids", ""),
                "enabled": u not in sp.disabled}

    people = {r["participant_id"]: r for r in admin_query(
        "SELECT participant_id, full_name, site_id, status FROM pii.participants ORDER BY participant_id")}
    return {
        "staff": [staff_row(u) for u in sp.users if sp.groups.get(u, set()) & {"coordinator", "PI"}],
        "participant": [{"username": u, **people.get(u, {}), "enabled": u not in pp.disabled} for u in sorted(pp.users)],
    }


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, payload, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        url = urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", dict(parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        try:
            if path in PAGES and self.command == "GET":
                f = UI / PAGES[path]
                return self._send(200, f.read_bytes(), TYPES[f.suffix])
            # Vendored browser libraries (coordinator-frontend/vendor/<lib>/...). Resolved and checked so ".." cannot
            # leave the folder.
            if path.startswith("/ui/vendor/") and self.command == "GET":
                vendor = (UI / "vendor").resolve()
                f = (UI / path[len("/ui/"):]).resolve()
                if f.is_relative_to(vendor) and f.is_file() and f.suffix in TYPES:
                    return self._send(200, f.read_bytes(), TYPES[f.suffix])
                return self._send(404, b'{"error": "not found"}', "application/json")
            if path.startswith("/api/"):
                return self._api(path[4:], query, body)
            if path.startswith("/dev/"):
                return self._dev(path, body)
            self._send(404, {"error": "Not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _api(self, path: str, query: dict, body: bytes | None):
        portal, _, username = self.headers.get("X-Dev-Identity", "").partition(":")
        username = unquote(username)
        if path.startswith("/staff/"):
            want, module, login, token = "staff", staff, "staff", staff_token
        elif path == "/me" or path.startswith("/me/"):
            want, module, login, token = "participant", participant, "participant", participant_token
        else:
            return self._send(404, {"error": "Not found"})
        # Each portal has its own app client, so a token from one is rejected by the others' authorizers.
        if portal != want or not username:
            return self._send(401, {"error": f"Sign in to the {want} portal"})
        with LOCK:
            try:
                claims = token(username)
            except AuthError as e:
                return self._send(401, {"error": f"Sign-in refused: {e}"})
            status, out = call_handler(module, login, claims, self.command, path, body, query)
        self._send(status, out)

    def _dev(self, path: str, body: bytes | None):
        with LOCK:
            return self._dev_locked(path, body)

    def _dev_locked(self, path: str, body: bytes | None):
        if path == "/dev/identities" and self.command == "GET":
            return self._send(200, identities())
        if path == "/dev/outbox" and self.command == "GET":
            sp = fakes.staff_pool
            return self._send(200, {
                "whatsapp": list(reversed(fakes.whatsapp.sent)),
                "schedules": [{"name": k, "expression": v["ScheduleExpression"],
                               "timezone": v.get("ScheduleExpressionTimezone"), "state": v.get("State"),
                               "input": json.loads(v["Target"]["Input"])} for k, v in fakes.scheduler.schedules.items()],
                "emails": list(reversed(fakes.emails)),
                "agent_calls": list(reversed(fakes.agent.calls)),
                "participant_logins": [{"username": k, **v, "disabled": k in fakes.participant_pool.disabled}
                                       for k, v in fakes.participant_pool.users.items()],
                "staff_pool": [{"username": k, "groups": sorted(sp.groups.get(k, set())), **v,
                                "status": sp.meta.get(k, {}).get("UserStatus"), "disabled": k in sp.disabled}
                               for k, v in sp.users.items()],
            })
        if path.startswith("/dev/fire/") and self.command == "POST":
            # What EventBridge Scheduler would do when rem-<id> comes due.
            return self._send(200, run_reminder({"reminder_id": path.rsplit("/", 1)[1]}))
        if path == "/dev/reset" and self.command == "POST":
            seed()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "Not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--real-agent", action="store_true",
                    help="send web chat to the deployed AgentCore runtime (needs AWS credentials and boto3)")
    ap.add_argument("--seed-telemetry", action="store_true",
                    help="add ~7 days of synthetic traces and token usage for the Observability dashboard "
                         "(re-added after Reset data)")
    args = ap.parse_args()

    bin_dir = os.environ.get("TT_PG_BIN")
    if not bin_dir:
        sys.exit("Set TT_PG_BIN to the folder holding initdb and pg_ctl (see backend/README.md, Testing).")
    if args.real_agent:
        try:
            import boto3  # noqa: F401
        except ImportError:
            sys.exit("--real-agent needs boto3 in this Python environment.")

    global pg, SEED_TELEMETRY
    SEED_TELEMETRY = args.seed_telemetry
    print("Starting local PostgreSQL...")
    pg = LocalPostgres(bin_dir)
    try:
        wire(args.real_agent)
        seed()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        base = f"http://127.0.0.1:{args.port}"
        print(f"TrialTalk portals (Ctrl+C to stop):\n  staff        {base}/staff\n  participant  {base}/participant")
        server.daemon_threads = True
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping PostgreSQL and deleting the data...")
        pg.stop()


if __name__ == "__main__":
    main()
