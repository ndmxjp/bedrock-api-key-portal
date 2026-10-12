"""Minimal structured logging. Callers must pass only non-secret fields."""

import json
import sys
import time
from typing import Any

NAMESPACE = "BedrockPoCPortal"


def log(level: str, event: str, **fields: Any) -> None:
    record = {"level": level, "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


def metric(name: str, value: float = 1) -> None:
    """Emit a CloudWatch Embedded Metric Format record."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": NAMESPACE, "Dimensions": [[]], "Metrics": [{"Name": name}]}
            ],
        },
        name: value,
    }
    print(json.dumps(record), file=sys.stdout, flush=True)
