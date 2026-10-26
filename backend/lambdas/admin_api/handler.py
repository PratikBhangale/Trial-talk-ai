"""Admin API: staff account management. Routes under /admin, behind the admin app client's JWT authorizer.

Admins are staff-pool users in the 'admin' group. This Lambda only calls Cognito on the staff pool:
it has no database login, so admins can grant access but never read participant data themselves.
Every change is printed as a structured audit line for CloudWatch.
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3

from common.auth import Admin, admin_claims
from common.http import HttpError, Router, json_body, query_params

STAFF_POOL_ID = os.environ.get("STAFF_POOL_ID", "")
ROLES = ("coordinator", "PI")
GROUPS = (*ROLES, "admin")
SITE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

router = Router()
_cognito = None


def cognito():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def handler(event, context):
    return router.dispatch(event, admin_claims)


def _audit(admin: Admin, action: str, **fields) -> None:
    print(json.dumps({"audit": action, "by": admin.actor, "at": datetime.now(timezone.utc).isoformat(), **fields},
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
    role = next((g for g in ("PI", "coordinator") if g in groups), None)
    return {
        "username": raw["Username"],
        "email": attrs.get("email"),
        "name": attrs.get("name"),
        "role": role,
        "admin": "admin" in groups,
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


def _set_groups(username: str, current: list[str], wanted: set[str]) -> None:
    for g in sorted(wanted - set(current)):
        cognito().admin_add_user_to_group(UserPoolId=STAFF_POOL_ID, Username=username, GroupName=g)
    for g in sorted(set(current) & set(GROUPS) - wanted):
        cognito().admin_remove_user_from_group(UserPoolId=STAFF_POOL_ID, Username=username, GroupName=g)


def _wanted_groups(role, is_admin: bool) -> set[str]:
    if role not in (*ROLES, None, ""):
        raise HttpError(400, f"role must be one of {list(ROLES)} or empty")
    return ({role} if role else set()) | ({"admin"} if is_admin else set())


def _not_self(admin: Admin, user: dict, what: str) -> None:
    if user["username"] == admin.username or (user["email"] and user["email"] == admin.email):
        raise HttpError(409, f"You cannot {what} your own account; ask another admin")


# ------------------------------------------------------------------ routes

@router.route("GET", "/admin/me")
def whoami(event, admin: Admin):
    return 200, {"email": admin.email, "username": admin.username}


@router.route("GET", "/admin/staff")
def list_staff(event, admin: Admin):
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
    return 200, {"staff": sorted(out, key=lambda u: (u["email"] or ""))}


@router.route("POST", "/admin/staff")
def create_staff(event, admin: Admin):
    """Invite a staff user. Cognito emails a temporary password; MFA setup is forced at first sign-in."""
    body = json_body(event)
    email = (body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HttpError(400, "A valid email is required")
    groups = _wanted_groups(body.get("role"), bool(body.get("admin")))
    if not groups:
        raise HttpError(400, "Give a role (coordinator or PI), admin access, or both")
    sites, studies = _ids(body.get("site_ids"), "site_ids"), _ids(body.get("study_ids"), "study_ids")
    if body.get("role") and not sites:
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
    _set_groups(raw["Username"], [], groups)
    _audit(admin, "staff.create", user=email, groups=sorted(groups), site_ids=sites, study_ids=studies)
    return 201, {"user": _get(raw["Username"])}


@router.route("GET", "/admin/staff/{username}")
def read_staff(event, admin: Admin, username: str):
    return 200, {"user": _get(username)}


@router.route("PATCH", "/admin/staff/{username}")
def update_staff(event, admin: Admin, username: str):
    """Change role, admin access, sites, studies or name. Takes effect at the user's next token refresh."""
    body = json_body(event)
    unknown = set(body) - {"role", "admin", "site_ids", "study_ids", "name"}
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

    groups_now = [g for g in (["admin"] if user["admin"] else []) + ([user["role"]] if user["role"] else [])]
    wanted = _wanted_groups(body.get("role", user["role"]), bool(body.get("admin", user["admin"])))
    if user["admin"] and "admin" not in wanted:
        _not_self(admin, user, "remove admin access from")
    if not wanted:
        raise HttpError(400, "A user needs a role or admin access; disable the account instead")
    sites_after = next((a["Value"] for a in attrs if a["Name"] == "custom:site_ids"), ",".join(user["site_ids"]))
    if wanted & set(ROLES) and not sites_after:
        raise HttpError(400, "Coordinators and PIs need at least one site")

    if attrs:
        cognito().admin_update_user_attributes(UserPoolId=STAFF_POOL_ID, Username=username, UserAttributes=attrs)
    _set_groups(username, groups_now, wanted)
    _audit(admin, "staff.update", user=user["email"], changes=body)
    return 200, {"user": _get(username)}


@router.route("POST", "/admin/staff/{username}/disable")
def disable_staff(event, admin: Admin, username: str):
    """Blocks sign-in and revokes refresh tokens. Issued ID tokens still work until they expire (60 min)."""
    user = _get(username)
    _not_self(admin, user, "disable")
    cognito().admin_disable_user(UserPoolId=STAFF_POOL_ID, Username=username)
    cognito().admin_user_global_sign_out(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(admin, "staff.disable", user=user["email"], reason=json_body(event).get("reason"))
    return 200, {"user": _get(username)}


@router.route("POST", "/admin/staff/{username}/enable")
def enable_staff(event, admin: Admin, username: str):
    user = _get(username)
    cognito().admin_enable_user(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(admin, "staff.enable", user=user["email"])
    return 200, {"user": _get(username)}


@router.route("POST", "/admin/staff/{username}/reset-password")
def reset_password(event, admin: Admin, username: str):
    """Cognito emails a reset code; the user must set a new password at next sign-in. MFA is unchanged."""
    user = _get(username)
    cognito().admin_reset_user_password(UserPoolId=STAFF_POOL_ID, Username=username)
    cognito().admin_user_global_sign_out(UserPoolId=STAFF_POOL_ID, Username=username)
    _audit(admin, "staff.reset_password", user=user["email"])
    return 200, {"user": _get(username)}
