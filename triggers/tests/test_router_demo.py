"""Router demo mode (no AWS needed): unknown numbers chat as the shared demo participant when it is configured."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.update({"AGENT_RUNTIME_ARN": "arn:test", "DB_CLUSTER_ARN": "c", "DB_SECRET_ARN": "s",
                   "AWS_DEFAULT_REGION": "us-east-1", "DEMO_PARTICIPANT_ID": "P-DEMO"})
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas" / "router"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas" / "common"))
import handler as router  # noqa: E402


def run(m):
    return router.handler({"Records": [{"body": json.dumps(m)}]}, None)


def test_unknown_sender_text_goes_to_agent_with_private_key():
    m = {"participant_id": None, "phone": "+15551230000", "type": "text", "text": "hello", "comm_id": "c1"}
    with mock.patch.object(router, "ask_agent", return_value=("hi there", {"session_id": "x"})) as ask, \
            mock.patch.object(router, "reply") as reply:
        run(m)
    demo, text, key = ask.call_args.args
    assert demo["participant_id"] == "P-DEMO" and text == "hello"
    assert key.startswith("demo-") and "5551230000" not in key
    assert reply.call_args.args[:4] == (m, None, "en", "hi there")


def test_unknown_sender_button_still_gets_unknown_reply():
    m = {"participant_id": None, "phone": "+15551230000", "type": "button", "text": "", "comm_id": "c1"}
    with mock.patch.object(router, "ask_agent") as ask, mock.patch.object(router, "reply") as reply:
        run(m)
    ask.assert_not_called()
    assert reply.call_args.args[3] == router.t("en", "unknown")


def test_demo_off_keeps_unknown_reply():
    m = {"participant_id": None, "phone": "+15551230000", "type": "text", "text": "hello", "comm_id": "c1"}
    with mock.patch.object(router, "DEMO_PARTICIPANT_ID", ""), mock.patch.object(router, "ask_agent") as ask, \
            mock.patch.object(router, "reply") as reply:
        run(m)
    ask.assert_not_called()
    assert reply.call_args.args[3] == router.t("en", "unknown")
