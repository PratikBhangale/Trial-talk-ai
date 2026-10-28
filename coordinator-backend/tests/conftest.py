"""Test setup.

Unit tests need nothing. Integration tests (test_api_*.py) need a local PostgreSQL 16:
set TT_PG_BIN to the folder holding initdb/pg_ctl, and they start a throwaway server,
build it from db/ (plus pending_db_additions.sql), and run the real handlers through a fake RDS Data API
that executes on that server as the same database logins the Lambdas use (see local_env.py).
"""

import json
import os
import types

import pytest

from local_env import FakeAgentCore, FakeCognito, FakeRdsData, FakeScheduler, FakeWhatsApp, LocalPostgres, wipe


@pytest.fixture(scope="session")
def pg():
    bin_dir = os.environ.get("TT_PG_BIN")
    if not bin_dir:
        pytest.skip("set TT_PG_BIN to run database integration tests")
    pytest.importorskip("psycopg")
    server = LocalPostgres(bin_dir)
    try:
        yield {"port": server.port, "admin_dsn": server.admin_dsn}
    finally:
        server.stop()


@pytest.fixture
def admin(pg):
    import psycopg
    with psycopg.connect(pg["admin_dsn"], autocommit=True) as c:
        yield c


@pytest.fixture(autouse=False)
def clean_db(admin):
    wipe(admin)
    yield


@pytest.fixture
def aws(pg, clean_db, monkeypatch):
    """Wire every Lambda module to the fakes."""
    from common import db, scheduling, whatsapp
    import staff_api.handler as staff
    import participant_api.handler as participant

    fakes = types.SimpleNamespace(rds=FakeRdsData(pg["port"]), scheduler=FakeScheduler(), cognito=FakeCognito(),
                                  whatsapp=FakeWhatsApp(), agent=FakeAgentCore(), emails=[])
    monkeypatch.setattr(db, "client", lambda: fakes.rds)
    monkeypatch.setattr(scheduling, "client", lambda: fakes.scheduler)
    monkeypatch.setattr(whatsapp, "client", lambda: fakes.whatsapp)
    monkeypatch.setattr(staff, "cognito", lambda: fakes.cognito)
    monkeypatch.setattr(staff, "PARTICIPANT_POOL_ID", "pool")
    monkeypatch.setattr(participant, "agentcore", lambda: fakes.agent)
    monkeypatch.setattr(participant, "ses", lambda: types.SimpleNamespace(
        send_email=lambda **kw: fakes.emails.append(kw)))
    monkeypatch.setattr(participant, "ALERT_FROM", "alerts@example.org")
    monkeypatch.setattr(participant, "ALERT_TO", {"RIVERSIDE": ["coordinator@riverside.example.org"],
                                                  "LAKESIDE": ["coordinator@lakeside.example.org"]})

    def invoke_reminder(FunctionName, InvocationType, Payload):
        import io
        import reminder.handler as rem
        monkeypatch.setattr(db, "SECRET_ARN", "messaging")
        try:
            result = rem.handler(json.loads(Payload), None)
        finally:
            monkeypatch.setattr(db, "SECRET_ARN", "staff")
        return {"Payload": io.BytesIO(json.dumps(result).encode())}

    monkeypatch.setattr(staff, "lambda_client", lambda: types.SimpleNamespace(invoke=invoke_reminder))
    return fakes


# ------------------------------------------------------------------ calling the handlers

def _event(method, path, claims, body=None, query=None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
    }


STAFF_CLAIMS = {"email": "coordinator@riverside.example.org", "cognito:groups": "[coordinator]",
                "custom:site_ids": "RIVERSIDE", "custom:study_ids": "KOA-301"}
OTHER_SITE_CLAIMS = {"email": "coordinator@lakeside.example.org", "cognito:groups": "[coordinator]",
                     "custom:site_ids": "LAKESIDE", "custom:study_ids": "KOA-301"}


@pytest.fixture
def call_staff(aws, monkeypatch):
    from common import db
    import staff_api.handler as staff

    def call(method, path, body=None, query=None, claims=STAFF_CLAIMS):
        monkeypatch.setattr(db, "SECRET_ARN", "staff")
        resp = staff.handler(_event(method, path, claims, body, query), None)
        return resp["statusCode"], json.loads(resp["body"])
    return call


@pytest.fixture
def call_participant(aws, monkeypatch):
    from common import db
    import participant_api.handler as participant

    def call(pid, method, path, body=None, query=None):
        monkeypatch.setattr(db, "SECRET_ARN", "participant")
        resp = participant.handler(_event(method, path, {"custom:participant_id": pid}, body, query), None)
        return resp["statusCode"], json.loads(resp["body"])
    return call
