"""Simulate an incoming WhatsApp message. Usage: python test_local.py 1XXXXXXXXXX "your message" """

import json
import os
import sys

os.environ.setdefault(
    "RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-2:461729033902:runtime/ContinuumAI_trial_agent-M50yJMBk8H",
)

import handler  # noqa: E402  (must come after setting RUNTIME_ARN)


def fake_send(origination_id, to, text):
    print(f"\n[WhatsApp reply to +{to}]\n{text}\n")


handler.send_whatsapp = fake_send  # print instead of sending, until the number is linked

sender, text = sys.argv[1], sys.argv[2]

whatsapp_payload = {
    "id": "test-waba",
    "changes": [{
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"profile": {"name": "Test"}, "wa_id": sender}],
            "messages": [{"from": sender, "id": "wamid.test", "type": "text", "text": {"body": text}}],
        },
    }],
}
header = {
    "context": {"MetaPhoneNumberIds": [{
        "metaPhoneNumberId": "test",
        "arn": "arn:aws:social-messaging:us-east-2:461729033902:phone-number-id/test",
    }]},
    "whatsAppWebhookEntry": json.dumps(whatsapp_payload),
}
event = {"Records": [{"Sns": {"Message": json.dumps(header)}}]}

handler.handler(event, None)