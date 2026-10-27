"""Local test UI: staff and participant portals over the real Lambda handlers. No AWS needed.

    TT_PG_BIN=<folder with initdb/pg_ctl> python backend/devserver/server.py [--port 8787]

Starts a throwaway PostgreSQL built from db/ (plus tests/pending_db_additions.sql), loads db/03_seed.sql, the
same synthetic data as the deployed database, adds synthetic staff, and serves:

    /staff, /participant            one page per portal (ui/*.html), as they will be deployed separately
    /inspector                      dev only: what would have gone to AWS
    /api/staff/...  -> staff_api.handler        signed in as a staff-pool user (coordinator / PI); this includes
                                                emergency alerts, staff accounts and the OpenTelemetry widget
    /api/me...      -> participant_api.handler  signed in as a participant-pool user

"Sign-in" is the X-Dev-Identity header (<portal>:<username>) in place of Cognito + the JWT authorizer.
The server builds the token claims from the fake user pools the way Cognito would, including the
real staff pre-token trigger, so what the staff "Team" panel changes (role, sites, disabled) is what the
next request gets. OpenTelemetry needs `pip install opentelemetry-sdk`; without it the widget says so. Database calls run as each Lambda's own login, so RLS and grants are real.
Scheduler, WhatsApp and SES are fakes (tests/local_env.py).
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

from local_env import (FakeCognito, FakeRdsData, FakeScheduler, FakeWhatsApp,  # noqa: E402
                       LocalPostgres, SEED_FILE, run_sql_file, wipe)

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


class AuthError(Exception):
    pass


def wire() -> None:
    """Point every Lambda module at the fakes, the way tests/conftest.py does with monkeypatch."""
    fakes.rds, fakes.scheduler = FakeRdsData(pg.port), FakeScheduler()
    fakes.participant_pool, fakes.staff_pool = FakeCognito(), FakeCognito()
    fakes.whatsapp, fakes.emails = FakeWhatsApp(), []
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
    participant.ALERT_FROM = "alerts@example.org"
    participant.ALERT_TO = {"RIVERSIDE": ["coordinator@riverside-clinic.org"],
                            "LAKEVIEW": ["coordinator@lakeview-health.org"]}


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
    for f in (fakes.scheduler.schedules, fakes.emails, fakes.whatsapp.sent):
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
    telemetry.reset()  # the window should start with the first thing you do, not the seeding
    print(f"Seeded {1 + len(SEED_STAFF)} staff users, participants "
          f"{', '.join(p['participant_id'] for p in participants)} (db/03_seed.sql)")


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
    args = ap.parse_args()

    bin_dir = os.environ.get("TT_PG_BIN")
    if not bin_dir:
        sys.exit("Set TT_PG_BIN to the folder holding initdb and pg_ctl (see backend/README.md, Testing).")

    global pg
    print("Starting local PostgreSQL...")
    pg = LocalPostgres(bin_dir)
    try:
        wire()
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
