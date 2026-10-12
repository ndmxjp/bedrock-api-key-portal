"""Register PoC participants in the Cognito user pool (self sign-up is disabled).

Cognito emails each participant an invitation with a temporary password (valid 7 days);
they must set their own password at first sign-in. Only the address owner receives the
temporary password, which proves ownership of the email. Share the portal URL separately.
Addresses outside the configured allowedDomains are refused here and by the Pre sign-up
trigger.

Cognito's built-in email has a small daily quota per account (invitations and password
resets count toward it): invite in batches if you have many participants.
Use --resend for users whose temporary password expired before first sign-in.

  # dry-run (default)
  python scripts/invite_users.py --user-pool-id <id> --region ap-northeast-1 a@example.com
  # from a file (one address per line), then apply
  python scripts/invite_users.py --user-pool-id <id> --region ap-northeast-1 \
      --file participants.txt --apply --expected-account 123456789012
  # list registered users
  python scripts/invite_users.py --user-pool-id <id> --region ap-northeast-1 --list
"""

import argparse
import json
import sys
from pathlib import Path

from _common import require_account, session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
from portal.domain import InvalidEmail, is_allowed_email, normalize_email  # noqa: E402

DEFAULT_CONFIG = ROOT / "infra" / "portal.config.json"


def allowed_domains(config_path: Path) -> frozenset[str]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    return frozenset(d.strip().lower() for d in cfg["allowedDomains"])


def collect(args: argparse.Namespace) -> list[str]:
    raw = list(args.emails)
    if args.file:
        raw += [
            line.strip()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return raw


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("emails", nargs="*")
    p.add_argument("--file")
    p.add_argument("--user-pool-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="portal config (allowedDomains)")
    p.add_argument("--profile")
    p.add_argument("--list", action="store_true")
    p.add_argument(
        "--resend", action="store_true", help="re-send the invitation (new temp password)"
    )
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-account")
    args = p.parse_args()
    if args.apply and not args.expected_account:
        p.error("--apply requires --expected-account")

    sess = session(args.profile, args.region)
    require_account(sess, args.expected_account)
    idp = sess.client("cognito-idp")

    if args.list:
        for page in idp.get_paginator("list_users").paginate(UserPoolId=args.user_pool_id):
            for u in page["Users"]:
                attrs = {a["Name"]: a["Value"] for a in u["Attributes"]}
                print(f"{attrs.get('email', '?'):40} {u['UserStatus']:22} enabled={u['Enabled']}")
        return

    domains = allowed_domains(Path(args.config))
    emails: list[str] = []
    for raw in collect(args):
        try:
            email = normalize_email(raw)
        except InvalidEmail:
            print(f"SKIP invalid address: {raw!r}")
            continue
        if not is_allowed_email(email, domains):
            print(f"SKIP domain not allowed: {email}")
            continue
        if email not in emails:
            emails.append(email)
    if not emails:
        sys.exit("No valid addresses.")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(emails)} participant(s) -> {args.user_pool_id}")
    created = existing = 0
    for email in emails:
        if not args.apply:
            print(f"  would register {email}")
            continue
        params = {
            "UserPoolId": args.user_pool_id,
            "Username": email,
            "UserAttributes": [
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            "DesiredDeliveryMediums": ["EMAIL"],
        }
        if args.resend:
            params["MessageAction"] = "RESEND"
        try:
            idp.admin_create_user(**params)
            created += 1
            print(f"  {'re-sent invitation to' if args.resend else 'invited'} {email}")
        except idp.exceptions.UsernameExistsException:
            existing += 1
            print(f"  already registered {email} (use --resend if the invitation expired)")
        except idp.exceptions.UserNotFoundException:
            print(f"  not registered yet {email} (run without --resend)")
    if args.apply:
        print(f"\nSent {created} invitation(s), already registered {existing}.")
    else:
        print("\nDry-run only. Re-run with --apply --expected-account <id>.")


if __name__ == "__main__":
    main()
