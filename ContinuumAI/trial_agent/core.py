"""One participant message in, one reply out. Used by both cli.py and app.py."""

from datetime import date
from pathlib import Path

import yaml
from strands import Agent
from strands.models import BedrockModel

import store
from tools import TOOLS

BASE_DIR = Path(__file__).parent
CONFIG = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())


def build_agent(pid: str) -> Agent:
    profile = store.get_profile(pid)
    if not profile:
        raise ValueError(f"No participant {pid} in DynamoDB")

    trial_day = (date.today() - date.fromisoformat(profile["enrolled_on"])).days + 1
    system_prompt = CONFIG["system_prompt"].format(
        name=profile["name"],
        participant_id=pid,
        trial_id=profile["trial_id"],
        trial_day=trial_day,
        dose_times=", ".join(profile["dose_times"]),
    )

    return Agent(
        model=BedrockModel(model_id=CONFIG["model"]["id"], region_name=CONFIG["model"]["region"]),
        system_prompt=system_prompt,
        tools=[TOOLS[name] for name in CONFIG["tools"]],
        messages=store.get_history(pid, CONFIG["history_max_messages"]),
        state={"participant_id": pid},
        callback_handler=None,  # don't print while running; the caller decides what to show
    )


def handle_message(pid: str, text: str) -> str:
    agent = build_agent(pid)
    before = len(agent.messages)
    result = agent(text)
    store.append_messages(pid, agent.messages[before:])
    return str(result).strip()