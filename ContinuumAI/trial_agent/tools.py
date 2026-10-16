from pathlib import Path

import boto3
import yaml
from strands import ToolContext, tool

import store

BASE_DIR = Path(__file__).parent
CONFIG = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())
ses = boto3.client("sesv2", region_name=CONFIG["model"]["region"])


@tool(context=True)
def escalate_to_coordinator(severity: str, summary: str, tool_context: ToolContext = None) -> str:
    """Alert the study coordinator about a participant's symptom or concern.

    Args:
        severity: one of "emergency", "same_day_review", or "routine".
        summary: a short, factual summary of what the participant reported.
    """
    pid = tool_context.agent.state.get("participant_id")
    profile = store.get_profile(pid)
    subject = f"[{severity.upper()}] {profile['trial_id']} participant alert: {profile['name']} ({pid})"
    body = (
        f"Severity: {severity}\n"
        f"Participant: {profile['name']} ({pid}), trial {profile['trial_id']}\n\n"
        f"Summary:\n{summary}\n\n"
        f"Sent automatically by Continuum AI."
    )
    try:
        ses.send_email(
            FromEmailAddress=CONFIG["email"]["sender"],
            Destination={"ToAddresses": [CONFIG["email"]["coordinator"]]},
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


    
@tool
def search_trial_docs(question: str) -> str:
    """Look up the approved participant guide to answer a protocol question.

    Args:
        question: the participant's question in plain words.
    """
    return (BASE_DIR / "trial_docs.md").read_text()


# Maps the tool names used in agent.yaml to the functions above
TOOLS = {
    "escalate_to_coordinator": escalate_to_coordinator,
    "search_trial_docs": search_trial_docs,
}