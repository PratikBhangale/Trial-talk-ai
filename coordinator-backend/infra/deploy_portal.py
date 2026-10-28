"""Deploy the staff portal API (coordinator-backend/lambdas/staff_api) next to the trigger Lambdas. Safe to re-run.

  python infra/deploy_portal.py --profile admin-ai-demo

Resources (all named tt-*):
  Cognito user pool tt-staff (groups coordinator / PI, custom:site_ids / custom:study_ids) + pre-token trigger
  Lambda tt-staff-api  -> Aurora as tt_portal (RLS by the caller's sites), Scheduler group "trialtalk" (rem-*),
                          tt-reminder for "Send now"
  HTTP API tt-portal-api: GET/POST/PATCH/DELETE /staff/{proxy+}, Cognito JWT authorizer (ID token)

The code under coordinator-backend/lambdas is deployed unchanged. POST /staff/participants also creates the
participant's website login in PARTICIPANT_POOL_ID, the pool the participant sign-in page uses (from the CDK stack
TrialTalk-Portal, not created here). The participant API (participant_api) is not deployed by this script; it runs behind trialtalk-api from that CDK stack.
Needs: pip install boto3 "botocore[crt]"
"""
import argparse
import json
import pathlib
import sys
import tempfile
import time

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "triggers" / "infra"))
from deploy import ACCOUNT, ARN, CLUSTER_ARN, GROUP, REGION, SCHEDULER_ROLE, bundle, ensure_role, fn_arn

PORTAL_SECRET_ARN = f"arn:aws:secretsmanager:{ARN}:secret:trialtalk/db/tt_portal-VwDkdB"
CODE_DIR = pathlib.Path(__file__).resolve().parent.parent / "lambdas"
POOL, CLIENT, API = "tt-staff", "tt-staff-web", "tt-portal-api"
DOMAIN_PREFIX = f"tt-staff-{ACCOUNT}"
CALLBACK_URLS = ["http://localhost:5173/"]
CORS_ORIGINS = ["http://localhost:5173", "http://localhost:5174",
                "https://trialtalk-ui.dqzri9vvxpyky.amplifyapp.com",   # old Amplify branch trialtalk_ui (deleted)
                "https://main.dqzri9vvxpyky.amplifyapp.com"]   # Amplify branch main (coordinator-frontend)
APPROVED_TEMPLATES = "dose_reminder_v2,visit_reminder_v1,daily_checkin_v1"
# The first staff user (the build plan leaves this to the console). Everyone else is invited from /staff/accounts,
# and nobody can grant more than they hold, so this user holds every site and study.
FIRST_STAFF = {"email": "divya.shah7999@gmail.com", "name": "Divya Shah", "group": "PI",
               "site_ids": "RIVERSIDE,LAKEVIEW", "study_ids": "KOA-301"}
TAGS = {"project": "trialtalk"}
# trialtalk-participants: where participants sign in (CONFIG.clients.participant in coordinator-frontend/common.js).
# Not tt-participant, which the sign-in page doesn't use.
PARTICIPANT_POOL_ID = "us-east-1_FVAiqMdUk"
# The staff API reads trial.telemetry_spans; it does not store its own request spans. The dashboard polls it every
# few seconds (alerts, usage, observability), so storing them would bury the pipeline traces the trigger Lambdas
# write (they keep TELEMETRY_DB_SINK=1). Explicitly "0": with DB_CLUSTER_ARN + DB_SECRET_ARN set, the sink is on
# unless told otherwise. The staff API never calls persist_spans, the other thing this flag turns off.
STAFF_TELEMETRY_ENV = {"TELEMETRY_DB_SINK": "0"}
# POST /staff/ask (staff_api/ask.py) calls Bedrock Converse with this model: the same global Sonnet inference profile
# as the participant agent (agent/trial_agent/root_agent.yaml).
ASK_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
# The staff role's Bedrock grant for /staff/ask, worded like the "Claude" statement in
# agent/trial_agent/runtime-policy.json but for any model: Converse needs bedrock:InvokeModel on the inference profile
# and on the foundation models a global profile routes to, in any region. Only the read-only tool loop uses it. What
# the model can see is limited by the database (RLS by site, read-only transactions), not by this grant.
ASK_BEDROCK_STATEMENT = {
    "Sid": "AskTheClinic", "Effect": "Allow", "Action": ["bedrock:InvokeModel"],
    "Resource": [f"arn:aws:bedrock:{ARN}:inference-profile/*",
                 "arn:aws:bedrock:*::foundation-model/*",
                 "arn:aws:bedrock:::foundation-model/*"]}


def staff_env(pool_id: str, sched_role_arn: str) -> dict:
    """Environment of tt-staff-api."""
    return {"DB_CLUSTER_ARN": CLUSTER_ARN, "DB_SECRET_ARN": PORTAL_SECRET_ARN, "DB_NAME": "trialtalk",
            "STAFF_POOL_ID": pool_id, "PARTICIPANT_POOL_ID": PARTICIPANT_POOL_ID,
            "SCHEDULE_GROUP": GROUP, "REMINDER_FUNCTION_ARN": fn_arn("tt-reminder"),
            "SCHEDULER_ROLE_ARN": sched_role_arn, "APPROVED_TEMPLATES": APPROVED_TEMPLATES, "LEGACY_TABLE": "",
            "ASK_MODEL_ID": ASK_MODEL_ID, **STAFF_TELEMETRY_ENV}


def build_zip(out_dir, deps_dir=None):
    """The one zip every portal Lambda shares (lambdas/ as is, minus migrate/, plus the OTel SDK) at
    out_dir/tt-portal.zip."""
    files = []
    for f in sorted(CODE_DIR.rglob("*.py")):
        rel = f.relative_to(CODE_DIR)
        if "__pycache__" not in rel.parts and rel.parts[0] != "migrate":
            files.append((rel.as_posix(), f))
    return bundle(pathlib.Path(out_dir) / "tt-portal.zip", files, deps_dir)


def ensure_function(lam, logs, name, role_arn, handler, timeout, env, code):
    try:
        logs.create_log_group(logGroupName=f"/aws/lambda/{name}", tags=TAGS)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=f"/aws/lambda/{name}", retentionInDays=30)
    cfg = dict(Role=role_arn, Handler=handler, Runtime="python3.12", Timeout=timeout, MemorySize=256,
               Environment={"Variables": env})
    for _ in range(10):                # a new IAM role takes a few seconds to become assumable
        try:
            lam.create_function(FunctionName=name, Code={"ZipFile": code}, Architectures=["arm64"], Tags=TAGS, **cfg)
            break
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=name, ZipFile=code)
            lam.get_waiter("function_updated_v2").wait(FunctionName=name)
            lam.update_function_configuration(FunctionName=name, **cfg)
            break
        except lam.exceptions.InvalidParameterValueException as e:
            if "cannot be assumed" not in str(e):
                raise
            time.sleep(5)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    print(f"  {name} ready")


def allow_invoke(lam, name, sid, principal, source_arn):
    try:
        lam.add_permission(FunctionName=name, StatementId=sid, Action="lambda:InvokeFunction",
                           Principal=principal, SourceArn=source_arn)
    except lam.exceptions.ResourceConflictException:
        pass


def ensure_pool(idp, pre_token_arn):
    pool_id = next((p["Id"] for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60)
                    for p in page["UserPools"] if p["Name"] == POOL), None)
    if not pool_id:
        pool_id = idp.create_user_pool(
            PoolName=POOL,
            UsernameAttributes=["email"], AutoVerifiedAttributes=["email"],
            Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
                                         "RequireNumbers": True, "RequireSymbols": True,
                                         "TemporaryPasswordValidityDays": 3}},
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            AccountRecoverySetting={"RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]},
            Schema=[{"Name": n, "AttributeDataType": "String", "Mutable": True,
                     "StringAttributeConstraints": {"MaxLength": "256"}} for n in ("site_ids", "study_ids")],
            LambdaConfig={"PreTokenGeneration": pre_token_arn},
            DeletionProtection="ACTIVE", UserPoolTags=TAGS,
        )["UserPool"]["Id"]
    # The CDK stack requires TOTP MFA. Optional here so a token can be fetched from the CLI while there is no web app.
    idp.set_user_pool_mfa_config(UserPoolId=pool_id, MfaConfiguration="OPTIONAL",
                                 SoftwareTokenMfaConfiguration={"Enabled": True})
    for group in ("coordinator", "PI"):
        try:
            idp.create_group(UserPoolId=pool_id, GroupName=group)
        except idp.exceptions.GroupExistsException:
            pass
    try:
        idp.create_user_pool_domain(Domain=DOMAIN_PREFIX, UserPoolId=pool_id)
    except idp.exceptions.InvalidParameterException as e:
        if "already" not in str(e).lower():
            raise

    client = dict(
        UserPoolId=pool_id, ClientName=CLIENT,
        ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        AllowedOAuthFlows=["code"], AllowedOAuthScopes=["openid", "email", "profile"],
        AllowedOAuthFlowsUserPoolClient=True, SupportedIdentityProviders=["COGNITO"],
        CallbackURLs=CALLBACK_URLS, LogoutURLs=CALLBACK_URLS,
        WriteAttributes=["name"],              # staff cannot edit their own role, sites or studies
        IdTokenValidity=60, AccessTokenValidity=60, RefreshTokenValidity=12,
        TokenValidityUnits={"IdToken": "minutes", "AccessToken": "minutes", "RefreshToken": "hours"},
        PreventUserExistenceErrors="ENABLED",
    )
    client_id = next((c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)
                      ["UserPoolClients"] if c["ClientName"] == CLIENT), None)
    if client_id:
        idp.update_user_pool_client(ClientId=client_id, **client)
    else:
        client_id = idp.create_user_pool_client(GenerateSecret=False, **client)["UserPoolClient"]["ClientId"]
    return pool_id, client_id


def ensure_first_staff(idp, pool_id):
    u = FIRST_STAFF
    try:
        idp.admin_get_user(UserPoolId=pool_id, Username=u["email"])
        print(f"  {u['email']} exists")
    except idp.exceptions.UserNotFoundException:
        idp.admin_create_user(
            UserPoolId=pool_id, Username=u["email"], DesiredDeliveryMediums=["EMAIL"],
            UserAttributes=[{"Name": "email", "Value": u["email"]}, {"Name": "email_verified", "Value": "true"},
                            {"Name": "name", "Value": u["name"]},
                            {"Name": "custom:site_ids", "Value": u["site_ids"]},
                            {"Name": "custom:study_ids", "Value": u["study_ids"]}])
        print(f"  {u['email']} created; Cognito emailed a temporary password")
    idp.admin_add_user_to_group(UserPoolId=pool_id, Username=u["email"], GroupName=u["group"])


def ensure_api(apigw, pool_id, client_id):
    api = next((a for a in apigw.get_apis()["Items"] if a["Name"] == API), None)
    cors = {"AllowOrigins": CORS_ORIGINS, "AllowMethods": ["GET", "POST", "PATCH", "DELETE"],
            "AllowHeaders": ["authorization", "content-type"], "MaxAge": 3600}
    if api:
        apigw.update_api(ApiId=api["ApiId"], CorsConfiguration=cors)
    else:
        api = apigw.create_api(Name=API, ProtocolType="HTTP", CorsConfiguration=cors, Tags=TAGS)
    api_id = api["ApiId"]

    jwt = {"Audience": [client_id], "Issuer": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"}
    auth = next((a for a in apigw.get_authorizers(ApiId=api_id)["Items"] if a["Name"] == "staff-jwt"), None)
    if auth:
        apigw.update_authorizer(ApiId=api_id, AuthorizerId=auth["AuthorizerId"], JwtConfiguration=jwt)
        auth_id = auth["AuthorizerId"]
    else:
        auth_id = apigw.create_authorizer(ApiId=api_id, Name="staff-jwt", AuthorizerType="JWT",
                                          IdentitySource=["$request.header.Authorization"],
                                          JwtConfiguration=jwt)["AuthorizerId"]

    integrations = apigw.get_integrations(ApiId=api_id)["Items"]
    integ_id = integrations[0]["IntegrationId"] if integrations else apigw.create_integration(
        ApiId=api_id, IntegrationType="AWS_PROXY", IntegrationUri=fn_arn("tt-staff-api"),
        PayloadFormatVersion="2.0", TimeoutInMillis=29000)["IntegrationId"]

    # No ANY: OPTIONS must reach the CORS preflight, not the authorizer.
    existing = {r["RouteKey"] for r in apigw.get_routes(ApiId=api_id)["Items"]}
    for method in ("GET", "POST", "PATCH", "DELETE"):
        key = f"{method} /staff/{{proxy+}}"
        if key not in existing:
            apigw.create_route(ApiId=api_id, RouteKey=key, AuthorizationType="JWT", AuthorizerId=auth_id,
                               Target=f"integrations/{integ_id}")

    stage = {"DefaultRouteSettings": {"ThrottlingRateLimit": 50, "ThrottlingBurstLimit": 100}}
    try:
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True, Tags=TAGS, **stage)
    except apigw.exceptions.ConflictException:
        apigw.update_stage(ApiId=api_id, StageName="$default", **stage)
    return api_id, api["ApiEndpoint"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    args = ap.parse_args()
    s = boto3.Session(profile_name=args.profile, region_name=REGION)
    iam, lam, logs, idp, apigw, cw = (s.client(c) for c in
                                      ("iam", "lambda", "logs", "cognito-idp", "apigatewayv2", "cloudwatch"))
    with tempfile.TemporaryDirectory() as out:
        code = build_zip(out).read_bytes()
    sched_role_arn = f"arn:aws:iam::{ACCOUNT}:role/{SCHEDULER_ROLE}"
    log_stmt = lambda name: {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                             "Resource": f"arn:aws:logs:{ARN}:log-group:/aws/lambda/{name}:*"}

    print("staff pool")
    pre_role, _ = ensure_role(iam, "tt-staff-pre-token-lambda", "lambda.amazonaws.com",
                              {"Version": "2012-10-17", "Statement": [log_stmt("tt-staff-pre-token")]})
    ensure_function(lam, logs, "tt-staff-pre-token", pre_role, "auth_triggers.staff_pre_token.handler", 5, {}, code)
    pool_id, client_id = ensure_pool(idp, fn_arn("tt-staff-pre-token"))
    pool_arn = f"arn:aws:cognito-idp:{ARN}:userpool/{pool_id}"
    allow_invoke(lam, "tt-staff-pre-token", "cognito-pre-token", "cognito-idp.amazonaws.com", pool_arn)
    ensure_first_staff(idp, pool_id)

    print("staff api")
    policy = {"Version": "2012-10-17", "Statement": [
        log_stmt("tt-staff-api"),
        # Not the cluster's master secret: that would bypass RLS.
        {"Effect": "Allow", "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
                                       "rds-data:BeginTransaction", "rds-data:CommitTransaction",
                                       "rds-data:RollbackTransaction"], "Resource": CLUSTER_ARN},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": PORTAL_SECRET_ARN},
        {"Effect": "Allow", "Action": ["scheduler:CreateSchedule", "scheduler:UpdateSchedule",
                                       "scheduler:DeleteSchedule", "scheduler:GetSchedule"],
         "Resource": f"arn:aws:scheduler:{ARN}:schedule/{GROUP}/rem-*"},
        {"Effect": "Allow", "Action": "iam:PassRole", "Resource": sched_role_arn},
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_arn("tt-reminder")},
        # /staff/accounts: staff are their own admins
        {"Effect": "Allow", "Resource": pool_arn, "Action": [
            "cognito-idp:ListUsers", "cognito-idp:AdminGetUser", "cognito-idp:AdminCreateUser",
            "cognito-idp:AdminUpdateUserAttributes", "cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser",
            "cognito-idp:AdminUserGlobalSignOut", "cognito-idp:AdminResetUserPassword",
            "cognito-idp:AdminListGroupsForUser", "cognito-idp:AdminAddUserToGroup",
            "cognito-idp:AdminRemoveUserFromGroup"]},
        # /staff/participants: create the website login, keep its phone number in step, disable it on withdrawal
        {"Effect": "Allow", "Resource": f"arn:aws:cognito-idp:{ARN}:userpool/{PARTICIPANT_POOL_ID}", "Action": [
            "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
            "cognito-idp:AdminUpdateUserAttributes", "cognito-idp:AdminDisableUser"]},
        # /staff/ask: Bedrock Converse for the read-only "Ask the clinic" tool loop
        ASK_BEDROCK_STATEMENT,
    ]}
    staff_role, _ = ensure_role(iam, "tt-staff-api-lambda", "lambda.amazonaws.com", policy)
    ensure_function(lam, logs, "tt-staff-api", staff_role, "staff_api.handler.handler", 29,
                    staff_env(pool_id, sched_role_arn), code)

    print("http api")
    api_id, endpoint = ensure_api(apigw, pool_id, client_id)
    allow_invoke(lam, "tt-staff-api", "http-api-staff", "apigateway.amazonaws.com",
                 f"arn:aws:execute-api:{ARN}:{api_id}/*/*/staff/*")
    cw.put_metric_alarm(AlarmName="tt-staff-api-errors", Namespace="AWS/Lambda", MetricName="Errors",
                        Dimensions=[{"Name": "FunctionName", "Value": "tt-staff-api"}], Statistic="Sum", Period=300,
                        EvaluationPeriods=1, Threshold=0, ComparisonOperator="GreaterThanThreshold",
                        TreatMissingData="notBreaching")
    print(json.dumps({"api_url": endpoint, "staff_pool_id": pool_id, "staff_client_id": client_id,
                      "login_domain": f"https://{DOMAIN_PREFIX}.auth.{REGION}.amazoncognito.com"}, indent=2))


if __name__ == "__main__":
    main()
