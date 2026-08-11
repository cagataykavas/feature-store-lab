from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FeatureRow:
    entity_id: str
    event_time: datetime
    values: dict[str, float]


class OfflineFeatureStore:
    """Tiny in-memory reference implementation of point-in-time correct retrieval."""

    def __init__(self) -> None:
        self.rows: list[FeatureRow] = []

    def write(self, row: FeatureRow) -> None:
        self.rows.append(row)
        self.rows.sort(key=lambda x: (x.entity_id, x.event_time))

    def get_as_of(self, entity_id: str, timestamp: datetime) -> dict[str, float]:
        candidates = [r for r in self.rows if r.entity_id == entity_id and r.event_time <= timestamp]
        if not candidates:
            raise KeyError(f"no features for {entity_id} as of {timestamp.isoformat()}")
        return candidates[-1].values.copy()


class OnlineFeatureStore:
    """Low-latency latest-value store; Redis can replace this adapter in production."""

    def __init__(self) -> None:
        self.latest: dict[str, dict[str, Any]] = {}

    def materialize(self, row: FeatureRow) -> None:
        current = self.latest.get(row.entity_id)
        if current is None or row.event_time >= current["event_time"]:
            self.latest[row.entity_id] = {"event_time": row.event_time, "values": row.values.copy()}

    def get(self, entity_id: str) -> dict[str, float]:
        return self.latest[entity_id]["values"].copy()


if __name__ == "__main__":
    store = OfflineFeatureStore()
    store.write(FeatureRow("customer-42", datetime(2026, 1, 1), {"txn_30d": 8, "avg_amount": 74.5}))
    store.write(FeatureRow("customer-42", datetime(2026, 2, 1), {"txn_30d": 15, "avg_amount": 91.2}))
    print(store.get_as_of("customer-42", datetime(2026, 1, 15)))
