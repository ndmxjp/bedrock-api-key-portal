import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from portal.claims import Identity
from portal.config import Settings
from portal.issuer import Issuer
from portal.repository import Repository
from portal.service import PortalService

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

TABLE = "BedrockPoCRegistrations"
POLICY_ARN = "arn:aws:iam::123456789012:policy/BedrockPoCUserPolicy"
CLIENT_ID = "client123"
ISSUER = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_test"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "table_name": TABLE,
        "allowed_domains": frozenset({"company.co.jp"}),
        "credential_age_days": 7,
        "registration_cap": 3,
        "bedrock_region": "us-west-2",
        "iam_path": "/bedrock-poc/",
        "user_policy_arn": POLICY_ARN,
        "permissions_boundary_arn": POLICY_ARN,
        "user_pool_client_id": CLIENT_ID,
        "token_issuer": ISSUER,
        "allowed_models": ("us.example.model-v1:0",),
        "issue_cooldown_seconds": 10,
        "issuing_lease_seconds": 90,
    }
    values.update(overrides)
    return Settings(**values)


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeIam:
    """In-memory IAM covering only the calls the portal makes."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.creds: dict[str, list[dict[str, Any]]] = {}
        self.attached: dict[str, set[str]] = {}
        self.calls: list[str] = []
        self.fail_next: dict[str, str] = {}
        self.not_visible_times = 0
        self._seq = 0
        self.now = datetime(2026, 10, 11, tzinfo=UTC)

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if op in self.fail_next:
            raise client_error(self.fail_next.pop(op), op)

    def get_user(self, UserName: str) -> dict[str, Any]:
        self._maybe_fail("get_user")
        if UserName not in self.users:
            raise client_error("NoSuchEntity")
        return {"User": self.users[UserName]}

    def create_user(self, Path: str, UserName: str, PermissionsBoundary: str, Tags: list) -> dict:
        self._maybe_fail("create_user")
        if UserName in self.users:
            raise client_error("EntityAlreadyExists")
        self.users[UserName] = {
            "UserName": UserName,
            "Path": Path,
            "Tags": Tags,
            "PermissionsBoundary": {"PermissionsBoundaryArn": PermissionsBoundary},
        }
        self.creds[UserName] = []
        return {"User": self.users[UserName]}

    def attach_user_policy(self, UserName: str, PolicyArn: str) -> None:
        self._maybe_fail("attach_user_policy")
        self.attached.setdefault(UserName, set()).add(PolicyArn)

    def list_service_specific_credentials(self, UserName: str, ServiceName: str) -> dict:
        self._maybe_fail("list_service_specific_credentials")
        if UserName not in self.users:
            raise client_error("NoSuchEntity")
        return {
            "ServiceSpecificCredentials": [
                {k: v for k, v in c.items() if k != "ServiceCredentialSecret"}
                for c in self.creds[UserName]
                if c["ServiceName"] == ServiceName
            ]
        }

    def create_service_specific_credential(
        self, UserName: str, ServiceName: str, CredentialAgeDays: int | None = None
    ) -> dict:
        self._maybe_fail("create_service_specific_credential")
        if self.not_visible_times > 0:
            self.not_visible_times -= 1
            raise client_error("NoSuchEntity")
        if UserName not in self.users:
            raise client_error("NoSuchEntity")
        if len(self.creds[UserName]) >= 2:
            raise client_error("LimitExceeded")
        self._seq += 1
        cred = {
            "ServiceSpecificCredentialId": f"ACCA{self._seq:016d}",
            "ServiceName": ServiceName,
            "UserName": UserName,
            "Status": "Active",
            "CreateDate": self.now,
            "ServiceCredentialSecret": f"SECRET-{self._seq}-do-not-log",
        }
        if CredentialAgeDays is not None:
            cred["ExpirationDate"] = self.now + timedelta(days=CredentialAgeDays)
        self.creds[UserName].append(cred)
        return {"ServiceSpecificCredential": dict(cred)}

    def _find(self, UserName: str, cid: str) -> dict[str, Any]:
        for c in self.creds.get(UserName, []):
            if c["ServiceSpecificCredentialId"] == cid:
                return c
        raise client_error("NoSuchEntity")

    def update_service_specific_credential(
        self, UserName: str, ServiceSpecificCredentialId: str, Status: str
    ) -> None:
        self._maybe_fail("update_service_specific_credential")
        self._find(UserName, ServiceSpecificCredentialId)["Status"] = Status

    def delete_service_specific_credential(
        self, UserName: str, ServiceSpecificCredentialId: str
    ) -> None:
        self._maybe_fail("delete_service_specific_credential")
        c = self._find(UserName, ServiceSpecificCredentialId)
        self.creds[UserName].remove(c)

    def active_creds(self, user: str) -> list[dict[str, Any]]:
        return [c for c in self.creds.get(user, []) if c["Status"] == "Active"]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def create_table(ddb: Any) -> None:
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        create_table(client)
        yield client


@pytest.fixture
def repo(ddb) -> Repository:
    return Repository(ddb, TABLE)


@pytest.fixture
def iam() -> FakeIam:
    return FakeIam()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def service(settings, repo, iam, clock) -> PortalService:
    issuer = Issuer(
        iam,
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
        sleep=lambda _s: None,
    )
    return PortalService(settings, repo, issuer, clock=clock)


def ident(sub: str = "11111111-aaaa-bbbb-cccc-000000000001", **kw: Any) -> Identity:
    return Identity(
        sub=sub,
        email=kw.get("email", "alice@company.co.jp"),
        email_verified=kw.get("ev", True),
        groups=tuple(kw.get("groups", ())),
    )


def admin(**kw: Any) -> Identity:
    return ident(sub="admin-0001", email="boss@company.co.jp", groups=("PortalAdmins",), **kw)
