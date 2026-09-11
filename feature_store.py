"""Compatibility layer for the repository's original in-memory API."""

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
    def __init__(self) -> None:
        self.rows: list[FeatureRow] = []

    def write(self, row: FeatureRow) -> None:
        self.rows.append(row)
        self.rows.sort(key=lambda candidate: (candidate.entity_id, candidate.event_time))

    def get_as_of(self, entity_id: str, timestamp: datetime) -> dict[str, float]:
        candidates = [
            row for row in self.rows if row.entity_id == entity_id and row.event_time <= timestamp
        ]
        if not candidates:
            raise KeyError(f"no features for {entity_id} as of {timestamp.isoformat()}")
        return candidates[-1].values.copy()


class OnlineFeatureStore:
    def __init__(self) -> None:
        self.latest: dict[str, dict[str, Any]] = {}

    def materialize(self, row: FeatureRow) -> None:
        current = self.latest.get(row.entity_id)
        if current is None or row.event_time >= current["event_time"]:
            self.latest[row.entity_id] = {
                "event_time": row.event_time,
                "values": row.values.copy(),
            }

    def get(self, entity_id: str) -> dict[str, float]:
        return self.latest[entity_id]["values"].copy()
