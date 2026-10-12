"""IAM operations: managed IAM user lifecycle and Bedrock service-specific credentials.

Verified against the botocore IAM model (boto3 1.43): CreateServiceSpecificCredential takes
UserName / ServiceName / CredentialAgeDays and returns the secret in
`ServiceSpecificCredential.ServiceCredentialSecret` (only on creation).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

BEDROCK_SERVICE_NAME = "bedrock.amazonaws.com"
MANAGED_BY_KEY = "ManagedBy"
MANAGED_BY_VALUE = "BedrockPoCPortal"
SUBJECT_HASH_KEY = "PortalSubjectHash"


class IamUserConflict(Exception):
    """An IAM user with our name exists but is not one this portal manages. Never touch it."""


@dataclass(frozen=True)
class CredentialInfo:
    credential_id: str
    status: str  # "Active" | "Inactive"
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class IssuedCredential:
    credential_id: str
    secret: str
    created_at: datetime
    expires_at: datetime | None

    def __repr__(self) -> str:  # keep the secret out of any accidental log/trace
        return f"IssuedCredential(credential_id={self.credential_id!r}, secret=<redacted>)"


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


class Issuer:
    def __init__(
        self,
        iam: Any,
        *,
        path: str,
        user_policy_arn: str,
        permissions_boundary_arn: str,
        sleep: Callable[[float], None] = time.sleep,
        create_retries: int = 5,
    ) -> None:
        self._iam = iam
        self._path = path
        self._policy_arn = user_policy_arn
        self._boundary_arn = permissions_boundary_arn
        self._sleep = sleep
        self._create_retries = create_retries

    # ---- users -------------------------------------------------------------------------

    def _assert_managed(self, user: dict[str, Any], subject_hash: str) -> None:
        tags = {t["Key"]: t["Value"] for t in user.get("Tags", [])}
        boundary = (user.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
        if (
            user.get("Path") != self._path
            or tags.get(MANAGED_BY_KEY) != MANAGED_BY_VALUE
            or tags.get(SUBJECT_HASH_KEY) != subject_hash
            or boundary != self._boundary_arn
        ):
            raise IamUserConflict(user.get("UserName", "?"))

    def _get_user(self, name: str) -> dict[str, Any] | None:
        try:
            return self._iam.get_user(UserName=name)["User"]
        except ClientError as e:
            if _code(e) == "NoSuchEntity":
                return None
            raise

    def ensure_user(self, name: str, subject_hash: str) -> None:
        """Create the managed IAM user if missing, or verify an existing one is ours."""
        user = self._get_user(name)
        if user is None:
            try:
                self._iam.create_user(
                    Path=self._path,
                    UserName=name,
                    PermissionsBoundary=self._boundary_arn,
                    Tags=[
                        {"Key": MANAGED_BY_KEY, "Value": MANAGED_BY_VALUE},
                        {"Key": "PoC", "Value": "true"},
                        {"Key": SUBJECT_HASH_KEY, "Value": subject_hash},
                    ],
                )
            except ClientError as e:
                if _code(e) != "EntityAlreadyExists":
                    raise
                user = self._get_user(name)
                if user is None:
                    raise
                self._assert_managed(user, subject_hash)
        else:
            self._assert_managed(user, subject_hash)
        # Idempotent; also re-applies the policy if someone detached it.
        self._iam.attach_user_policy(UserName=name, PolicyArn=self._policy_arn)

    # ---- credentials -------------------------------------------------------------------

    def list_credentials(self, name: str) -> list[CredentialInfo]:
        try:
            resp = self._iam.list_service_specific_credentials(
                UserName=name, ServiceName=BEDROCK_SERVICE_NAME
            )
        except ClientError as e:
            if _code(e) == "NoSuchEntity":
                return []
            raise
        return [
            CredentialInfo(
                credential_id=c["ServiceSpecificCredentialId"],
                status=c["Status"],
                created_at=c.get("CreateDate"),
                expires_at=c.get("ExpirationDate"),
            )
            for c in resp.get("ServiceSpecificCredentials", [])
        ]

    def purge_credentials(self, name: str) -> int:
        """Delete every Bedrock credential on this managed user.

        Only called while the user holds the ISSUING lease and no ACTIVE key is recorded, so
        whatever remains is revoked/expired or an orphan from a crashed attempt whose secret
        was never delivered. This also keeps us under IAM's per-user credential limit.
        """
        removed = 0
        for cred in self.list_credentials(name):
            if cred.status == "Active":
                self.deactivate(name, cred.credential_id)
            self.delete_credential(name, cred.credential_id)
            removed += 1
        return removed

    def create_credential(self, name: str, age_days: int | None) -> IssuedCredential:
        """`age_days=None` creates a key without expiry (CredentialAgeDays omitted)."""
        params: dict[str, Any] = {"UserName": name, "ServiceName": BEDROCK_SERVICE_NAME}
        if age_days is not None:
            params["CredentialAgeDays"] = age_days
        attempt = 0
        while True:
            try:
                resp = self._iam.create_service_specific_credential(**params)
                break
            except ClientError as e:
                # A freshly created user may not be visible yet (IAM eventual consistency).
                attempt += 1
                if _code(e) != "NoSuchEntity" or attempt >= self._create_retries:
                    raise
                self._sleep(min(2.0, 0.5 * attempt))
        c = resp["ServiceSpecificCredential"]
        return IssuedCredential(
            credential_id=c["ServiceSpecificCredentialId"],
            secret=c["ServiceCredentialSecret"],
            created_at=c["CreateDate"],
            expires_at=c.get("ExpirationDate"),
        )

    def deactivate(self, name: str, credential_id: str) -> bool:
        """Set Inactive. Returns False if the credential no longer exists (already gone)."""
        try:
            self._iam.update_service_specific_credential(
                UserName=name, ServiceSpecificCredentialId=credential_id, Status="Inactive"
            )
            return True
        except ClientError as e:
            if _code(e) == "NoSuchEntity":
                return False
            raise

    def reactivate(self, name: str, credential_id: str) -> None:
        """Set a (suspended) credential back to Active."""
        self._iam.update_service_specific_credential(
            UserName=name, ServiceSpecificCredentialId=credential_id, Status="Active"
        )

    def delete_credential(self, name: str, credential_id: str) -> None:
        try:
            self._iam.delete_service_specific_credential(
                UserName=name, ServiceSpecificCredentialId=credential_id
            )
        except ClientError as e:
            if _code(e) != "NoSuchEntity":
                raise
