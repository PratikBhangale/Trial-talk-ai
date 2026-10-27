"""Participant pool passwordless login: phone number -> one-time code sent on WhatsApp.

Three Cognito custom-auth triggers share this module:
  define_challenge  -> decides the next step
  create_challenge  -> generates the code and sends it with an approved authentication template
  verify_challenge  -> checks the answer
"""

import hmac
import os
import secrets

from common import whatsapp

LOGIN_TEMPLATE = os.environ.get("LOGIN_TEMPLATE", "login_code")
LOGIN_TEMPLATE_LANGUAGE = os.environ.get("LOGIN_TEMPLATE_LANGUAGE", "en")
MAX_ATTEMPTS = 3


def define_challenge(event, context):
    session = event["request"].get("session", [])
    resp = event["response"]
    if session and session[-1].get("challengeName") == "CUSTOM_CHALLENGE" and session[-1].get("challengeResult"):
        resp.update(issueTokens=True, failAuthentication=False)
    elif len(session) >= MAX_ATTEMPTS or (session and session[-1].get("challengeName") != "CUSTOM_CHALLENGE"):
        resp.update(issueTokens=False, failAuthentication=True)
    else:
        resp.update(issueTokens=False, failAuthentication=False, challengeName="CUSTOM_CHALLENGE")
    return event


def create_challenge(event, context):
    session = event["request"].get("session", [])
    previous = next((s.get("challengeMetadata", "") for s in reversed(session)
                     if s.get("challengeMetadata", "").startswith("CODE-")), "")
    if previous:
        code = previous[len("CODE-"):]  # wrong answer: keep the same code, don't send another message
    else:
        code = f"{secrets.randbelow(10**6):06d}"
        phone = event["request"]["userAttributes"]["phone_number"]
        # Authentication templates take the code as the body parameter and the copy-code button parameter.
        whatsapp.send_template(phone, LOGIN_TEMPLATE, LOGIN_TEMPLATE_LANGUAGE, [code], url_button_param=code)
    event["response"].update(
        publicChallengeParameters={"channel": "whatsapp"},
        privateChallengeParameters={"code": code},
        challengeMetadata=f"CODE-{code}",
    )
    return event


def verify_challenge(event, context):
    expected = event["request"]["privateChallengeParameters"].get("code", "")
    answer = (event["request"].get("challengeAnswer") or "").strip()
    event["response"]["answerCorrect"] = bool(expected) and hmac.compare_digest(answer, expected)
    return event
