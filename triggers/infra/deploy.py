"""Deploy the TrialTalk trigger Lambdas and their wiring. Safe to re-run (creates or updates).

  python infra/deploy.py --profile admin-ai-demo            deploy / update everything
  python infra/deploy.py --profile admin-ai-demo --cutover  also turn on ingest -> router forwarding

Resources (all named tt-*):
  SNS whatsapp_topic -> tt-ingest -> SQS tt-inbound.fifo (DLQ tt-inbound-dlq.fifo) -> tt-router -> agent
  EventBridge Scheduler group "trialtalk": rem-<reminder_id> / snooze-* / fu-* -> tt-reminder
                                           tt-reconcile-nightly -> tt-reconcile
Needs: pip install boto3 "botocore[crt]"
"""
import argparse
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

ACCOUNT = "180026596905"
REGION = "us-east-1"
ARN = f"{REGION}:{ACCOUNT}"
CLUSTER_ARN = f"arn:aws:rds:{ARN}:cluster:trialtalk"
MESSAGING_SECRET_ARN = f"arn:aws:secretsmanager:{ARN}:secret:trialtalk/db/tt_messaging-YP5JhO"
AGENT_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{ARN}:runtime/ContinuumAI_trial_agent-UL2ODbF7Ie"
WHATSAPP_TOPIC_ARN = f"arn:aws:sns:{ARN}:whatsapp_topic"
ORIGINATION_PHONE_NUMBER_ID = "phone-number-id-5a019ac8025448978f5adcbf1d14dbd7"
WABA_ID = "waba-e1e5c9ff54c0406986db6c05acaf8e8d"
COORDINATOR_EMAIL = "continuum.ai.team@gmail.com"
SENDER_EMAIL = "Continuum AI <continuum.ai.team@gmail.com>"
GROUP = "trialtalk"
QUEUE, DLQ = "tt-inbound.fifo", "tt-inbound-dlq.fifo"
SCHEDULER_ROLE = "tt-scheduler-invoke"
LAMBDA_DIR = pathlib.Path(__file__).resolve().parent.parent / "lambdas"
OTEL_REQUIREMENTS = pathlib.Path(__file__).resolve().parents[2] / "infra-requirements-otel.txt"
TELEMETRY_ENV = {"TELEMETRY_DB_SINK": "1"}   # the DB span sink also needs DB_CLUSTER_ARN + DB_SECRET_ARN (already set)

FUNCTIONS = {
    #  name           timeout  extra permissions (beyond logs, Data API and the tt_messaging secret)
    "tt-ingest":    (30,  ["sqs_send"]),
    "tt-router":    (120, ["sqs_consume", "agent", "whatsapp", "ses", "schedule_create"]),
    "tt-reminder":  (60,  ["whatsapp", "ses", "schedule_create"]),
    "tt-reconcile": (120, ["schedule_manage"]),
}


def fn_arn(name):
    return f"arn:aws:lambda:{ARN}:function:{name}"


def statements(name, extras, queue_arn):
    sched_arn = f"arn:aws:scheduler:{ARN}:schedule/{GROUP}/*"
    pass_role = {"Effect": "Allow", "Action": "iam:PassRole",
                 "Resource": f"arn:aws:iam::{ACCOUNT}:role/{SCHEDULER_ROLE}"}
    s = [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{ARN}:log-group:/aws/lambda/{name}:*"},
        {"Effect": "Allow", "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
                                       "rds-data:BeginTransaction",
                                       "rds-data:CommitTransaction", "rds-data:RollbackTransaction"],
         "Resource": CLUSTER_ARN},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": MESSAGING_SECRET_ARN},
    ]
    extra = {
        "sqs_send": [{"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": queue_arn}],
        "sqs_consume": [{"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage",
                                                       "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
                         "Resource": queue_arn}],
        "agent": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
                   "Resource": [AGENT_RUNTIME_ARN, f"{AGENT_RUNTIME_ARN}/*"]}],
        "whatsapp": [{"Effect": "Allow", "Action": "social-messaging:SendWhatsAppMessage",
                      "Resource": f"arn:aws:social-messaging:{ARN}:phone-number-id/*"},
                     {"Effect": "Allow", "Action": "social-messaging:ListWhatsAppMessageTemplates",
                      "Resource": f"arn:aws:social-messaging:{ARN}:waba/*"}],
        "ses": [{"Effect": "Allow", "Action": "ses:SendEmail", "Resource": f"arn:aws:ses:{ARN}:identity/*"}],
        "schedule_create": [{"Effect": "Allow", "Action": "scheduler:CreateSchedule", "Resource": sched_arn},
                            pass_role],
        "schedule_manage": [{"Effect": "Allow", "Action": "scheduler:ListSchedules", "Resource": "*"},
                            {"Effect": "Allow", "Action": ["scheduler:CreateSchedule", "scheduler:DeleteSchedule",
                                                           "scheduler:GetSchedule"], "Resource": sched_arn},
                            pass_role],
    }
    for e in extras:
        s += extra[e]
    return {"Version": "2012-10-17", "Statement": s}


def ensure_role(iam, name, service, policy, condition=None):
    stmt = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if condition:
        stmt["Condition"] = condition
    trust = json.dumps({"Version": "2012-10-17", "Statement": [stmt]})
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                              Tags=[{"Key": "project", "Value": "trialtalk"}])["Role"]["Arn"]
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
        arn, created = iam.get_role(RoleName=name)["Role"]["Arn"], False
    iam.put_role_policy(RoleName=name, PolicyName="permissions", PolicyDocument=json.dumps(policy))
    return arn, created


def install_otel_deps(staging):
    """pip install the pinned OTel SDK into `staging`. Pure-python wheels, resolved for the Lambda runtime (3.12)
    whatever Python runs this script, so the build host's architecture does not matter."""
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--python-version", "3.12", "--implementation", "cp", "--only-binary=:all:",
                    "-r", str(OTEL_REQUIREMENTS), "-t", str(staging)], check=True)


def bundle(zip_path, files, deps_dir=None):
    """Write `zip_path` from `files` ((name in zip, source path) pairs) plus the OTel SDK. `deps_dir` is an
    already-installed staging folder (deploy.py builds one per run and reuses it for every function)."""
    zip_path = pathlib.Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        if deps_dir is None:
            deps_dir = pathlib.Path(tmp) / "deps"
            install_otel_deps(deps_dir)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, src in files:
                z.write(src, arc)
            for f in sorted(pathlib.Path(deps_dir).rglob("*")):
                rel = f.relative_to(deps_dir)
                if f.is_file() and "__pycache__" not in rel.parts:
                    z.write(f, rel.as_posix())
    return zip_path


def build_zip(name, out_dir, deps_dir=None):
    """The zip for one trigger Lambda (flat: handler.py + common/*.py + the OTel SDK) at out_dir/<name>.zip."""
    files = [("handler.py", LAMBDA_DIR / name.removeprefix("tt-") / "handler.py")]
    files += [(f.name, f) for f in sorted((LAMBDA_DIR / "common").glob("*.py"))]
    return bundle(pathlib.Path(out_dir) / f"{name}.zip", files, deps_dir)


def ensure_function(lam, logs, name, role_arn, timeout, env, deps_dir=None):
    try:
        logs.create_log_group(logGroupName=f"/aws/lambda/{name}", tags={"project": "trialtalk"})
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=f"/aws/lambda/{name}", retentionInDays=30)
    with tempfile.TemporaryDirectory() as out:
        code = build_zip(name, out, deps_dir).read_bytes()
    cfg = dict(Role=role_arn, Handler="handler.handler", Runtime="python3.12",
                                    Timeout=timeout, MemorySize=256,
               Environment={"Variables": {**env, **TELEMETRY_ENV}})
    for attempt in range(10):          # a new IAM role takes a few seconds to become assumable
        try:
            lam.create_function(FunctionName=name, Code={"ZipFile": code}, Architectures=["arm64"],
                                Tags={"project": "trialtalk"}, **cfg)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--cutover", action="store_true", help="forward inbound messages from tt-ingest to tt-router")
    args = ap.parse_args()
    s = boto3.Session(profile_name=args.profile, region_name=REGION)
    iam, lam, logs, sqs, sns, scheduler, cw = (s.client(c) for c in
                                               ("iam", "lambda", "logs", "sqs", "sns", "scheduler", "cloudwatch"))

    print("scheduler group")
    try:
        scheduler.create_schedule_group(Name=GROUP, Tags=[{"Key": "project", "Value": "trialtalk"}])
    except scheduler.exceptions.ConflictException:
        pass

    print("queues")
    dlq_url = sqs.create_queue(QueueName=DLQ, Attributes={"FifoQueue": "true", "SqsManagedSseEnabled": "true",
                                                          "MessageRetentionPeriod": "1209600"})["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    q_attrs = {"FifoQueue": "true", "SqsManagedSseEnabled": "true", "VisibilityTimeout": "720",
               "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"})}
    try:
        q_url = sqs.create_queue(QueueName=QUEUE, Attributes=q_attrs)["QueueUrl"]
    except sqs.exceptions.QueueNameExists:
        q_url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
        sqs.set_queue_attributes(QueueUrl=q_url, Attributes={k: v for k, v in q_attrs.items() if k != "FifoQueue"})
    q_arn = sqs.get_queue_attributes(QueueUrl=q_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    print("roles")
    sched_role_arn, _ = ensure_role(
        iam, SCHEDULER_ROLE, "scheduler.amazonaws.com",
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
         "Resource": [fn_arn("tt-reminder"), fn_arn("tt-reconcile")]}]},
        condition={"StringEquals": {"aws:SourceAccount": ACCOUNT}})
    roles = {n: ensure_role(iam, f"{n}-lambda", "lambda.amazonaws.com", statements(n, extras, q_arn))[0]
             for n, (_, extras) in FUNCTIONS.items()}

    print("functions")
    common = {"DB_CLUSTER_ARN": CLUSTER_ARN, "DB_SECRET_ARN": MESSAGING_SECRET_ARN, "DB_NAME": "trialtalk",
              "ORIGINATION_PHONE_NUMBER_ID": ORIGINATION_PHONE_NUMBER_ID, "WABA_ID": WABA_ID,
              "META_API_VERSION": "v20.0",
              "SCHEDULE_GROUP": GROUP, "REMINDER_FUNCTION_ARN": fn_arn("tt-reminder"),
              "SCHEDULER_ROLE_ARN": sched_role_arn,
              "COORDINATOR_EMAIL": COORDINATOR_EMAIL, "SENDER_EMAIL": SENDER_EMAIL}
    try:   # keep the current forwarding setting unless --cutover is given
        current = lam.get_function_configuration(FunctionName="tt-ingest")["Environment"]["Variables"]
        forward = "1" if args.cutover else current.get("FORWARD_TO_ROUTER", "0")
    except lam.exceptions.ResourceNotFoundException:
        forward = "1" if args.cutover else "0"
    env = {
        "tt-ingest": {**common, "INBOUND_QUEUE_URL": q_url, "FORWARD_TO_ROUTER": forward},
        "tt-router": {**common, "AGENT_RUNTIME_ARN": AGENT_RUNTIME_ARN, "DEMO_PARTICIPANT_ID": "P-DEMO"},   # unknown numbers chat as P-DEMO, like continuum-whatsapp did
        "tt-reminder": common,
        "tt-reconcile": {**common, "DRY_RUN": "1"},
    }
    with tempfile.TemporaryDirectory() as deps_dir:   # pip once, reuse for every function
        install_otel_deps(deps_dir)
        for name, (timeout, _) in FUNCTIONS.items():
            ensure_function(lam, logs, name, roles[name], timeout, env[name], deps_dir)

    print("triggers")
    try:
        lam.add_permission(FunctionName="tt-ingest", StatementId="sns-whatsapp-topic", Action="lambda:InvokeFunction",
                           Principal="sns.amazonaws.com", SourceArn=WHATSAPP_TOPIC_ARN)
    except lam.exceptions.ResourceConflictException:
        pass
    sns.subscribe(TopicArn=WHATSAPP_TOPIC_ARN, Protocol="lambda", Endpoint=fn_arn("tt-ingest"))

    mappings = lam.list_event_source_mappings(EventSourceArn=q_arn, FunctionName="tt-router")["EventSourceMappings"]
    esm = dict(BatchSize=1, ScalingConfig={"MaximumConcurrency": 2})   # bursts wait in the queue, not on Aurora
    if mappings:
        lam.update_event_source_mapping(UUID=mappings[0]["UUID"], **esm)
    else:
        lam.create_event_source_mapping(EventSourceArn=q_arn, FunctionName="tt-router", **esm)

    nightly = dict(Name="tt-reconcile-nightly", GroupName=GROUP, ScheduleExpression="cron(0 2 * * ? *)",
                   ScheduleExpressionTimezone="America/Chicago", FlexibleTimeWindow={"Mode": "OFF"},
                   Target={"Arn": fn_arn("tt-reconcile"), "RoleArn": sched_role_arn, "Input": "{}"})
    try:
        scheduler.create_schedule(**nightly)
    except scheduler.exceptions.ConflictException:
        scheduler.update_schedule(**nightly)

    cw.put_metric_alarm(AlarmName="tt-inbound-dlq-not-empty", Namespace="AWS/SQS",
                        MetricName="ApproximateNumberOfMessagesVisible", Dimensions=[{"Name": "QueueName", "Value": DLQ}],
                        Statistic="Maximum", Period=300, EvaluationPeriods=1, Threshold=0,
                        ComparisonOperator="GreaterThanThreshold", TreatMissingData="notBreaching")
    for name in FUNCTIONS:
        cw.put_metric_alarm(AlarmName=f"{name}-errors", Namespace="AWS/Lambda", MetricName="Errors",
                            Dimensions=[{"Name": "FunctionName", "Value": name}], Statistic="Sum", Period=300,
                            EvaluationPeriods=1, Threshold=0, ComparisonOperator="GreaterThanThreshold",
                            TreatMissingData="notBreaching")
    print(f"done. ingest -> router forwarding: {'ON' if forward == '1' else 'OFF'}")


if __name__ == "__main__":
    main()
