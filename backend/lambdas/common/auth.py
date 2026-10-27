"""Claims from the API Gateway JWT authorizer, and per-action permission checks (enforcement layer 4).

API Gateway has already verified the Cognito ID token (signature, expiry, audience).
Row-level security in Aurora is the last layer, so a bug here still cannot leak other sites.
"""

from dataclasses import dataclass

from common.http import HttpError

# Per the RBAC table in the build plan. Both staff roles manage participants and reminders, triage emergency
# alerts, manage staff accounts ("accounts:manage": staff are their own admins) and read observability;
# AE sign-off ("ae:sign") is PI only.
_COMMON = {
    "participant:read", "participant:write", "participant:withdraw",
    "reminder:read", "reminder:write", "reminder:send",
    "events:read", "requests:read", "requests:write",
    "alerts:read", "alerts:write",
    "accounts:manage", "ops:read",
}
PERMISSIONS = {"coordinator": set(_COMMON), "PI": _COMMON | {"ae:sign"}}


def _claims(event: dict) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {}) or {}


def _split(value) -> list[str]:
    """HTTP API flattens list claims into strings like '[a b]' or 'a,b'."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    text = str(value).strip().strip("[]")
    return [p for p in text.replace(",", " ").split() if p]


@dataclass
class Staff:
    email: str
    role: str
    site_ids: list[str]
    study_ids: list[str]

    @property
    def actor(self) -> str:
        return f"staff:{self.email}"


@dataclass
class Participant:
    participant_id: str


def staff_claims(event: dict) -> Staff:
    c = _claims(event)
    groups = _split(c.get("cognito:groups"))
    role = c.get("role") or ("PI" if "PI" in groups else "coordinator" if "coordinator" in groups else "")
    if role not in PERMISSIONS:
        raise HttpError(403, "Not a staff user")
    sites = _split(c.get("site_ids") or c.get("custom:site_ids"))
    if not sites:
        raise HttpError(403, "No sites assigned")
    return Staff(
        email=c.get("email", c.get("sub", "unknown")),
        role=role,
        site_ids=sites,
        study_ids=_split(c.get("study_ids") or c.get("custom:study_ids")),
    )


def participant_claims(event: dict) -> Participant:
    pid = _claims(event).get("custom:participant_id")
    if not pid:
        raise HttpError(403, "Not a participant account")
    return Participant(participant_id=pid)


def require(staff: Staff, action: str, site_id: str | None = None, study_id: str | None = None) -> None:
    if action not in PERMISSIONS.get(staff.role, set()):
        raise HttpError(403, f"Role {staff.role} cannot {action}")
    if site_id is not None and site_id not in staff.site_ids:
        raise HttpError(403, "Not your site")
    if study_id is not None and staff.study_ids and study_id not in staff.study_ids:
        raise HttpError(403, "Not your study")
