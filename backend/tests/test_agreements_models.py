from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from conftest import TABLE, ident, make_settings
from portal import agreements
from portal.issuer import Issuer
from portal.models import ModelCatalog
from portal.service import PortalService


@pytest.fixture
def bedrock():
    client = boto3.client("bedrock", region_name="us-west-2")
    with Stubber(client) as stub:
        yield client, stub


OFFER = {
    "offerToken": "tok-1",
    "termDetails": {"usageBasedPricingTerm": {"rateCard": []}, "legalTerm": {}, "supportTerm": {}},
}


def avail(model, agreement, auth="AUTHORIZED"):
    return {
        "modelId": model,
        "agreementAvailability": {"status": agreement},
        "authorizationStatus": auth,
        "entitlementAvailability": "AVAILABLE",
        "regionAvailability": "AVAILABLE",
    }


def test_target_models_all_active(bedrock):
    client, stub = bedrock
    stub.add_response(
        "list_foundation_models",
        {
            "modelSummaries": [
                {"modelArn": "arn:a", "modelId": "a.one", "modelLifecycle": {"status": "ACTIVE"}},
                {"modelArn": "arn:b", "modelId": "b.old", "modelLifecycle": {"status": "LEGACY"}},
                {"modelArn": "arn:c", "modelId": "c.two"},
            ]
        },
        {},
    )
    assert agreements.target_models(client, ["*"]) == ["a.one", "c.two"]
    stub.add_response(
        "list_foundation_models",
        {
            "modelSummaries": [
                {"modelArn": "1", "modelId": "anthropic.claude-x"},
                {"modelArn": "2", "modelId": "openai.gpt-x"},
                {"modelArn": "3", "modelId": "stability.sd-x"},
            ]
        },
        {},
    )
    assert agreements.target_models(client, ["*"], ["anthropic", "OpenAI"]) == [
        "anthropic.claude-x",
        "openai.gpt-x",
    ]
    assert agreements.target_models(client, ["x.y"]) == ["x.y"]  # explicit list: no API call


def test_accept_agreements(bedrock):
    client, stub = bedrock
    # 1: needs agreement -> accepted
    stub.add_response(
        "get_foundation_model_availability", avail("m1", "NOT_AVAILABLE"), {"modelId": "m1"}
    )
    stub.add_response(
        "list_foundation_model_agreement_offers",
        {"modelId": "m1", "offers": [OFFER]},
        {"modelId": "m1", "offerType": "PUBLIC"},
    )
    stub.add_response(
        "create_foundation_model_agreement",
        {"modelId": "m1"},
        {"modelId": "m1", "offerToken": "tok-1"},
    )
    # 2: already agreed
    stub.add_response(
        "get_foundation_model_availability", avail("m2", "AVAILABLE"), {"modelId": "m2"}
    )
    # 3: use-case form not submitted -> never auto-accepted
    stub.add_response(
        "get_foundation_model_availability",
        avail("m3", "NOT_AVAILABLE", "NOT_AUTHORIZED"),
        {"modelId": "m3"},
    )
    # 4: no marketplace offer (first-party model)
    stub.add_response(
        "get_foundation_model_availability", avail("m4", "NOT_AVAILABLE"), {"modelId": "m4"}
    )
    stub.add_response(
        "list_foundation_model_agreement_offers",
        {"modelId": "m4", "offers": []},
        {"modelId": "m4", "offerType": "PUBLIC"},
    )
    # 5: error is recorded, processing continues
    stub.add_client_error("get_foundation_model_availability", "ValidationException")

    out = agreements.accept_agreements(client, ["m1", "m2", "m3", "m4", "m5"], pause=0)
    assert out == {
        "accepted": ["m1"],
        "alreadyAgreed": ["m2"],
        "notAuthorized": ["m3"],
        "noOffer": ["m4"],
        "failed": ["m5:ValidationException"],
    }
    stub.assert_no_pending_responses()


def test_handler_never_fails_and_ignores_delete(monkeypatch):
    assert agreements.handler({"RequestType": "Delete"}, None) == {
        "PhysicalResourceId": "bedrock-model-agreements"
    }

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(agreements, "target_models", boom)
    out = agreements.handler(
        {"RequestType": "Update", "ResourceProperties": {"ModelIds": ["*"]}}, None
    )
    assert out["Data"]["Summary"] == "error"


def test_model_catalog_caches(bedrock, ddb):
    client, stub = bedrock
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    cat = ModelCatalog(client, ddb, TABLE, clock=lambda: now[0])
    stub.add_response(
        "list_inference_profiles",
        {
            "inferenceProfileSummaries": [
                _profile("us.b.model", "ACTIVE"),
                _profile("global.a.model", "ACTIVE"),
            ],
            "nextToken": "n",
        },
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000},
    )
    stub.add_response(
        "list_inference_profiles",
        {"inferenceProfileSummaries": [_profile("us.c.model", "ACTIVE")]},
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000, "nextToken": "n"},
    )
    stub.add_response(
        "list_foundation_models",
        {"modelSummaries": [{"modelArn": "x", "modelId": "d.direct-1:0"}]},
        {"byInferenceType": "ON_DEMAND"},
    )
    assert cat.profile_ids() == ["d.direct-1:0", "global.a.model", "us.b.model", "us.c.model"]
    now[0] += timedelta(hours=23)
    assert cat.profile_ids() == ["d.direct-1:0", "global.a.model", "us.b.model", "us.c.model"]
    now[0] += timedelta(hours=2)
    stub.add_client_error("list_inference_profiles", "ThrottlingException")
    assert cat.profile_ids()[0] == "d.direct-1:0"  # stale cache on error
    stub.assert_no_pending_responses()


def _profile(pid, status):
    return {
        "inferenceProfileName": pid,
        "inferenceProfileArn": f"arn:aws:bedrock:us-west-2:123456789012:inference-profile/{pid}",
        "inferenceProfileId": pid,
        "models": [{"modelArn": "arn:aws:bedrock:us-west-2::foundation-model/x"}],
        "status": status,
        "type": "SYSTEM_DEFINED",
    }


def test_me_lists_all_models_in_all_mode(bedrock, ddb, repo, iam):
    client, stub = bedrock
    settings = make_settings(allow_all_models=True, allowed_models=())
    cat = ModelCatalog(client, ddb, TABLE)
    issuer = Issuer(
        iam,
        path=settings.iam_path,
        user_policy_arn=settings.user_policy_arn,
        permissions_boundary_arn=settings.permissions_boundary_arn,
    )
    svc = PortalService(settings, repo, issuer, models=cat)
    stub.add_response(
        "list_inference_profiles",
        {"inferenceProfileSummaries": [_profile("us.x.model", "ACTIVE")]},
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000},
    )
    stub.add_response(
        "list_foundation_models", {"modelSummaries": []}, {"byInferenceType": "ON_DEMAND"}
    )
    me = svc.me(ident())
    assert me["allModels"] is True and me["allowedModels"] == ["us.x.model"]


def test_catalog_provider_filter(bedrock, ddb):
    client, stub = bedrock
    cat = ModelCatalog(client, ddb, TABLE, providers=("anthropic", "openai"))
    stub.add_response(
        "list_inference_profiles",
        {
            "inferenceProfileSummaries": [
                _profile("us.anthropic.claude-x", "ACTIVE"),
                _profile("global.openai.gpt-x", "ACTIVE"),
                _profile("us.amazon.nova-x", "ACTIVE"),
                _profile("us.anthropic.old", "LEGACY"),
            ]
        },
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000},
    )
    stub.add_response(
        "list_foundation_models",
        {
            "modelSummaries": [
                {"modelArn": "1", "modelId": "openai.gpt-oss-120b-1:0"},
                {"modelArn": "2", "modelId": "meta.llama-x"},
            ]
        },
        {"byInferenceType": "ON_DEMAND"},
    )
    assert cat.profile_ids() == [
        "global.openai.gpt-x",
        "openai.gpt-oss-120b-1:0",
        "us.anthropic.claude-x",
    ]
