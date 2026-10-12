"""Lambda entry point for the HTTP API (payload format 2.0)."""

import hmac
import json
import os
import re
import time
from typing import Any

import boto3

from portal.claims import identity_from_event
from portal.config import Settings
from portal.costs import CostReporter
from portal.errors import ApiError
from portal.issuer import Issuer
from portal.log import log
from portal.models import ModelCatalog
from portal.repository import Repository
from portal.service import PortalService

_service: PortalService | None = None
_origin_secret: str | None = None
_origin_secret_loaded_at = 0.0
ORIGIN_SECRET_TTL = 300.0  # re-read periodically so a rotated secret is picked up
_settings: Settings | None = None

SECURITY_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def build_service(settings: Settings) -> PortalService:
    ddb = boto3.client("dynamodb")
    repo = Repository(ddb, settings.table_name)
    # Cost Explorer is a global service served from us-east-1.
    costs = CostReporter(boto3.client("ce", region_name="us-east-1"), ddb, settings.table_name)
    issuer = Issuer(
        boto3.client("iam"),
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
    )
    models = ModelCatalog(
        boto3.client("bedrock", region_name=settings.bedrock_region),
        ddb,
        settings.table_name,
        providers=settings.allowed_providers,
    )
    return PortalService(settings, repo, issuer, costs=costs, models=models)


def _get() -> tuple[Settings, PortalService]:
    global _service, _settings
    if _service is None or _settings is None:
        _settings = Settings.from_env()
        _service = build_service(_settings)
    return _settings, _service


def _expected_origin_secret() -> str | None:
    """Secret CloudFront adds as X-Origin-Verify when the portal IP filter is enabled."""
    global _origin_secret, _origin_secret_loaded_at
    arn = os.environ.get("ORIGIN_VERIFY_SECRET_ARN")
    if not arn:
        return None
    if _origin_secret is None or time.time() - _origin_secret_loaded_at > ORIGIN_SECRET_TTL:
        sm = boto3.client("secretsmanager")
        _origin_secret = sm.get_secret_value(SecretId=arn)["SecretString"]
        _origin_secret_loaded_at = time.time()
    return _origin_secret


def _require_cloudfront(event: dict[str, Any]) -> None:
    expected = _expected_origin_secret()
    if expected is None:
        return
    got = (event.get("headers") or {}).get("x-origin-verify", "")
    if not hmac.compare_digest(got.encode(), expected.encode()):
        raise ApiError(403, "FORBIDDEN")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": SECURITY_HEADERS,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _reject_unexpected_body(event: dict[str, Any]) -> None:
    """POST endpoints take no input: identity comes from the JWT, everything else from
    server config. Reject any attempt to pass parameters."""
    body = event.get("body")
    if body is None or body.strip() in ("", "{}"):
        return
    raise ApiError(400, "INVALID_REQUEST")


_SUB_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")


def _target_sub(event: dict[str, Any]) -> str:
    sub = (event.get("pathParameters") or {}).get("sub", "")
    if not isinstance(sub, str) or not _SUB_RE.match(sub):
        raise ApiError(400, "INVALID_REQUEST")
    return sub


def _issuance_body(event: dict[str, Any]) -> bool:
    """The only admin endpoint with input: exactly {"enabled": true|false}."""
    try:
        body = json.loads(event.get("body") or "")
    except ValueError:
        raise ApiError(400, "INVALID_REQUEST") from None
    if (
        not isinstance(body, dict)
        or set(body) != {"enabled"}
        or not isinstance(body["enabled"], bool)
    ):
        raise ApiError(400, "INVALID_REQUEST")
    return body["enabled"]


def _cap_body(event: dict[str, Any]) -> int:
    """Exactly {"cap": <int>} (booleans are rejected even though they are ints in Python)."""
    try:
        body = json.loads(event.get("body") or "")
    except ValueError:
        raise ApiError(400, "INVALID_REQUEST") from None
    if not isinstance(body, dict) or set(body) != {"cap"}:
        raise ApiError(400, "INVALID_REQUEST")
    cap = body["cap"]
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise ApiError(400, "INVALID_REQUEST")
    return cap


def _age_body(event: dict[str, Any]) -> int | None:
    """Exactly {"days": <int> | null}; null means no expiry."""
    try:
        body = json.loads(event.get("body") or "")
    except ValueError:
        raise ApiError(400, "INVALID_REQUEST") from None
    if not isinstance(body, dict) or set(body) != {"days"}:
        raise ApiError(400, "INVALID_REQUEST")
    days = body["days"]
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, int):
        raise ApiError(400, "INVALID_REQUEST")
    return days


def route(event: dict[str, Any], settings: Settings, service: PortalService) -> tuple[int, dict]:
    route_key = event.get("routeKey", "")
    identity = identity_from_event(event, settings, int(time.time()))
    if route_key == "GET /api/me":
        return 200, service.me(identity)
    if route_key == "POST /api/keys":
        _reject_unexpected_body(event)
        return 201, service.issue(identity)
    if route_key == "POST /api/keys/revoke":
        _reject_unexpected_body(event)
        return 200, service.revoke(identity)
    if route_key == "GET /api/admin/overview":
        return 200, service.admin_overview(identity)
    if route_key == "GET /api/admin/users":
        return 200, service.admin_list_users(identity)
    if route_key == "POST /api/admin/users/{sub}/revoke":
        _reject_unexpected_body(event)
        return 200, service.admin_revoke(identity, _target_sub(event))
    if route_key == "POST /api/admin/users/{sub}/release-slot":
        _reject_unexpected_body(event)
        return 200, service.admin_release_slot(identity, _target_sub(event))
    if route_key == "POST /api/admin/users/{sub}/suspend":
        _reject_unexpected_body(event)
        return 200, service.admin_suspend(identity, _target_sub(event))
    if route_key == "POST /api/admin/users/{sub}/resume":
        _reject_unexpected_body(event)
        return 200, service.admin_resume(identity, _target_sub(event))
    if route_key == "PUT /api/admin/credential-age":
        return 200, service.admin_set_credential_age(identity, _age_body(event))
    if route_key == "PUT /api/admin/registration-cap":
        return 200, service.admin_set_registration_cap(identity, _cap_body(event))
    if route_key == "GET /api/admin/costs":
        params = event.get("queryStringParameters") or {}
        if set(params) - {"refresh"} or params.get("refresh", "0") not in ("0", "1"):
            raise ApiError(400, "INVALID_REQUEST")
        return 200, service.admin_costs(identity, refresh=params.get("refresh") == "1")
    if route_key == "PUT /api/admin/issuance":
        enabled = _issuance_body(event)
        return 200, service.admin_set_issuance(identity, enabled)
    raise ApiError(404, "NOT_FOUND")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = (event.get("requestContext") or {}).get("requestId")
    route_key = event.get("routeKey")
    try:
        _require_cloudfront(event)
        settings, service = _get()
        status, body = route(event, settings, service)
    except ApiError as e:
        log("INFO", "request", route=route_key, requestId=request_id, status=e.status, code=e.code)
        return _response(e.status, e.to_body())
    except Exception as e:
        # Log the type only: exception messages are not guaranteed to be secret-free.
        log("ERROR", "unhandled", route=route_key, requestId=request_id, type=type(e).__name__)
        err = ApiError(500, "INTERNAL_ERROR")
        return _response(err.status, err.to_body())
    log("INFO", "request", route=route_key, requestId=request_id, status=status)
    return _response(status, body)
