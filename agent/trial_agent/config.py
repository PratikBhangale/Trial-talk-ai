"""Agent configuration (root_agent.yaml), loaded from S3 with a short cache and a safe fallback.

Order of preference: fresh copy from S3 -> last good copy from S3 -> root_agent.yaml bundled with the code.
"""

import os
import time
from pathlib import Path

import boto3
import yaml

BASE_DIR = Path(__file__).parent
BUCKET = os.environ.get("CONFIG_BUCKET", "trialtalk-kb-docs-180026596905")
KEY = os.environ.get("CONFIG_KEY", "agent-config/root_agent.yaml")
TTL_SECONDS = int(os.environ.get("CONFIG_TTL_SECONDS", "300"))   # re-read from S3 at most every 5 minutes
REQUIRED_KEYS = ("model", "tools", "system_prompt", "history_max_messages")

_s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_bundled = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())
_cache = {"config": None, "fetched_at": 0.0, "version": None}


def _validate(cfg) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("config is not a YAML mapping")
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ValueError(f"config is missing {missing}")
    if "{participant_id}" not in cfg["system_prompt"]:
        raise ValueError("system_prompt must contain {participant_id}")


def get_config() -> dict:
    now = time.time()
    if _cache["config"] and now - _cache["fetched_at"] < TTL_SECONDS:
        return _cache["config"]
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=KEY)
        cfg = yaml.safe_load(obj["Body"].read())
        _validate(cfg)
        version = obj.get("VersionId") or obj.get("ETag", "").strip('"')
        if version != _cache["version"]:
            print(f"Loaded agent config from s3://{BUCKET}/{KEY} (version {version})")
        _cache.update(config=cfg, version=version)
    except Exception as e:
        fallback = "last good S3 copy" if _cache["config"] else "bundled root_agent.yaml"
        print(f"Agent config load failed ({e}); using {fallback}")
        if not _cache["config"]:
            _cache.update(config=_bundled, version="bundled")
    _cache["fetched_at"] = now   # wait for the TTL before trying S3 again, even after a failure
    return _cache["config"]


def config_version() -> str | None:
    return _cache["version"]