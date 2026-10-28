"""Forgot password for the tt-staff pool: Cognito emails a code, you type it here with a new password.

  python coordinator-backend/infra/reset_password.py --email you@example.org

Needs no AWS credentials (public app client). Afterwards sign in with staff_token.py or portal_ui.py.
"""
import argparse
import getpass

import staff_token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    args = ap.parse_args()
    c = staff_token.idp()
    sent = c.forgot_password(ClientId=staff_token.CLIENT_ID, Username=args.email)
    print("Code sent to", sent["CodeDeliveryDetails"]["Destination"])
    code = input("Code from the email: ").strip()
    print("New password: 12+ characters, upper, lower, number, symbol.")
    c.confirm_forgot_password(ClientId=staff_token.CLIENT_ID, Username=args.email, ConfirmationCode=code,
                              Password=getpass.getpass("New password: "))
    print("Password changed.")


if __name__ == "__main__":
    main()
