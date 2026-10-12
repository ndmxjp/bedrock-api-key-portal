"""Cognito Pre sign-up trigger: reject non-allowed domains early.

This is only an early filter. The issuing Lambda re-validates the domain from the token.
The error text is surfaced to the client as "PreSignUp failed with error <text>."
"""

import os
from typing import Any

from portal.domain import is_allowed_email, parse_allowed_domains
from portal.log import log

_ALLOWED = parse_allowed_domains(os.environ.get("ALLOWED_DOMAINS", "invalid.invalid"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("triggerSource") == "PreSignUp_ExternalProvider":
        log("INFO", "presignup_rejected", reason="external_provider")
        raise Exception("SIGNUP_NOT_ALLOWED")
    email = (event.get("request", {}).get("userAttributes") or {}).get("email", "")
    if not is_allowed_email(email, _ALLOWED):
        log("INFO", "presignup_rejected", reason="domain")
        raise Exception("DOMAIN_NOT_ALLOWED")
    # Never auto-confirm or auto-verify: the email must be proven by the code Cognito sends.
    event.setdefault("response", {})
    event["response"]["autoConfirmUser"] = False
    event["response"]["autoVerifyEmail"] = False
    return event
