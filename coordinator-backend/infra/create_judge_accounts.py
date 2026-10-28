"""Create (or reset) shared staff logins for demo judges in the tt-staff pool, and turn them off afterwards.

  python coordinator-backend/infra/create_judge_accounts.py --profile admin-ai-demo            # 2 accounts
  python coordinator-backend/infra/create_judge_accounts.py --profile admin-ai-demo --count 3
  python coordinator-backend/infra/create_judge_accounts.py --profile admin-ai-demo --disable  # after judging
  python coordinator-backend/infra/create_judge_accounts.py --dry-run                          # no AWS calls

Each account is a coordinator at RIVERSIDE for study KOA-301, so a judge can enroll a participant with their own
phone, create reminders, send them now, and triage alerts, on synthetic data. The password is random, permanent
(no first-login prompt) and printed once here: it is never stored or committed. Re-running resets the password
and re-enables the account.
Needs: pip install boto3 "botocore[crt]"
"""
import argparse
import secrets

POOL_NAME = "tt-staff"
DOMAIN = "trialtalk-demo.example.org"      # the address is only a login name: no email is sent
GROUP, SITES, STUDIES = "coordinator", "RIVERSIDE", "KOA-301"
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"   # no look-alikes (0/O, 1/l/I)


def password() -> str:
    """Like Tq7k-Wm4p-Rx9d-Zn3c: an upper, a lower, a number and the symbol the pool's policy needs; about 92 bits."""
    while True:
        pw = "-".join("".join(secrets.choice(LETTERS) for _ in range(4)) for _ in range(4))
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def logins(count: int) -> list[str]:
    return [f"judge{n}@{DOMAIN}" for n in range(1, count + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--disable", action="store_true", help="turn the judge accounts off")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; no AWS calls")
    args = ap.parse_args()
    emails = logins(args.count)
    if args.dry_run:
        for e in emails:
            print(f"would {'disable' if args.disable else 'create or reset'} {e}"
                  + ("" if args.disable else f" (group {GROUP}, sites {SITES}, studies {STUDIES})"))
        return

    import boto3
    idp = boto3.Session(profile_name=args.profile, region_name="us-east-1").client("cognito-idp")
    pool = next((p["Id"] for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60)
                 for p in page["UserPools"] if p["Name"] == POOL_NAME), None)
    if not pool:
        raise SystemExit(f"user pool {POOL_NAME} not found")

    for n, email in enumerate(emails, 1):
        if args.disable:
            try:
                idp.admin_disable_user(UserPoolId=pool, Username=email)
                print(f"disabled {email}")
            except idp.exceptions.UserNotFoundException:
                print(f"{email} does not exist")
            continue
        try:
            idp.admin_create_user(
                UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                                {"Name": "name", "Value": f"Judge {n}"},
                                {"Name": "custom:site_ids", "Value": SITES},
                                {"Name": "custom:study_ids", "Value": STUDIES}])
        except idp.exceptions.UsernameExistsException:
            idp.admin_enable_user(UserPoolId=pool, Username=email)
        pw = password()
        idp.admin_set_user_password(UserPoolId=pool, Username=email, Password=pw, Permanent=True)
        idp.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=GROUP)
        print(f"{email}   password: {pw}")
    if not args.disable:
        print("\nShare these once. Run again with --disable after judging.")


if __name__ == "__main__":
    main()
