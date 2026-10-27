"""CloudFormation custom resource: applies schema.sql and creates the per-Lambda database logins.

Runs as the cluster's master user through the Data API. Safe to re-run: every statement is idempotent.
"""

import json
import os
from pathlib import Path

import boto3

CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN", "")
MASTER_SECRET_ARN = os.environ.get("DB_MASTER_SECRET_ARN", "")
DATABASE = os.environ.get("DB_NAME", "trialtalk")
# {"staff_api": "<secret arn>", ...}: login name -> secret holding its password
LOGIN_SECRETS = json.loads(os.environ.get("LOGIN_SECRETS", "{}"))
# login name -> the one group role it joins
LOGIN_ROLES = {"staff_api": "portal_role", "participant_api": "participant_role", "messaging_api": "messaging_role",
               "agent_api": "agent_role"}

rds = boto3.client("rds-data")
secrets = boto3.client("secretsmanager")


def split_sql(sql: str) -> list[str]:
    """Split on ';' at statement level, keeping $$ ... $$ bodies, quotes and comments intact."""
    statements, buf, i, n = [], [], 0, len(sql)
    in_dollar = in_single = False
    while i < n:
        ch = sql[i]
        if not in_dollar and not in_single and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            buf.append("\n")
            continue
        if not in_single and sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if not in_dollar and ch == "'":
            in_single = not in_single
        if ch == ";" and not in_dollar and not in_single:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def run(sql: str) -> None:
    rds.execute_statement(resourceArn=CLUSTER_ARN, secretArn=MASTER_SECRET_ARN, database=DATABASE, sql=sql)


def ensure_login(name: str, role: str, secret_arn: str) -> None:
    password = json.loads(secrets.get_secret_value(SecretId=secret_arn)["SecretString"])["password"]
    if "'" in password:
        raise ValueError(f"Password for {name} contains a quote; regenerate the secret")
    run(f"""DO $$ BEGIN
      CREATE ROLE {name} LOGIN;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$""")
    run(f"ALTER ROLE {name} WITH LOGIN PASSWORD '{password}'")
    run(f"GRANT {role} TO {name}")
    run(f"GRANT CONNECT ON DATABASE {DATABASE} TO {name}")


def handler(event, context):
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "trialtalk-schema")}

    sql = (Path(__file__).parent / "schema.sql").read_text()
    for stmt in split_sql(sql):
        run(stmt)
    for name, secret_arn in LOGIN_SECRETS.items():
        ensure_login(name, LOGIN_ROLES[name], secret_arn)

    return {"PhysicalResourceId": "trialtalk-schema"}
