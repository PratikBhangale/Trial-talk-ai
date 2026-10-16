"""WhatsApp Lambda: receives messages from SNS, asks the agent on AgentCore, replies on WhatsApp."""

import json
import os

import boto3

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
TABLE_NAME = os.environ.get("TABLE_NAME", "trial_agent")

table = boto3.resource("dynamodb").Table(TABLE_NAME)
agentcore = boto3.client("bedrock-agentcore")
social = boto3.client("socialmessaging")

UNKNOWN_SENDER_REPLY = (
    "This number is for participants enrolled in a Continuum AI study. "
    "Please contact your study site for help."
)
NOT_TEXT_REPLY = "Sorry, I can only read text messages for now. Please type your message."
ERROR_REPLY = (
    "Sorry, something went wrong on my side. "
    "If this is urgent, please contact your study site or emergency services directly."
)


# ---------- 1. unpack the SNS event ----------

def incoming_messages(event):
    """Yield (origination_id, sender, message) for each incoming WhatsApp message."""
    for record in event.get("Records", []):
        header = json.loads(record["Sns"]["Message"])
        origination_id = header["context"]["MetaPhoneNumberIds"][0]["arn"]  # our WhatsApp number
        entry = json.loads(header["whatsAppWebhookEntry"])
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):  # status updates have none
                yield origination_id, message["from"], message


# ---------- 2. who is this? ----------

def find_participant(phone: str) -> str | None:
    item = table.get_item(Key={"PK": f"PHONE#{phone}", "SK": "LOOKUP"}).get("Item")
    return item["participant_id"] if item else None


# ---------- 3. ask the agent ----------

def ask_agent(pid: str, text: str) -> str:
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=f"whatsapp-session-{pid}".ljust(33, "0"),  # must be 33+ characters
        payload=json.dumps({"participant_id": pid, "text": text}).encode(),
    )
    return json.loads(resp["response"].read()).get("reply") or ERROR_REPLY


# ---------- 4. reply on WhatsApp ----------

def send_whatsapp(origination_id: str, to: str, text: str) -> None:
    message = {
        "messaging_product": "whatsapp",
        "to": f"+{to}",
        "type": "text",
        "text": {"body": text[:4096]},  # WhatsApp's limit for a text message
    }
    social.send_whatsapp_message(
        originationPhoneNumberId=origination_id,
        message=json.dumps(message).encode(),
        metaApiVersion="v20.0",
    )


# ---------- the Lambda entry point ----------

def handler(event, context):
    for origination_id, sender, message in incoming_messages(event):
        # Unknown numbers never reach the agent
        pid = find_participant(sender)
        if not pid:
            print(f"Message from unknown number ending {sender[-4:]}")
            send_whatsapp(origination_id, sender, UNKNOWN_SENDER_REPLY)
            continue

        if message.get("type") != "text":
            send_whatsapp(origination_id, sender, NOT_TEXT_REPLY)
            continue

        text = message["text"]["body"].strip()[:1000]
        try:
            reply = ask_agent(pid, text)
        except Exception as e:
            print(f"Agent call failed for {pid}: {e}")
            reply = ERROR_REPLY

        send_whatsapp(origination_id, sender, reply)

    return {"ok": True}