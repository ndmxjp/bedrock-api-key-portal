import json

import pytest

import portal.handler as h
from conftest import CLIENT_ID, ISSUER, admin, ident, make_settings
from portal.errors import ApiError
from portal.issuer import Issuer
from portal.naming import iam_user_name
from portal.service import PortalService


@pytest.fixture
def unlimited_service(repo, iam, clock):
    settings = make_settings(max_credential_age_days=0)  # unlimited allowed
    issuer = Issuer(
        iam,
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
        sleep=lambda _s: None,
    )
    return PortalService(settings, repo, issuer, clock=clock)


def test_default_ceiling_rejects_unlimited_and_long_values(service):
    with pytest.raises(ApiError) as e:
        service.admin_set_credential_age(admin(), None)
    assert e.value.code == "INVALID_AGE"
    with pytest.raises(ApiError):
        service.admin_set_credential_age(admin(), 31)
    with pytest.raises(ApiError):
        service.admin_set_credential_age(admin(), 0)
    assert service.admin_set_credential_age(admin(), 30) == {"credentialAgeDays": 30}
    assert service.issue(ident())["expiresAt"] == "2026-11-10T00:00:00Z"


def test_unlimited_keys(unlimited_service, iam):
    svc = unlimited_service
    assert svc.admin_set_credential_age(admin(), None) == {"credentialAgeDays": None}
    assert svc.me(ident())["credentialAgeDays"] is None
    assert svc.admin_overview(admin())["maxCredentialAgeDays"] is None

    out = svc.issue(ident())
    assert out["expiresAt"] is None
    cred = iam.creds[iam_user_name(ident().sub)][0]
    assert "ExpirationDate" not in cred  # CredentialAgeDays was omitted
    me = svc.me(ident())
    assert me["status"] == "ACTIVE" and me["key"]["expiresAt"] is None


def test_long_values_allowed_under_unlimited_ceiling(unlimited_service):
    unlimited_service.admin_set_credential_age(admin(), 3650)
    assert unlimited_service.issue(ident())["expiresAt"] == "2036-10-08T00:00:00Z"
    with pytest.raises(ApiError):
        unlimited_service.admin_set_credential_age(admin(), 36601)


def test_stored_value_above_lowered_ceiling_falls_back_to_default(
    repo, iam, clock, unlimited_service
):
    unlimited_service.admin_set_credential_age(admin(), None)
    # Redeployed later with a 30-day ceiling: the stored "unlimited" must not be honored.
    strict = PortalService(make_settings(), repo, unlimited_service._issuer, clock=clock)
    assert strict.me(ident())["credentialAgeDays"] == 7
    assert strict.issue(ident())["expiresAt"] == "2026-10-18T00:00:00Z"


def test_requires_admin(service):
    with pytest.raises(ApiError) as e:
        service.admin_set_credential_age(ident(), 7)
    assert e.value.code == "FORBIDDEN"


def test_settings_validation():
    make_settings(max_credential_age_days=0, credential_age_days=0)
    with pytest.raises(ValueError):
        make_settings(credential_age_days=0)  # unlimited default needs an unlimited ceiling
    with pytest.raises(ValueError):
        make_settings(max_credential_age_days=36601)


@pytest.fixture
def wired(unlimited_service, monkeypatch):
    monkeypatch.setattr(h, "_service", unlimited_service)
    monkeypatch.setattr(h, "_settings", unlimited_service._s)


def call(body):
    claims = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "4000000000",
        "sub": "admin-0001",
        "email": "boss@company.co.jp",
        "email_verified": "true",
        "cognito:groups": "[PortalAdmins]",
    }
    r = h.handler(
        {
            "routeKey": "PUT /api/admin/credential-age",
            "body": body,
            "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        },
        None,
    )
    return r["statusCode"], json.loads(r["body"])


def test_route(wired):
    assert call('{"days": null}') == (200, {"credentialAgeDays": None})
    assert call('{"days": 90}') == (200, {"credentialAgeDays": 90})


@pytest.mark.parametrize(
    "body", ['{"days": true}', '{"days": "7"}', '{"days": 1.5}', "{}", '{"days": 7, "x": 1}']
)
def test_route_body_strict(wired, body):
    assert call(body)[0] == 400
