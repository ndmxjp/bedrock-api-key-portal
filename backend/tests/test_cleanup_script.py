"""The cleanup script must only ever touch portal-managed IAM users."""

import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cleanup

MANAGED_TAGS = [{"Key": "ManagedBy", "Value": "BedrockPoCPortal"}]


class IamWithCredentials:
    """moto IAM client plus an in-memory store for service-specific credentials
    (moto does not implement them)."""

    def __init__(self, client):
        self._c = client
        self.creds: dict[str, list[dict]] = {}

    def __getattr__(self, name):
        return getattr(self._c, name)

    def add_credential(self, user, cid):
        self.creds.setdefault(user, []).append(
            {
                "ServiceSpecificCredentialId": cid,
                "ServiceName": "bedrock.amazonaws.com",
                "Status": "Active",
            }
        )

    def list_service_specific_credentials(self, UserName, ServiceName=None):
        return {"ServiceSpecificCredentials": [dict(c) for c in self.creds.get(UserName, [])]}

    def update_service_specific_credential(self, UserName, ServiceSpecificCredentialId, Status):
        for c in self.creds[UserName]:
            if c["ServiceSpecificCredentialId"] == ServiceSpecificCredentialId:
                c["Status"] = Status

    def delete_service_specific_credential(self, UserName, ServiceSpecificCredentialId):
        self.creds[UserName] = [
            c
            for c in self.creds[UserName]
            if c["ServiceSpecificCredentialId"] != ServiceSpecificCredentialId
        ]

    def delete_user(self, UserName):
        if self.creds.get(UserName):
            raise RuntimeError("DeleteConflict: user still has service-specific credentials")
        self._c.delete_user(UserName=UserName)


@pytest.fixture
def iam(monkeypatch):
    with mock_aws():
        client = IamWithCredentials(boto3.client("iam", region_name="us-east-1"))
        sts = boto3.client("sts", region_name="us-east-1")

        class FakeSession:
            def client(self, name):
                return {"iam": client, "sts": sts}[name]

        monkeypatch.setattr(cleanup, "session", lambda *_a, **_k: FakeSession())
        client.create_user(Path="/bedrock-poc/", UserName="poc-bedrock-aaa", Tags=MANAGED_TAGS)
        client.add_credential("poc-bedrock-aaa", "ACCA0000000000000001")
        client.add_credential("poc-bedrock-untagged", "ACCA0000000000000002")
        # Same path/prefix but missing the tag: must be untouched.
        client.create_user(Path="/bedrock-poc/", UserName="poc-bedrock-untagged")
        # Tagged but outside the path: must be untouched.
        client.create_user(Path="/", UserName="poc-bedrock-elsewhere", Tags=MANAGED_TAGS)
        # Tagged, in path, but with an access key: report and skip.
        client.create_user(Path="/bedrock-poc/", UserName="poc-bedrock-weird", Tags=MANAGED_TAGS)
        client.create_access_key(UserName="poc-bedrock-weird")
        # Unrelated user.
        client.create_user(UserName="alice-admin")
        yield client


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["cleanup.py", *argv])
    cleanup.main()


def names(iam):
    return {u["UserName"] for u in iam.list_users()["Users"]}


def test_dry_run_changes_nothing(iam, monkeypatch, capsys):
    before = names(iam)
    run(monkeypatch)
    assert names(iam) == before
    assert "DRY-RUN" in capsys.readouterr().out


def test_apply_requires_expected_account(iam, monkeypatch):
    with pytest.raises(SystemExit):
        run(monkeypatch, "--apply")
    with pytest.raises(SystemExit):
        run(monkeypatch, "--apply", "--expected-account", "000000000000")
    assert "poc-bedrock-aaa" in names(iam)


def test_deactivate_only(iam, monkeypatch):
    run(monkeypatch, "--apply", "--deactivate-only", "--expected-account", "123456789012")
    assert [c["Status"] for c in iam.creds["poc-bedrock-aaa"]] == ["Inactive"]
    assert [c["Status"] for c in iam.creds["poc-bedrock-untagged"]] == ["Active"]
    assert "poc-bedrock-aaa" in names(iam)


def test_apply_deletes_only_managed_users(iam, monkeypatch, capsys):
    run(monkeypatch, "--apply", "--expected-account", "123456789012")
    remaining = names(iam)
    assert "poc-bedrock-aaa" not in remaining
    assert {
        "poc-bedrock-untagged",
        "poc-bedrock-elsewhere",
        "poc-bedrock-weird",
        "alice-admin",
    } <= remaining
    assert "SKIP delete: unexpected access keys" in capsys.readouterr().out
    assert iam.creds["poc-bedrock-untagged"][0]["Status"] == "Active"
