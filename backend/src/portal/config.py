"""Runtime settings, loaded from Lambda environment variables set by CDK."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from portal.domain import parse_allowed_domains

# IAM accepts CredentialAgeDays 1..36600. 0 means "no expiry" (parameter omitted).
IAM_MAX_CREDENTIAL_AGE_DAYS = 36600
UNLIMITED = 0


def age_within(days: int, max_days: int) -> bool:
    """True if `days` (0 = unlimited) is allowed under `max_days` (0 = unlimited allowed)."""
    if days == UNLIMITED:
        return max_days == UNLIMITED
    return 1 <= days <= (IAM_MAX_CREDENTIAL_AGE_DAYS if max_days == UNLIMITED else max_days)


@dataclass(frozen=True)
class Settings:
    table_name: str
    allowed_domains: frozenset[str]
    credential_age_days: int  # default for new keys; 0 = no expiry
    registration_cap: int
    bedrock_region: str
    iam_path: str
    user_policy_arn: str
    permissions_boundary_arn: str
    user_pool_client_id: str
    token_issuer: str
    allowed_models: tuple[str, ...] = ()
    issue_cooldown_seconds: int = 10
    issuing_lease_seconds: int = 90
    admin_group: str = "PortalAdmins"
    max_registration_cap: int = 1000
    allow_all_models: bool = False
    allowed_providers: tuple[str, ...] = ()  # with allow_all_models; empty = every provider
    max_credential_age_days: int = 30  # ceiling admins may choose; 0 = unlimited allowed
    restrict_region: bool = True  # False: keys may call Bedrock in any region

    def __post_init__(self) -> None:
        if not 0 <= self.max_credential_age_days <= IAM_MAX_CREDENTIAL_AGE_DAYS:
            raise ValueError("max_credential_age_days out of range")
        if not age_within(self.credential_age_days, self.max_credential_age_days):
            raise ValueError("credential_age_days exceeds max_credential_age_days")
        if self.registration_cap < 1:
            raise ValueError("registration_cap must be >= 1")
        if not (self.iam_path.startswith("/") and self.iam_path.endswith("/")):
            raise ValueError("iam_path must start and end with '/'")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        return cls(
            table_name=env["TABLE_NAME"],
            allowed_domains=parse_allowed_domains(env["ALLOWED_DOMAINS"]),
            credential_age_days=int(env.get("CREDENTIAL_AGE_DAYS", "7")),
            registration_cap=int(env.get("REGISTRATION_CAP", "10")),
            bedrock_region=env.get("BEDROCK_REGION", "us-west-2"),
            iam_path=env.get("IAM_PATH", "/bedrock-poc/"),
            user_policy_arn=env["USER_POLICY_ARN"],
            permissions_boundary_arn=env["PERMISSIONS_BOUNDARY_ARN"],
            user_pool_client_id=env["USER_POOL_CLIENT_ID"],
            token_issuer=env["TOKEN_ISSUER"],
            allowed_models=tuple(json.loads(env.get("ALLOWED_MODELS", "[]"))),
            issue_cooldown_seconds=int(env.get("ISSUE_COOLDOWN_SECONDS", "10")),
            issuing_lease_seconds=int(env.get("ISSUING_LEASE_SECONDS", "90")),
            admin_group=env.get("ADMIN_GROUP", "PortalAdmins"),
            allow_all_models=env.get("MODEL_MODE", "list") == "all",
            allowed_providers=tuple(json.loads(env.get("ALLOWED_PROVIDERS", "[]"))),
            max_credential_age_days=int(env.get("MAX_CREDENTIAL_AGE_DAYS", "30")),
            restrict_region=env.get("RESTRICT_REGION", "1") != "0",
        )
