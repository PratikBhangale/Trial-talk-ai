"""Business-initiated WhatsApp sends through AWS End User Messaging Social. Approved templates only."""

import json
import os

import boto3

from common import telemetry

ORIGINATION_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
META_API_VERSION = "v20.0"

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client("socialmessaging")
    return _client


def ordered_params(template_params: dict) -> list[str]:
    """{"1": "evening", "2": "with food"} -> ["evening", "with food"]; non-numeric keys are ignored."""
    keys = sorted((k for k in template_params if str(k).isdigit()), key=int)
    return [str(template_params[k]) for k in keys]


def send_template(to_e164: str, name: str, language: str, body_params: list[str],
                  quick_reply_payloads: list[str] | None = None, url_button_param: str | None = None) -> str:
    """Send an approved template; returns the WhatsApp message id (wamid)."""
    components = []
    if body_params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]})
    for i, payload in enumerate(quick_reply_payloads or []):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": payload}]})
    if url_button_param is not None:
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": url_button_param}]})
    message = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {"name": name, "language": {"code": language}, "components": components},
    }
    with telemetry.span("whatsapp.send_template", **{"messaging.system": "whatsapp", "messaging.template": name}):
        resp = client().send_whatsapp_message(
            originationPhoneNumberId=ORIGINATION_ID,
            message=json.dumps(message).encode(),
            metaApiVersion=META_API_VERSION,
        )
    return resp["messageId"]
