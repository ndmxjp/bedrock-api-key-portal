import json

import pytest

import portal.handler as h
from conftest import CLIENT_ID, ISSUER, admin, ident
from portal.errors import ApiError
from portal.naming import iam_user_name
from portal.repository import Status

ALICE = "alice-0001"
NAME = iam_user_name(ALICE)


def alice():
    return ident(sub=ALICE)


# ---- registration cap ---------------------------------------------------------------


def test_cap_change_takes_effect_and_survives_kill_switch(service, repo, clock):
    for i in range(3):  # settings default cap = 3
        service.issue(ident(sub=f"u{i}"))
    with pytest.raises(ApiError) as e:
        service.issue(ident(sub="u3"))
    assert e.value.code == "REGISTRATION_CAP_REACHED"

    assert service.admin_set_registration_cap(admin(), 5) == {
        "registrationCap": 5,
        "reservedSlots": 3,
    }
    service.admin_set_issuance(admin(), False)  # must not wipe the stored cap
    service.admin_set_issuance(admin(), True)
    assert service.admin_overview(admin())["registrationCap"] == 5
    assert service.issue(ident(sub="u3"))["status"] == "ACTIVE"


def test_lowering_cap_below_usage_blocks_new_users_only(service, clock):
    service.issue(ident(sub="u0"))
    service.issue(ident(sub="u1"))
    service.admin_set_registration_cap(admin(), 1)
    with pytest.raises(ApiError):
        service.issue(ident(sub="u2"))
    # Existing slot holders can still reissue.
    service.revoke(ident(sub="u0"))
    clock.advance(30)
    assert service.issue(ident(sub="u0"))["status"] == "ACTIVE"


@pytest.mark.parametrize("bad", [0, -1, 1001])
def test_cap_bounds(service, bad):
    with pytest.raises(ApiError) as e:
        service.admin_set_registration_cap(admin(), bad)
    assert e.value.code == "INVALID_CAP"


def test_cap_requires_admin(service):
    with pytest.raises(ApiError) as e:
        service.admin_set_registration_cap(ident(), 10)
    assert e.value.code == "FORBIDDEN"


# ---- suspend / resume ---------------------------------------------------------------


def test_suspend_and_resume_same_key(service, iam, repo):
    issued = service.issue(alice())
    out = service.admin_suspend(admin(), ALICE)["user"]
    assert out["status"] == "SUSPENDED" and out["suspended"] is True
    assert iam.creds[NAME][0]["Status"] == "Inactive"

    me = service.me(alice())
    assert me["status"] == "SUSPENDED" and me["suspended"] is True
    assert me["key"]["credentialId"] == issued["credentialId"]

    with pytest.raises(ApiError) as e:
        service.issue(alice())
    assert (e.value.status, e.value.code) == (403, "USER_SUSPENDED")
    with pytest.raises(ApiError) as e:
        service.admin_release_slot(admin(), ALICE)
    assert e.value.code == "SLOT_IN_USE"

    assert service.admin_suspend(admin(), ALICE)["user"]["status"] == "SUSPENDED"  # idempotent

    resumed = service.admin_resume(admin(), ALICE)["user"]
    assert resumed["status"] == "ACTIVE" and resumed["suspended"] is False
    assert iam.creds[NAME][0]["Status"] == "Active"
    assert len(iam.creds[NAME]) == 1  # same key, not a new one
    with pytest.raises(ApiError) as e:
        service.admin_resume(admin(), ALICE)
    assert e.value.code == "NOT_SUSPENDED"


def test_suspension_survives_key_expiry(service, iam, clock):
    service.issue(alice())
    service.admin_suspend(admin(), ALICE)
    clock.advance(8 * 86400)
    assert service.me(alice())["status"] == "EXPIRED"
    with pytest.raises(ApiError) as e:  # expiry is not a way around the hold
        service.issue(alice())
    assert e.value.code == "USER_SUSPENDED"
    out = service.admin_resume(admin(), ALICE)["user"]
    assert out["status"] == "EXPIRED" and out["suspended"] is False
    assert service.issue(alice())["status"] == "ACTIVE"


def test_suspended_user_cannot_bypass_by_self_revoke(service):
    service.issue(alice())
    service.admin_suspend(admin(), ALICE)
    assert service.revoke(alice())["status"] == "REVOKED"
    with pytest.raises(ApiError) as e:
        service.issue(alice())
    assert e.value.code == "USER_SUSPENDED"


def test_admin_can_revoke_suspended_key(service, iam):
    service.issue(alice())
    service.admin_suspend(admin(), ALICE)
    out = service.admin_revoke(admin(), ALICE)["user"]
    assert out["status"] == "REVOKED" and out["suspended"] is True
    out = service.admin_resume(admin(), ALICE)["user"]
    assert out["status"] == "REVOKED" and out["suspended"] is False  # key stays revoked


def test_resume_when_key_was_deleted(service, iam):
    service.issue(alice())
    service.admin_suspend(admin(), ALICE)
    iam.creds[NAME].clear()
    assert service.admin_resume(admin(), ALICE)["user"]["status"] == "REVOKED"


def test_suspend_user_without_live_key(service):
    service.issue(alice())
    service.revoke(alice())
    out = service.admin_suspend(admin(), ALICE)["user"]
    assert out["status"] == "REVOKED" and out["suspended"] is True
    with pytest.raises(ApiError):
        service.issue(alice())


def test_suspend_rolls_back_when_iam_fails(service, iam, repo):
    service.issue(alice())
    iam.fail_next["update_service_specific_credential"] = "ServiceFailure"
    with pytest.raises(ApiError) as e:
        service.admin_suspend(admin(), ALICE)
    assert e.value.code == "AWS_API_ERROR"
    rec = repo.get_registration(ALICE)
    assert rec.status == Status.ACTIVE and rec.suspended is False
    assert iam.creds[NAME][0]["Status"] == "Active"


def test_suspend_unknown_user(service):
    with pytest.raises(ApiError) as e:
        service.admin_suspend(admin(), "nobody")
    assert e.value.code == "USER_NOT_FOUND"


# ---- handler ------------------------------------------------------------------------


@pytest.fixture
def wired(service, settings, monkeypatch):
    monkeypatch.setattr(h, "_service", service)
    monkeypatch.setattr(h, "_settings", settings)
    return service


def ev(route, body=None, sub_param=None):
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
    e = {
        "routeKey": route,
        "body": body,
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }
    if sub_param:
        e["pathParameters"] = {"sub": sub_param}
    return e


def call(e):
    r = h.handler(e, None)
    return r["statusCode"], json.loads(r["body"])


def test_routes(wired):
    wired.issue(alice())
    assert (
        call(ev("POST /api/admin/users/{sub}/suspend", sub_param=ALICE))[1]["user"]["status"]
        == "SUSPENDED"
    )
    assert (
        call(ev("POST /api/admin/users/{sub}/resume", sub_param=ALICE))[1]["user"]["status"]
        == "ACTIVE"
    )
    assert (
        call(ev("PUT /api/admin/registration-cap", body='{"cap": 25}'))[1]["registrationCap"] == 25
    )


@pytest.mark.parametrize(
    "body", ['{"cap": true}', '{"cap": "10"}', '{"cap": 1.5}', "{}", '{"cap": 5, "x": 1}', ""]
)
def test_cap_body_strict(wired, body):
    assert call(ev("PUT /api/admin/registration-cap", body=body))[0] == 400
