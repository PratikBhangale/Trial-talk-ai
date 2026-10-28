import json
from pathlib import Path

import boto3
import yaml
from strands import ToolContext, tool

import store

import time



BASE_DIR = Path(__file__).parent
CONFIG = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())
ses = boto3.client("sesv2", region_name=CONFIG["model"]["region"])

DOCS_CFG = CONFIG.get("trial_docs", {})
s3 = boto3.client("s3", region_name=DOCS_CFG.get("region", "us-east-1"))

_docs_cache = {"text": None, "fetched_at": 0.0}
DOCS_TTL_SECONDS = 300  # re-read from S3 at most every 5 minutes


# Only these profile fields are ever shown to the agent
PROFILE_FIELDS = ("participant_id", "study_id", "site_id", "status", "preferred_language",
                  "timezone", "dose_reminders")

@tool(context=True)
def get_participant_profile(tool_context: ToolContext = None) -> str:
    """Look up the current participant's profile: name, trial, enrollment date, dose times, and timezone.
    Use this when you need details about the participant you are talking to.
    """
    pid = tool_context.agent.state.get("participant_id")  # set by code, never by the model
    profile = store.get_profile(pid)
    if not profile:
        return "No profile is on file for this participant yet. Do not guess their details."
    return json.dumps({k: profile[k] for k in PROFILE_FIELDS if k in profile}, default=str)


@tool(context=True)
def escalate_to_coordinator(severity: str, summary: str, tool_context: ToolContext = None) -> str:
    """Alert the study coordinator about a participant's symptom or concern.

    Args:
        severity: one of "emergency", "same_day_review", or "routine".
        summary: a short, factual summary of what the participant reported.
    """
    pid = tool_context.agent.state.get("participant_id")
    profile = store.get_profile(pid) or {}
    name = f"site {profile.get('site_id', 'unknown')}"
    trial = profile.get("study_id", "unknown study")

    subject = f"[{severity.upper()}] {trial} participant alert: {name} ({pid})"
    body = (
        f"Severity: {severity}\n"
        f"Participant: {name} ({pid}), trial {trial}\n\n"
        f"Summary:\n{summary}\n\n"
        f"Sent automatically by Continuum AI."
    )

    coordinator = CONFIG.get("email", {}).get("coordinator")
    if not coordinator:
        print("\n*** EMAIL FAILED: no coordinator email configured ***\n")
        return "Alert could not be sent automatically. Tell the participant to contact their study site directly."

    try:
        ses.send_email(
            FromEmailAddress=CONFIG["email"]["sender"],
            Destination={"ToAddresses": [coordinator]},
            Content={"Simple": {
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            }},
        )
        print(f"\n*** EMAIL SENT [{severity}] ***\n")
        return f"Coordinator alerted by email with severity {severity}."
    except Exception as e:
        print(f"\n*** EMAIL FAILED: {e} ***\n")
        return "Alert could not be sent automatically. Tell the participant to contact their study site directly."




def _load_trial_docs() -> str | None:
    """Fetch the participant guide from S3, cached briefly. Falls back to the last good copy."""
    now = time.time()
    if _docs_cache["text"] and now - _docs_cache["fetched_at"] < DOCS_TTL_SECONDS:
        return _docs_cache["text"]
    try:
        obj = s3.get_object(Bucket=DOCS_CFG["bucket"], Key=DOCS_CFG.get("key", "trial_docs.md"))
        _docs_cache["text"] = obj["Body"].read().decode("utf-8")
        _docs_cache["fetched_at"] = now
    except Exception as e:
        print(f"Trial docs fetch failed: {e}")
    return _docs_cache["text"]  # last good copy, or None if never loaded


@tool
def search_trial_docs(question: str) -> str:
    """Look up the approved participant guide to answer a protocol question.

    Args:
        question: the participant's question in plain words.
    """
    docs = _load_trial_docs()
    if not docs:
        return ("The participant guide is unavailable right now. Do not answer protocol questions "
                "from memory. Tell the participant to contact their study site.")
    return docs

# Maps the tool names used in root_agent.yaml to the functions above
TOOLS = {
    "get_participant_profile": get_participant_profile,
    "escalate_to_coordinator": escalate_to_coordinator,
    "search_trial_docs": search_trial_docs,
}