"""Month-to-date Bedrock cost per portal user, from AWS Cost Explorer.

Bedrock attributes inference cost to the calling IAM principal and its tags ("IAM principal
cost allocation"). Every portal IAM user carries the tag PortalSubjectHash, which surfaces in
Cost Explorer as the cost allocation tag `iamPrincipal/PortalSubjectHash` once it has been
activated in the payer account. Only portal users carry it, so no SERVICE filter is applied
(Anthropic models are billed under Marketplace service names, not "Amazon Bedrock").

Cost Explorer charges per API request and data lags up to 24 hours, so results are cached in
DynamoDB and refreshed at most every `ttl_seconds`.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from portal.log import log

COST_TAG_KEY = "iamPrincipal/PortalSubjectHash"
METRIC = "UnblendedCost"


@dataclass
class CostSnapshot:
    month: str  # YYYY-MM (UTC)
    currency: str
    total: str  # decimal string, portal users only
    by_subject: dict[str, str] = field(default_factory=dict)  # full sha256(sub) -> amount
    fetched_at: str = ""
    available: bool = True  # False when Cost Explorer could not be queried

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str) -> CostSnapshot:
        return cls(**json.loads(raw))

    def amount_for(self, subject_hash: str) -> str:
        return self.by_subject.get(subject_hash, "0")


def month_bounds(now: datetime) -> tuple[str, str, str]:
    """(YYYY-MM, start date, exclusive end date) for Cost Explorer, in UTC."""
    today = now.astimezone(UTC).date()
    start = today.replace(day=1)
    end = today + timedelta(days=1)  # End is exclusive; include today
    return start.strftime("%Y-%m"), start.isoformat(), end.isoformat()


def query_cost_explorer(ce: Any, now: datetime) -> CostSnapshot:
    month, start, end = month_bounds(now)
    by_subject: dict[str, Decimal] = {}
    currency = "USD"
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": [METRIC],
            "GroupBy": [{"Type": "TAG", "Key": COST_TAG_KEY}],
        }
        if token:
            params["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**params)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                key = group["Keys"][0]  # "iamPrincipal/PortalSubjectHash$<value>"
                value = key.split("$", 1)[1] if "$" in key else ""
                if not value:
                    continue  # costs without the tag are not portal users
                metric = group["Metrics"][METRIC]
                currency = metric.get("Unit", currency)
                by_subject[value] = by_subject.get(value, Decimal(0)) + Decimal(metric["Amount"])
        token = resp.get("NextPageToken")
        if not token:
            break
    total = sum(by_subject.values(), Decimal(0))
    return CostSnapshot(
        month=month,
        currency=currency,
        total=str(total.quantize(Decimal("0.0001"))),
        by_subject={k: str(v.quantize(Decimal("0.0001"))) for k, v in by_subject.items()},
        fetched_at=now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


class CostReporter:
    def __init__(
        self,
        ce: Any,
        ddb: Any,
        table: str,
        *,
        ttl_seconds: int = 6 * 3600,
        min_refresh_seconds: int = 15 * 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ce = ce
        self._ddb = ddb
        self._table = table
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._clock = clock

    def _cache_pk(self, month: str) -> str:
        return f"CACHE#COSTS#{month}"

    def _load(self, month: str) -> tuple[CostSnapshot, int] | None:
        item = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": self._cache_pk(month)}}, ConsistentRead=True
        ).get("Item")
        if not item:
            return None
        return CostSnapshot.from_json(item["payload"]["S"]), int(item["fetchedEpoch"]["N"])

    def _save(self, snap: CostSnapshot, fetched_epoch: int) -> None:
        self._ddb.put_item(
            TableName=self._table,
            Item={
                "pk": {"S": self._cache_pk(snap.month)},
                "payload": {"S": snap.to_json()},
                "fetchedEpoch": {"N": str(fetched_epoch)},
            },
        )

    def snapshot(self, *, force: bool = False) -> CostSnapshot:
        """Cached month-to-date costs. `force` (admin) bypasses the TTL but not the minimum
        refresh interval. Never raises: on failure returns the stale cache or an
        unavailable snapshot."""
        now = self._clock()
        month, _, _ = month_bounds(now)
        epoch = int(now.timestamp())
        cached = self._load(month)
        if cached:
            snap, fetched = cached
            age = epoch - fetched
            if age < self._ttl and (not force or age < self._min_refresh):
                return snap
        try:
            snap = query_cost_explorer(self._ce, now)
        except Exception as e:  # Cost Explorer disabled, AccessDenied, throttling...
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            log("WARN", "cost_explorer_failed", code=code)
            if cached:
                return cached[0]
            return CostSnapshot(month=month, currency="USD", total="0", available=False)
        self._save(snap, epoch)
        log("INFO", "cost_explorer_refreshed", month=month, users=len(snap.by_subject))
        return snap
