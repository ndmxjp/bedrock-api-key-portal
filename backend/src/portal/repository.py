"""DynamoDB access: per-user registration state, the global slot counter and the kill switch.

DynamoDB is an index/cache of IAM state. IAM remains the source of truth for key status.

Items (single table, partition key `pk`):
  USER#<sub>      registration state for one Cognito user
  META#COUNTER    reservedSlots: number of users that have consumed a registration slot
  CONFIG#GLOBAL   issuanceEnabled: kill switch (absent => enabled)
"""

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

COUNTER_PK = "META#COUNTER"
CONFIG_PK = "CONFIG#GLOBAL"


class Status(StrEnum):
    NOT_ISSUED = "NOT_ISSUED"  # virtual: no item
    ISSUING = "ISSUING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"  # key set Inactive by an admin; reversible


@dataclass(frozen=True)
class PortalConfig:
    issuance_enabled: bool
    registration_cap: int
    credential_age_days: int  # 0 = no expiry


class BeginRejected(Exception):
    """begin_issuance's transaction was cancelled. `reason` is USER_STATE or CAP."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def user_pk(sub: str) -> str:
    return f"USER#{sub}"


@dataclass
class Registration:
    sub: str
    status: Status
    email: str | None = None
    iam_user_name: str | None = None
    credential_id: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None
    updated_at: str | None = None
    operation_id: str | None = None
    lease_until: int | None = None
    last_attempt_at: int | None = None
    error_code: str | None = None
    slot_reserved: bool = False
    suspended: bool = False  # admin hold: blocks issuance until resumed

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Registration:
        def s(name: str) -> str | None:
            v = item.get(name)
            return v["S"] if v and "S" in v else None

        def n(name: str) -> int | None:
            v = item.get(name)
            return int(v["N"]) if v and "N" in v else None

        return cls(
            sub=item["pk"]["S"].removeprefix("USER#"),
            status=Status(item["status"]["S"]),
            email=s("email"),
            iam_user_name=s("iamUserName"),
            credential_id=s("credentialId"),
            issued_at=s("issuedAt"),
            expires_at=s("expiresAt"),
            updated_at=s("updatedAt"),
            operation_id=s("operationId"),
            lease_until=n("leaseUntil"),
            last_attempt_at=n("lastAttemptAt"),
            error_code=s("errorCode"),
            slot_reserved=bool(item.get("slotReserved", {}).get("BOOL", False)),
            suspended=bool(item.get("suspended", {}).get("BOOL", False)),
        )


class Repository:
    def __init__(self, client: Any, table_name: str) -> None:
        self._ddb = client
        self._table = table_name

    # ---- reads -------------------------------------------------------------------------

    def get_registration(self, sub: str) -> Registration | None:
        resp = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": user_pk(sub)}}, ConsistentRead=True
        )
        item = resp.get("Item")
        return Registration.from_item(item) if item else None

    def get_config(self, default_cap: int, default_age_days: int = 7) -> PortalConfig:
        resp = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": CONFIG_PK}}, ConsistentRead=True
        )
        item = resp.get("Item") or {}
        enabled = item.get("issuanceEnabled", {}).get("BOOL", True)
        cap = int(item["registrationCap"]["N"]) if "registrationCap" in item else default_cap
        age = (
            int(item["credentialAgeDays"]["N"]) if "credentialAgeDays" in item else default_age_days
        )
        return PortalConfig(
            issuance_enabled=bool(enabled), registration_cap=cap, credential_age_days=age
        )

    def issuance_enabled(self) -> bool:
        return self.get_config(default_cap=0).issuance_enabled

    def reserved_slots(self) -> int:
        resp = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": COUNTER_PK}}, ConsistentRead=True
        )
        item = resp.get("Item") or {}
        return int(item.get("reservedSlots", {}).get("N", "0"))

    # ---- admin -------------------------------------------------------------------------

    def set_issuance_enabled(self, enabled: bool, now_iso: str) -> None:
        # update_item (not put_item) so other config attributes are preserved.
        self._ddb.update_item(
            TableName=self._table,
            Key={"pk": {"S": CONFIG_PK}},
            UpdateExpression="SET issuanceEnabled = :v, updatedAt = :now",
            ExpressionAttributeValues={":v": {"BOOL": enabled}, ":now": {"S": now_iso}},
        )

    def set_credential_age_days(self, days: int, now_iso: str) -> None:
        self._ddb.update_item(
            TableName=self._table,
            Key={"pk": {"S": CONFIG_PK}},
            UpdateExpression="SET credentialAgeDays = :v, updatedAt = :now",
            ExpressionAttributeValues={":v": {"N": str(days)}, ":now": {"S": now_iso}},
        )

    def set_registration_cap(self, cap: int, now_iso: str) -> None:
        self._ddb.update_item(
            TableName=self._table,
            Key={"pk": {"S": CONFIG_PK}},
            UpdateExpression="SET registrationCap = :v, updatedAt = :now",
            ExpressionAttributeValues={":v": {"N": str(cap)}, ":now": {"S": now_iso}},
        )

    # ---- state transitions -------------------------------------------------------------

    def begin_issuance(
        self,
        *,
        sub: str,
        email: str,
        iam_user_name: str,
        operation_id: str,
        now_epoch: int,
        now_iso: str,
        cap: int,
        cooldown_seconds: int,
        lease_seconds: int,
        needs_slot: bool,
    ) -> None:
        """Atomically move the user to ISSUING and, for first-time users, reserve a slot.

        The user-item condition allows the transition only from "no item", a terminal
        non-active state, or an ISSUING whose lease has expired (crashed attempt). It also
        enforces a per-user cooldown and asserts the slot flag we read is still current,
        so a stale read can never double-count a user against the cap.
        """
        values: dict[str, Any] = {
            ":issuing": {"S": Status.ISSUING},
            ":revoked": {"S": Status.REVOKED},
            ":expired": {"S": Status.EXPIRED},
            ":failed": {"S": Status.FAILED},
            ":now": {"N": str(now_epoch)},
            ":cooldownEdge": {"N": str(now_epoch - cooldown_seconds)},
            ":op": {"S": operation_id},
            ":lease": {"N": str(now_epoch + lease_seconds)},
            ":email": {"S": email},
            ":iamUser": {"S": iam_user_name},
            ":nowIso": {"S": now_iso},
            ":true": {"BOOL": True},
        }
        slot_condition = (
            "(attribute_not_exists(slotReserved) OR slotReserved <> :true)"
            if needs_slot
            else "slotReserved = :true"
        )
        condition = (
            "(attribute_not_exists(pk) OR #s IN (:revoked, :expired, :failed)"
            " OR (#s = :issuing AND leaseUntil < :now))"
            " AND (attribute_not_exists(lastAttemptAt) OR lastAttemptAt <= :cooldownEdge)"
            f" AND {slot_condition}"
            " AND (attribute_not_exists(suspended) OR suspended <> :true)"
        )
        user_update = {
            "Update": {
                "TableName": self._table,
                "Key": {"pk": {"S": user_pk(sub)}},
                "UpdateExpression": (
                    "SET #s = :issuing, operationId = :op, leaseUntil = :lease,"
                    " lastAttemptAt = :now, email = :email, iamUserName = :iamUser,"
                    " updatedAt = :nowIso, slotReserved = :true REMOVE errorCode"
                ),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": values,
            }
        }
        items: list[dict[str, Any]] = [user_update]
        if needs_slot:
            items.append(
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": {"pk": {"S": COUNTER_PK}},
                        "UpdateExpression": "ADD reservedSlots :one",
                        "ConditionExpression": (
                            "attribute_not_exists(reservedSlots) OR reservedSlots < :cap"
                        ),
                        "ExpressionAttributeValues": {
                            ":one": {"N": "1"},
                            ":cap": {"N": str(cap)},
                        },
                    }
                }
            )
        try:
            self._ddb.transact_write_items(TransactItems=items)
        except self._ddb.exceptions.TransactionCanceledException as e:
            reasons = e.response.get("CancellationReasons") or []
            codes = [r.get("Code") for r in reasons]
            if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                raise BeginRejected("CAP") from None
            raise BeginRejected("USER_STATE") from None

    def complete_issuance(
        self,
        *,
        sub: str,
        operation_id: str,
        credential_id: str,
        issued_at: str,
        expires_at: str | None,
        now_iso: str,
    ) -> None:
        names = {"#s": "status"}
        values: dict[str, Any] = {
            ":active": {"S": Status.ACTIVE},
            ":issuing": {"S": Status.ISSUING},
            ":op": {"S": operation_id},
            ":cid": {"S": credential_id},
            ":issuedAt": {"S": issued_at},
            ":nowIso": {"S": now_iso},
        }
        update = "SET #s = :active, credentialId = :cid, issuedAt = :issuedAt, updatedAt = :nowIso"
        if expires_at:
            update += ", expiresAt = :expiresAt"
            values[":expiresAt"] = {"S": expires_at}
            update += " REMOVE leaseUntil, errorCode"
        else:
            update += " REMOVE leaseUntil, errorCode, expiresAt"
        self._ddb.update_item(
            TableName=self._table,
            Key={"pk": {"S": user_pk(sub)}},
            UpdateExpression=update,
            ConditionExpression="#s = :issuing AND operationId = :op",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def fail_issuance(self, *, sub: str, operation_id: str, error_code: str, now_iso: str) -> None:
        """Record FAILED for our own attempt only. Credential fields are cleared because any
        credential created during this attempt has been (or will be) deleted."""
        with contextlib.suppress(self._ddb.exceptions.ConditionalCheckFailedException):
            self._ddb.update_item(
                TableName=self._table,
                Key={"pk": {"S": user_pk(sub)}},
                UpdateExpression=(
                    "SET #s = :failed, errorCode = :code, updatedAt = :nowIso"
                    " REMOVE leaseUntil, credentialId, issuedAt, expiresAt"
                ),
                ConditionExpression="#s = :issuing AND operationId = :op",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": Status.FAILED},
                    ":issuing": {"S": Status.ISSUING},
                    ":op": {"S": operation_id},
                    ":code": {"S": error_code},
                    ":nowIso": {"S": now_iso},
                },
            )

    def transition(
        self, *, sub: str, credential_id: str, from_status: Status, to_status: Status, now_iso: str
    ) -> bool:
        """Conditional status change for a specific credential. Returns False if the record
        no longer matches (another request already moved it)."""
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key={"pk": {"S": user_pk(sub)}},
                UpdateExpression="SET #s = :to, updatedAt = :nowIso",
                ConditionExpression="#s = :from AND credentialId = :cid",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":to": {"S": to_status},
                    ":from": {"S": from_status},
                    ":cid": {"S": credential_id},
                    ":nowIso": {"S": now_iso},
                },
            )
            return True
        except self._ddb.exceptions.ConditionalCheckFailedException:
            return False

    # ---- admin queries -----------------------------------------------------------------

    def list_registrations(self) -> list[Registration]:
        """All user records. A Scan is fine at PoC scale (tens to low hundreds of users)."""
        out: list[Registration] = []
        paginator = self._ddb.get_paginator("scan")
        for page in paginator.paginate(
            TableName=self._table,
            ConsistentRead=True,
            FilterExpression="begins_with(pk, :p)",
            ExpressionAttributeValues={":p": {"S": "USER#"}},
        ):
            out.extend(Registration.from_item(i) for i in page.get("Items", []))
        return out

    def release_slot(self, *, sub: str, now_iso: str) -> bool:
        """Give a user's registration slot back. Refused while the user has a live key or an
        issuance in progress. Returns False if the condition did not hold."""
        try:
            self._ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": {"pk": {"S": user_pk(sub)}},
                            "UpdateExpression": "SET slotReserved = :false, updatedAt = :now",
                            "ConditionExpression": (
                                "slotReserved = :true"
                                " AND NOT (#s IN (:active, :issuing, :suspended))"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":true": {"BOOL": True},
                                ":false": {"BOOL": False},
                                ":active": {"S": Status.ACTIVE},
                                ":issuing": {"S": Status.ISSUING},
                                ":suspended": {"S": Status.SUSPENDED},
                                ":now": {"S": now_iso},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": {"pk": {"S": COUNTER_PK}},
                            "UpdateExpression": "ADD reservedSlots :minus",
                            "ConditionExpression": "reservedSlots > :zero",
                            "ExpressionAttributeValues": {
                                ":minus": {"N": "-1"},
                                ":zero": {"N": "0"},
                            },
                        }
                    },
                ]
            )
            return True
        except self._ddb.exceptions.TransactionCanceledException:
            return False

    # ---- suspension --------------------------------------------------------------------

    def suspend(self, *, sub: str, credential_id: str | None, now_iso: str) -> bool:
        """Put the user on hold. With a credential, the ACTIVE key becomes SUSPENDED in the same
        write; without one (no live key) only the hold flag is set."""
        values: dict[str, Any] = {":true": {"BOOL": True}, ":now": {"S": now_iso}}
        if credential_id:
            update = "SET suspended = :true, #s = :suspended, updatedAt = :now"
            condition = "#s = :active AND credentialId = :cid"
            values |= {
                ":suspended": {"S": Status.SUSPENDED},
                ":active": {"S": Status.ACTIVE},
                ":cid": {"S": credential_id},
            }
        else:
            update = "SET suspended = :true, updatedAt = :now"
            condition = "attribute_exists(pk) AND NOT (#s IN (:active, :issuing))"
            values |= {":active": {"S": Status.ACTIVE}, ":issuing": {"S": Status.ISSUING}}
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key={"pk": {"S": user_pk(sub)}},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
            return True
        except self._ddb.exceptions.ConditionalCheckFailedException:
            return False

    def resume(self, *, sub: str, to_status: Status | None, now_iso: str) -> bool:
        """Lift the hold. If the record is SUSPENDED, move it to `to_status` (ACTIVE when the
        key was reactivated, else REVOKED/EXPIRED)."""
        values: dict[str, Any] = {
            ":true": {"BOOL": True},
            ":false": {"BOOL": False},
            ":now": {"S": now_iso},
        }
        names: dict[str, str] = {}
        update = "SET suspended = :false, updatedAt = :now"
        condition = "suspended = :true"
        if to_status is not None:
            update += ", #s = :to"
            condition += " AND #s = :suspended"
            names["#s"] = "status"
            values |= {":to": {"S": to_status}, ":suspended": {"S": Status.SUSPENDED}}
        try:
            kwargs: dict[str, Any] = {
                "TableName": self._table,
                "Key": {"pk": {"S": user_pk(sub)}},
                "UpdateExpression": update,
                "ConditionExpression": condition,
                "ExpressionAttributeValues": values,
            }
            if names:
                kwargs["ExpressionAttributeNames"] = names
            self._ddb.update_item(**kwargs)
            return True
        except self._ddb.exceptions.ConditionalCheckFailedException:
            return False
