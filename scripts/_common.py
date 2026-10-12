"""Shared helpers for admin scripts. Run via: uv run --project backend python scripts/<x>.py"""

import sys

import boto3

IAM_PATH = "/bedrock-poc/"
USER_PREFIX = "poc-bedrock-"
MANAGED_BY = ("ManagedBy", "BedrockPoCPortal")


def session(profile: str | None, region: str | None = None) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def require_account(sess: boto3.Session, expected: str | None) -> str:
    ident = sess.client("sts").get_caller_identity()
    account = ident["Account"]
    print(f"Caller: {ident['Arn']}  (account {account})")
    if expected is not None and expected != account:
        sys.exit(f"ERROR: expected account {expected} but credentials are for {account}. Aborting.")
    return account


def managed_users(iam) -> list[dict]:
    """Only users under the portal path, with the portal prefix AND ManagedBy tag."""
    out = []
    for page in iam.get_paginator("list_users").paginate(PathPrefix=IAM_PATH):
        for u in page["Users"]:
            if u["Path"] != IAM_PATH or not u["UserName"].startswith(USER_PREFIX):
                continue
            tags = {
                t["Key"]: t["Value"] for t in iam.list_user_tags(UserName=u["UserName"])["Tags"]
            }
            if tags.get(MANAGED_BY[0]) == MANAGED_BY[1]:
                out.append(u)
    return out
