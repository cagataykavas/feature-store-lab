from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from feature_store import FeatureRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    values_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_id, event_time)
);
CREATE INDEX IF NOT EXISTS idx_feature_rows_entity_time
    ON feature_rows(entity_id, event_time DESC);

CREATE TABLE IF NOT EXISTS materialization_state (
    name TEXT PRIMARY KEY,
    watermark TEXT NOT NULL
);
"""


class SQLiteOfflineFeatureStore:
    def __init__(self, database_path: str | Path = "features.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _normalize_time(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def write(self, row: FeatureRow) -> None:
        values_json = json.dumps(row.values, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feature_rows(entity_id, event_time, values_json)
                VALUES (?, ?, ?)
                ON CONFLICT(entity_id, event_time) DO UPDATE SET
                    values_json = excluded.values_json
                """,
                (row.entity_id, self._normalize_time(row.event_time), values_json),
            )

    def write_many(self, rows: Iterable[FeatureRow]) -> None:
        for row in rows:
            self.write(row)

    def get_as_of(self, entity_id: str, timestamp: datetime) -> FeatureRow:
        normalized = self._normalize_time(timestamp)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT entity_id, event_time, values_json
                FROM feature_rows
                WHERE entity_id = ? AND event_time <= ?
                ORDER BY event_time DESC
                LIMIT 1
                """,
                (entity_id, normalized),
            ).fetchone()
        if row is None:
            raise KeyError(f"no features for {entity_id} as of {normalized}")
        return FeatureRow(
            entity_id=row["entity_id"],
            event_time=datetime.fromisoformat(row["event_time"]),
            values={key: float(value) for key, value in json.loads(row["values_json"]).items()},
        )

    def latest(self, entity_id: str) -> FeatureRow:
        return self.get_as_of(entity_id, datetime.now(UTC))

    def changed_since(self, watermark: datetime | None, limit: int = 1000) -> list[FeatureRow]:
        query = "SELECT entity_id, event_time, values_json FROM feature_rows"
        params: list[object] = []
        if watermark is not None:
            query += " WHERE event_time > ?"
            params.append(self._normalize_time(watermark))
        query += " ORDER BY event_time ASC LIMIT ?"
        params.append(max(1, min(limit, 10000)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            FeatureRow(
                entity_id=row["entity_id"],
                event_time=datetime.fromisoformat(row["event_time"]),
                values={key: float(value) for key, value in json.loads(row["values_json"]).items()},
            )
            for row in rows
        ]

    def get_watermark(self, name: str = "online") -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT watermark FROM materialization_state WHERE name = ?",
                (name,),
            ).fetchone()
        return datetime.fromisoformat(row["watermark"]) if row else None

    def set_watermark(self, value: datetime, name: str = "online") -> None:
        normalized = self._normalize_time(value)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO materialization_state(name, watermark) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET watermark = excluded.watermark
                """,
                (name, normalized),
            )
