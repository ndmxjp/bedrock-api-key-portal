"""Detect drift between DynamoDB registrations and IAM (the source of truth).

Reports:
  * portal IAM users with no registration record (orphan users)
  * keys on portal users that are not the recorded ACTIVE key (orphan keys, e.g. a crash
    between CreateServiceSpecificCredential and the DynamoDB write)
  * ACTIVE records whose IAM key is missing / Inactive / expired
With --fix-orphan-keys --apply, orphan keys are deactivated and deleted (their secrets were
never delivered, or belong to keys the portal already considers revoked/expired). Records are
not modified: the portal self-heals them on the next /api/me.
"""

import argparse
from datetime import UTC, datetime

from _common import managed_users, require_account, session


def scan_registrations(ddb, table: str) -> dict[str, dict]:
    by_user: dict[str, dict] = {}
    for page in ddb.get_paginator("scan").paginate(TableName=table, ConsistentRead=True):
        for item in page["Items"]:
            if not item["pk"]["S"].startswith("USER#"):
                continue
            name = item.get("iamUserName", {}).get("S")
            if name:
                by_user[name] = item
    return by_user


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--profile")
    p.add_argument("--region", help="region of the DynamoDB table")
    p.add_argument("--table", required=True)
    p.add_argument("--fix-orphan-keys", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-account")
    args = p.parse_args()
    if args.apply and not args.expected_account:
        p.error("--apply requires --expected-account")

    sess = session(args.profile, args.region)
    require_account(sess, args.expected_account)
    iam, ddb = sess.client("iam"), sess.client("dynamodb")
    records = scan_registrations(ddb, args.table)
    now = datetime.now(UTC)
    findings = 0

    for u in managed_users(iam):
        name = u["UserName"]
        rec = records.get(name)
        creds = iam.list_service_specific_credentials(
            UserName=name, ServiceName="bedrock.amazonaws.com"
        )["ServiceSpecificCredentials"]
        if rec is None:
            findings += 1
            print(f"ORPHAN USER  {name} (no registration record, {len(creds)} key(s))")
        status = rec["status"]["S"] if rec else None
        recorded = rec.get("credentialId", {}).get("S") if rec else None
        # SUSPENDED keys are deliberately Inactive and must be kept for a later resume.
        keep = recorded if status in ("ACTIVE", "SUSPENDED") else None
        if status == "ISSUING":
            lease = int(rec.get("leaseUntil", {}).get("N", "0"))
            if lease >= now.timestamp():
                print(f"SKIP         {name} issuance in progress")
                continue
        for c in creds:
            cid = c["ServiceSpecificCredentialId"]
            if cid == keep:
                if status == "SUSPENDED":
                    if c["Status"] == "Active":
                        findings += 1
                        print(
                            f"SUSPENDED BUT ACTIVE {name} {cid} (reactivated outside the portal?)"
                        )
                    continue
                if c["Status"] != "Active" or (
                    c.get("ExpirationDate") and c["ExpirationDate"] <= now
                ):
                    findings += 1
                    print(
                        f"STALE RECORD {name} record ACTIVE but IAM key {cid} "
                        f"is {c['Status']}/expired"
                    )
                continue
            if c["Status"] == "Active":
                findings += 1
                print(
                    f"ORPHAN KEY   {name} {cid} Active "
                    f"(record status={status}, recorded={recorded})"
                )
                if args.fix_orphan_keys and args.apply:
                    iam.update_service_specific_credential(
                        UserName=name, ServiceSpecificCredentialId=cid, Status="Inactive"
                    )
                    iam.delete_service_specific_credential(
                        UserName=name, ServiceSpecificCredentialId=cid
                    )
                    print(f"             -> deactivated and deleted {cid}")
        if (
            status in ("ACTIVE", "SUSPENDED")
            and keep
            and keep not in {c["ServiceSpecificCredentialId"] for c in creds}
        ):
            findings += 1
            print(f"STALE RECORD {name} record ACTIVE but key {keep} no longer exists in IAM")

    print(f"\n{findings} finding(s).")


if __name__ == "__main__":
    main()
