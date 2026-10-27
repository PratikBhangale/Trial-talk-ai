"""Staff account management, inside the staff API: /staff/accounts. Cognito on the staff pool only.

Staff are their own admins: any coordinator or PI can invite colleagues, change roles and sites, and disable
accounts. There is no separate admin group or portal. These routes never touch participant data; they only call
Cognito. Every change prints a structured audit line for CloudWatch.
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3

from common.auth import Staff, require
from common.http import HttpError, Router, json_body, query_params

STAFF_POOL_ID = os.environ.get("STAFF_POOL_ID", "")
ROLES = ("coordinator", "PI")
SITE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

router = Router()
_cognito = None


def cognito():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def _audit(staff: Staff, action: str, **fields) -> None:
    print(json.dumps({"audit": action, "by": staff.actor, "at": datetime.now(timezone.utc).isoformat(), **fields},
                     default=str))


def _ids(value, field: str) -> list[str]:
    """['RIVERSIDE', 'LAKESIDE'] or 'RIVERSIDE,LAKESIDE' -> validated upper-case ids."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    out = sorted({str(v).strip().upper() for v in items if str(v).strip()})
    bad = [v for v in out if not SITE_RE.match(v)]
    if bad:
        raise HttpError(400, f"{field} has invalid ids: {bad}")
    if len(",".join(out)) > 256:
        raise HttpError(400, f"{field} is too long")
    return out


def _groups(username: str) -> list[str]:
    resp = cognito().admin_list_groups_for_user(UserPoolId=STAFF_POOL_ID, Username=username)
    return [g["GroupName"] for g in resp.get("Groups", [])]


def _user(raw: dict, groups: list[str]) -> dict:
    attrs = {a["Name"]: a["Value"] for a in raw.get("Attributes") or raw.get("UserAttributes") or []}
    return {
        "username": raw["Username"],
        "email": attrs.get("email"),
        "name": attrs.get("name"),
        "role": next((g for g in ("PI", "coordinator") if g in groups), None),
        "site_ids": [s for s in attrs.get("custom:site_ids", "").split(",") if s],
        "study_ids": [s for s in attrs.get("custom:study_ids", "").split(",") if s],
        "enabled": raw.get("Enabled", True),
        "status": raw.get("UserStatus"),
        "created_at": raw.get("UserCreateDate"),
        "updated_at": raw.get("UserLastModifiedDate"),
    }


def _get(username: str) -> dict:
    try:
        raw = cognito().admin_get_user(UserPoolId=STAFF_POOL_ID, Username=username)
    except cognito().exceptions.UserNotFoundException:
        raise HttpError(404, "Staff user not found")
    return _user(raw, _groups(raw["Username"]))


def _set_role(username: str, current: str | None, wanted: str) -> None:
    if current == wanted:
        return
    if current:
        cognito().admin_remove_user_from_group(UserPoolId=STAFF_POOL_ID, Username=username, GroupName=current)
    cognito().admin_add_user_to_group(UserPoolId=STAFF_POOL_ID, Username=username, GroupName=wanted)


def _role(value) -> str:
    if value not in ROLES:
        raise HttpError(400, f"role must be one of {list(ROLES)}")
    return value


def _not_self(staff: Staff, user: dict, what: str) -> None:
    if user["email"] and user["email"] == staff.email:
        raise HttpError(409, f"You cannot {what} your own account; ask a colleague")


# ------------------------------------------------------------------ routes

@router.route("GET", "/staff/accounts")
def list_accounts(event, staff: Staff):
    require(staff, "accounts:manage")
    q = (query_params(event).get("q") or "").lower()
    users, token = [], None
    while True:
        kwargs = {"UserPoolId": STAFF_POOL_ID, "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        resp = cognito().list_users(**kwargs)
        users += resp.get("Users", [])
        token = resp.get("PaginationToken")
        if not token or len(users) >= 600:
            break
    out = [_user(u, _groups(u["Username"])) for u in users]
    if q:
        out = [u for u in out if q in (u["email"] or "").lower() or q in (u["name"] or "").lower()
               or any(q in s.lower() for s in u["site_ids"])]
    return 200, {"accounts": sorted(out, key=lambda u: (u["email"] or ""))}


@router.route("POST", "/staff/accounts")
def create_account(event, staff: Staff):
    """Invite a colleague. Cognito emails a temporary password; MFA setup is forced at first sign-in."""
    require(staff, "accounts:manage")
    body = json_body(event)
    email = (body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HttpError(400, "A valid email is required")
    role = _role(body.get("role"))
    sites, studies = _ids(body.get("site_ids"), "site_ids"), _ids(body.get("study_ids"), "study_ids")
    if not sites:
        raise HttpError(400, "Coordinators and PIs need at least one site")

    attrs = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
             {"Name": "custom:site_ids", "Value": ",".join(sites)},
             {"Name": "custom:study_ids", "Value": ",".join(studies)}]
    if body.get("name"):
        attrs.append({"Name": "name", "Value": str(body["name"])[:128]})
    try:
        raw = cognito().admin_create_user(UserPoolId=STAFF_POOL_ID, Username=email, UserAttributes=attrs,
                                          DesiredDeliveryMediums=["EMAIL"])["User"]
    except cognito().exceptions.UsernameExistsException:
        raise HttpError(409, "A staff user with that email already exists")
    _set_role(raw["Username"], None, role)
    _audit(staff, "account.create", user=email, role=role, site_ids=sites, study_ids=studies)
    return 201, {"user": _get(raw["Username"])}


@router.route("GET", "/staff/accounts/{username}")
def read_account(event, staff: Staff, username: str):
    require(staff, "accounts:manage")
    return 200, {"user": _get(username)}


@router.route("PATCH", "/staff/accounts/{username}")
def update_account(event, staff: Staff, username: str):
    """Change role, sites, studies or name. Takes effect at the user's next token refresh."""
    require(staff, "accounts:manage")
    body = json_body(event)
    unknown = set(body) - {"role", "site_ids", "study_ids", "name"}
    if unknown:
        raise HttpError(400, f"Cannot update: {sorted(unknown)}")
    if not body:
        raise HttpError(400, "Nothing to update")
    user = _get(username)

    attrs = []
    if "site_ids" in body:
        attrs.append({"Name": "custom:site_ids", "Value": ",".join(_ids(body["site_ids"], "site_ids"))})
    if "study_ids" in body:
        attrs.append({"Name": "custom:study_ids", "Value": ",".join(_ids(body["study_ids"], "study_ids"))})
    if "name" in body:
        attrs.append({"Name": "name", "Value": str(body["name"] or "")[:128]})
    role = _role(body["role"]) if "role" in body else user["role"]
    if not role:
        raise HttpError(400, "A user needs a role; disable the account instead")
    sites_after = next((a["Value"] for a in attrs if a["Name"] == "custom:site_ids"), ",".join(user["site_ids"]))
    if not sites_after:
        raise HttpError(400, "Coordinators and PIs need at least one site")

    if attrs:
        cognito().admin_update_user_attributes(UserPoolId=STAFF_POOL_ID, Username=username, UserAttributes=attrs)
    _set_role(username, user["role"], role)
    _audit(staff, "account.update", user=user["email"], changes=body)
    return 200, {"user": _get(username)}


@router.route("POST", "/staff/accounts/{username}/disable")
def disable_account(event, staff: Staff, username: str):
    """Blocks sign-in and revokes refresh tokens. Issued ID tokens still work until they expire (60 min)."""
    require(staff, "accounts:manage")
    user = _get(username)
    _not_self(staff, user, "disable")
    cognito().admin_disable_user(UserPoolId=STAFF_POOL_ID, Username=username)
    cognito().admin_user_global_sign_out(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(staff, "account.disable", user=user["email"], reason=json_body(event).get("reason"))
    return 200, {"user": _get(username)}


@router.route("POST", "/staff/accounts/{username}/enable")
def enable_account(event, staff: Staff, username: str):
    require(staff, "accounts:manage")
    user = _get(username)
    cognito().admin_enable_user(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(staff, "account.enable", user=user["email"])
    return 200, {"user": _get(username)}


@router.route("POST", "/staff/accounts/{username}/reset-password")
def reset_password(event, staff: Staff, username: str):
    """Cognito emails a reset code; the user must set a new password at next sign-in. MFA is unchanged."""
    require(staff, "accounts:manage")
    user = _get(username)
    cognito().admin_reset_user_password(UserPoolId=STAFF_POOL_ID, Username=username)
    cognito().admin_user_global_sign_out(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(staff, "account.reset_password", user=user["email"])
    return 200, {"user": _get(username)}
