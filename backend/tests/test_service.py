import json

import pytest

from conftest import client_error, ident, make_settings
from portal.errors import ApiError
from portal.issuer import Issuer
from portal.naming import iam_user_name, subject_hash
from portal.repository import Status
from portal.service import PortalService

SUB = "11111111-aaaa-bbbb-cccc-000000000001"
NAME = iam_user_name(SUB)


def test_issue_happy_path(service, iam, repo):
    out = service.issue(ident())
    assert out["status"] == "ACTIVE"
    assert out["apiKey"].startswith("SECRET-")
    assert out["region"] == "us-west-2"
    assert out["expiresAt"] == "2026-10-18T00:00:00Z"
    assert "••••" in out["credentialId"]

    user = iam.users[NAME]
    assert user["Path"] == "/bedrock-poc/"
    tags = {t["Key"]: t["Value"] for t in user["Tags"]}
    assert tags["ManagedBy"] == "BedrockPoCPortal" and tags["PoC"] == "true"
    assert iam.attached[NAME] == {"arn:aws:iam::123456789012:policy/BedrockPoCUserPolicy"}
    assert len(iam.active_creds(NAME)) == 1

    rec = repo.get_registration(SUB)
    assert rec.status == Status.ACTIVE
    # The secret is never persisted.
    assert "SECRET" not in json.dumps(rec.__dict__, default=str)


def test_me_never_returns_secret(service):
    service.issue(ident())
    me = service.me(ident())
    assert me["status"] == "ACTIVE"
    assert "apiKey" not in me
    assert "SECRET" not in json.dumps(me)
    assert me["key"]["credentialId"].startswith("ACCA")


def test_me_not_issued(service):
    me = service.me(ident())
    assert me == {
        "email": "alice@company.co.jp",
        "emailVerified": True,
        "allowed": True,
        "status": "NOT_ISSUED",
        "key": None,
        "region": "us-west-2",
        "regionRestricted": True,
        "allowedModels": ["us.example.model-v1:0"],
        "allModels": False,
        "modelProviders": [],
        "credentialAgeDays": 7,
        "suspended": False,
        "issuanceEnabled": True,
        "isAdmin": False,
        "usage": None,
    }


def test_double_issue_returns_409_and_no_second_key(service, iam, clock):
    service.issue(ident())
    clock.advance(60)
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert (e.value.status, e.value.code) == (409, "ALREADY_ISSUED")
    assert len(iam.creds[NAME]) == 1


def test_disallowed_domain(service, iam):
    with pytest.raises(ApiError) as e:
        service.issue(ident(email="alice@evilcompany.co.jp"))
    assert (e.value.status, e.value.code) == (403, "DOMAIN_NOT_ALLOWED")
    assert iam.calls == []
    assert service.me(ident(email="alice@evilcompany.co.jp"))["allowed"] is False


def test_unverified_email(service, iam):
    with pytest.raises(ApiError) as e:
        service.issue(ident(ev=False))
    assert e.value.code == "EMAIL_NOT_VERIFIED"
    assert iam.calls == []


def test_kill_switch(service, repo, iam):
    repo.set_issuance_enabled(False, "t")
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert (e.value.status, e.value.code) == (503, "ISSUANCE_DISABLED")
    assert iam.calls == []


def test_registration_cap(service, iam, repo):
    for i in range(3):
        service.issue(ident(sub=f"user-{i}"))
    with pytest.raises(ApiError) as e:
        service.issue(ident(sub="user-4"))
    assert (e.value.status, e.value.code) == (403, "REGISTRATION_CAP_REACHED")
    assert repo.reserved_slots() == 3
    assert iam_user_name("user-4") not in iam.users


def test_reissue_after_revoke_does_not_consume_slot(service, repo, iam, clock):
    service.issue(ident())
    old = iam.creds[NAME][0]["ServiceSpecificCredentialId"]
    revoked = service.revoke(ident())
    assert revoked["status"] == "REVOKED"
    assert iam.creds[NAME][0]["Status"] == "Inactive"

    clock.advance(30)
    out = service.issue(ident())
    assert out["apiKey"]
    ids = [c["ServiceSpecificCredentialId"] for c in iam.creds[NAME]]
    assert old not in ids and len(ids) == 1  # old key deleted before new one created
    assert repo.reserved_slots() == 1


def test_revoke_is_idempotent(service, iam):
    service.issue(ident())
    first = service.revoke(ident())
    second = service.revoke(ident())
    assert first["status"] == second["status"] == "REVOKED"
    assert iam.calls.count("update_service_specific_credential") == 1


def test_revoke_without_key(service):
    with pytest.raises(ApiError) as e:
        service.revoke(ident())
    assert (e.value.status, e.value.code) == (404, "KEY_NOT_FOUND")


def test_users_only_touch_their_own_key(service, iam):
    service.issue(ident(sub="alice"))
    service.issue(ident(sub="bob", email="bob@company.co.jp"))
    service.revoke(ident(sub="bob", email="bob@company.co.jp"))
    assert len(iam.active_creds(iam_user_name("alice"))) == 1
    assert len(iam.active_creds(iam_user_name("bob"))) == 0


def test_revoke_allowed_even_if_domain_removed_later(settings, repo, iam, clock, service):
    service.issue(ident())
    narrowed = PortalService(
        make_settings(allowed_domains=frozenset({"other.example"})),
        repo,
        Issuer(
            iam,
            path=settings.iam_path,
            user_policy_arn=settings.user_policy_arn,
            permissions_boundary_arn=settings.permissions_boundary_arn,
        ),
        clock=clock,
    )
    assert narrowed.revoke(ident())["status"] == "REVOKED"


def test_iam_failure_marks_failed_and_retry_succeeds(service, iam, repo, clock):
    iam.fail_next["create_service_specific_credential"] = "ServiceFailure"
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert (e.value.status, e.value.code) == (502, "AWS_API_ERROR")
    assert repo.get_registration(SUB).status == Status.FAILED
    assert service.me(ident())["status"] == "FAILED"

    with pytest.raises(ApiError) as e:  # cooldown
        service.issue(ident())
    assert e.value.code == "RATE_LIMITED"

    clock.advance(11)
    assert service.issue(ident())["status"] == "ACTIVE"
    assert repo.reserved_slots() == 1


def test_eventual_consistency_retry(service, iam):
    iam.not_visible_times = 2
    assert service.issue(ident())["status"] == "ACTIVE"


def test_db_failure_after_create_rolls_back_credential(service, iam, repo, monkeypatch):
    def boom(**_kw):
        raise client_error("InternalServerError")

    monkeypatch.setattr(repo, "complete_issuance", boom)
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert e.value.status == 500
    assert iam.creds[NAME] == []
    assert repo.get_registration(SUB).status == Status.FAILED


def test_crashed_attempt_orphan_is_recovered(service, iam, repo, clock):
    # Simulate a Lambda that created a key and died before recording it.
    repo.begin_issuance(
        sub=SUB,
        email="alice@company.co.jp",
        iam_user_name=NAME,
        operation_id="dead",
        now_epoch=int(clock.now.timestamp()),
        now_iso="t",
        cap=3,
        cooldown_seconds=10,
        lease_seconds=90,
        needs_slot=True,
    )
    iam.create_user(
        Path="/bedrock-poc/",
        UserName=NAME,
        PermissionsBoundary="arn:aws:iam::123456789012:policy/BedrockPoCUserPolicy",
        Tags=[
            {"Key": "ManagedBy", "Value": "BedrockPoCPortal"},
            {"Key": "PortalSubjectHash", "Value": subject_hash(SUB)},
        ],
    )
    iam.create_service_specific_credential(
        UserName=NAME, ServiceName="bedrock.amazonaws.com", CredentialAgeDays=7
    )

    with pytest.raises(ApiError) as e:  # lease still valid
        service.issue(ident())
    assert e.value.code == "ISSUING"

    clock.advance(91)
    assert service.me(ident())["status"] == "FAILED"
    out = service.issue(ident())
    assert out["status"] == "ACTIVE"
    assert len(iam.creds[NAME]) == 1  # orphan deleted
    assert repo.reserved_slots() == 1


def test_foreign_iam_user_is_never_touched(service, iam, repo):
    iam.users[NAME] = {"UserName": NAME, "Path": "/", "Tags": []}
    iam.creds[NAME] = [
        {
            "ServiceSpecificCredentialId": "ACCAFOREIGN",
            "ServiceName": "bedrock.amazonaws.com",
            "Status": "Active",
        }
    ]
    with pytest.raises(ApiError) as e:
        service.issue(ident())
    assert e.value.status == 500
    assert iam.creds[NAME][0]["Status"] == "Active"
    assert "delete_service_specific_credential" not in iam.calls
    assert "attach_user_policy" not in iam.calls
    assert repo.get_registration(SUB).error_code == "IAM_USER_CONFLICT"


def test_expired_key_reflected_from_iam_and_reissuable(service, iam, clock, repo):
    service.issue(ident())
    clock.advance(7 * 86400 + 1)
    me = service.me(ident())
    assert me["status"] == "EXPIRED"
    assert repo.get_registration(SUB).status == Status.EXPIRED
    assert service.revoke(ident())["status"] == "EXPIRED"
    assert service.issue(ident())["status"] == "ACTIVE"


def test_admin_deactivation_reflected(service, iam):
    service.issue(ident())
    iam.creds[NAME][0]["Status"] = "Inactive"
    assert service.me(ident())["status"] == "REVOKED"


def test_me_tolerates_iam_errors(service, iam):
    service.issue(ident())
    iam.fail_next["list_service_specific_credentials"] = "Throttling"
    assert service.me(ident())["status"] == "ACTIVE"
