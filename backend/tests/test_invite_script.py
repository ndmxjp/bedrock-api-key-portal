import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import invite_users


@pytest.fixture
def pool(tmp_path, monkeypatch):
    with mock_aws():
        idp = boto3.client("cognito-idp", region_name="ap-northeast-1")
        sts = boto3.client("sts", region_name="ap-northeast-1")
        pool_id = idp.create_user_pool(PoolName="p", UsernameAttributes=["email"])["UserPool"]["Id"]

        class FakeSession:
            def client(self, name):
                return {"cognito-idp": idp, "sts": sts}[name]

        monkeypatch.setattr(invite_users, "session", lambda *_a, **_k: FakeSession())
        cfg = tmp_path / "portal.config.json"
        cfg.write_text(json.dumps({"allowedDomains": ["example.com"]}))
        yield idp, pool_id, cfg


def run(monkeypatch, pool_id, cfg, *argv):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "invite_users.py",
            "--user-pool-id",
            pool_id,
            "--region",
            "ap-northeast-1",
            "--config",
            str(cfg),
            *argv,
        ],
    )
    invite_users.main()


def users(idp, pool_id):
    return {
        {a["Name"]: a["Value"] for a in u["Attributes"]}["email"]: u
        for u in idp.list_users(UserPoolId=pool_id)["Users"]
    }


def test_dry_run_creates_nothing(pool, monkeypatch):
    idp, pool_id, cfg = pool
    run(monkeypatch, pool_id, cfg, "alice@example.com")
    assert users(idp, pool_id) == {}


def test_apply_registers_only_allowed_domains(pool, monkeypatch, capsys):
    idp, pool_id, cfg = pool
    run(
        monkeypatch,
        pool_id,
        cfg,
        " Alice@Example.com",
        "bob@evil-example.com",
        "carol@sub.example.com",
        "not-an-email",
        "alice@example.com",
        "--apply",
        "--expected-account",
        "123456789012",
    )
    registered = users(idp, pool_id)
    assert set(registered) == {"alice@example.com"}
    attrs = {a["Name"]: a["Value"] for a in registered["alice@example.com"]["Attributes"]}
    assert attrs["email_verified"] == "true"
    out = capsys.readouterr().out
    assert "SKIP domain not allowed: bob@evil-example.com" in out
    assert "SKIP domain not allowed: carol@sub.example.com" in out

    run(
        monkeypatch,
        pool_id,
        cfg,
        "alice@example.com",
        "--apply",
        "--expected-account",
        "123456789012",
    )
    assert "already registered alice@example.com" in capsys.readouterr().out

    run(
        monkeypatch,
        pool_id,
        cfg,
        "alice@example.com",
        "--resend",
        "--apply",
        "--expected-account",
        "123456789012",
    )
    assert "re-sent invitation to alice@example.com" in capsys.readouterr().out


def test_apply_requires_matching_account(pool, monkeypatch):
    idp, pool_id, cfg = pool
    with pytest.raises(SystemExit):
        run(monkeypatch, pool_id, cfg, "alice@example.com", "--apply")
    with pytest.raises(SystemExit):
        run(
            monkeypatch,
            pool_id,
            cfg,
            "alice@example.com",
            "--apply",
            "--expected-account",
            "000000000000",
        )
    assert users(idp, pool_id) == {}
