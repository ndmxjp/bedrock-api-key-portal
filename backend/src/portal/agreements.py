"""Deploy-time acceptance of Bedrock model access agreements (CloudFormation custom resource).

Marketplace-offered serverless models (e.g. Anthropic) need an account-level agreement before
anyone can invoke them. Portal users intentionally have no Marketplace permissions, so their
first call cannot create the agreement. This handler runs on every deploy and accepts the
public offer for each target model that is authorized but not yet agreed.

Never fails the deployment: per-model problems are logged and summarized in the response.
Deleting the stack does NOT cancel agreements.
"""

import os
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from portal.log import log

DONE = ("AVAILABLE", "PENDING")


def target_models(bedrock: Any, wanted: list[str], providers: list[str] | None = None) -> list[str]:
    """All ACTIVE foundation models in the region when `wanted` is ["*"] (optionally only
    those whose model ID starts with `<provider>.`), else `wanted`."""
    if wanted != ["*"]:
        return wanted
    prefixes = tuple(f"{p.lower()}." for p in providers or [])
    summaries = bedrock.list_foundation_models().get("modelSummaries", [])
    return sorted(
        {
            m["modelId"]
            for m in summaries
            if (m.get("modelLifecycle") or {}).get("status", "ACTIVE") == "ACTIVE"
            and (not prefixes or m["modelId"].lower().startswith(prefixes))
        }
    )


def accept_agreements(
    bedrock: Any, model_ids: list[str], *, pause: float = 0.2
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "accepted": [],
        "alreadyAgreed": [],
        "notAuthorized": [],
        "noOffer": [],
        "failed": [],
    }
    for model_id in model_ids:
        try:
            avail = bedrock.get_foundation_model_availability(modelId=model_id)
            status = (avail.get("agreementAvailability") or {}).get("status")
            if status in DONE:
                result["alreadyAgreed"].append(model_id)
                continue
            if avail.get("authorizationStatus") != "AUTHORIZED":
                # e.g. Anthropic's first-time use form not submitted for this account
                result["notAuthorized"].append(model_id)
                continue
            offers = bedrock.list_foundation_model_agreement_offers(
                modelId=model_id, offerType="PUBLIC"
            ).get("offers", [])
            if not offers:
                result["noOffer"].append(model_id)
                continue
            bedrock.create_foundation_model_agreement(
                modelId=model_id, offerToken=offers[0]["offerToken"]
            )
            result["accepted"].append(model_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            result["failed"].append(f"{model_id}:{code}")
        time.sleep(pause)
    return result


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    physical_id = "bedrock-model-agreements"
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": physical_id}
    props = event.get("ResourceProperties") or {}
    wanted = list(props.get("ModelIds") or [])
    providers = list(props.get("Providers") or [])
    region = os.environ.get("BEDROCK_REGION", "us-west-2")
    bedrock = boto3.client(
        "bedrock",
        region_name=region,
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
    )
    try:
        models = target_models(bedrock, wanted, providers)
        result = accept_agreements(bedrock, models)
    except Exception as e:  # never block a deployment on this
        log("ERROR", "model_agreements_failed", type=type(e).__name__)
        return {"PhysicalResourceId": physical_id, "Data": {"Summary": "error"}}
    log("INFO", "model_agreements", region=region, **result)
    summary = ", ".join(f"{k}={len(v)}" for k, v in result.items())
    return {"PhysicalResourceId": physical_id, "Data": {"Summary": summary}}
