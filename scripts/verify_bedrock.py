"""Verify an issued key against Bedrock Runtime (Converse API).

The key is read from AWS_BEARER_TOKEN_BEDROCK (never pass it as a CLI argument; it would
land in shell history / process lists). The key is never printed.

  export AWS_BEARER_TOKEN_BEDROCK=...   # paste, do not commit
  python scripts/verify_bedrock.py --region us-west-2 --allowed <profile-id> \
      --denied <other-model-or-profile-id> [--denied-region us-east-1]

Expected: allowed -> HTTP 200; denied model / region -> HTTP 403 (AccessDenied).
After revoking the key in the portal, re-run: allowed -> HTTP 403.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def converse(region: str, model_id: str, key: str) -> tuple[int, str]:
    url = (
        f"https://bedrock-runtime.{region}.amazonaws.com/model/"
        f"{urllib.parse.quote(model_id, safe='')}/converse"
    )
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": [{"text": "Reply with OK."}]}],
            "inferenceConfig": {"maxTokens": 16},
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.status, "ok"
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("message", "")
        except ValueError:
            detail = ""
        return e.code, (detail or e.reason)[:200]


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--region", required=True)
    p.add_argument("--allowed", required=True, help="allowed inference profile ID")
    p.add_argument("--denied", help="a model/profile ID that must be denied")
    p.add_argument("--denied-region", help="a region that must be denied (uses --allowed model)")
    args = p.parse_args()
    key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not key:
        sys.exit("Set AWS_BEARER_TOKEN_BEDROCK first.")

    checks = [("allowed", args.region, args.allowed, 200)]
    if args.denied:
        checks.append(("denied model", args.region, args.denied, 403))
    if args.denied_region:
        checks.append(("denied region", args.denied_region, args.allowed, 403))

    failed = False
    for label, region, model, expected in checks:
        status, detail = converse(region, model, key)
        ok = status == expected
        failed |= not ok
        print(
            f"[{'PASS' if ok else 'FAIL'}] {label}: {region} {model} -> "
            f"HTTP {status} (expected {expected}) {detail}"
        )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
