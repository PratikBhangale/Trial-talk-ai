import sys
from datetime import date
from pathlib import Path

import yaml
from strands import Agent
from strands.models import BedrockModel

import store
from tools import TOOLS

BASE_DIR = Path(__file__).parent
config = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())

# Which participant? Default P001, or pass one: python agent.py P002
pid = sys.argv[1] if len(sys.argv) > 1 else "P001"
profile = store.get_profile(pid)
if not profile:
    sys.exit(f"No participant {pid} in DynamoDB")

# Fill the prompt placeholders from the participant's profile
trial_day = (date.today() - date.fromisoformat(profile["enrolled_on"])).days + 1
system_prompt = config["system_prompt"].format(
    name=profile["name"],
    participant_id=pid,
    trial_id=profile["trial_id"],
    trial_day=trial_day,
    dose_times=", ".join(profile["dose_times"]),
)

model = BedrockModel(
    model_id=config["model"]["id"],
    region_name=config["model"]["region"],
)

agent = Agent(
    model=model,
    system_prompt=system_prompt,
    tools=[TOOLS[name] for name in config["tools"]],
    messages=store.get_history(pid, config["history_max_messages"]),
    state={"participant_id": pid},  # tools read the participant from here, never from the model
)

while True:
    text = input("\nParticipant: ")
    if text.lower() in ("quit", "exit"):
        break
    before = len(agent.messages)
    agent(text)
    # Save only what this turn added: the participant's message, tool calls/results, and the reply
    store.append_messages(pid, agent.messages[before:])