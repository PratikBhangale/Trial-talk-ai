from bedrock_agentcore.runtime import BedrockAgentCoreApp

from core import handle_message

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore calls this once per message, e.g. {"participant_id": "P-0201", "text": "..."} Demo users also get "conversation_key" (set by the WhatsApp Lambda) for private memory."""
    pid = payload.get("participant_id")
    text = (payload.get("text") or "").strip()
    if not pid or not text:
        return {"error": "participant_id and text are required"}
    conversation_key = payload.get("conversation_key")  # set by our Lambda for demo users, never by the user
    try:
        return {"reply": handle_message(pid, text, conversation_key)}
    except Exception as e:
        print(f"Agent failed for {pid}: {e}")
        return {"reply": (
            "Sorry, I'm having trouble responding right now. "
            "If this is urgent, please contact your study site or emergency services directly."
        )}


if __name__ == "__main__":
    app.run()