"""WhatsApp through AWS End User Messaging Social: send helpers and inbound event parsing."""
import json
import os

import boto3

social = boto3.client("socialmessaging")
ORIGINATION_ID = os.environ.get("ORIGINATION_PHONE_NUMBER_ID")
META_API_VERSION = os.environ.get("META_API_VERSION", "v20.0")
STUB = os.environ.get("WHATSAPP_STUB") == "1"   # log instead of sending (tests with fake numbers)


def _send(to, msg):
    """Returns the provider message id. Raises botocore ClientError on failure."""
    body = {"messaging_product": "whatsapp", "to": to, **msg}
    if STUB:
        print(f"STUB send to ...{to[-4:]}: {json.dumps(msg)[:300]}")
        return f"stub.{os.urandom(8).hex()}"
    r = social.send_whatsapp_message(originationPhoneNumberId=ORIGINATION_ID,
                                     message=json.dumps(body).encode(), metaApiVersion=META_API_VERSION)
    return r["messageId"]


def send_text(to, text):
    return _send(to, {"type": "text", "text": {"body": text[:4096]}})


def send_buttons(to, text, buttons):
    """Reply buttons (max 3, title max 20 chars). Free-form, so only inside the 24h window.
    buttons: [(id, title)]"""
    return _send(to, {"type": "interactive", "interactive": {
        "type": "button", "body": {"text": text[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": t[:20]}}
                               for i, t in buttons[:3]]}}})


def send_template(to, name, language, body_params, button_payloads=()):
    """Approved template. body_params: positional values for {{1}}, {{2}}...
    button_payloads: payload per quick-reply button, in template order."""
    components = []
    if body_params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in body_params]})
    for i, payload in enumerate(button_payloads):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": payload}]})
    template = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    return _send(to, {"type": "template", "template": template})


_approved = {}


def template_approved(name, language):
    """True if Meta has approved this template in this language. Cached per Lambda container."""
    key = (name, language)
    if key not in _approved:
        pages = social.get_paginator("list_whatsapp_message_templates").paginate(id=os.environ["WABA_ID"])
        _approved[key] = any(t["templateName"] == name and t["templateLanguage"] == language
                             and t["templateStatus"] == "APPROVED"
                             for page in pages for t in page["templates"])
    return _approved[key]


def parse_sns(event):
    """Yield ("message", msg, aws_message_id) and ("status", status, aws_message_id) from SNS records.
    Status events are matched on the SNS header's messageId: it is the id send_whatsapp_message
    returned, and failed sends have no Meta wamid at all."""
    for record in event.get("Records", []):
        header = json.loads(record["Sns"]["Message"])
        entry = json.loads(header["whatsAppWebhookEntry"])
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                yield "message", m, header.get("messageId")
            for s in value.get("statuses", []):
                yield "status", s, header.get("messageId")


def message_fields(m):
    """Flatten an inbound WhatsApp message to what we store and route on."""
    t = m.get("type")
    out = {"wamid": m["id"], "phone": "+" + m["from"].lstrip("+"), "type": t,
           "text": None, "button_id": None, "button_text": None, "media_id": None}
    if t == "text":
        out["text"] = m["text"]["body"]
    elif t == "button":                                   # template quick-reply
        out["button_id"] = m["button"].get("payload")
        out["button_text"] = out["text"] = m["button"].get("text")
    elif t == "interactive":
        reply = m["interactive"].get("button_reply") or m["interactive"].get("list_reply") or {}
        out["button_id"] = reply.get("id")
        out["button_text"] = out["text"] = reply.get("title")
        out["type"] = "button_reply" if "button_reply" in m["interactive"] else "list_reply"
    elif t in ("image", "audio", "document", "video", "sticker"):
        out["media_id"] = m[t].get("id")
        out["text"] = m[t].get("caption")
    elif t == "location":
        loc = m["location"]
        out["text"] = f"{loc.get('latitude')},{loc.get('longitude')} {loc.get('name') or ''}".strip()
    return out
