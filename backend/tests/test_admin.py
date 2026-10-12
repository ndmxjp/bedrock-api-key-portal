import json

import pytest

import portal.handler as h
from conftest import CLIENT_ID, ISSUER, admin, ident
from portal.errors import ApiError
from portal.naming import iam_user_name

ALICE = "alice-0001"


def test_non_admins_are_forbidden(service):
    for who in [
        ident(),  # no group
        ident(groups=("SomethingElse",)),
        ident(groups=("PortalAdmins",), email="boss@evilcompany.co.jp"),  # domain still checked
        ident(groups=("PortalAdmins",), ev=False),  # verified email still required
    ]:
        for call in (
            lambda w=who: service.admin_overview(w),
            lambda w=who: service.admin_list_users(w),
            lambda w=who: service.admin_revoke(w, ALICE),
            lambda w=who: service.admin_release_slot(w, ALICE),
            lambda w=who: service.admin_set_issuance(w, False),
        ):
            with pytest.raises(ApiError) as e:
                call()
            assert (e.value.status, e.value.code) == (403, "FORBIDDEN")


def test_me_reports_admin(service):
    assert service.me(admin())["isAdmin"] is True
    assert service.me(ident())["isAdmin"] is False


def test_overview_and_list(service, clock):
    service.issue(ident(sub=ALICE))
    clock.advance(1)
    service.issue(ident(sub="bob-0001", email="bob@company.co.jp"))
    service.revoke(ident(sub="bob-0001", email="bob@company.co.jp"))

    ov = service.admin_overview(admin())
    assert ov["reservedSlots"] == 2
    assert ov["registrationCap"] == 3
    assert ov["statusCounts"] == {"ACTIVE": 1, "REVOKED": 1}
    assert ov["issuanceEnabled"] is True

    users = service.admin_list_users(admin())["users"]
    assert [u["email"] for u in users] == ["bob@company.co.jp", "alice@company.co.jp"]
    dumped = json.dumps(users)
    assert "SECRET" not in dumped and "apiKey" not in dumped
    assert all("••••" in u["credentialId"] for u in users)


def test_force_revoke_and_release_slot(service, iam, repo, clock):
    service.issue(ident(sub=ALICE))
    with pytest.raises(ApiError) as e:  # cannot release while ACTIVE
        service.admin_release_slot(admin(), ALICE)
    assert e.value.code == "SLOT_IN_USE"

    out = service.admin_revoke(admin(), ALICE)
    assert out["user"]["status"] == "REVOKED"
    assert iam.creds[iam_user_name(ALICE)][0]["Status"] == "Inactive"
    assert service.me(ident(sub=ALICE))["status"] == "REVOKED"
    assert service.admin_revoke(admin(), ALICE)["user"]["status"] == "REVOKED"  # idempotent

    assert service.admin_release_slot(admin(), ALICE)["user"]["slotReserved"] is False
    assert repo.reserved_slots() == 0
    with pytest.raises(ApiError) as e:
        service.admin_release_slot(admin(), ALICE)
    assert e.value.code == "NO_SLOT"

    # Released user can come back and takes a slot again.
    clock.advance(30)
    service.issue(ident(sub=ALICE))
    assert repo.reserved_slots() == 1


def test_unknown_user(service):
    with pytest.raises(ApiError) as e:
        service.admin_revoke(admin(), "nobody")
    assert e.value.code == "USER_NOT_FOUND"


def test_kill_switch(service, repo):
    assert service.admin_set_issuance(admin(), False) == {"issuanceEnabled": False}
    assert not repo.issuance_enabled()
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert e.value.code == "ISSUANCE_DISABLED"
    service.admin_set_issuance(admin(), True)
    assert service.issue(ident())["status"] == "ACTIVE"


# ---- handler -----------------------------------------------------------------------


@pytest.fixture
def wired(service, settings, monkeypatch):
    monkeypatch.setattr(h, "_service", service)
    monkeypatch.setattr(h, "_settings", settings)
    return service


def ev(route, body=None, sub_param=None, groups="[PortalAdmins]", sub="admin-0001"):
    claims = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "4000000000",
        "sub": sub,
        "email": "boss@company.co.jp",
        "email_verified": "true",
        "cognito:groups": groups,
    }
    e = {
        "routeKey": route,
        "body": body,
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }
    if sub_param is not None:
        e["pathParameters"] = {"sub": sub_param}
    return e


def call(e):
    r = h.handler(e, None)
    return r["statusCode"], json.loads(r["body"])


def test_admin_routes(wired):
    wired.issue(ident(sub=ALICE))
    assert call(ev("GET /api/admin/overview"))[0] == 200
    status, body = call(ev("GET /api/admin/users"))
    assert status == 200 and body["users"][0]["sub"] == ALICE
    assert (
        call(ev("POST /api/admin/users/{sub}/revoke", sub_param=ALICE))[1]["user"]["status"]
        == "REVOKED"
    )
    assert call(ev("POST /api/admin/users/{sub}/release-slot", sub_param=ALICE))[0] == 200
    assert call(ev("PUT /api/admin/issuance", body='{"enabled": false}')) == (
        200,
        {"issuanceEnabled": False},
    )


def test_admin_routes_reject_non_admin_tokens(wired):
    assert call(ev("GET /api/admin/users", groups=""))[0] == 403
    assert call(ev("GET /api/admin/users", groups="[Other]"))[0] == 403


@pytest.mark.parametrize(
    "body",
    ["", "{}", '{"enabled": "false"}', '{"enabled": 0}', '{"enabled": true, "x": 1}', "nope"],
)
def test_issuance_body_strict(wired, body):
    assert call(ev("PUT /api/admin/issuance", body=body))[0] == 400


@pytest.mark.parametrize("bad", ["../x", "a b", "", "x" * 200])
def test_path_param_validated(wired, bad):
    assert call(ev("POST /api/admin/users/{sub}/revoke", sub_param=bad))[0] == 400
