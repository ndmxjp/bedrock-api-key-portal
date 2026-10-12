import pytest

from conftest import CLIENT_ID, ISSUER, make_settings
from portal.claims import identity_from_event
from portal.domain import InvalidEmail, is_allowed_email, normalize_email, parse_allowed_domains
from portal.errors import ApiError
from portal.naming import iam_user_name, mask_credential_id, subject_hash

ALLOWED = frozenset({"company.co.jp"})


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("alice@company.co.jp", True),
        ("  Alice@Company.CO.JP ", True),
        ("alice@company.co.jp.", True),
        ("alice@evilcompany.co.jp", False),
        ("alice@sub.company.co.jp", False),
        ("alice@company.co.jp.evil.com", False),
        ("alice@company.co.jp@evil.com", False),
        ("alice", False),
        ("@company.co.jp", False),
        ("al ice@company.co.jp", False),
        ("alice@\uff43ompany.co.jp", False),  # full-width look-alike is rejected
        ("alice@cömpany.co.jp", False),
        ("", False),
    ],
)
def test_exact_domain_match(email, ok):
    assert is_allowed_email(email, ALLOWED) is ok


def test_parse_allowed_domains():
    assert parse_allowed_domains(" Company.co.jp, example.com ,") == {
        "company.co.jp",
        "example.com",
    }
    with pytest.raises(ValueError):
        parse_allowed_domains(" , ")


def test_normalize_email_rejects_garbage():
    with pytest.raises(InvalidEmail):
        normalize_email("a@b@c")
    assert normalize_email("A@B.com") == "a@b.com"


def test_iam_user_name_is_deterministic_and_bounded():
    sub = "11111111-aaaa-bbbb-cccc-000000000001"
    name = iam_user_name(sub)
    assert name == iam_user_name(sub)
    assert name.startswith("poc-bedrock-")
    assert name == "poc-bedrock-" + subject_hash(sub)[:24]
    assert len(name) <= 64
    assert iam_user_name("other") != name
    with pytest.raises(ValueError):
        iam_user_name("")


def test_mask_credential_id():
    assert mask_credential_id("ACCA1234567890ABCDEF") == "ACCA••••••••CDEF"
    assert mask_credential_id(None) is None


def _event(**claims):
    base = {
        "token_use": "id",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": "2000000000",
        "sub": "11111111-aaaa-bbbb-cccc-000000000001",
        "email": "alice@company.co.jp",
        "email_verified": "true",
    }
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    return {"requestContext": {"authorizer": {"jwt": {"claims": base}}}}


def test_claims_ok():
    i = identity_from_event(_event(), make_settings(), 1_900_000_000)
    assert i.email_verified and i.sub.startswith("1111")


@pytest.mark.parametrize(
    "bad",
    [
        {"token_use": "access"},
        {"iss": "https://evil.example.com"},
        {"aud": "other-client"},
        {"exp": "1000"},
        {"exp": "nope"},
        {"sub": None},
        {"sub": "../../etc"},
        {"email": None},
    ],
)
def test_claims_rejected(bad):
    with pytest.raises(ApiError) as e:
        identity_from_event(_event(**bad), make_settings(), 1_900_000_000)
    assert e.value.status == 401


def test_claims_missing_authorizer():
    with pytest.raises(ApiError) as e:
        identity_from_event({"requestContext": {}}, make_settings(), 0)
    assert e.value.code == "UNAUTHENTICATED"


@pytest.mark.parametrize("value", ["false", "False", "", "1", None])
def test_email_verified_false(value):
    i = identity_from_event(_event(email_verified=value), make_settings(), 1_900_000_000)
    assert i.email_verified is False


def test_aud_list_rendering():
    i = identity_from_event(_event(aud=f"[{CLIENT_ID} x]"), make_settings(), 1_900_000_000)
    assert i.sub


def test_settings_validation():
    with pytest.raises(ValueError):
        make_settings(credential_age_days=31)
    with pytest.raises(ValueError):
        make_settings(credential_age_days=0)
    with pytest.raises(ValueError):
        make_settings(registration_cap=0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[PortalAdmins]", ("PortalAdmins",)),
        ("[PortalAdmins Other]", ("PortalAdmins", "Other")),
        (["PortalAdmins"], ("PortalAdmins",)),
        ("", ()),
        (None, ()),
    ],
)
def test_groups_claim(raw, expected):
    i = identity_from_event(_event(**{"cognito:groups": raw}), make_settings(), 1_900_000_000)
    assert i.groups == expected
