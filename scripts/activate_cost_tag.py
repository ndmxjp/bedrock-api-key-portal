"""Activate the portal's IAM principal cost allocation tag in the PAYER account.

Bedrock attributes inference cost to the calling IAM principal's tags. The portal tags each
IAM user with PortalSubjectHash; Cost Explorer only reports it after the tag is activated
as `iamPrincipal/PortalSubjectHash`. Activation is only possible from the organization's
management (payer) account, and the tag only appears there after a portal user has made a
Bedrock call that reached billing (up to 24 hours). Activation is not retroactive.

  python scripts/activate_cost_tag.py --profile <payer-profile>            # check only
  python scripts/activate_cost_tag.py --profile <payer-profile> --apply \
      --expected-account <payer-account-id>
"""

import argparse
import sys

from _common import require_account, session

TAG_KEY = "iamPrincipal/PortalSubjectHash"


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--profile", help="profile for the payer (management) account")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-account")
    args = p.parse_args()
    if args.apply and not args.expected_account:
        p.error("--apply requires --expected-account")

    sess = session(args.profile, "us-east-1")
    require_account(sess, args.expected_account)
    ce = sess.client("ce")
    tags = ce.list_cost_allocation_tags(TagKeys=[TAG_KEY]).get("CostAllocationTags", [])
    if not tags:
        sys.exit(
            f"{TAG_KEY} is not visible yet. It appears after a portal user's Bedrock usage "
            "reaches billing (up to 24h). Re-run later."
        )
    tag = tags[0]
    print(f"{TAG_KEY}: {tag['Status']} (last used {tag.get('LastUsedDate', '-')})")
    if tag["Status"] == "Active":
        return
    if not args.apply:
        print("Dry-run only. Re-run with --apply --expected-account <payer-account-id>.")
        return
    resp = ce.update_cost_allocation_tags_status(
        CostAllocationTagsStatus=[{"TagKey": TAG_KEY, "Status": "Active"}]
    )
    if resp.get("Errors"):
        sys.exit(f"Activation failed: {resp['Errors']}")
    print("Activated. Costs from now on are attributed per user (data lags up to 24h).")


if __name__ == "__main__":
    main()
