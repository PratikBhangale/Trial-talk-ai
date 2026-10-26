"""Admin API against a fake staff user pool. No database: the admin Lambda only talks to Cognito."""

import json

import pytest

from local_env import FakeCognito

ADMIN = {"email": "admin@example.org", "cognito:username": "admin@example.org", "cognito:groups": "[admin]"}


@pytest.fixture
def pool(monkeypatch):
    import admin_api.handler as admin
    fake = FakeCognito()
    fake.admin_create_user(UserPoolId="p", Username="admin@example.org",
                           UserAttributes=[{"Name": "email", "Value": "admin@example.org"}])
    fake.admin_add_user_to_group(UserPoolId="p", Username="admin@example.org", GroupName="admin")
    monkeypatch.setattr(admin, "cognito", lambda: fake)
    monkeypatch.setattr(admin, "STAFF_POOL_ID", "p")
    return fake


def call(method, path, body=None, claims=ADMIN):
    import admin_api.handler as admin
    event = {"rawPath": path, "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
             "body": json.dumps(body) if body is not None else None}
    resp = admin.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_only_admins(pool):
    coordinator = {"email": "c@example.org", "cognito:groups": "[coordinator]", "custom:site_ids": "RIVERSIDE"}
    assert call("GET", "/admin/staff", claims=coordinator)[0] == 403


def test_create_list_update_staff(pool):
    status, out = call("POST", "/admin/staff", {"email": "Pat@Example.org", "name": "Pat", "role": "coordinator",
                                                "site_ids": ["riverside"], "study_ids": "KOA-301"})
    assert status == 201
    user = out["user"]
    assert (user["email"], user["role"], user["site_ids"], user["admin"]) == \
        ("pat@example.org", "coordinator", ["RIVERSIDE"], False)
    assert pool.groups["pat@example.org"] == {"coordinator"}

    assert call("POST", "/admin/staff", {"email": "pat@example.org", "role": "PI", "site_ids": ["X1"]})[0] == 409

    status, out = call("PATCH", "/admin/staff/pat@example.org", {"role": "PI", "site_ids": "RIVERSIDE,LAKESIDE"})
    assert status == 200
    assert out["user"]["role"] == "PI" and out["user"]["site_ids"] == ["LAKESIDE", "RIVERSIDE"]
    assert pool.groups["pat@example.org"] == {"PI"}

    emails = [u["email"] for u in call("GET", "/admin/staff")[1]["staff"]]
    assert emails == ["admin@example.org", "pat@example.org"]


def test_validation(pool):
    assert call("POST", "/admin/staff", {"email": "x@example.org", "role": "coordinator"})[0] == 400  # no site
    assert call("POST", "/admin/staff", {"email": "x@example.org"})[0] == 400  # no role or admin
    assert call("POST", "/admin/staff", {"email": "x@example.org", "role": "owner", "site_ids": ["A1"]})[0] == 400
    assert call("POST", "/admin/staff", {"email": "x@example.org", "role": "PI", "site_ids": ["bad site!"]})[0] == 400
    call("POST", "/admin/staff", {"email": "y@example.org", "role": "PI", "site_ids": ["A1"]})
    assert call("PATCH", "/admin/staff/y@example.org", {"role": None})[0] == 400  # would leave no access
    assert call("PATCH", "/admin/staff/y@example.org", {"password": "x"})[0] == 400


def test_disable_enable_reset(pool):
    call("POST", "/admin/staff", {"email": "y@example.org", "role": "PI", "site_ids": ["A1"]})
    status, out = call("POST", "/admin/staff/y@example.org/disable", {"reason": "left the site"})
    assert status == 200 and out["user"]["enabled"] is False
    assert "y@example.org" in pool.signed_out
    assert call("POST", "/admin/staff/y@example.org/enable")[1]["user"]["enabled"] is True
    assert call("POST", "/admin/staff/y@example.org/reset-password")[1]["user"]["status"] == "RESET_REQUIRED"
    assert call("GET", "/admin/staff/nobody@example.org")[0] == 404


def test_admin_cannot_lock_themselves_out(pool):
    assert call("POST", "/admin/staff/admin@example.org/disable")[0] == 409
    assert call("PATCH", "/admin/staff/admin@example.org", {"admin": False, "role": "PI", "site_ids": ["A1"]})[0] == 409
    # Adding a role to yourself while staying admin is fine.
    assert call("PATCH", "/admin/staff/admin@example.org", {"role": "PI", "site_ids": ["A1"]})[0] == 200
