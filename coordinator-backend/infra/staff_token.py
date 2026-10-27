"""Sign in to the tt-staff pool from the terminal and print an ID token for the portal API (until there is a web app).

  python infra/staff_token.py --email you@example.org
  curl -H "Authorization: Bearer $(cat .staff_token)" https://<api>/staff/me

First sign-in: use the temporary password Cognito emailed you; you are then asked for a new one.
The token is written to .staff_token (git-ignored) and is valid for 60 minutes.
"""
import argparse
import getpass
import pathlib

import boto3

CLIENT_ID = "6tfennq5nb6hu8i50u2cvh3mo1"
OUT = pathlib.Path(__file__).resolve().parent.parent / ".staff_token"


def idp():
    return boto3.client("cognito-idp", region_name="us-east-1")   # public client: no AWS credentials needed


def sign_in(email):
    """Password sign-in in the terminal, answering the first-time new-password and MFA challenges.
    Returns Cognito's AuthenticationResult (IdToken, RefreshToken, ...)."""
    c = idp()
    r = c.initiate_auth(ClientId=CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
                        AuthParameters={"USERNAME": email, "PASSWORD": getpass.getpass("Password: ")})
    while "ChallengeName" in r:
        name, session = r["ChallengeName"], r["Session"]
        if name == "NEW_PASSWORD_REQUIRED":
            print("Choose a new password (12+ characters, upper, lower, number, symbol).")
            answer = {"NEW_PASSWORD": getpass.getpass("New password: ")}
        elif name == "SOFTWARE_TOKEN_MFA":
            answer = {"SOFTWARE_TOKEN_MFA_CODE": input("Authenticator code: ")}
        else:
            raise SystemExit(f"Unsupported challenge {name}")
        r = c.respond_to_auth_challenge(ClientId=CLIENT_ID, ChallengeName=name, Session=session,
                                        ChallengeResponses={"USERNAME": email, **answer})
    return r["AuthenticationResult"]


def refresh(refresh_token):
    """A new ID token without asking for the password again (the refresh token lasts 12 hours)."""
    return idp().initiate_auth(ClientId=CLIENT_ID, AuthFlow="REFRESH_TOKEN_AUTH",
                               AuthParameters={"REFRESH_TOKEN": refresh_token})["AuthenticationResult"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    args = ap.parse_args()
    OUT.write_text(sign_in(args.email)["IdToken"])
    print(f"ID token written to {OUT} (valid 60 min)")


if __name__ == "__main__":
    main()
