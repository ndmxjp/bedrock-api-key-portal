"""Portal admin operations backed by the registrations table.

status                      kill-switch state and slot usage
disable-issuance            kill switch ON (new keys cannot be issued; revoke still works)
enable-issuance             kill switch OFF
release-slot --sub <sub>    free a registration slot of a user without a live key
add-admin --email <e>       grant the admin screen (Cognito group PortalAdmins)
remove-admin --email <e>    revoke admin
list-admins                 list admin group members

The table commands need --table; the admin commands need --user-pool-id.
Admins must sign out and back in for a group change to take effect (it is a token claim).
"""

import argparse
import sys
from datetime import UTC, datetime

from _common import require_account, session

ADMIN_GROUP = "PortalAdmins"
CONFIG_PK = "CONFIG#GLOBAL"
COUNTER_PK = "META#COUNTER"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "command",
        choices=[
            "status",
            "disable-issuance",
            "enable-issuance",
            "release-slot",
            "add-admin",
            "remove-admin",
            "list-admins",
        ],
    )
    p.add_argument("--profile")
    p.add_argument("--region", help="region of the DynamoDB table")
    p.add_argument("--table")
    p.add_argument("--user-pool-id")
    p.add_argument("--email", help="admin user's email (add-admin / remove-admin)")
    p.add_argument("--sub", help="Cognito sub (release-slot)")
    p.add_argument("--expected-account")
    args = p.parse_args()

    sess = session(args.profile, args.region)
    require_account(sess, args.expected_account)
    if args.command in ("add-admin", "remove-admin", "list-admins"):
        if not args.user_pool_id:
            sys.exit("--user-pool-id is required")
        idp = sess.client("cognito-idp")
        if args.command == "list-admins":
            pages = idp.get_paginator("list_users_in_group").paginate(
                UserPoolId=args.user_pool_id, GroupName=ADMIN_GROUP
            )
            for page in pages:
                for u in page["Users"]:
                    attrs = {a["Name"]: a["Value"] for a in u["Attributes"]}
                    print(attrs.get("email", u["Username"]))
            return
        if not args.email:
            sys.exit("--email is required")
        email = args.email.strip().lower()
        if args.command == "add-admin":
            idp.admin_add_user_to_group(
                UserPoolId=args.user_pool_id, Username=email, GroupName=ADMIN_GROUP
            )
            print(f"{email} is now an admin (sign out and back in to apply)")
        else:
            idp.admin_remove_user_from_group(
                UserPoolId=args.user_pool_id, Username=email, GroupName=ADMIN_GROUP
            )
            print(f"{email} is no longer an admin (takes effect when their token expires)")
        return

    if not args.table:
        sys.exit("--table is required")
    ddb = sess.client("dynamodb")

    if args.command == "status":
        cfg = ddb.get_item(TableName=args.table, Key={"pk": {"S": CONFIG_PK}}, ConsistentRead=True)
        item = cfg.get("Item", {})
        enabled = item.get("issuanceEnabled", {"BOOL": True})["BOOL"]
        cap = item.get("registrationCap", {}).get("N", "(deploy default)")
        cnt = ddb.get_item(TableName=args.table, Key={"pk": {"S": COUNTER_PK}}, ConsistentRead=True)
        slots = cnt.get("Item", {}).get("reservedSlots", {"N": "0"})["N"]
        print(f"issuanceEnabled={enabled}  reservedSlots={slots}  registrationCap={cap}")
    elif args.command in ("disable-issuance", "enable-issuance"):
        enabled = args.command == "enable-issuance"
        # update_item (not put_item) so the stored registration cap is preserved.
        ddb.update_item(
            TableName=args.table,
            Key={"pk": {"S": CONFIG_PK}},
            UpdateExpression="SET issuanceEnabled = :v, updatedAt = :now",
            ExpressionAttributeValues={":v": {"BOOL": enabled}, ":now": {"S": now_iso()}},
        )
        print(f"issuanceEnabled={enabled}")
    elif args.command == "release-slot":
        if not args.sub:
            sys.exit("--sub is required")
        try:
            ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": args.table,
                            "Key": {"pk": {"S": f"USER#{args.sub}"}},
                            "UpdateExpression": "SET slotReserved = :false, updatedAt = :now",
                            "ConditionExpression": (
                                "slotReserved = :true"
                                " AND NOT (#s IN (:active, :issuing, :suspended))"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":true": {"BOOL": True},
                                ":false": {"BOOL": False},
                                ":active": {"S": "ACTIVE"},
                                ":issuing": {"S": "ISSUING"},
                                ":suspended": {"S": "SUSPENDED"},
                                ":now": {"S": now_iso()},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": args.table,
                            "Key": {"pk": {"S": COUNTER_PK}},
                            "UpdateExpression": "ADD reservedSlots :minus",
                            "ConditionExpression": "reservedSlots > :zero",
                            "ExpressionAttributeValues": {
                                ":minus": {"N": "-1"},
                                ":zero": {"N": "0"},
                            },
                        }
                    },
                ]
            )
        except ddb.exceptions.TransactionCanceledException:
            sys.exit("Not released: user has no reserved slot, or still has an ACTIVE/ISSUING key.")
        print(f"Released slot of {args.sub}")


if __name__ == "__main__":
    main()
