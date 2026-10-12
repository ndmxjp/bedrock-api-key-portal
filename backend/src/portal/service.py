"""Business logic for /api/me, /api/keys and /api/keys/revoke."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from portal.claims import Identity
from portal.config import UNLIMITED, Settings, age_within
from portal.costs import CostReporter, CostSnapshot
from portal.domain import is_allowed_email, normalize_email
from portal.errors import ApiError
from portal.issuer import IamUserConflict, Issuer
from portal.log import log, metric
from portal.models import ModelCatalog
from portal.naming import iam_user_name, mask_credential_id, subject_hash
from portal.repository import BeginRejected, Registration, Repository, Status


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _client_error_code(e: Exception) -> str:
    if isinstance(e, ClientError):
        return str(e.response.get("Error", {}).get("Code", "ClientError"))
    return type(e).__name__


class PortalService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        issuer: Issuer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_operation_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        costs: CostReporter | None = None,
        models: ModelCatalog | None = None,
    ) -> None:
        self._costs = costs
        self._models = models
        self._s = settings
        self._repo = repo
        self._issuer = issuer
        self._clock = clock
        self._new_op = new_operation_id

    # ---- helpers -----------------------------------------------------------------------

    def _config(self):
        return self._repo.get_config(self._s.registration_cap, self._s.credential_age_days)

    def _age_days(self) -> int:
        """Effective validity for new keys (0 = no expiry), clamped to the deploy-time ceiling
        in case the ceiling was lowered after an admin chose a longer value."""
        days = self._config().credential_age_days
        if not age_within(days, self._s.max_credential_age_days):
            return self._s.credential_age_days
        return days

    @staticmethod
    def _age_view(days: int) -> int | None:
        return None if days == UNLIMITED else days

    def _allowed_models(self) -> list[str]:
        if self._s.allow_all_models and self._models is not None:
            return self._models.profile_ids()
        return list(self._s.allowed_models)

    def _now(self) -> tuple[datetime, int, str]:
        now = self._clock()
        return now, int(now.timestamp()), iso(now) or ""

    def _allowed(self, identity: Identity) -> bool:
        return identity.email_verified and is_allowed_email(identity.email, self._s.allowed_domains)

    def _require_verified(self, identity: Identity) -> None:
        if not identity.email_verified:
            raise ApiError(403, "EMAIL_NOT_VERIFIED")

    def _require_allowed(self, identity: Identity) -> None:
        self._require_verified(identity)
        if not is_allowed_email(identity.email, self._s.allowed_domains):
            raise ApiError(403, "DOMAIN_NOT_ALLOWED")

    def _refresh(self, rec: Registration | None) -> Registration | None:
        """Reconcile an ACTIVE or SUSPENDED record with IAM (source of truth): expired /
        deactivated / deleted-by-admin keys are reflected back into DynamoDB. A SUSPENDED key is
        expected to be Inactive in IAM."""
        if rec is None or rec.status not in (Status.ACTIVE, Status.SUSPENDED):
            return rec
        if not rec.credential_id:
            return rec
        now, _, now_iso = self._now()
        new_status: Status | None = None

        local_exp = parse_iso(rec.expires_at)
        if local_exp and local_exp <= now:
            new_status = Status.EXPIRED
        else:
            try:
                creds = self._issuer.list_credentials(rec.iam_user_name or iam_user_name(rec.sub))
            except ClientError as e:
                log("WARN", "iam_refresh_failed", code=_client_error_code(e))
                return rec
            match = next((c for c in creds if c.credential_id == rec.credential_id), None)
            deactivated = match is not None and match.status != "Active"
            if match is None or (deactivated and rec.status == Status.ACTIVE):
                new_status = Status.REVOKED
            elif match.expires_at and match.expires_at <= now:
                new_status = Status.EXPIRED

        if new_status:
            self._repo.transition(
                sub=rec.sub,
                credential_id=rec.credential_id,
                from_status=rec.status,
                to_status=new_status,
                now_iso=now_iso,
            )
            rec.status = new_status
        return rec

    def _effective_status(self, rec: Registration | None) -> Status:
        if rec is None:
            return Status.NOT_ISSUED
        if rec.status == Status.ISSUING:
            _, now_epoch, _ = self._now()
            if rec.lease_until is not None and rec.lease_until < now_epoch:
                return Status.FAILED  # crashed attempt; next POST /api/keys recovers it
        return rec.status

    def _key_view(self, rec: Registration | None, status: Status) -> dict[str, Any] | None:
        if rec is None or not rec.credential_id:
            return None
        if status not in (Status.ACTIVE, Status.SUSPENDED, Status.REVOKED, Status.EXPIRED):
            return None
        return {
            "credentialId": mask_credential_id(rec.credential_id),
            "status": status.value,
            "issuedAt": rec.issued_at,
            "expiresAt": rec.expires_at,
        }

    def _view(self, identity: Identity, rec: Registration | None) -> dict[str, Any]:
        status = self._effective_status(rec)
        return {
            "email": identity.email,
            "emailVerified": identity.email_verified,
            "allowed": self._allowed(identity),
            "status": status.value,
            "key": self._key_view(rec, status),
            "region": self._s.bedrock_region,
            "regionRestricted": self._s.restrict_region,
            "allowedModels": self._allowed_models(),
            "allModels": self._s.allow_all_models,
            "modelProviders": list(self._s.allowed_providers),
            "credentialAgeDays": self._age_view(self._age_days()),
            "suspended": bool(rec and rec.suspended),
        }

    # ---- GET /api/me -------------------------------------------------------------------

    def me(self, identity: Identity) -> dict[str, Any]:
        rec = self._refresh(self._repo.get_registration(identity.sub))
        view = self._view(identity, rec)
        view["issuanceEnabled"] = self._config().issuance_enabled
        view["isAdmin"] = self.is_admin(identity)
        view["usage"] = self._usage_for(identity.sub)
        return view

    # ---- costs -------------------------------------------------------------------------

    @staticmethod
    def _usage_view(snap: CostSnapshot, amount: str) -> dict[str, Any]:
        return {
            "month": snap.month,
            "amount": amount if snap.available else None,
            "currency": snap.currency,
            "fetchedAt": snap.fetched_at or None,
            "available": snap.available,
        }

    def _usage_for(self, sub: str) -> dict[str, Any] | None:
        if self._costs is None:
            return None
        snap = self._costs.snapshot()
        return self._usage_view(snap, snap.amount_for(subject_hash(sub)))

    def admin_costs(self, identity: Identity, *, refresh: bool = False) -> dict[str, Any]:
        actor = self._require_admin(identity)
        if self._costs is None:
            raise ApiError(404, "NOT_FOUND")
        snap = self._costs.snapshot(force=refresh)
        if refresh:
            log("INFO", "admin_cost_refresh", actor=actor)
        known = {subject_hash(r.sub): r for r in self._repo.list_registrations()}
        users = [
            {"sub": rec.sub, "email": rec.email, "amount": snap.amount_for(h)}
            for h, rec in known.items()
        ]
        users.sort(key=lambda u: float(u["amount"]), reverse=True)
        # Tagged spend from IAM users no longer in the table (e.g. records deleted).
        unmatched = sum(float(v) for h, v in snap.by_subject.items() if h not in known)
        return {
            **self._usage_view(snap, snap.total),
            "users": users if snap.available else [],
            "unmatchedAmount": f"{unmatched:.4f}",
        }

    # ---- POST /api/keys ----------------------------------------------------------------

    def issue(self, identity: Identity) -> dict[str, Any]:
        self._require_allowed(identity)
        cfg = self._config()
        if not cfg.issuance_enabled:
            raise ApiError(503, "ISSUANCE_DISABLED")

        rec = self._refresh(self._repo.get_registration(identity.sub))
        if rec is not None and rec.suspended:
            raise ApiError(403, "USER_SUSPENDED")
        status = self._effective_status(rec)
        if status == Status.ACTIVE:
            raise ApiError(409, "ALREADY_ISSUED")
        if status == Status.ISSUING:
            raise ApiError(409, "ISSUING")

        name = iam_user_name(identity.sub)
        sub_hash = subject_hash(identity.sub)
        op = self._new_op()
        _, now_epoch, now_iso = self._now()

        try:
            self._repo.begin_issuance(
                sub=identity.sub,
                email=normalize_email(identity.email),
                iam_user_name=name,
                operation_id=op,
                now_epoch=now_epoch,
                now_iso=now_iso,
                cap=cfg.registration_cap,
                cooldown_seconds=self._s.issue_cooldown_seconds,
                lease_seconds=self._s.issuing_lease_seconds,
                needs_slot=not (rec and rec.slot_reserved),
            )
        except BeginRejected as rejected:
            raise self._classify_rejection(identity.sub, rejected.reason, now_epoch) from None

        # From here we hold the ISSUING lease for this user.
        try:
            self._issuer.ensure_user(name, sub_hash)
            removed = self._issuer.purge_credentials(name)
            if removed:
                log("INFO", "purged_old_credentials", iamUser=name, count=removed)
            age = self._age_days()
            cred = self._issuer.create_credential(name, None if age == UNLIMITED else age)
        except IamUserConflict:
            self._repo.fail_issuance(
                sub=identity.sub, operation_id=op, error_code="IAM_USER_CONFLICT", now_iso=now_iso
            )
            log("ERROR", "iam_user_conflict", iamUser=name)
            metric("IssueFailed")
            raise ApiError(500, "INTERNAL_ERROR") from None
        except ClientError as e:
            code = _client_error_code(e)
            self._repo.fail_issuance(
                sub=identity.sub, operation_id=op, error_code=code[:64], now_iso=now_iso
            )
            log("ERROR", "iam_issue_failed", iamUser=name, code=code)
            metric("IssueFailed")
            if code in ("AccessDenied", "AccessDeniedException"):
                metric("IamAccessDenied")
            raise ApiError(502, "AWS_API_ERROR") from None

        try:
            self._repo.complete_issuance(
                sub=identity.sub,
                operation_id=op,
                credential_id=cred.credential_id,
                issued_at=iso(cred.created_at) or now_iso,
                expires_at=iso(cred.expires_at),
                now_iso=now_iso,
            )
        except Exception as e:
            # The secret was never returned to anyone: roll the credential back.
            log("ERROR", "record_issue_failed", iamUser=name, code=_client_error_code(e))
            try:
                self._issuer.delete_credential(name, cred.credential_id)
            except Exception as cleanup_error:
                log(
                    "ERROR",
                    "rollback_failed",
                    iamUser=name,
                    credentialId=cred.credential_id,
                    code=_client_error_code(cleanup_error),
                )
            self._repo.fail_issuance(
                sub=identity.sub, operation_id=op, error_code="RECORD_FAILED", now_iso=now_iso
            )
            metric("IssueFailed")
            raise ApiError(500, "INTERNAL_ERROR") from None

        log("INFO", "key_issued", iamUser=name, credentialId=cred.credential_id)
        metric("KeyIssued")
        return {
            "status": Status.ACTIVE.value,
            "apiKey": cred.secret,
            "expiresAt": iso(cred.expires_at),
            "issuedAt": iso(cred.created_at),
            "region": self._s.bedrock_region,
            "regionRestricted": self._s.restrict_region,
            "credentialId": mask_credential_id(cred.credential_id),
            "allowedModels": self._allowed_models(),
        }

    def _classify_rejection(self, sub: str, reason: str, now_epoch: int) -> ApiError:
        if reason == "CAP":
            return ApiError(403, "REGISTRATION_CAP_REACHED")
        current = self._repo.get_registration(sub)
        if current is not None:
            if current.suspended:
                return ApiError(403, "USER_SUSPENDED")
            if current.status == Status.ACTIVE:
                return ApiError(409, "ALREADY_ISSUED")
            if current.status == Status.ISSUING and (current.lease_until or 0) >= now_epoch:
                return ApiError(409, "ISSUING")
            edge = now_epoch - self._s.issue_cooldown_seconds
            if current.last_attempt_at is not None and current.last_attempt_at > edge:
                return ApiError(429, "RATE_LIMITED")
        # The slot flag changed between our read and the write; a retry will succeed.
        return ApiError(409, "ISSUING")

    # ---- POST /api/keys/revoke ---------------------------------------------------------

    def _revoke_record(self, rec: Registration | None, *, actor: str) -> Registration | None:
        """Deactivate the record's ACTIVE key in IAM and mark it REVOKED. Idempotent."""
        rec = self._refresh(rec)
        status = self._effective_status(rec)
        if status == Status.ISSUING:
            raise ApiError(409, "ISSUING")
        if rec is None or not rec.credential_id or status in (Status.FAILED, Status.NOT_ISSUED):
            raise ApiError(404, "KEY_NOT_FOUND")
        if status in (Status.REVOKED, Status.EXPIRED):
            return rec

        name = rec.iam_user_name or iam_user_name(rec.sub)
        try:
            self._issuer.deactivate(name, rec.credential_id)  # idempotent for SUSPENDED keys
        except ClientError as e:
            log("ERROR", "iam_revoke_failed", iamUser=name, code=_client_error_code(e))
            raise ApiError(502, "AWS_API_ERROR") from None
        _, _, now_iso = self._now()
        self._repo.transition(
            sub=rec.sub,
            credential_id=rec.credential_id,
            from_status=status,
            to_status=Status.REVOKED,
            now_iso=now_iso,
        )
        log("INFO", "key_revoked", iamUser=name, credentialId=rec.credential_id, actor=actor)
        metric("KeyRevoked")
        return self._repo.get_registration(rec.sub)

    def revoke(self, identity: Identity) -> dict[str, Any]:
        # Domain is not re-checked: a user must always be able to kill their own key.
        self._require_verified(identity)
        rec = self._revoke_record(self._repo.get_registration(identity.sub), actor="self")
        return self._view(identity, rec)

    # ---- admin -------------------------------------------------------------------------

    def is_admin(self, identity: Identity) -> bool:
        return self._allowed(identity) and self._s.admin_group in identity.groups

    def _require_admin(self, identity: Identity) -> str:
        if not self.is_admin(identity):
            log("WARN", "admin_denied", actor=subject_hash(identity.sub)[:12])
            raise ApiError(403, "FORBIDDEN")
        return subject_hash(identity.sub)[:12]

    def _admin_row(self, rec: Registration) -> dict[str, Any]:
        status = self._effective_status(rec)
        exp = parse_iso(rec.expires_at)
        if status == Status.ACTIVE and exp is not None and exp <= self._now()[0]:
            status = Status.EXPIRED  # display only; reconciled with IAM on the user's next visit
        return {
            "sub": rec.sub,
            "email": rec.email,
            "status": status.value,
            "credentialId": mask_credential_id(rec.credential_id),
            "issuedAt": rec.issued_at,
            "expiresAt": rec.expires_at,
            "updatedAt": rec.updated_at,
            "slotReserved": rec.slot_reserved,
            "suspended": rec.suspended,
            "errorCode": rec.error_code,
        }

    def admin_overview(self, identity: Identity) -> dict[str, Any]:
        self._require_admin(identity)
        rows = [self._admin_row(r) for r in self._repo.list_registrations()]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        cfg = self._config()
        return {
            "issuanceEnabled": cfg.issuance_enabled,
            "reservedSlots": self._repo.reserved_slots(),
            "registrationCap": cfg.registration_cap,
            "maxRegistrationCap": self._s.max_registration_cap,
            "credentialAgeDays": self._age_view(self._age_days()),
            "maxCredentialAgeDays": self._age_view(self._s.max_credential_age_days),
            "region": self._s.bedrock_region,
            "regionRestricted": self._s.restrict_region,
            "allowedModels": self._allowed_models(),
            "allowedDomains": sorted(self._s.allowed_domains),
            "statusCounts": counts,
        }

    def admin_list_users(self, identity: Identity) -> dict[str, Any]:
        self._require_admin(identity)
        rows = [self._admin_row(r) for r in self._repo.list_registrations()]
        rows.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
        return {"users": rows}

    def admin_revoke(self, identity: Identity, target_sub: str) -> dict[str, Any]:
        actor = self._require_admin(identity)
        rec = self._repo.get_registration(target_sub)
        if rec is None:
            raise ApiError(404, "USER_NOT_FOUND")
        updated = self._revoke_record(rec, actor=f"admin:{actor}")
        log("INFO", "admin_revoke", actor=actor, target=subject_hash(target_sub)[:12])
        return {"user": self._admin_row(updated or rec)}

    def admin_release_slot(self, identity: Identity, target_sub: str) -> dict[str, Any]:
        actor = self._require_admin(identity)
        rec = self._refresh(self._repo.get_registration(target_sub))
        if rec is None:
            raise ApiError(404, "USER_NOT_FOUND")
        if not rec.slot_reserved:
            raise ApiError(409, "NO_SLOT")
        if self._effective_status(rec) in (Status.ACTIVE, Status.ISSUING, Status.SUSPENDED):
            raise ApiError(409, "SLOT_IN_USE")
        _, _, now_iso = self._now()
        if not self._repo.release_slot(sub=target_sub, now_iso=now_iso):
            raise ApiError(409, "SLOT_IN_USE")
        log("INFO", "admin_release_slot", actor=actor, target=subject_hash(target_sub)[:12])
        return {"user": self._admin_row(self._repo.get_registration(target_sub) or rec)}

    def admin_set_registration_cap(self, identity: Identity, cap: int) -> dict[str, Any]:
        actor = self._require_admin(identity)
        if not 1 <= cap <= self._s.max_registration_cap:
            raise ApiError(400, "INVALID_CAP")
        _, _, now_iso = self._now()
        self._repo.set_registration_cap(cap, now_iso)
        log("INFO", "admin_set_registration_cap", actor=actor, cap=cap)
        return {"registrationCap": cap, "reservedSlots": self._repo.reserved_slots()}

    def admin_set_credential_age(self, identity: Identity, days: int | None) -> dict[str, Any]:
        """Validity for keys issued from now on. None = no expiry (only if the deploy-time
        ceiling allows it). Existing keys keep their expiry."""
        actor = self._require_admin(identity)
        value = UNLIMITED if days is None else days
        if days is not None and days < 1:
            raise ApiError(400, "INVALID_AGE")
        if not age_within(value, self._s.max_credential_age_days):
            raise ApiError(400, "INVALID_AGE")
        _, _, now_iso = self._now()
        self._repo.set_credential_age_days(value, now_iso)
        log("INFO", "admin_set_credential_age", actor=actor, days=value)
        return {"credentialAgeDays": days}

    def admin_suspend(self, identity: Identity, target_sub: str) -> dict[str, Any]:
        """Hold the user: their ACTIVE key is set Inactive (reversible) and they cannot issue
        a new key until resumed. Works on users without a live key too (hold only)."""
        actor = self._require_admin(identity)
        rec = self._refresh(self._repo.get_registration(target_sub))
        if rec is None:
            raise ApiError(404, "USER_NOT_FOUND")
        if rec.suspended:
            return {"user": self._admin_row(rec)}  # idempotent
        status = self._effective_status(rec)
        if status == Status.ISSUING:
            raise ApiError(409, "ISSUING")
        _, _, now_iso = self._now()
        if status == Status.ACTIVE and rec.credential_id:
            # Record first, then IAM: a failure leaves at worst a SUSPENDED record over a
            # still-active key, which we roll back below.
            if not self._repo.suspend(
                sub=rec.sub, credential_id=rec.credential_id, now_iso=now_iso
            ):
                raise ApiError(409, "ISSUING")
            name = rec.iam_user_name or iam_user_name(rec.sub)
            try:
                self._issuer.deactivate(name, rec.credential_id)
            except ClientError as e:
                self._repo.resume(sub=rec.sub, to_status=Status.ACTIVE, now_iso=now_iso)
                log("ERROR", "iam_suspend_failed", iamUser=name, code=_client_error_code(e))
                raise ApiError(502, "AWS_API_ERROR") from None
        elif not self._repo.suspend(sub=rec.sub, credential_id=None, now_iso=now_iso):
            raise ApiError(409, "ISSUING")
        log("INFO", "admin_suspend", actor=actor, target=subject_hash(target_sub)[:12])
        metric("KeySuspended")
        return {"user": self._admin_row(self._repo.get_registration(target_sub) or rec)}

    def admin_resume(self, identity: Identity, target_sub: str) -> dict[str, Any]:
        """Lift the hold. A suspended key that still exists and has not expired is set Active
        again; otherwise the record becomes REVOKED/EXPIRED and the user may reissue."""
        actor = self._require_admin(identity)
        rec = self._refresh(self._repo.get_registration(target_sub))
        if rec is None:
            raise ApiError(404, "USER_NOT_FOUND")
        if not rec.suspended:
            raise ApiError(409, "NOT_SUSPENDED")
        now, _, now_iso = self._now()
        to_status: Status | None = None
        if rec.status == Status.SUSPENDED and rec.credential_id:
            name = rec.iam_user_name or iam_user_name(rec.sub)
            try:
                creds = self._issuer.list_credentials(name)
                match = next((c for c in creds if c.credential_id == rec.credential_id), None)
                if match is None:
                    to_status = Status.REVOKED
                elif match.expires_at and match.expires_at <= now:
                    to_status = Status.EXPIRED
                else:
                    self._issuer.reactivate(name, rec.credential_id)
                    to_status = Status.ACTIVE
            except ClientError as e:
                log("ERROR", "iam_resume_failed", iamUser=name, code=_client_error_code(e))
                raise ApiError(502, "AWS_API_ERROR") from None
        self._repo.resume(sub=rec.sub, to_status=to_status, now_iso=now_iso)
        log("INFO", "admin_resume", actor=actor, target=subject_hash(target_sub)[:12])
        return {"user": self._admin_row(self._repo.get_registration(target_sub) or rec)}

    def admin_set_issuance(self, identity: Identity, enabled: bool) -> dict[str, Any]:
        actor = self._require_admin(identity)
        _, _, now_iso = self._now()
        self._repo.set_issuance_enabled(enabled, now_iso)
        log("INFO", "admin_set_issuance", actor=actor, enabled=enabled)
        return {"issuanceEnabled": enabled}
