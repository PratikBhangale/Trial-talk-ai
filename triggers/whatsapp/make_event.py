"""Write a fake WhatsApp SNS event to test_event.json. Usage: python make_event.py 1XXXXXXXXXX "message" """

import json
import sys

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

with open("test_event.json", "w") as f:
    json.dump(event, f, indent=2)
print("Wrote test_event.json")