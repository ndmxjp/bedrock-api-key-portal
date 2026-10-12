"""Model list shown to users when the portal allows all models (cached in DynamoDB)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from portal.log import log

CACHE_PK = "CACHE#MODELS#v2"


class ModelCatalog:
    def __init__(
        self,
        bedrock: Any,
        ddb: Any,
        table: str,
        *,
        ttl_seconds: int = 24 * 3600,
        providers: tuple[str, ...] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bedrock = bedrock
        self._ddb = ddb
        self._table = table
        self._ttl = ttl_seconds
        self._clock = clock
        self._providers = tuple(p.lower() for p in providers)

    def _provider_ok(self, model_or_profile_id: str, *, profile: bool) -> bool:
        if not self._providers:
            return True
        parts = model_or_profile_id.lower().split(".")
        provider = parts[1] if profile and len(parts) > 1 else parts[0]
        return provider in self._providers

    def _fetch(self) -> list[str]:
        ids: set[str] = set()
        token = None
        while True:
            params: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
            if token:
                params["nextToken"] = token
            resp = self._bedrock.list_inference_profiles(**params)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p["inferenceProfileId"]
                if p.get("status", "ACTIVE") == "ACTIVE" and self._provider_ok(pid, profile=True):
                    ids.add(pid)
            token = resp.get("nextToken")
            if not token:
                break
        # Models invoked directly (no inference profile), e.g. open-weight models.
        resp = self._bedrock.list_foundation_models(byInferenceType="ON_DEMAND")
        for m in resp.get("modelSummaries", []):
            active = (m.get("modelLifecycle") or {}).get("status", "ACTIVE") == "ACTIVE"
            if active and self._provider_ok(m["modelId"], profile=False):
                ids.add(m["modelId"])
        return sorted(ids)

    def profile_ids(self) -> list[str]:
        now = int(self._clock().timestamp())
        item = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": CACHE_PK}}, ConsistentRead=True
        ).get("Item")
        cached = json.loads(item["ids"]["S"]) if item else None
        if item and now - int(item["fetchedEpoch"]["N"]) < self._ttl:
            return cached
        try:
            ids = self._fetch()
        except Exception as e:
            log("WARN", "model_catalog_failed", type=type(e).__name__)
            return cached or []
        self._ddb.put_item(
            TableName=self._table,
            Item={
                "pk": {"S": CACHE_PK},
                "ids": {"S": json.dumps(ids)},
                "fetchedEpoch": {"N": str(now)},
            },
        )
        return ids
