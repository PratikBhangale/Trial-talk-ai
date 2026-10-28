import sys

from core import handle_message

pid = sys.argv[1] if len(sys.argv) > 1 else "P001"

while True:
    text = input("\nParticipant: ")
    if text.lower() in ("quit", "exit"):
        break
    reply, usage, _trace = handle_message(pid, text)
    print(f"\nAgent: {reply}\n  [{usage['input_tokens']} in / {usage['output_tokens']} out tokens, {usage['latency_ms']} ms]")