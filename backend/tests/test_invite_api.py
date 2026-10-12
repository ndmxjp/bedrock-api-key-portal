"""Admin invitation API: POST /api/admin/users -> Cognito AdminCreateUser."""

import json

import pytest

import portal.handler as h
from conftest import admin, client_error, ident, make_settings
from portal.errors import ApiError
from portal.issuer import Issuer
from portal.service import PortalService
from test_handler import call, event

POOL = "ap-northeast-1_test"


class FakeIdp:
    """In-memory cognito-idp covering AdminCreateUser only."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.users: dict[str, str] = {}  # email -> status
        self.fail_next: str | None = None

    def admin_create_user(self, **params):
        self.calls.append(params)
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            raise client_error(code, "AdminCreateUser")
        email = params["Username"]
        if params.get("MessageAction") == "RESEND":
            if email not in self.users:
                raise client_error("UserNotFoundException", "AdminCreateUser")
            if self.users[email] != "FORCE_CHANGE_PASSWORD":
                raise client_error("UnsupportedUserStateException", "AdminCreateUser")
            return {}
        if email in self.users:
            raise client_error("UsernameExistsException", "AdminCreateUser")
        self.users[email] = "FORCE_CHANGE_PASSWORD"
        return {"User": {"Username": email, "UserStatus": "FORCE_CHANGE_PASSWORD"}}


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def inviting(repo, iam, clock, idp, monkeypatch):
    settings = make_settings(user_pool_id=POOL)
    issuer = Issuer(
        iam,
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
        sleep=lambda _s: None,
    )
    service = PortalService(settings, repo, issuer, clock=clock, idp=idp)
    monkeypatch.setattr(h, "_service", service)
    monkeypatch.setattr(h, "_settings", settings)
    return service


def test_invite_normalizes_and_sends_temporary_password(inviting, idp, capsys):
    out = inviting.admin_invite_user(admin(), "  Carol@Company.CO.JP ")
    assert out == {"email": "carol@company.co.jp", "resent": False}
    [params] = idp.calls
    assert params["UserPoolId"] == POOL
    assert params["Username"] == "carol@company.co.jp"
    assert {a["Name"]: a["Value"] for a in params["UserAttributes"]} == {
        "email": "carol@company.co.jp",
        "email_verified": "true",
    }
    assert params["DesiredDeliveryMediums"] == ["EMAIL"]
    assert "MessageAction" not in params
    assert "TemporaryPassword" not in params  # Cognito generates and emails it
    logged = capsys.readouterr().out
    assert "carol" not in logged and "company.co.jp" in logged


def test_invite_requires_admin(inviting, idp):
    for who in [ident(), ident(groups=("PortalAdmins",), email="boss@evilcompany.co.jp")]:
        with pytest.raises(ApiError) as e:
            inviting.admin_invite_user(who, "carol@company.co.jp")
        assert (e.value.status, e.value.code) == (403, "FORBIDDEN")
    assert idp.calls == []


@pytest.mark.parametrize(
    ("email", "code"),
    [
        ("not-an-email", "INVALID_EMAIL"),
        ("a@b@company.co.jp", "INVALID_EMAIL"),
        ("mallory@evilcompany.co.jp", "DOMAIN_NOT_ALLOWED"),
        ("mallory@sub.company.co.jp", "DOMAIN_NOT_ALLOWED"),
        ("mallory@company.co.jp.evil.example", "DOMAIN_NOT_ALLOWED"),
    ],
)
def test_invite_rejects_bad_addresses_before_calling_cognito(inviting, idp, email, code):
    with pytest.raises(ApiError) as e:
        inviting.admin_invite_user(admin(), email)
    assert (e.value.status, e.value.code) == (400, code)
    assert idp.calls == []


def test_invite_existing_user_and_resend(inviting, idp):
    inviting.admin_invite_user(admin(), "carol@company.co.jp")
    with pytest.raises(ApiError) as e:
        inviting.admin_invite_user(admin(), "carol@company.co.jp")
    assert (e.value.status, e.value.code) == (409, "USER_EXISTS")

    out = inviting.admin_invite_user(admin(), "carol@company.co.jp", resend=True)
    assert out == {"email": "carol@company.co.jp", "resent": True}
    assert idp.calls[-1]["MessageAction"] == "RESEND"

    with pytest.raises(ApiError) as e:
        inviting.admin_invite_user(admin(), "nobody@company.co.jp", resend=True)
    assert (e.value.status, e.value.code) == (404, "USER_NOT_FOUND")

    idp.users["carol@company.co.jp"] = "CONFIRMED"
    with pytest.raises(ApiError) as e:
        inviting.admin_invite_user(admin(), "carol@company.co.jp", resend=True)
    assert (e.value.status, e.value.code) == (409, "USER_ALREADY_CONFIRMED")


@pytest.mark.parametrize(
    ("aws_code", "status", "code"),
    [
        ("LimitExceededException", 429, "EMAIL_LIMIT"),
        ("TooManyRequestsException", 429, "EMAIL_LIMIT"),
        ("UserLambdaValidationException", 400, "DOMAIN_NOT_ALLOWED"),
        ("InternalErrorException", 502, "AWS_API_ERROR"),
    ],
)
def test_invite_maps_cognito_errors(inviting, idp, aws_code, status, code):
    idp.fail_next = aws_code
    with pytest.raises(ApiError) as e:
        inviting.admin_invite_user(admin(), "carol@company.co.jp")
    assert (e.value.status, e.value.code) == (status, code)


def test_invite_disabled_without_user_pool(service):
    with pytest.raises(ApiError) as e:
        service.admin_invite_user(admin(), "carol@company.co.jp")
    assert (e.value.status, e.value.code) == (503, "INVITE_NOT_AVAILABLE")


def _admin_event(body):
    return event(
        "POST /api/admin/users",
        body=body,
        sub="admin-0001",
        email="boss@company.co.jp",
        **{"cognito:groups": "[PortalAdmins]"},
    )


def test_route_creates_then_resends(inviting, idp):
    status, body, _ = call(_admin_event(json.dumps({"email": "Carol@company.co.jp"})))
    assert (status, body) == (201, {"email": "carol@company.co.jp", "resent": False})
    status, body, _ = call(_admin_event(json.dumps({"email": "carol@company.co.jp"})))
    assert (status, body["error"]["code"]) == (409, "USER_EXISTS")
    resend = json.dumps({"email": "carol@company.co.jp", "resend": True})
    status, body, _ = call(_admin_event(resend))
    assert (status, body) == (200, {"email": "carol@company.co.jp", "resent": True})


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "x",
        "[]",
        '{"email": 1}',
        '{"email": "a@company.co.jp", "resend": "yes"}',
        '{"email": "a@company.co.jp", "temporaryPassword": "Hunter2!Hunter2"}',
        '{"resend": true}',
        '{"email": "' + "a" * 250 + '@company.co.jp"}',
    ],
)
def test_route_rejects_malformed_bodies(inviting, idp, body):
    status, resp, _ = call(_admin_event(body))
    assert (status, resp["error"]["code"]) == (400, "INVALID_REQUEST")
    assert idp.calls == []


def test_route_forbids_non_admins(inviting, idp):
    status, resp, _ = call(event("POST /api/admin/users", body='{"email": "c@company.co.jp"}'))
    assert (status, resp["error"]["code"]) == (403, "FORBIDDEN")
    assert idp.calls == []
