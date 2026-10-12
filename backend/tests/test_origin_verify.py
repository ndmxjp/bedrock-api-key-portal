import json

import pytest

import portal.handler as h
from conftest import CLIENT_ID, ISSUER


@pytest.fixture
def wired(service, settings, monkeypatch):
    monkeypatch.setattr(h, "_service", service)
    monkeypatch.setattr(h, "_settings", settings)
    return service


def ev(headers=None):
    claims = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "4000000000",
        "sub": "u-1",
        "email": "alice@company.co.jp",
        "email_verified": "true",
    }
    return {
        "routeKey": "GET /api/me",
        "headers": headers or {},
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }


def status(e):
    r = h.handler(e, None)
    return r["statusCode"], json.loads(r["body"])


def test_no_check_when_ip_filter_disabled(wired, monkeypatch):
    monkeypatch.delenv("ORIGIN_VERIFY_SECRET_ARN", raising=False)
    assert status(ev())[0] == 200


def test_requests_must_come_through_cloudfront(wired, monkeypatch):
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:s")
    monkeypatch.setattr(h, "_origin_secret", "s3cr3t-value")
    monkeypatch.setattr(h, "_origin_secret_loaded_at", __import__("time").time())
    code, body = status(ev())
    assert (code, body["error"]["code"]) == (403, "FORBIDDEN")
    assert status(ev({"x-origin-verify": "wrong"}))[0] == 403
    assert status(ev({"x-origin-verify": "s3cr3t-value"}))[0] == 200


def test_secret_is_loaded_and_cached(wired, monkeypatch):
    calls = []

    class FakeSM:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": "abc"}

    monkeypatch.setenv("ORIGIN_VERIFY_SECRET_ARN", "arn:s")
    monkeypatch.setattr(h, "_origin_secret", None)
    monkeypatch.setattr(h.boto3, "client", lambda name, **_k: FakeSM())
    assert status(ev({"x-origin-verify": "abc"}))[0] == 200
    assert status(ev({"x-origin-verify": "abc"}))[0] == 200
    assert calls == ["arn:s"]
