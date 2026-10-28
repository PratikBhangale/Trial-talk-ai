import json
import os
from pathlib import Path

import boto3
import yaml
from strands import ToolContext, tool

import store
import tracing

BASE_DIR = Path(__file__).parent
CONFIG = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())
ses = boto3.client("sesv2", region_name=CONFIG["model"]["region"])
kb = boto3.client("bedrock-agent-runtime", region_name=CONFIG["model"]["region"])
# Read lazily-tolerant: a missing id degrades search_trial_docs instead of crashing the whole agent
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID")

# Only these profile fields are ever shown to the agent. They match agent_role's column grant on
# pii.participants (db/02_roles_rls.sql: no name, contact details or date of birth) plus the
# participant's active dose reminders from trial.reminders (SELECT granted, RLS-scoped).
PROFILE_FIELDS = ("participant_id", "study_id", "site_id", "status", "preferred_language",
                  "timezone", "dose_reminders")
SEVERITIES = ("emergency", "same_day_review", "routine")


@tool(context=True)
def get_participant_profile(tool_context: ToolContext = None) -> str:
    """Look up the current participant's trial details: study, study site, enrollment status, preferred
    language, timezone, and active dose reminders (label, and schedule as a cron expression in the
    reminder's own timezone). It does not include their name or contact details.
    Use this when you need details about the participant you are talking to.
    """
    # Counts only: the profile is participant data, so no field values go into the span
    with tracing.span("tool.get_participant_profile") as span:
        pid = tool_context.agent.state.get("participant_id")  # set by code, never by the model
        try:
            profile = store.get_profile(pid)
        except Exception as e:
            print(f"\n*** PROFILE LOOKUP FAILED: {type(e).__name__} ***\n")
            tracing.mark_error(span, f"profile lookup failed: {type(e).__name__}")
            return ("The participant's profile could not be loaded right now. "
                    "Do not guess their details; tell them the study team can confirm.")
        if not profile:
            tracing.set_attrs(span, **{"trialtalk.profile.found": False})
            return "No profile is on file for this participant yet. Do not guess their details."
        shown = {k: profile[k] for k in PROFILE_FIELDS if k in profile}
        tracing.set_attrs(span, **{
            "trialtalk.profile.found": True,
            "trialtalk.profile.fields": len(shown),
            "trialtalk.profile.dose_reminders": len(shown.get("dose_reminders") or []),
        })
        return json.dumps(shown, default=str)


@tool(context=True)
def escalate_to_coordinator(severity: str, summary: str, tool_context: ToolContext = None) -> str:
    """Alert the study coordinator about a participant's symptom or concern.

    Args:
        severity: one of "emergency", "same_day_review", or "routine".
        summary: a short, factual summary of what the participant reported.
    """
    # Severity comes from the model: record only the known values, never free text
    with tracing.span("tool.escalate_to_coordinator", **{
        "trialtalk.escalation.severity": severity if severity in SEVERITIES else "other",
    }) as span:
        pid = tool_context.agent.state.get("participant_id")
        # The alert must go out even if the database is down: site/study degrade to "unknown"
        try:
            profile = store.get_profile(pid) or {}
        except Exception as e:
            print(f"\n*** PROFILE LOOKUP FAILED during escalation: {type(e).__name__} ***\n")
            tracing.set_attrs(span, **{"trialtalk.profile.lookup_failed": type(e).__name__})
            profile = {}
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
            tracing.mark_error(span, "no coordinator email configured")
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
            tracing.mark_error(span, f"email send failed: {type(e).__name__}")
            return "Alert could not be sent automatically. Tell the participant to contact their study site directly."


@tool
def search_trial_docs(question: str) -> str:
    """Search the approved trial documents for passages that answer a protocol question.

    Args:
        question: the participant's question in plain words.
    """
    with tracing.span("tool.search_trial_docs") as span:  # counts and scores only, never the question
        unavailable = ("The trial documents could not be searched right now. "
                       "Tell the participant the study team will reply, and escalate with severity routine.")
        if not KNOWLEDGE_BASE_ID:
            print("\n*** KB SEARCH FAILED: KNOWLEDGE_BASE_ID is not set ***\n")
            tracing.mark_error(span, "KNOWLEDGE_BASE_ID is not set")
            return unavailable

        try:
            resp = kb.retrieve(
                knowledgeBaseId=KNOWLEDGE_BASE_ID,
                retrievalQuery={"text": question},
                # Managed knowledge bases take managedSearchConfiguration, not vectorSearchConfiguration
                retrievalConfiguration={"managedSearchConfiguration": {
                    "numberOfResults": CONFIG.get("knowledge_base", {}).get("max_results", 5),
                }},
            )
        except Exception as e:
            print(f"\n*** KB SEARCH FAILED: {e} ***\n")
            tracing.mark_error(span, f"kb retrieve failed: {type(e).__name__}")
            return unavailable

        results = resp.get("retrievalResults", [])
        scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
        tracing.set_attrs(span, **{
            "trialtalk.kb.results": len(results),
            "trialtalk.kb.top_score": max(scores) if scores else None,
        })
        passages = []
        for r in results:
            text = r.get("content", {}).get("text")
            if text:
                uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
                passages.append(f"[{uri.rsplit('/', 1)[-1] or 'trial document'}]\n{text}")
        if not passages:
            return "No passage in the trial documents covers this question."
        return "\n\n---\n\n".join(passages)

# Maps the tool names used in root_agent.yaml to the functions above
TOOLS = {
    "get_participant_profile": get_participant_profile,
    "escalate_to_coordinator": escalate_to_coordinator,
    "search_trial_docs": search_trial_docs,
}