import json

import pytest

import portal.handler as h
from conftest import CLIENT_ID, ISSUER
from portal import presignup


@pytest.fixture
def wired(service, settings, monkeypatch):
    monkeypatch.setattr(h, "_service", service)
    monkeypatch.setattr(h, "_settings", settings)
    return service


def event(route, body=None, **claims):
    c = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "4000000000",
        "sub": "11111111-aaaa-bbbb-cccc-000000000001",
        "email": "alice@company.co.jp",
        "email_verified": "true",
    }
    c.update(claims)
    return {
        "routeKey": route,
        "body": body,
        "requestContext": {"requestId": "r1", "authorizer": {"jwt": {"claims": c}}},
    }


def call(e):
    resp = h.handler(e, None)
    return resp["statusCode"], json.loads(resp["body"]), resp["headers"]


def test_full_flow_and_no_secret_in_logs(wired, capsys):
    status, body, headers = call(event("POST /api/keys"))
    assert status == 201
    secret = body["apiKey"]
    assert headers["Cache-Control"] == "no-store"

    status, me, _ = call(event("GET /api/me"))
    assert status == 200 and me["status"] == "ACTIVE" and secret not in json.dumps(me)

    status, body, _ = call(event("POST /api/keys"))
    assert (status, body["error"]["code"]) == (409, "ALREADY_ISSUED")

    status, body, _ = call(event("POST /api/keys/revoke"))
    assert status == 200 and body["status"] == "REVOKED"

    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err


def test_error_format(wired):
    status, body, _ = call(event("POST /api/keys", email="mallory@evilcompany.co.jp"))
    assert status == 403
    assert body == {
        "error": {"code": "DOMAIN_NOT_ALLOWED", "message": "このメールドメインは利用対象外です。"}
    }


@pytest.mark.parametrize(
    "body",
    ['{"userName": "admin"}', '{"days": 365}', '{"email": "alice@company.co.jp"}', "x"],
)
def test_client_parameters_rejected(wired, iam, body):
    status, resp, _ = call(event("POST /api/keys", body=body))
    assert (status, resp["error"]["code"]) == (400, "INVALID_REQUEST")
    assert iam.calls == []


def test_empty_object_body_accepted(wired):
    assert call(event("POST /api/keys", body="{}"))[0] == 201


def test_unauthenticated(wired):
    resp = h.handler({"routeKey": "GET /api/me", "requestContext": {}}, None)
    assert resp["statusCode"] == 401


def test_tampered_token_claims(wired):
    assert call(event("POST /api/keys", iss="https://evil"))[0] == 401
    assert call(event("POST /api/keys", exp="1"))[0] == 401
    assert call(event("POST /api/keys", email_verified="false"))[0] == 403


def test_unknown_route(wired):
    assert call(event("DELETE /api/keys"))[0] == 404


def test_unhandled_exception_is_generic(wired, monkeypatch, capsys):
    def boom(_identity):
        raise RuntimeError("SENSITIVE detail")

    monkeypatch.setattr(wired, "me", boom)
    status, body, _ = call(event("GET /api/me"))
    assert status == 500 and body["error"]["code"] == "INTERNAL_ERROR"
    assert "SENSITIVE" not in capsys.readouterr().out


def _signup(email, source="PreSignUp_SignUp"):
    return {
        "triggerSource": source,
        "request": {"userAttributes": {"email": email}},
        "response": {},
    }


def test_presignup(monkeypatch):
    monkeypatch.setattr(presignup, "_ALLOWED", frozenset({"company.co.jp"}))
    out = presignup.handler(_signup("alice@company.co.jp"), None)
    assert out["response"]["autoConfirmUser"] is False
    assert out["response"]["autoVerifyEmail"] is False
    with pytest.raises(Exception, match="DOMAIN_NOT_ALLOWED"):
        presignup.handler(_signup("alice@evilcompany.co.jp"), None)
    with pytest.raises(Exception, match="SIGNUP_NOT_ALLOWED"):
        presignup.handler(_signup("alice@company.co.jp", "PreSignUp_ExternalProvider"), None)
