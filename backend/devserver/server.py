"""Local test UI: staff and participant portals over the real Lambda handlers. No AWS needed.

    TT_PG_BIN=<folder with initdb/pg_ctl> python backend/devserver/server.py [--port 8787] [--real-agent]

Starts a throwaway PostgreSQL with the real schema, seeds synthetic staff and participants, and serves:

    /staff, /participant            one page per portal (ui/*.html), as they will be deployed separately
    /inspector                      dev only: what would have gone to AWS
    /api/staff/...  -> staff_api.handler        signed in as a staff-pool user (coordinator / PI); this includes
                                                emergency alerts, staff accounts and the OpenTelemetry widget
    /api/me...      -> participant_api.handler  signed in as a participant-pool user

"Sign-in" is the X-Dev-Identity header (<portal>:<username>) in place of Cognito + the JWT authorizer.
The server builds the token claims from the fake user pools the way Cognito would, including the
real staff pre-token trigger, so what the staff "Team" panel changes (role, sites, disabled) is what the
next request gets. OpenTelemetry needs `pip install opentelemetry-sdk`; without it the widget says so. Database calls run as each Lambda's own login, so RLS and grants are real.
Scheduler, WhatsApp, SES and the agent are fakes (tests/local_env.py), unless --real-agent, which
sends web chat to the deployed AgentCore runtime with your local AWS credentials.
Synthetic data only. Everything is deleted when the server stops.
"""

import argparse
import io
import json
import os
import sys
import traceback
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

HERE = Path(__file__).resolve().parent
UI = HERE / "ui"
sys.path.insert(0, str(HERE.parent / "tests"))

from local_env import (FakeAgentCore, fake_usage, FakeCognito, FakeRdsData, FakeScheduler,  # noqa: E402
                       FakeWhatsApp, LocalPostgres)

from common import db, scheduling, telemetry, whatsapp  # noqa: E402
from auth_triggers import staff_pre_token  # noqa: E402
import participant_api.handler as participant  # noqa: E402
import reminder.handler as reminder  # noqa: E402
import staff_api.accounts as accounts  # noqa: E402
import staff_api.handler as staff  # noqa: E402

PAGES = {"/": "index.html", "/staff": "staff.html", "/participant": "participant.html",
         "/inspector": "inspector.html", "/ui/common.css": "common.css", "/ui/common.js": "common.js",
         "/ui/widgets.js": "widgets.js"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8"}

# The first staff user is created in the Cognito console (the plan leaves that to the console); every other
# account is invited from the staff portal's Team panel (/staff/accounts), signed in as that first user.
BOOTSTRAP_STAFF = {"email": "lead@example.org", "name": "Jamie Fox", "role": "coordinator",
                   "site_ids": ["RIVERSIDE", "LAKESIDE"], "study_ids": ["KOA-301"]}
SEED_STAFF = [
    {"email": "coordinator@riverside.example.org", "name": "Riley Chen", "role": "coordinator",
     "site_ids": ["RIVERSIDE"], "study_ids": ["KOA-301"]},
    {"email": "pi@riverside.example.org", "name": "Dr. Patel", "role": "PI",
     "site_ids": ["RIVERSIDE"], "study_ids": ["KOA-301"]},
    {"email": "coordinator@lakeside.example.org", "name": "Morgan Diaz", "role": "coordinator",
     "site_ids": ["LAKESIDE"], "study_ids": ["KOA-301"]},
]

# Synthetic participants (555 numbers, example.org addresses). Never real PHI.
SEED_PARTICIPANTS = [
    ("coordinator@riverside.example.org", {
        "full_name": "Alex Rivera", "phone_e164": "+17325550142", "email": "alex@example.org", "study_id": "KOA-301",
        "site_id": "RIVERSIDE", "timezone": "America/New_York", "status": "enrolled", "whatsapp_opt_in": True}),
    ("coordinator@riverside.example.org", {
        "full_name": "Sam Ortiz", "phone_e164": "+17325550177", "preferred_language": "es", "study_id": "KOA-301",
        "site_id": "RIVERSIDE", "timezone": "America/Chicago", "status": "enrolled", "whatsapp_opt_in": True}),
    ("coordinator@riverside.example.org", {
        "full_name": "Jordan Lee", "phone_e164": "+17325550188", "study_id": "KOA-301", "site_id": "RIVERSIDE",
        "timezone": "America/New_York", "status": "screening", "whatsapp_opt_in": False}),
    ("coordinator@lakeside.example.org", {
        "full_name": "Taylor Brooks", "phone_e164": "+13125550101", "study_id": "KOA-301", "site_id": "LAKESIDE",
        "timezone": "America/Chicago", "status": "enrolled", "whatsapp_opt_in": True}),
]

fakes = types.SimpleNamespace()
# Requests are served on threads (browsers open idle speculative connections that would otherwise stall a
# single-threaded server for seconds), but the Lambda code and the fakes share state: db.SECRET_ARN is switched per
# request. So everything that runs a handler or touches the fakes holds this lock; static files do not.
LOCK = threading.RLock()
pg: LocalPostgres | None = None


class AuthError(Exception):
    pass


class EchoAgent(FakeAgentCore):
    def invoke_agent_runtime(self, agentRuntimeArn, runtimeSessionId, payload):
        body = json.loads(payload)
        self.calls.append({"session": runtimeSessionId, **body})
        reply = (f"(local fake agent) You said: \"{body['text']}\". Start the server with --real-agent "
                 "to talk to the deployed trial_agent.")
        return {"response": io.BytesIO(json.dumps({"reply": reply, "usage": fake_usage(body["text"], reply)}).encode())}


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
    telemetry.init()
    participant.ses = lambda: types.SimpleNamespace(send_email=lambda **kw: fakes.emails.append(kw))
    participant.ALERT_FROM, participant.ALERT_TO = "alerts@example.org", ["coordinator@example.org"]
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
    admin_query("TRUNCATE trial.clinical_events, trial.communications, trial.reminders, pii.participants CASCADE")
    admin_query("ALTER SEQUENCE pii.participant_seq RESTART WITH 1001")
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

    pids = []
    for who, body in SEED_PARTICIPANTS:
        status, out = call_handler(staff, "staff", staff_token(who), "POST", "/staff/participants",
                                   json.dumps(body).encode(), {})
        assert status == 201, out
        pids.append(out["participant"]["participant_id"])
    coord = staff_token("coordinator@riverside.example.org")
    status, out = call_handler(staff, "staff", coord, "POST", f"/staff/participants/{pids[0]}/reminders", json.dumps({
        "reminder_type": "dose", "label": "Evening dose", "dose_label": "evening",
        "dose_instructions": "1 tablet with food", "time": "20:00", "escalate_after_misses": 3,
    }).encode(), {})
    assert status == 201, out
    # One send, so the outbox, communications and DOSE timeline have something in them.
    call_handler(staff, "staff", coord, "POST", f"/staff/reminders/{out['reminder']['reminder_id']}/send-now", None, {})
    telemetry.reset()  # the window should start with the first thing you do, not the seeding
    print(f"Seeded {1 + len(SEED_STAFF)} staff users, participants {', '.join(pids)}")


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
    args = ap.parse_args()

    bin_dir = os.environ.get("TT_PG_BIN")
    if not bin_dir:
        sys.exit("Set TT_PG_BIN to the folder holding initdb and pg_ctl (see backend/README.md, Testing).")
    if args.real_agent:
        try:
            import boto3  # noqa: F401
        except ImportError:
            sys.exit("--real-agent needs boto3 in this Python environment.")

    global pg
    print("Starting local PostgreSQL...")
    pg = LocalPostgres(bin_dir)
    try:
        wire(args.real_agent)
        seed()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        base = f"http://127.0.0.1:{args.port}"
        print(f"TrialTalk portals (Ctrl+C to stop):\n  staff        {base}/staff\n  participant  {base}/participant\n"
              f"  inspector    {base}/inspector  (dev only)")
        server.daemon_threads = True
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping PostgreSQL and deleting the data...")
        pg.stop()


if __name__ == "__main__":
    main()
