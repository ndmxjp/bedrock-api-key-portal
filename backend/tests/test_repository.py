import pytest

from portal.repository import BeginRejected, Status


def begin(repo, sub="u1", op="op1", now=1000, cap=2, needs_slot=True, cooldown=10, lease=90):
    repo.begin_issuance(
        sub=sub,
        email=f"{sub}@company.co.jp",
        iam_user_name=f"poc-bedrock-{sub}",
        operation_id=op,
        now_epoch=now,
        now_iso="2026-10-11T00:00:00Z",
        cap=cap,
        cooldown_seconds=cooldown,
        lease_seconds=lease,
        needs_slot=needs_slot,
    )


def test_begin_creates_issuing_and_reserves_slot(repo):
    begin(repo)
    rec = repo.get_registration("u1")
    assert rec.status == Status.ISSUING
    assert rec.slot_reserved
    assert repo.reserved_slots() == 1


def test_second_begin_while_issuing_is_rejected_without_consuming_slot(repo):
    begin(repo)
    with pytest.raises(BeginRejected) as e:
        begin(repo, op="op2", now=1100 - 50, needs_slot=False)  # lease still valid
    assert e.value.reason == "USER_STATE"
    assert repo.reserved_slots() == 1


def test_stale_read_cannot_double_count(repo):
    # Two requests read "no item" concurrently; both try needs_slot=True.
    begin(repo, op="a")
    with pytest.raises(BeginRejected):
        begin(repo, op="b", needs_slot=True, now=2000)  # lease expired, but slot flag mismatch
    assert repo.reserved_slots() == 1


def test_cap_enforced(repo):
    begin(repo, sub="u1", cap=2)
    begin(repo, sub="u2", cap=2)
    with pytest.raises(BeginRejected) as e:
        begin(repo, sub="u3", cap=2)
    assert e.value.reason == "CAP"
    assert repo.reserved_slots() == 2
    assert repo.get_registration("u3") is None


def test_cooldown(repo):
    begin(repo)
    repo.fail_issuance(sub="u1", operation_id="op1", error_code="X", now_iso="t")
    with pytest.raises(BeginRejected):
        begin(repo, op="op2", now=1005, needs_slot=False)
    begin(repo, op="op3", now=1011, needs_slot=False)
    assert repo.get_registration("u1").operation_id == "op3"


def test_stale_lease_can_be_taken_over(repo):
    begin(repo, now=1000, lease=90)
    begin(repo, op="op2", now=1091, needs_slot=False)
    assert repo.get_registration("u1").operation_id == "op2"
    assert repo.reserved_slots() == 1


def test_complete_requires_matching_operation(repo, ddb):
    begin(repo)
    with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):
        repo.complete_issuance(
            sub="u1",
            operation_id="other",
            credential_id="ACCA1",
            issued_at="t",
            expires_at="t2",
            now_iso="t",
        )
    repo.complete_issuance(
        sub="u1",
        operation_id="op1",
        credential_id="ACCA1",
        issued_at="t",
        expires_at="t2",
        now_iso="t",
    )
    rec = repo.get_registration("u1")
    assert rec.status == Status.ACTIVE and rec.credential_id == "ACCA1" and rec.lease_until is None


def test_active_cannot_begin(repo):
    begin(repo)
    repo.complete_issuance(
        sub="u1",
        operation_id="op1",
        credential_id="ACCA1",
        issued_at="t",
        expires_at=None,
        now_iso="t",
    )
    with pytest.raises(BeginRejected):
        begin(repo, op="op2", now=5000, needs_slot=False)


def test_transition_is_conditional(repo):
    begin(repo)
    repo.complete_issuance(
        sub="u1",
        operation_id="op1",
        credential_id="ACCA1",
        issued_at="t",
        expires_at=None,
        now_iso="t",
    )
    assert not repo.transition(
        sub="u1",
        credential_id="WRONG",
        from_status=Status.ACTIVE,
        to_status=Status.REVOKED,
        now_iso="t",
    )
    assert repo.transition(
        sub="u1",
        credential_id="ACCA1",
        from_status=Status.ACTIVE,
        to_status=Status.REVOKED,
        now_iso="t",
    )
    assert repo.get_registration("u1").status == Status.REVOKED


def test_kill_switch(repo):
    assert repo.issuance_enabled()
    repo.set_issuance_enabled(False, "t")
    assert not repo.issuance_enabled()
    repo.set_issuance_enabled(True, "t")
    assert repo.issuance_enabled()
