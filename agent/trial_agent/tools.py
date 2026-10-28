import json
import os

import boto3
from strands import ToolContext, tool

import store
from config import get_config

REGION = "us-east-1"
ses = boto3.client("sesv2", region_name=REGION)
kb = boto3.client("bedrock-agent-runtime", region_name=REGION)
# Read lazily-tolerant: a missing id degrades search_trial_docs instead of crashing the whole agent
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID")

# Only these profile fields are ever shown to the agent
PROFILE_FIELDS = ("participant_id", "study_id", "site_id", "status", "preferred_language",
                  "timezone", "dose_reminders")

SEVERITY_HINT = {"emergency": "urgent", "same_day_review": "moderate", "routine": "mild"}


@tool(context=True)
def escalate_to_coordinator(severity: str, summary: str, participant_words: str = "",
                            is_symptom: bool = True, tool_context: ToolContext = None) -> str:
    """Alert the study coordinator about a participant's symptom or concern.
    Symptoms are also recorded in the participant's clinical record.

    Args:
        severity: one of "emergency", "same_day_review", or "routine".
        summary: a short, factual summary of what the participant reported.
        participant_words: the participant's own words describing the problem, quoted as closely as possible.
        is_symptom: true for symptoms, side effects, or health changes; false for other requests
            (questions the documents don't answer, withdrawal, rescheduling, supply problems).
    """
    pid = tool_context.agent.state.get("participant_id")
    profile = store.get_profile(pid) or {}
    name = f"site {profile.get('site_id', 'unknown')}"
    trial = profile.get("study_id", "unknown study")

    # 1. Record the symptom first, so the clinical record exists even if the email fails
    event_id = None
    if is_symptom:
        try:
            event_id = store.record_symptom(pid, participant_words or summary, summary,
                                            SEVERITY_HINT.get(severity, "moderate"))
        except Exception as e:
            print(f"\n*** SYMPTOM RECORD FAILED: {e} ***\n")   # still send the alert

    subject = f"[{severity.upper()}] {trial} participant alert: {name} ({pid})"
    body = (
        f"Severity: {severity}\n"
        f"Participant: {name} ({pid}), trial {trial}\n\n"
        f"Summary:\n{summary}\n\n"
        + (f"Participant's words:\n\"{participant_words}\"\n\n" if participant_words else "")
        + (f"Clinical record: {event_id}\n\n" if event_id else "")
        + "Sent automatically by Continuum AI."
    )

    email_cfg = get_config().get("email", {})
    sender, coordinator = email_cfg.get("sender"), email_cfg.get("coordinator")
    if not sender or not coordinator:
        print("\n*** EMAIL FAILED: no sender or coordinator email configured ***\n")
        return "Alert could not be sent automatically. Tell the participant to contact their study site directly."

    # 2. Send the email
    message_id, status = None, "sent"
    try:
        resp = ses.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [coordinator]},
            Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}},
        )
        message_id = resp["MessageId"]
        print(f"\n*** EMAIL SENT [{severity}] ***\n")
    except Exception as e:
        status = "failed"
        print(f"\n*** EMAIL FAILED: {e} ***\n")

    # 3. Log the email in the communications audit trail, sent or failed
    try:
        store.log_email(pid, sender, coordinator, subject, body, event_id, message_id, status)
    except Exception as e:
        print(f"\n*** EMAIL LOG FAILED: {e} ***\n")

    if status == "failed":
        return "Alert could not be sent automatically. Tell the participant to contact their study site directly."
    return f"Coordinator alerted by email with severity {severity}."

@tool
def search_trial_docs(question: str) -> str:
    """Search the approved trial documents for passages that answer a protocol question.

    Args:
        question: the participant's question in plain words.
    """
    unavailable = ("The trial documents could not be searched right now. "
                   "Tell the participant the study team will reply, and escalate with severity routine.")
    if not KNOWLEDGE_BASE_ID:
        print("\n*** KB SEARCH FAILED: KNOWLEDGE_BASE_ID is not set ***\n")
        return unavailable

    try:
        resp = kb.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": question},
            retrievalConfiguration={"managedSearchConfiguration": {
                "numberOfResults": get_config().get("knowledge_base", {}).get("max_results", 5),
            }},
        )
    except Exception as e:
        print(f"\n*** KB SEARCH FAILED: {e} ***\n")
        return unavailable

    passages = []
    for r in resp.get("retrievalResults", []):
        text = r.get("content", {}).get("text")
        if text:
            uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
            passages.append(f"[{uri.rsplit('/', 1)[-1] or 'trial document'}]\n{text}")
    if not passages:
        return "No passage in the trial documents covers this question."
    return "\n\n---\n\n".join(passages)


@tool(context=True)
def get_clinical_history(record_type: str = "all", days: int = 14, tool_context: ToolContext = None) -> str:
    """Look up the participant's recorded study history: doses (taken, skipped, or unanswered),
    symptoms they reported, and study visits.
    Use this when they ask about past doses, missed doses, symptoms they reported earlier,
    or their visits, and treat it as the official record.

    Args:
        record_type: "all", or one of "DOSE", "SYMPTOM", "VISIT", "VISIT_CHANGE", "CONSENT_Q".
        days: how many days back to look, from 1 to 90. Default 14.
    """
    pid = tool_context.agent.state.get("participant_id")  # set by code, never by the model
    try:
        records = store.get_clinical_history(pid, record_type.upper(), days)
    except Exception as e:
        print(f"\n*** HISTORY LOOKUP FAILED: {e} ***\n")
        return "The participant's records could not be loaded right now. Do not guess; offer to have the study team follow up."
    if not records:
        return f"No {record_type.lower() if record_type != 'all' else ''} records in the last {days} days.".replace("  ", " ")
    return json.dumps(records, default=str)

# Maps the tool names used in root_agent.yaml to the functions above
TOOLS = {
    "get_clinical_history": get_clinical_history,
    "escalate_to_coordinator": escalate_to_coordinator,
    "search_trial_docs": search_trial_docs,
}