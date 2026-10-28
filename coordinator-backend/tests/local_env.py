"""Local stand-ins for AWS, shared by the tests (conftest.py) and the dev server (devserver/server.py).

LocalPostgres runs a throwaway PostgreSQL 16 built from db/ (the schema's source of truth: tables, triggers,
roles, RLS and the tt_* logins).
The Fake* classes replace the boto3 clients: FakeRdsData runs Data API calls on that server as the
same login the Lambda would use, so grants and RLS are exercised for real.
"""

import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
DB_SQL = Path(__file__).resolve().parents[2] / "db"
for _path in (LAMBDAS, DB_SQL):  # DB_SQL for run_sql.split_sql, the splitter written for those files
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Applied in order to a fresh database. db/ files are not idempotent (plain CREATE TABLE/ROLE/POLICY): once only.
SCHEMA_FILES = [DB_SQL / "01_schema.sql", DB_SQL / "02_roles_rls.sql", DB_SQL / "05_telemetry.sql",
                DB_SQL / "06_alerts_web.sql", DB_SQL / "07_studies.sql"]
SEED_FILE = DB_SQL / "03_seed.sql"

# boto3 isn't needed locally; every AWS client is replaced with a fake.
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        _fake = types.ModuleType("boto3")
        _fake.client = lambda *a, **k: None
        _fake.resource = lambda *a, **k: None
        sys.modules["boto3"] = _fake

# Fake secret ARN -> the db/02_roles_rls.sql login it stands for. The server trusts local connections,
# so the CHANGE_ME passwords in that file are never used.
LOGINS = {"staff": "tt_portal", "participant": "tt_participant", "messaging": "tt_messaging", "agent": "tt_agent",
          "admin": "postgres"}  # admin = the cluster master user: db/ migrations and db/seed_demo_clinic.py


# ------------------------------------------------------------------ throwaway Postgres

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalPostgres:
    """initdb + pg_ctl start on a free port, then SCHEMA_FILES (which also create the logins)."""

    def __init__(self, bin_dir: str):
        import psycopg
        self.bin_dir = Path(bin_dir)
        self.data = Path(tempfile.mkdtemp(prefix="ttpg-"))
        self.port = _free_port()
        subprocess.run([self._exe("initdb"), "-D", str(self.data), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
                       check=True, capture_output=True)
        # No captured pipes here: the server inherits them on Windows and run() would never return.
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-o", f"-p {self.port} -c timezone=UTC", "-l",
                        str(self.data / "log.txt"), "-w", "start"], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            admin = f"host=127.0.0.1 port={self.port} user=postgres"
            with psycopg.connect(admin + " dbname=postgres", autocommit=True) as c:
                c.execute("CREATE DATABASE trialtalk")
            self.admin_dsn = admin + " dbname=trialtalk"

            with psycopg.connect(self.admin_dsn, autocommit=True) as c:
                for path in SCHEMA_FILES:
                    run_sql_file(c, path)
        except BaseException:
            self.stop()
            raise

    def _exe(self, name: str) -> str:
        return str(self.bin_dir / name)

    def stop(self) -> None:
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-m", "fast", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.data, ignore_errors=True)


def run_sql_file(conn, path: Path) -> None:
    from run_sql import split_sql
    for stmt in split_sql(path.read_text(encoding="utf-8")):
        conn.execute(stmt)


def wipe(conn) -> None:
    """Delete all data, as the superuser. db/'s append-only trigger blocks TRUNCATE even for the owner, so it is
    switched off for this one transaction. Local throwaway databases only; never do this to Aurora."""
    with conn.transaction():
        conn.execute("ALTER TABLE trial.clinical_events DISABLE TRIGGER clinical_events_no_truncate")
        conn.execute("TRUNCATE trial.clinical_events, trial.communications, trial.reminders, pii.participants,"
                     " trial.telemetry_spans CASCADE")
        conn.execute("ALTER TABLE trial.clinical_events ENABLE TRIGGER clinical_events_no_truncate")


# ------------------------------------------------------------------ fake RDS Data API

_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _to_python(v: dict):
    if v.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "booleanValue", "doubleValue"):
        if key in v:
            return v[key]
    raise ValueError(v)


def _to_json_value(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (dict, list)) and not isinstance(v, str):
        return json.dumps(v)  # the Data API returns json/jsonb as text
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return v


class FakeRdsData:
    """Implements the Data API calls common.db uses, against real Postgres."""

    def __init__(self, port: int):
        self.port = port
        self.tx = {}

    def _connect(self, secret: str):
        import psycopg
        user = LOGINS[secret]
        conn = psycopg.connect(f"host=127.0.0.1 port={self.port} dbname=trialtalk user={user}")
        conn.execute("SET timezone = 'UTC'")
        conn.commit()
        return conn

    def begin_transaction(self, resourceArn, secretArn, database):
        tid = str(uuid.uuid4())
        self.tx[tid] = self._connect(secretArn)
        return {"transactionId": tid}

    def commit_transaction(self, resourceArn, secretArn, transactionId):
        conn = self.tx.pop(transactionId)
        conn.commit()
        conn.close()

    def rollback_transaction(self, resourceArn, secretArn, transactionId):
        conn = self.tx.pop(transactionId)
        conn.rollback()
        conn.close()

    def execute_statement(self, resourceArn, secretArn, database, sql, parameters=(), formatRecordsAs=None,
                          transactionId=None, _conn=None):
        conn = _conn or (self.tx.get(transactionId) if transactionId else self._connect(secretArn))
        params = {p["name"]: _to_python(p["value"]) for p in parameters}
        pg_sql = _PARAM.sub(lambda m: f"%({m.group(1)})s", sql.replace("%", "%%"))
        with conn.cursor() as cur:
            cur.execute(pg_sql, params)
            out = {"numberOfRecordsUpdated": max(cur.rowcount, 0) if cur.description is None else 0}
            if cur.description is not None:
                cols = [d.name for d in cur.description]
                rows = [{c: _to_json_value(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
                out["formattedRecords"] = json.dumps(rows)
        if not transactionId and _conn is None:
            conn.commit()
            conn.close()
        return out


    def batch_execute_statement(self, resourceArn, secretArn, database, sql, parameterSets=()):
        """What telemetry.persist_spans calls: one statement, many parameter sets, one transaction."""
        conn = self._connect(secretArn)
        try:
            for params in parameterSets:
                self.execute_statement(resourceArn, secretArn, database, sql, params, _conn=conn)
            conn.commit()
        finally:
            conn.close()
        return {}


class FakeScheduler:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.schedules = {}

    def create_schedule(self, **req):
        self.schedules[req["Name"]] = req
        return {"ScheduleArn": f"arn:aws:scheduler:::schedule/{req['GroupName']}/{req['Name']}"}

    def update_schedule(self, **req):
        if req["Name"] not in self.schedules:
            raise self.exceptions.ResourceNotFoundException()
        self.schedules[req["Name"]] = req
        return {"ScheduleArn": f"arn:aws:scheduler:::schedule/{req['GroupName']}/{req['Name']}"}

    def delete_schedule(self, Name, GroupName):
        if Name not in self.schedules:
            raise self.exceptions.ResourceNotFoundException()
        del self.schedules[Name]


class FakeCognito:
    """One user pool: users, attributes, groups, enabled flag. Enough for the participant and staff pools."""

    class exceptions:
        class UserNotFoundException(Exception):
            pass

        class UsernameExistsException(Exception):
            pass

        class AliasExistsException(Exception):
            pass

    def __init__(self):
        self.users, self.disabled, self.groups, self.meta = {}, set(), {}, {}
        self.signed_out, self.resets, self.passwords = [], [], {}

    def _need(self, Username):
        if Username not in self.users:
            raise self.exceptions.UserNotFoundException()

    def admin_create_user(self, UserPoolId, Username, UserAttributes, MessageAction=None, DesiredDeliveryMediums=None,
                          TemporaryPassword=None):
        if Username in self.users:
            raise self.exceptions.UsernameExistsException()
        attrs = {a["Name"]: a["Value"] for a in UserAttributes}
        phone = attrs.get("phone_number")  # a verified phone number is a sign-in alias: one user per number
        if phone and any(u.get("phone_number") == phone for u in self.users.values()):
            raise self.exceptions.AliasExistsException()
        self.users[Username] = attrs
        self.passwords[Username] = TemporaryPassword
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.meta[Username] = {"UserStatus": "FORCE_CHANGE_PASSWORD", "UserCreateDate": now,
                               "UserLastModifiedDate": now}
        return {"User": self._raw(Username, "Attributes")}

    def _raw(self, Username, key):
        return {"Username": Username, key: [{"Name": k, "Value": v} for k, v in self.users[Username].items()],
                "Enabled": Username not in self.disabled, **self.meta.get(Username, {})}

    def admin_get_user(self, UserPoolId, Username):
        self._need(Username)
        return self._raw(Username, "UserAttributes")

    def list_users(self, UserPoolId, Limit=60, PaginationToken=None):
        return {"Users": [self._raw(u, "Attributes") for u in self.users]}

    def admin_set_user_password(self, UserPoolId="", Username="", Password=None, Permanent=False):
        self._need(Username)
        self.passwords[Username] = Password
        self.meta.setdefault(Username, {})["UserStatus"] = "CONFIRMED" if Permanent else "FORCE_CHANGE_PASSWORD"

    def admin_update_user_attributes(self, UserPoolId, Username, UserAttributes):
        self._need(Username)
        self.users[Username].update({a["Name"]: a["Value"] for a in UserAttributes})
        self.meta.setdefault(Username, {})["UserLastModifiedDate"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def admin_disable_user(self, UserPoolId, Username):
        self.disabled.add(Username)

    def admin_enable_user(self, UserPoolId, Username):
        self.disabled.discard(Username)

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.signed_out.append(Username)

    def admin_reset_user_password(self, UserPoolId, Username):
        self._need(Username)
        self.resets.append(Username)
        self.meta[Username]["UserStatus"] = "RESET_REQUIRED"

    def admin_list_groups_for_user(self, UserPoolId, Username):
        return {"Groups": [{"GroupName": g} for g in sorted(self.groups.get(Username, set()))]}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self._need(Username)
        self.groups.setdefault(Username, set()).add(GroupName)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.groups.get(Username, set()).discard(GroupName)


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_whatsapp_message(self, originationPhoneNumberId, message, metaApiVersion):
        self.sent.append(json.loads(message))
        return {"messageId": f"wamid.{uuid.uuid4().hex[:12]}"}


class FakeAgentCore:
    """Mimics the agent's response contract: {"reply", "usage", "trace"} with plausible random numbers.

    usage = {input_tokens 300-900, output_tokens 60-250, latency_ms, model}; trace = {trace_id, spans: [agent.invoke
    (SERVER) plus 1-3 tool.search_trial_docs children]}. A traceparent in the payload is continued, as the real agent
    does. `next` (a dict) replaces the next response, e.g. an error reply with no usage; `responses` records them.
    """

    MODEL = "fake-agent-model"

    def __init__(self):
        self.calls, self.responses, self.next = [], [], None

    def reply_for(self, body: dict) -> str:
        return f"echo: {body['text']}"

    def build_response(self, body: dict, reply: str) -> dict:
        rng = random.Random()
        tin, tout = rng.randint(300, 900), rng.randint(60, 250)
        m = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}", body.get("traceparent") or "")
        trace_id, remote_parent = (m.group(1), m.group(2)) if m else (f"{rng.getrandbits(128):032x}", None)
        started = datetime.now(timezone.utc)

        def iso(dt):
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        root_id, total_ms = f"{rng.getrandbits(64):016x}", rng.randint(900, 3500)
        spans = [{"span_id": root_id, "parent_id": remote_parent, "name": "agent.invoke", "start": iso(started),
                  "duration_ms": float(total_ms), "status": "OK", "kind": "SERVER",
                  "attributes": {"gen_ai.usage.input_tokens": tin, "gen_ai.usage.output_tokens": tout,
                                 "gen_ai.request.model": self.MODEL, "trialtalk.config_hash": "fake0000cafe"}}]
        for i in range(rng.randint(1, 3)):
            spans.append({"span_id": f"{rng.getrandbits(64):016x}", "parent_id": root_id,
                          "name": "tool.search_trial_docs", "start": iso(started + timedelta(milliseconds=50 + 100 * i)),
                          "duration_ms": float(rng.randint(40, 300)), "status": "OK", "kind": "INTERNAL",
                          "attributes": {"trialtalk.kb.results": rng.randint(1, 5),
                                         "trialtalk.kb.top_score": round(rng.uniform(0.3, 0.95), 3)}})
        return {"reply": reply, "usage": {"input_tokens": tin, "output_tokens": tout, "latency_ms": total_ms,
                                          "model": self.MODEL},
                "trace": {"trace_id": trace_id, "spans": spans}}

    def invoke_agent_runtime(self, agentRuntimeArn, runtimeSessionId, payload):
        body = json.loads(payload)
        self.calls.append({"session": runtimeSessionId, **body})
        out, self.next = self.next or self.build_response(body, self.reply_for(body)), None
        self.responses.append(out)
        return {"response": io.BytesIO(json.dumps(out).encode())}
