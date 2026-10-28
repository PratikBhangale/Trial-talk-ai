"""Aurora access through the RDS Data API.

Each Lambda connects as its own database login (staff_api, participant_api, messaging_api),
which is a member of exactly one group role (portal_role, participant_role, messaging_role).
Every transaction starts with set_config(...) so row-level security can scope the rows.
"""

import json
import os
from contextlib import contextmanager
from datetime import date, datetime

import boto3

from common import telemetry

CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN", "")
SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
DATABASE = os.environ.get("DB_NAME", "trialtalk")

# Columns stored as jsonb / text[]; the Data API hands them back as JSON text.
JSON_COLUMNS = {
    "template_params", "change_log", "details", "attachments", "parsed_action",
    "buttons", "characteristics", "urgent_keywords",
}

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client("rds-data", region_name=os.environ.get("DB_REGION") or None)
    return _client


def _param(name: str, value) -> dict:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": name, "value": {"doubleValue": value}}
    if isinstance(value, (dict, list)):
        return {"name": name, "value": {"stringValue": json.dumps(value)}}
    if isinstance(value, (datetime, date)):
        return {"name": name, "value": {"stringValue": value.isoformat()}}
    return {"name": name, "value": {"stringValue": str(value)}}


def _decode(row: dict) -> dict:
    for key in JSON_COLUMNS & row.keys():
        if isinstance(row[key], str):
            try:
                row[key] = json.loads(row[key])
            except ValueError:
                pass
    return row


class Tx:
    def __init__(self, transaction_id: str | None):
        self.transaction_id = transaction_id

    def _run(self, sql: str, params: dict) -> dict:
        kwargs = {
            "resourceArn": CLUSTER_ARN,
            "secretArn": SECRET_ARN,
            "database": DATABASE,
            "sql": sql,
            "parameters": [_param(k, v) for k, v in params.items()],
            "formatRecordsAs": "JSON",
        }
        if self.transaction_id:
            kwargs["transactionId"] = self.transaction_id
        verb = (sql.lstrip().split(None, 1) or ["?"])[0].upper()  # the SQL verb only; never the statement
        with telemetry.span(f"db {verb}", **{"db.system": "postgresql", "db.operation.name": verb}):
            return client().execute_statement(**kwargs)

    def query(self, sql: str, **params) -> list[dict]:
        """Run a statement and return its rows as dicts (casts belong in the SQL, e.g. :id::uuid)."""
        resp = self._run(sql, params)
        rows = json.loads(resp.get("formattedRecords") or "[]")
        return [_decode(r) for r in rows]

    def one(self, sql: str, **params) -> dict | None:
        rows = self.query(sql, **params)
        return rows[0] if rows else None

    def execute(self, sql: str, **params) -> int:
        """Run a statement that returns no rows; returns the affected row count."""
        return self._run(sql, params).get("numberOfRecordsUpdated", 0)


@contextmanager
def tx(*, site_ids: list[str] | None = None, participant_id: str | None = None):
    """Open a transaction scoped for RLS. Commits on success, rolls back on any error."""
    c = client()
    tid = c.begin_transaction(resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, database=DATABASE)["transactionId"]
    t = Tx(tid)
    try:
        if site_ids is not None:
            t.query("SELECT set_config('app.site_ids', :v, true)", v=",".join(site_ids))
        if participant_id is not None:
            t.query("SELECT set_config('app.participant_id', :v, true)", v=participant_id)
        yield t
        c.commit_transaction(resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, transactionId=tid)
    except BaseException:
        c.rollback_transaction(resourceArn=CLUSTER_ARN, secretArn=SECRET_ARN, transactionId=tid)
        raise


def is_unique_violation(err: Exception) -> bool:
    return "duplicate key value violates unique constraint" in str(err)
