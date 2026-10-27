"""Staff pool pre-token-generation trigger: adds role, site_ids and study_ids to the ID token."""


def handler(event, context):
    attrs = event["request"].get("userAttributes", {})
    groups = event["request"].get("groupConfiguration", {}).get("groupsToOverride", []) or []
    role = "PI" if "PI" in groups else "coordinator" if "coordinator" in groups else ""
    claims = {
        "role": role,
        "site_ids": attrs.get("custom:site_ids", ""),
        "study_ids": attrs.get("custom:study_ids", ""),
    }
    event["response"]["claimsOverrideDetails"] = {"claimsToAddOrOverride": {k: v for k, v in claims.items() if v}}
    return event
