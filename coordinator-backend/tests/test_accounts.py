"""Staff account management (/staff/accounts) against a fake staff user pool. No database: Cognito only."""

import json

import pytest

from local_env import FakeCognito

ME = {"email": "lead@example.org", "cognito:groups": "[coordinator]", "custom:site_ids": "RIVERSIDE,LAKESIDE"}
PI = {"email": "pi@example.org", "cognito:groups": "[PI]", "custom:site_ids": "RIVERSIDE,LAKESIDE"}


def _add(fake, email, group, sites):
    fake.admin_create_user(UserPoolId="p", Username=email, UserAttributes=[
        {"Name": "email", "Value": email}, {"Name": "custom:site_ids", "Value": sites}])
    fake.admin_add_user_to_group(UserPoolId="p", Username=email, GroupName=group)


@pytest.fixture
def pool(monkeypatch):
    import staff_api.accounts as accounts
    fake = FakeCognito()
    _add(fake, "lead@example.org", "coordinator", "RIVERSIDE,LAKESIDE")
    _add(fake, "pi@example.org", "PI", "RIVERSIDE,LAKESIDE")
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


def test_staff_invite_and_edit_within_their_own_access(pool):
    status, out = call("POST", "/staff/accounts", {"email": "Pat@Example.org", "name": "Pat", "role": "coordinator",
                                                   "site_ids": ["riverside", "LAKESIDE"], "study_ids": "KOA-301"})
    assert status == 201, out
    user = out["user"]
    assert (user["email"], user["role"], user["site_ids"]) == ("pat@example.org", "coordinator", ["LAKESIDE", "RIVERSIDE"])
    assert call("POST", "/staff/accounts", {"email": "pat@example.org", "role": "coordinator",
                                            "site_ids": ["RIVERSIDE"]})[0] == 409

    assert call("PATCH", "/staff/accounts/pat@example.org", {"role": "PI"})[0] == 403  # coordinators can't grant PI
    status, out = call("PATCH", "/staff/accounts/pat@example.org", {"role": "PI", "site_ids": "RIVERSIDE"}, claims=PI)
    assert status == 200 and out["user"]["role"] == "PI" and out["user"]["site_ids"] == ["RIVERSIDE"]
    assert pool.groups["pat@example.org"] == {"PI"}  # moved, not added

    emails = [u["email"] for u in call("GET", "/staff/accounts")[1]["accounts"]]
    assert emails == ["lead@example.org", "pat@example.org", "pi@example.org"]


def test_nobody_changes_their_own_access(pool):
    for change in ({"site_ids": ["RIVERSIDE", "LAKEVIEW"]}, {"role": "PI"}, {"study_ids": ["OTHER"]}):
        assert call("PATCH", "/staff/accounts/lead@example.org", change)[0] == 409
    status, out = call("PATCH", "/staff/accounts/lead@example.org", {"name": "Jamie"})
    assert status == 200 and out["user"]["name"] == "Jamie"  # a name is not access


def test_only_your_own_sites_studies_and_role_can_be_granted(pool):
    invite = {"email": "x@example.org", "role": "coordinator"}
    assert call("POST", "/staff/accounts", {**invite, "site_ids": ["LAKEVIEW"]})[0] == 403
    assert call("POST", "/staff/accounts", {**invite, "site_ids": ["RIVERSIDE", "LAKEVIEW"]})[0] == 403
    assert call("POST", "/staff/accounts", {**invite, "role": "PI", "site_ids": ["RIVERSIDE"]})[0] == 403

    scoped = {**ME, "custom:study_ids": "KOA-301"}
    assert call("POST", "/staff/accounts", {**invite, "site_ids": ["RIVERSIDE"], "study_ids": ["OTHER"]},
                claims=scoped)[0] == 403
    assert call("POST", "/staff/accounts", {**invite, "site_ids": ["RIVERSIDE"]}, claims=scoped)[0] == 403  # = any
    assert call("POST", "/staff/accounts", {**invite, "site_ids": ["RIVERSIDE"], "study_ids": ["KOA-301"]},
                claims=scoped)[0] == 201


def test_other_sites_accounts_are_hidden_and_shared_ones_need_all_their_sites(pool):
    _add(pool, "far@example.org", "coordinator", "LAKEVIEW")
    _add(pool, "both@example.org", "coordinator", "RIVERSIDE,LAKEVIEW")
    assert "far@example.org" not in [u["email"] for u in call("GET", "/staff/accounts")[1]["accounts"]]
    for method, path in (("GET", ""), ("POST", "/disable"), ("POST", "/reset-password")):
        assert call(method, f"/staff/accounts/far@example.org{path}")[0] == 404

    assert call("GET", "/staff/accounts/both@example.org")[0] == 200  # shares RIVERSIDE, so visible
    assert call("PATCH", "/staff/accounts/both@example.org", {"site_ids": ["RIVERSIDE"]})[0] == 403
    assert call("POST", "/staff/accounts/both@example.org/disable")[0] == 403


def test_coordinators_cannot_change_a_pi_account(pool):
    assert call("POST", "/staff/accounts/pi@example.org/disable")[0] == 403
    assert call("PATCH", "/staff/accounts/pi@example.org", {"role": "coordinator"})[0] == 403


def test_validation(pool):
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "coordinator"})[0] == 400  # no site
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "site_ids": ["RIVERSIDE"]})[0] == 400  # no role
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "admin", "site_ids": ["RIVERSIDE"]})[0] == 400
    assert call("POST", "/staff/accounts", {"email": "x@example.org", "role": "PI", "site_ids": ["bad site!"]})[0] == 400
    call("POST", "/staff/accounts", {"email": "y@example.org", "role": "coordinator", "site_ids": ["RIVERSIDE"]})
    assert call("PATCH", "/staff/accounts/y@example.org", {"role": None})[0] == 400  # would leave no access
    assert call("PATCH", "/staff/accounts/y@example.org", {"site_ids": []})[0] == 400
    assert call("PATCH", "/staff/accounts/y@example.org", {"password": "x"})[0] == 400


def test_disable_enable_reset(pool):
    call("POST", "/staff/accounts", {"email": "y@example.org", "role": "coordinator", "site_ids": ["RIVERSIDE"]})
    status, out = call("POST", "/staff/accounts/y@example.org/disable", {"reason": "left the site"})
    assert status == 200 and out["user"]["enabled"] is False
    assert pool.signed_out == ["y@example.org"]
    assert call("POST", "/staff/accounts/y@example.org/enable")[1]["user"]["enabled"] is True
    assert call("POST", "/staff/accounts/y@example.org/reset-password")[1]["user"]["status"] == "RESET_REQUIRED"
    assert call("GET", "/staff/accounts/nobody@example.org")[0] == 404


def test_cannot_lock_yourself_out(pool):
    assert call("POST", "/staff/accounts/lead@example.org/disable")[0] == 409


def test_encoded_usernames_in_the_path(pool):
    """The portal sends encodeURIComponent(email); API Gateway's rawPath keeps the %40."""
    call("POST", "/staff/accounts", {"email": "y@example.org", "role": "coordinator", "site_ids": ["RIVERSIDE"]})
    assert call("GET", "/staff/accounts/y%40example.org")[1]["user"]["email"] == "y@example.org"
    assert call("PATCH", "/staff/accounts/lead%40example.org", {"site_ids": ["LAKEVIEW"]})[0] == 409
