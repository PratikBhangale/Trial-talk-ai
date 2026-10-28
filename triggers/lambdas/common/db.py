"""Aurora access through the RDS Data API. Every call runs in one transaction."""
import json
import os
import time

import boto3

rds = boto3.client("rds-data")
DB = dict(resourceArn=os.environ["DB_CLUSTER_ARN"], secretArn=os.environ["DB_SECRET_ARN"],
          database=os.environ.get("DB_NAME", "trialtalk"))


def _param(k, v):
    if v is None:
        return {"name": k, "value": {"isNull": True}}
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    return {"name": k, "value": {"stringValue": str(v)}}


class Tx:
    """with Tx() as tx: rows = tx.q("SELECT ... :x", x=1)
    Pass every value as a string and cast in SQL (:id::uuid, :d::jsonb)."""

    def __init__(self, participant_id=None):
        self.participant_id = participant_id

    def __enter__(self):
        for attempt in range(6):
            try:
                self.id = rds.begin_transaction(**DB)["transactionId"]
                break
            except rds.exceptions.DatabaseResumingException:
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError("database did not resume")
        if self.participant_id:
            self.q("SELECT set_config('app.participant_id', :v, true)", v=self.participant_id)
        return self

    def q(self, sql, **params):
        r = rds.execute_statement(**DB, transactionId=self.id, sql=sql, formatRecordsAs="JSON",
                                  parameters=[_param(k, v) for k, v in params.items()])
        return json.loads(r.get("formattedRecords") or "[]")

    def one(self, sql, **params):
        rows = self.q(sql, **params)
        return rows[0] if rows else None

    def __exit__(self, exc_type, exc, tb):
        end = rds.rollback_transaction if exc_type else rds.commit_transaction
        end(resourceArn=DB["resourceArn"], secretArn=DB["secretArn"], transactionId=self.id)
        return False
