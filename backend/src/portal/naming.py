"""Deterministic IAM user naming derived from the Cognito subject."""

import hashlib

USER_NAME_PREFIX = "poc-bedrock-"
HASH_LENGTH = 24


def subject_hash(sub: str) -> str:
    if not sub:
        raise ValueError("sub is required")
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()


def iam_user_name(sub: str) -> str:
    """`poc-bedrock-<first 24 hex chars of sha256(sub)>`. Never user-controlled."""
    return USER_NAME_PREFIX + subject_hash(sub)[:HASH_LENGTH]


def mask_credential_id(credential_id: str | None) -> str | None:
    """Credential IDs are not secret, but we still show only a recognizable fragment."""
    if not credential_id:
        return None
    if len(credential_id) <= 8:
        return "•" * len(credential_id)
    return f"{credential_id[:4]}••••••••{credential_id[-4:]}"
