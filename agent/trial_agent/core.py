"""One participant message in, one reply out. Conversation memory lives in AgentCore Memory."""

import os

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel

from config import config_version, get_config
from tools import TOOLS

MEMORY_ID = os.environ["MEMORY_ID"]


def build_agent(pid: str, conversation_key: str | None = None) -> Agent:
    cfg = get_config()   # from S3 (cached), so prompt changes apply without a redeploy

    # Demo users share the P-DEMO profile but get their own private memory (conversation_key).
    # Real participants have no key, so their memory is keyed by their participant ID.
    memory_owner = conversation_key or pid
    session_manager = AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            actor_id=memory_owner,
            session_id=f"participant-{memory_owner}",
        ),
        region_name=cfg["model"]["region"],
    )

    return Agent(
        model=BedrockModel(model_id=cfg["model"]["id"], region_name=cfg["model"]["region"]),
        system_prompt=cfg["system_prompt"].format(participant_id=pid),
        tools=[TOOLS[name] for name in cfg["tools"] if name in TOOLS],   # unknown names are skipped
        session_manager=session_manager,
        conversation_manager=SlidingWindowConversationManager(window_size=cfg["history_max_messages"]),
        state={"participant_id": pid},  # database reads always use the participant ID, never the key
        callback_handler=None,
    )


def handle_message(pid: str, text: str, conversation_key: str | None = None) -> str:
    agent = build_agent(pid, conversation_key)
    print(f"Handling message for {pid} with config version {config_version()}")
    return str(agent(text)).strip()