import sys

from core import handle_message

pid = sys.argv[1] if len(sys.argv) > 1 else "P001"

while True:
    text = input("\nParticipant: ")
    if text.lower() in ("quit", "exit"):
        break
    print(f"\nAgent: {handle_message(pid, text)}")