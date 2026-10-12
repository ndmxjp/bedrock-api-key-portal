from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from conftest import TABLE, admin, ident
from portal.costs import COST_TAG_KEY, CostReporter, month_bounds
from portal.errors import ApiError
from portal.issuer import Issuer
from portal.naming import subject_hash
from portal.service import PortalService

ALICE, BOB = "alice-0001", "bob-0001"


def ce_response(groups, token=None):
    resp = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-12"},
                "Groups": [
                    {
                        "Keys": [f"{COST_TAG_KEY}${k}"],
                        "Metrics": {"UnblendedCost": {"Amount": v, "Unit": "USD"}},
                    }
                    for k, v in groups
                ],
            }
        ]
    }
    if token:
        resp["NextPageToken"] = token
    return resp


EXPECTED = {
    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-12"},
    "Granularity": "MONTHLY",
    "Metrics": ["UnblendedCost"],
    "GroupBy": [{"Type": "TAG", "Key": COST_TAG_KEY}],
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def ce():
    client = boto3.client("ce", region_name="us-east-1")
    with Stubber(client) as stub:
        yield client, stub


@pytest.fixture
def setup(ce, ddb, settings, repo, iam):
    client, stub = ce
    clock = Clock()
    reporter = CostReporter(client, ddb, TABLE, clock=clock)
    issuer = Issuer(
        iam,
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
        sleep=lambda _s: None,
    )
    svc = PortalService(settings, repo, issuer, clock=clock, costs=reporter)
    return svc, stub, clock, reporter


def test_month_bounds_are_utc_and_end_exclusive():
    # 2026-11-01 03:00 JST is still October in UTC.
    jst = datetime(2026, 11, 1, 3, 0, tzinfo=UTC) - timedelta(hours=9)
    assert month_bounds(jst) == ("2026-10", "2026-10-01", "2026-11-01")


def test_user_sees_own_cost_and_cache_is_reused(setup):
    svc, stub, clock, _ = setup
    svc.issue(ident(sub=ALICE))
    svc.issue(ident(sub=BOB, email="bob@company.co.jp"))
    stub.add_response(
        "get_cost_and_usage",
        ce_response([(subject_hash(ALICE), "1.23456"), (subject_hash(BOB), "0.5"), ("", "99")]),
        EXPECTED,
    )
    usage = svc.me(ident(sub=ALICE))["usage"]
    assert usage == {
        "month": "2026-10",
        "amount": "1.2346",
        "currency": "USD",
        "fetchedAt": "2026-10-11T12:00:00Z",
        "available": True,
    }
    # Served from cache: no second Cost Explorer call (Stubber would fail on an unexpected call).
    clock.now += timedelta(hours=5)
    assert svc.me(ident(sub=BOB, email="bob@company.co.jp"))["usage"]["amount"] == "0.5000"
    assert svc.me(ident(sub="nobody"))["usage"]["amount"] == "0"
    stub.assert_no_pending_responses()


def test_cache_expires_and_pagination(setup):
    svc, stub, clock, _ = setup
    stub.add_response("get_cost_and_usage", ce_response([(subject_hash(ALICE), "1")]), EXPECTED)
    svc.me(ident(sub=ALICE))
    clock.now += timedelta(hours=7)
    later = {**EXPECTED, "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-12"}}
    stub.add_response(
        "get_cost_and_usage", ce_response([(subject_hash(ALICE), "2")], token="t"), later
    )
    stub.add_response(
        "get_cost_and_usage",
        ce_response([(subject_hash(ALICE), "0.25")]),
        {**later, "NextPageToken": "t"},
    )
    assert svc.me(ident(sub=ALICE))["usage"]["amount"] == "2.2500"


def test_cost_explorer_failure_degrades_gracefully(setup):
    svc, stub, clock, _ = setup
    stub.add_client_error("get_cost_and_usage", "AccessDeniedException")
    usage = svc.me(ident(sub=ALICE))["usage"]
    assert usage["available"] is False and usage["amount"] is None

    # A stale cache is preferred over nothing when a refresh fails.
    stub.add_response("get_cost_and_usage", ce_response([(subject_hash(ALICE), "3")]), EXPECTED)
    assert svc.me(ident(sub=ALICE))["usage"]["amount"] == "3.0000"
    clock.now += timedelta(hours=7)
    stub.add_client_error("get_cost_and_usage", "ThrottlingException")
    assert svc.me(ident(sub=ALICE))["usage"]["amount"] == "3.0000"


def test_admin_costs(setup):
    svc, stub, _clock, _ = setup
    svc.issue(ident(sub=ALICE))
    svc.issue(ident(sub=BOB, email="bob@company.co.jp"))
    stub.add_response(
        "get_cost_and_usage",
        ce_response(
            [(subject_hash(ALICE), "0.1"), (subject_hash(BOB), "2"), ("gone-user-hash", "0.4")]
        ),
        EXPECTED,
    )
    out = svc.admin_costs(admin())
    assert out["amount"] == "2.5000"
    assert [(u["email"], u["amount"]) for u in out["users"]] == [
        ("bob@company.co.jp", "2.0000"),
        ("alice@company.co.jp", "0.1000"),
    ]
    assert out["unmatchedAmount"] == "0.4000"

    with pytest.raises(ApiError) as e:
        svc.admin_costs(ident())
    assert e.value.code == "FORBIDDEN"


def test_admin_force_refresh_is_rate_limited(setup):
    svc, stub, clock, _ = setup
    stub.add_response("get_cost_and_usage", ce_response([]), EXPECTED)
    svc.admin_costs(admin())
    clock.now += timedelta(minutes=5)
    svc.admin_costs(admin(), refresh=True)  # within 15 min: served from cache, no CE call
    clock.now += timedelta(minutes=11)
    stub.add_response("get_cost_and_usage", ce_response([]), EXPECTED)
    svc.admin_costs(admin(), refresh=True)
    stub.assert_no_pending_responses()


def test_admin_costs_route_validates_query(setup, monkeypatch):
    import json

    import portal.handler as h
    from conftest import CLIENT_ID, ISSUER

    svc, stub, _, _ = setup
    monkeypatch.setattr(h, "_service", svc)
    monkeypatch.setattr(h, "_settings", svc._s)
    claims = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "4000000000",
        "sub": "admin-0001",
        "email": "boss@company.co.jp",
        "email_verified": "true",
        "cognito:groups": "[PortalAdmins]",
    }

    def call(qs):
        e = {
            "routeKey": "GET /api/admin/costs",
            "queryStringParameters": qs,
            "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        }
        r = h.handler(e, None)
        return r["statusCode"], json.loads(r["body"])

    assert call({"refresh": "yes"})[0] == 400
    assert call({"other": "1"})[0] == 400
    stub.add_response("get_cost_and_usage", ce_response([]), EXPECTED)
    assert call(None)[0] == 200
