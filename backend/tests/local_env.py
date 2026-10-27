"""Local stand-ins for AWS, shared by the tests (conftest.py) and the dev server (devserver/server.py).

start_postgres() runs a throwaway PostgreSQL 16 with the schema and the Lambdas' database logins.
The Fake* classes replace the boto3 clients: FakeRdsData runs Data API calls on that server as the
same login the Lambda would use, so grants and RLS are exercised for real.
"""

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

# boto3 isn't needed locally; every AWS client is replaced with a fake.
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        _fake = types.ModuleType("boto3")
        _fake.client = lambda *a, **k: None
        _fake.resource = lambda *a, **k: None
        sys.modules["boto3"] = _fake

LOGINS = {"staff": "staff_api", "participant": "participant_api", "messaging": "messaging_api"}
PASSWORD = "test-password-123"


# ------------------------------------------------------------------ throwaway Postgres

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalPostgres:
    """initdb + pg_ctl start on a free port, schema applied (twice: it must be idempotent), logins created."""

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

            from migrate.handler import LOGIN_ROLES, split_sql
            schema = (LAMBDAS / "migrate" / "schema.sql").read_text()
            with psycopg.connect(self.admin_dsn, autocommit=True) as c:
                for _ in range(2):
                    for stmt in split_sql(schema):
                        c.execute(stmt)
                for login, role in LOGIN_ROLES.items():
                    c.execute(f"DO $$ BEGIN CREATE ROLE {login} LOGIN; "
                              "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
                    c.execute(f"ALTER ROLE {login} WITH LOGIN PASSWORD '{PASSWORD}'")
                    c.execute(f"GRANT {role} TO {login}")
        except BaseException:
            self.stop()
            raise

    def _exe(self, name: str) -> str:
        return str(self.bin_dir / name)

    def stop(self) -> None:
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-m", "fast", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.data, ignore_errors=True)


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
        conn = psycopg.connect(f"host=127.0.0.1 port={self.port} dbname=trialtalk user={user} password={PASSWORD}")
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
                          transactionId=None):
        conn = self.tx.get(transactionId) if transactionId else self._connect(secretArn)
        params = {p["name"]: _to_python(p["value"]) for p in parameters}
        pg_sql = _PARAM.sub(lambda m: f"%({m.group(1)})s", sql.replace("%", "%%"))
        with conn.cursor() as cur:
            cur.execute(pg_sql, params)
            out = {"numberOfRecordsUpdated": max(cur.rowcount, 0) if cur.description is None else 0}
            if cur.description is not None:
                cols = [d.name for d in cur.description]
                rows = [{c: _to_json_value(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
                out["formattedRecords"] = json.dumps(rows)
        if not transactionId:
            conn.commit()
            conn.close()
        return out


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

    def __init__(self):
        self.users, self.disabled, self.groups, self.meta = {}, set(), {}, {}
        self.signed_out, self.resets = [], []

    def _need(self, Username):
        if Username not in self.users:
            raise self.exceptions.UserNotFoundException()

    def admin_create_user(self, UserPoolId, Username, UserAttributes, MessageAction=None, DesiredDeliveryMediums=None):
        if Username in self.users:
            raise self.exceptions.UsernameExistsException()
        self.users[Username] = {a["Name"]: a["Value"] for a in UserAttributes}
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

    def admin_set_user_password(self, **kw):
        self.meta.setdefault(kw["Username"], {})["UserStatus"] = "CONFIRMED"

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


def fake_usage(text: str, reply: str) -> dict:
    """Made-up token counts (about 4 characters per token, plus a fixed system prompt) until the real agent reports them."""
    return {"input_tokens": 420 + len(text) // 4, "output_tokens": max(1, len(reply) // 4), "model_id": "fake-agent-model"}



class FakeAgentCore:
    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, agentRuntimeArn, runtimeSessionId, payload):
        body = json.loads(payload)
        self.calls.append({"session": runtimeSessionId, **body})
        reply = f"echo: {body['text']}"
        return {"response": io.BytesIO(json.dumps({"reply": reply, "usage": fake_usage(body["text"], reply)}).encode())}
