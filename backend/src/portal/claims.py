"""Server-side re-validation of Cognito ID-token claims.

API Gateway's JWT authorizer already verified signature, issuer, audience and expiry.
We re-check the claims we depend on so a misconfigured authorizer fails closed.
"""

import re
from dataclasses import dataclass
from typing import Any

from portal.config import Settings
from portal.errors import ApiError

_SUB_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    groups: tuple[str, ...] = ()


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    except KeyError, TypeError:
        raise ApiError(401, "UNAUTHENTICATED") from None
    if not isinstance(claims, dict) or not claims:
        raise ApiError(401, "UNAUTHENTICATED")
    return claims


def _audience_matches(aud: Any, client_id: str) -> bool:
    if isinstance(aud, str):
        # API Gateway may render list claims as "[a b]".
        values = aud.strip("[]").split()
    elif isinstance(aud, list):
        values = [str(a) for a in aud]
    else:
        return False
    return client_id in values


def _list_claim(value: Any) -> tuple[str, ...]:
    """API Gateway renders JWT array claims as "[a b]" strings in the authorizer context."""
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if isinstance(value, str) and value.strip():
        return tuple(value.strip("[]").split())
    return ()


def _is_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def identity_from_event(event: dict[str, Any], settings: Settings, now_epoch: int) -> Identity:
    claims = _claims(event)

    if claims.get("token_use") != "id":
        raise ApiError(401, "INVALID_TOKEN")
    if claims.get("iss") != settings.token_issuer:
        raise ApiError(401, "INVALID_TOKEN")
    if not _audience_matches(claims.get("aud"), settings.user_pool_client_id):
        raise ApiError(401, "INVALID_TOKEN")
    try:
        exp = int(claims.get("exp", 0))
    except TypeError, ValueError:
        raise ApiError(401, "INVALID_TOKEN") from None
    if exp <= now_epoch:
        raise ApiError(401, "INVALID_TOKEN")

    sub = claims.get("sub")
    email = claims.get("email")
    if not isinstance(sub, str) or not _SUB_PATTERN.match(sub):
        raise ApiError(401, "INVALID_TOKEN")
    if not isinstance(email, str) or not email:
        raise ApiError(401, "INVALID_TOKEN")

    return Identity(
        sub=sub,
        email=email,
        email_verified=_is_true(claims.get("email_verified")),
        groups=_list_claim(claims.get("cognito:groups")),
    )
