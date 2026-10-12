"""End-of-PoC cleanup: deactivate / delete portal-issued Bedrock keys and portal IAM users.

Safety:
  * Dry-run by default. Nothing changes without --apply.
  * --apply requires --expected-account to match the caller's account.
  * Only touches users with Path=/bedrock-poc/, name prefix poc-bedrock- AND tag
    ManagedBy=BedrockPoCPortal. Any other IAM user is never touched.
  * A matching user that unexpectedly has console passwords, access keys, MFA, groups,
    certificates or SSH keys is reported and skipped (never deleted).

Recommended order:
  1. python scripts/cleanup.py --deactivate-only --apply --expected-account 123456789012
  2. confirm nobody needs the keys, then
     python scripts/cleanup.py --apply --expected-account 123456789012
"""

import argparse

from _common import managed_users, require_account, session


def unexpected_credentials(iam, name: str) -> list[str]:
    problems = []
    if iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
        problems.append("access keys")
    try:
        iam.get_login_profile(UserName=name)
        problems.append("console password")
    except iam.exceptions.NoSuchEntityException:
        pass
    if iam.list_mfa_devices(UserName=name)["MFADevices"]:
        problems.append("MFA devices")
    if iam.list_groups_for_user(UserName=name)["Groups"]:
        problems.append("group memberships")
    if iam.list_signing_certificates(UserName=name)["Certificates"]:
        problems.append("signing certificates")
    if iam.list_ssh_public_keys(UserName=name)["SSHPublicKeys"]:
        problems.append("SSH keys")
    return problems


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--profile")
    p.add_argument("--apply", action="store_true", help="actually make changes")
    p.add_argument("--deactivate-only", action="store_true", help="only set keys Inactive")
    p.add_argument("--expected-account", help="required with --apply")
    args = p.parse_args()
    if args.apply and not args.expected_account:
        p.error("--apply requires --expected-account")

    sess = session(args.profile)
    require_account(sess, args.expected_account)
    iam = sess.client("iam")
    users = managed_users(iam)
    mode = "APPLY" if args.apply else "DRY-RUN"
    action = "deactivate keys" if args.deactivate_only else "deactivate+delete keys, delete users"
    print(f"[{mode}] {len(users)} portal-managed IAM user(s); action: {action}\n")

    for u in users:
        name = u["UserName"]
        creds = iam.list_service_specific_credentials(UserName=name)["ServiceSpecificCredentials"]
        print(f"- {name}  created={u['CreateDate']:%Y-%m-%d}  keys={len(creds)}")
        for c in creds:
            print(f"    key {c['ServiceSpecificCredentialId']} {c['ServiceName']} {c['Status']}")
        if not args.apply:
            continue

        for c in creds:
            if c["Status"] == "Active":
                iam.update_service_specific_credential(
                    UserName=name,
                    ServiceSpecificCredentialId=c["ServiceSpecificCredentialId"],
                    Status="Inactive",
                )
                print(f"    deactivated {c['ServiceSpecificCredentialId']}")
        if args.deactivate_only:
            continue

        problems = unexpected_credentials(iam, name)
        if problems:
            print(f"    SKIP delete: unexpected {', '.join(problems)} — investigate manually")
            continue
        for c in creds:
            iam.delete_service_specific_credential(
                UserName=name, ServiceSpecificCredentialId=c["ServiceSpecificCredentialId"]
            )
        for pol in iam.list_attached_user_policies(UserName=name)["AttachedPolicies"]:
            iam.detach_user_policy(UserName=name, PolicyArn=pol["PolicyArn"])
        for pol_name in iam.list_user_policies(UserName=name)["PolicyNames"]:
            iam.delete_user_policy(UserName=name, PolicyName=pol_name)
        iam.delete_user(UserName=name)
        print(f"    deleted user {name}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply --expected-account <id> to make changes.")


if __name__ == "__main__":
    main()
