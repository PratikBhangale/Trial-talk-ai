"""Staff account management (/staff/accounts) against a fake staff user pool. No database: Cognito only."""

import json

import pytest

from local_env import FakeCognito

ME = {"email": "lead@example.org", "cognito:groups": "[coordinator]", "custom:site_ids": "RIVERSIDE"}


@pytest.fixture
def pool(monkeypatch):
    import staff_api.accounts as accounts
    fake = FakeCognito()
    fake.admin_create_user(UserPoolId="p", Username="lead@example.org",
                           UserAttributes=[{"Name": "email", "Value": "lead@example.org"}])
    fake.admin_add_user_to_group(UserPoolId="p", Username="lead@example.org", GroupName="coordinator")
    monkeypatch.setattr(accounts, "cognito", lambda: fake)
    monkeypatch.setattr(accounts, "STAFF_POOL_ID", "p")
    return fake


def call(method, path, body=None, claims=ME):
    import staff_api.handler as staff
    event = {"rawPath": path, "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
             "body": json.dumps(body) if body is not None else None}
    resp = staff.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_needs_a_staff_token(pool):
    assert call("GET", "/staff/accounts", claims={"email": "x@example.org"})[0] == 403
    # The old admin group no longer grants anything.
    assert call("GET", "/staff/accounts", claims={"email": "a@example.org", "cognito:groups": "[admin]",
                                                  "custom:site_ids": "RIVERSIDE"})[0] == 403


def test_any_staff_member_can_invite_and_edit(pool):
    status, out = call("POST", "/staff/accounts", {"email": "Pat@Example.org", "name": "Pat", "role": "coordinator",
                                                   "site_ids": ["riverside", "LAKESIDE"], "study_ids": "KOA-301"})
    assert status == 201, out
    user = out["user"]
    assert (user["email"], user["role"], user["site_ids"]) == ("pat@example.org", "coordinator", ["LAKESIDE", "RIVERSIDE"])
    assert call("POST", "/staff/accounts", {"email": "pat@example.org", "role": "PI", "site_ids": ["X1"]})[0] == 409

    status, out = call("PATCH", "/staff/accounts/pat@example.org", {"role": "PI", "site_ids": "RIVERSIDE,LAKESIDE"})
    assert status == 200 and out["user"]["role"] == "PI"
    assert pool.groups["pat@example.org"] == {"PI"}  # moved, not added

    emails = [u["email"] for u in call("GET", "/staff/accounts")[1]["accounts"]]
    assert emails == ["lead@example.org", "pat@example.org"]
    assert [u["email"] for u in call("GET", "/staff/accounts", None)[1]["accounts"]] == emails


def test_validation(pool):
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "coordinator"})[0] == 400  # no site
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "site_ids": ["A1"]})[0] == 400  # no role
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "admin", "site_ids": ["A1"]})[0] == 400
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "PI", "site_ids": ["bad site!"]})[0] == 400
    call("POST", "/staff/accounts", {"email": "y@example.org", "role": "PI", "site_ids": ["A1"]})
    assert call("PATCH", "/staff/accounts/y@example.org", {"role": None})[0] == 400  # would leave no access
    assert call("PATCH", "/staff/accounts/y@example.org", {"site_ids": []})[0] == 400
    assert call("PATCH", "/staff/accounts/y@example.org", {"password": "x"})[0] == 400


def test_disable_enable_reset(pool):
    call("POST", "/staff/accounts", {"email": "y@example.org", "role": "PI", "site_ids": ["A1"]})
    status, out = call("POST", "/staff/accounts/y@example.org/disable", {"reason": "left the site"})
    assert status == 200 and out["user"]["enabled"] is False
    assert pool.signed_out == ["y@example.org"]
    assert call("POST", "/staff/accounts/y@example.org/enable")[1]["user"]["enabled"] is True
    assert call("POST", "/staff/accounts/y@example.org/reset-password")[1]["user"]["status"] == "RESET_REQUIRED"
    assert call("GET", "/staff/accounts/nobody@example.org")[0] == 404


def test_cannot_lock_yourself_out(pool):
    assert call("POST", "/staff/accounts/lead@example.org/disable")[0] == 409
