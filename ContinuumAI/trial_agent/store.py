"""All DynamoDB reads and writes for the agent."""

import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from boto3.dynamodb.conditions import Key

BASE_DIR = Path(__file__).parent
CONFIG = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())
TABLE = boto3.resource("dynamodb", region_name=CONFIG["model"]["region"]).Table(CONFIG["table_name"])


def get_profile(pid: str) -> dict | None:
    """Read the participant's PROFILE record."""
    return TABLE.get_item(Key={"PK": f"P#{pid}", "SK": "PROFILE"}).get("Item")


def get_history(pid: str, limit: int) -> list:
    """Return the most recent messages, oldest first, ready to give to the agent."""
    resp = TABLE.query(
        KeyConditionExpression=Key("PK").eq(f"P#{pid}") & Key("SK").begins_with("MSG#"),
        ScanIndexForward=False,  # newest first, so Limit keeps the most recent ones
        Limit=limit,
    )
    messages = [json.loads(item["message"]) for item in reversed(resp["Items"])]

    # Start at a plain participant message, so a tool call is never separated from its result.
    for i, m in enumerate(messages):
        if m["role"] == "user" and any("text" in c for c in m["content"]):
            return messages[i:]
    return []


def append_messages(pid: str, messages: list) -> None:
    """Append each new message as its own record. Nothing is ever overwritten."""
    for m in messages:
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        TABLE.put_item(Item={
            "PK": f"P#{pid}",
            "SK": f"MSG#{ts}",
            "role": m["role"],
            "message": json.dumps(m),
        })