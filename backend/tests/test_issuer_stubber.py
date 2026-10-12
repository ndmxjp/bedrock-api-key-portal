"""Validate our IAM request shapes against the real botocore service model."""

from datetime import UTC, datetime

import boto3
from botocore.stub import Stubber

from portal.issuer import BEDROCK_SERVICE_NAME, Issuer

ARN = "arn:aws:iam::123456789012:policy/BedrockPoCUserPolicy"


def test_request_shapes_match_iam_model():
    iam = boto3.client("iam", region_name="us-east-1")
    issuer = Issuer(iam, path="/bedrock-poc/", user_policy_arn=ARN, permissions_boundary_arn=ARN)
    now = datetime(2026, 10, 11, tzinfo=UTC)
    with Stubber(iam) as stub:
        stub.add_client_error("get_user", "NoSuchEntity", expected_params={"UserName": "u"})
        stub.add_response(
            "create_user",
            {
                "User": {
                    "Path": "/bedrock-poc/",
                    "UserName": "u",
                    "UserId": "AIDAEXAMPLE0000000",
                    "Arn": "arn:aws:iam::123456789012:user/bedrock-poc/u",
                    "CreateDate": now,
                }
            },
            {
                "Path": "/bedrock-poc/",
                "UserName": "u",
                "PermissionsBoundary": ARN,
                "Tags": [
                    {"Key": "ManagedBy", "Value": "BedrockPoCPortal"},
                    {"Key": "PoC", "Value": "true"},
                    {"Key": "PortalSubjectHash", "Value": "h"},
                ],
            },
        )
        stub.add_response("attach_user_policy", {}, {"UserName": "u", "PolicyArn": ARN})
        stub.add_response(
            "create_service_specific_credential",
            {
                "ServiceSpecificCredential": {
                    "CreateDate": now,
                    "ExpirationDate": now,
                    "ServiceName": BEDROCK_SERVICE_NAME,
                    "ServiceCredentialAlias": "u-at-123456789012",
                    "ServiceCredentialSecret": "secret-value",
                    "ServiceSpecificCredentialId": "ACCAEXAMPLE000000000",
                    "UserName": "u",
                    "Status": "Active",
                }
            },
            {"UserName": "u", "ServiceName": BEDROCK_SERVICE_NAME, "CredentialAgeDays": 7},
        )
        stub.add_response(
            "list_service_specific_credentials",
            {"ServiceSpecificCredentials": []},
            {"UserName": "u", "ServiceName": BEDROCK_SERVICE_NAME},
        )
        stub.add_response(
            "update_service_specific_credential",
            {},
            {
                "UserName": "u",
                "ServiceSpecificCredentialId": "ACCAEXAMPLE000000000",
                "Status": "Inactive",
            },
        )
        stub.add_response(
            "delete_service_specific_credential",
            {},
            {"UserName": "u", "ServiceSpecificCredentialId": "ACCAEXAMPLE000000000"},
        )

        issuer.ensure_user("u", "h")
        cred = issuer.create_credential("u", 7)
        assert cred.secret == "secret-value"
        assert "secret-value" not in repr(cred)
        assert issuer.list_credentials("u") == []
        assert issuer.deactivate("u", cred.credential_id)
        issuer.delete_credential("u", cred.credential_id)
        stub.add_response(
            "create_service_specific_credential",
            {
                "ServiceSpecificCredential": {
                    "CreateDate": now,
                    "ServiceName": BEDROCK_SERVICE_NAME,
                    "ServiceCredentialSecret": "no-expiry-secret",
                    "ServiceSpecificCredentialId": "ACCAEXAMPLE000000001",
                    "UserName": "u",
                    "Status": "Active",
                }
            },
            {"UserName": "u", "ServiceName": BEDROCK_SERVICE_NAME},  # no CredentialAgeDays
        )
        assert issuer.create_credential("u", None).expires_at is None
        stub.assert_no_pending_responses()
