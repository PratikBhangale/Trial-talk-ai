"""Run .sql files against Aurora through the RDS Data API, one statement at a time.

The RDS query editor splits scripts on every ';', including ones inside comments and
$$ function bodies. This splitter understands comments, quoted strings and dollar quotes.

Usage (CloudShell or any machine with AWS credentials):
  python3 run_sql.py --cluster-arn <cluster ARN> --secret-arn <master secret ARN> 01_schema.sql 02_roles_rls.sql
Add --profile <name> to use a named AWS CLI profile. A profile created with `aws login` also needs
`pip install "botocore[crt]"`.
Stops at the first failing statement.
"""
import argparse
import json
import re
import sys
import time

DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def split_sql(text):
    """Split a script into statements. Comments outside dollar-quoted bodies are dropped."""
    stmts, buf, i, n = [], [], 0, len(text)
    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        c = text[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:   # doubled quote is an escaped quote
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if c == "$":
            m = DOLLAR_TAG.match(text, i)
            if m:
                tag = m.group()
                j = text.find(tag, m.end())
                j = n if j == -1 else j + len(tag)
                buf.append(text[i:j])
                i = j
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        stmts.append(stmt)
    return stmts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-arn", required=True)
    ap.add_argument("--secret-arn", required=True)
    ap.add_argument("--database", default="trialtalk")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()

    import boto3
    rds = boto3.Session(profile_name=args.profile, region_name=args.region).client("rds-data")
    db = dict(resourceArn=args.cluster_arn, secretArn=args.secret_arn, database=args.database)

    for path in args.files:
        with open(path, encoding="utf-8") as f:
            stmts = split_sql(f.read())
        print(f"==> {path} ({len(stmts)} statements)")
        for k, sql in enumerate(stmts, 1):
            first_line = " ".join(sql.split())[:90]
            for attempt in range(6):
                try:
                    r = rds.execute_statement(**db, sql=sql, formatRecordsAs="JSON")
                    break
                except rds.exceptions.DatabaseResumingException:
                    print("   cluster is resuming, retrying in 10s...")
                    time.sleep(10)
                except Exception as e:
                    print(f"   FAILED [{k}] {first_line}\n   {e}")
                    sys.exit(1)
            else:
                print("   cluster did not resume")
                sys.exit(1)
            print(f"   ok [{k}] {first_line}")
            for row in json.loads(r.get("formattedRecords") or "[]"):
                print(f"        {row}")
    print("done")


if __name__ == "__main__":
    main()
