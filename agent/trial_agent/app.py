from bedrock_agentcore.runtime import BedrockAgentCoreApp

import tracing
from core import handle_message

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore calls this once per message, e.g. {"participant_id": "P-0201", "text": "...", "traceparent": "00-..."}.
    Demo users also get "conversation_key" (set by the WhatsApp Lambda) for private memory.

    Returns {"reply", "usage": {input_tokens, output_tokens, latency_ms, model}, "trace": {trace_id, spans}}.
    The caller persists the trace; callers that only read "reply" are unaffected."""
    traceparent = payload.get("traceparent")  # W3C header from the caller; missing/invalid starts a new trace
    pid = payload.get("participant_id")
    text = (payload.get("text") or "").strip()
    if not pid or not text:
        return {"error": "participant_id and text are required", "trace": tracing.empty_trace(traceparent)}
    conversation_key = payload.get("conversation_key")  # set by our Lambda for demo users, never by the user
    try:
        reply, usage, trace = handle_message(pid, text, conversation_key, traceparent=traceparent)
        return {"reply": reply, "usage": usage, "trace": trace}
    except Exception as e:
        print(f"Agent failed for {pid}: {e}")
        return {
            "reply": (
                "Sorry, I'm having trouble responding right now. "
                "If this is urgent, please contact your study site or emergency services directly."
            ),
            "trace": getattr(e, "trace", None) or tracing.empty_trace(traceparent),
        }


if __name__ == "__main__":
    app.run()
