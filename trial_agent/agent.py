import json
from pathlib import Path

import yaml
from strands import Agent
from strands.models import BedrockModel

from tools import TOOLS

BASE_DIR = Path(__file__).parent

# Load settings from agent.yaml
config = yaml.safe_load((BASE_DIR / "root_agent.yaml").read_text())

model = BedrockModel(
    model_id=config["model"]["id"],
    region_name=config["model"]["region"],
)

agent = Agent(
    model=model,
    system_prompt=config["system_prompt"],
    tools=[TOOLS[name] for name in config["tools"]],
    messages=json.loads((BASE_DIR / config["history_file"]).read_text()),
)

while True:
    text = input("\nParticipant: ")
    if text.lower() in ("quit", "exit"):
        break
    agent(text)