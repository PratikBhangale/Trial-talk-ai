from bedrock_agentcore.runtime import BedrockAgentCoreApp

from core import handle_message

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore calls this once per message, e.g. {"participant_id": "P001", "text": "..."}"""
    pid = payload.get("participant_id")
    text = (payload.get("text") or "").strip()
    if not pid or not text:
        return {"error": "participant_id and text are required"}
    return {"reply": handle_message(pid, text)}


if __name__ == "__main__":
    app.run()