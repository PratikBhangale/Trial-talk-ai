"""One participant message in, one reply out. Conversation memory lives in AgentCore Memory.
Each message is traced (root span agent.invoke) and its token usage read from the Strands result."""

import hashlib
import os
import time
from pathlib import Path

import yaml
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel

import tracing
from tools import TOOLS

BASE_DIR = Path(__file__).parent
_CONFIG_BYTES = (BASE_DIR / "root_agent.yaml").read_bytes()
CONFIG = yaml.safe_load(_CONFIG_BYTES)
# Identifies the prompt/model/config version on every trace, so the dashboard can mark config changes
CONFIG_HASH = hashlib.sha256(_CONFIG_BYTES).hexdigest()[:12]
MODEL_ID = CONFIG["model"]["id"]
MEMORY_ID = os.environ["MEMORY_ID"]
_usage_warned = False


class AgentInvokeError(Exception):
    """The agent failed; .trace holds the spans collected up to the failure."""

    def __init__(self, cause: Exception, trace: dict):
        super().__init__(str(cause))
        self.trace = trace


def build_agent(pid: str, conversation_key: str | None = None) -> Agent:
    # Demo users share the P-DEMO profile but get their own private memory (conversation_key).
    # Real participants have no key, so their memory is keyed by their participant ID.
    memory_owner = conversation_key or pid

    session_manager = AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            actor_id=memory_owner,
            session_id=f"participant-{memory_owner}",
        ),
        region_name=CONFIG["model"]["region"],
    )

    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=CONFIG["model"]["region"]),
        system_prompt=CONFIG["system_prompt"].format(participant_id=pid),
        tools=[TOOLS[name] for name in CONFIG["tools"]],
        session_manager=session_manager,
        conversation_manager=SlidingWindowConversationManager(window_size=CONFIG["history_max_messages"]),
        state={"participant_id": pid},  # database reads always use the participant ID, never the key
        callback_handler=None,
    )


def _token_usage(result) -> tuple[int, int]:
    """(input, output) tokens from a Strands AgentResult: result.metrics.accumulated_usage, a Usage
    TypedDict with camelCase keys (verified on strands-agents 1.62). Zeros if the shape ever changes."""
    global _usage_warned
    try:
        u = result.metrics.accumulated_usage
        return int(u["inputTokens"]), int(u["outputTokens"])
    except Exception as e:
        if not _usage_warned:
            _usage_warned = True
            print(f"tracing: token usage unavailable on the agent result ({type(e).__name__}); reporting zeros")
        return 0, 0


def handle_message(pid: str, text: str, conversation_key: str | None = None,
                   traceparent: str | None = None) -> tuple[str, dict, dict]:
    """Returns (reply, usage, trace). On failure raises AgentInvokeError carrying the partial trace."""
    started = time.monotonic()
    with tracing.collect(traceparent) as collector:
        try:
            with tracing.span("agent.invoke", kind="SERVER", **{
                "gen_ai.request.model": MODEL_ID,
                "trialtalk.config_hash": CONFIG_HASH,
            }) as root:
                agent = build_agent(pid, conversation_key)
                result = agent(text)
                input_tokens, output_tokens = _token_usage(result)
                tracing.set_attrs(root, **{
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                })
                reply = str(result).strip()
        except Exception as e:
            raise AgentInvokeError(e, collector.result()) from e

    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "model": MODEL_ID,
    }
    return reply, usage, collector.result()
