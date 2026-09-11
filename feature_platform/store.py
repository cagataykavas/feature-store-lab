from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from feature_platform.errors import (
    FeatureMutationError,
    FeatureViewNotFound,
    MaterializationLeaseError,
)
from feature_platform.models import (
    FeatureDefinition,
    FeatureRow,
    FeatureType,
    FeatureView,
    OnlineFeature,
    TrainingExample,
    utc,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS feature_views (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(name, version),
    UNIQUE(name, fingerprint)
);

CREATE TABLE IF NOT EXISTS feature_rows (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    view_name TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    entity_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    values_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ingestion_id TEXT,
    FOREIGN KEY(view_name, view_version) REFERENCES feature_views(name, version),
    UNIQUE(view_name, view_version, entity_id, event_time),
    UNIQUE(ingestion_id)
);
CREATE INDEX IF NOT EXISTS idx_rows_point_in_time
    ON feature_rows(view_name, view_version, entity_id, event_time DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS online_features (
    view_name TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_sequence INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    materialized_at TEXT NOT NULL,
    PRIMARY KEY(view_name, entity_id),
    FOREIGN KEY(row_sequence) REFERENCES feature_rows(sequence)
);

CREATE TABLE IF NOT EXISTS materialization_jobs (
    name TEXT PRIMARY KEY,
    cursor_sequence INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    updated_at TEXT NOT NULL
);
"""


class SQLiteFeatureRepository:
    """Durable local adapter with transactionally consistent materialization."""

    def __init__(self, database_path: str | Path = "features.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _time(value: datetime) -> str:
        return utc(value).isoformat()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def register_view(self, view: FeatureView, *, activate: bool = True) -> FeatureView:
        schema_json = json.dumps(view.as_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT schema_json FROM feature_views WHERE name = ? AND version = ?",
                (view.name, view.version),
            ).fetchone()
            if existing:
                registered = self._view_from_json(existing["schema_json"])
                if registered.fingerprint != view.fingerprint:
                    raise FeatureMutationError(
                        f"feature view {view.name}:{view.version} is immutable once registered"
                    )
                connection.commit()
                return registered

            latest = connection.execute(
                "SELECT schema_json FROM feature_views WHERE name = ? ORDER BY version DESC LIMIT 1",
                (view.name,),
            ).fetchone()
            if latest:
                self._view_from_json(latest["schema_json"]).assert_compatible_successor(view)
            connection.execute(
                """
                INSERT INTO feature_views(
                    name, version, entity_type, schema_json, fingerprint, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view.name,
                    view.version,
                    view.entity_type,
                    schema_json,
                    view.fingerprint,
                    int(activate),
                    self._time(self._now()),
                ),
            )
            if activate:
                connection.execute(
                    "UPDATE feature_views SET active = (version = ?) WHERE name = ?",
                    (view.version, view.name),
                )
            connection.commit()
        return view

    def get_view(self, name: str, version: int | None = None) -> FeatureView:
        if version is None:
            query = "SELECT schema_json FROM feature_views WHERE name = ? AND active = 1"
            params: tuple[Any, ...] = (name,)
        else:
            query = "SELECT schema_json FROM feature_views WHERE name = ? AND version = ?"
            params = (name, version)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        if not row:
            suffix = "active" if version is None else str(version)
            raise FeatureViewNotFound(f"feature view {name}:{suffix} not found")
        return self._view_from_json(row["schema_json"])

    def list_views(self) -> list[FeatureView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT schema_json FROM feature_views ORDER BY name, version"
            ).fetchall()
        return [self._view_from_json(row["schema_json"]) for row in rows]

    def write(self, row: FeatureRow) -> FeatureRow:
        view = self.get_view(row.view_name, row.view_version)
        view.validate_values(row.values)
        values_json = json.dumps(row.values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        event_time = self._time(row.event_time)
        created_at = self._time(row.created_at)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if row.ingestion_id:
                duplicate = connection.execute(
                    "SELECT * FROM feature_rows WHERE ingestion_id = ?", (row.ingestion_id,)
                ).fetchone()
                if duplicate:
                    existing = self._row_from_record(duplicate)
                    if existing.content_hash != row.content_hash:
                        raise FeatureMutationError(
                            f"ingestion id {row.ingestion_id!r} was reused with different content"
                        )
                    connection.commit()
                    return existing
            existing = connection.execute(
                """
                SELECT * FROM feature_rows
                WHERE view_name = ? AND view_version = ? AND entity_id = ? AND event_time = ?
                """,
                (row.view_name, row.view_version, row.entity_id, event_time),
            ).fetchone()
            if existing:
                stored = self._row_from_record(existing)
                if stored.content_hash != row.content_hash:
                    raise FeatureMutationError(
                        "logical feature rows are immutable; publish a correction at a new event time"
                    )
                connection.commit()
                return stored
            cursor = connection.execute(
                """
                INSERT INTO feature_rows(
                    view_name, view_version, entity_id, event_time, created_at,
                    values_json, content_hash, ingestion_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.view_name,
                    row.view_version,
                    row.entity_id,
                    event_time,
                    created_at,
                    values_json,
                    row.content_hash,
                    row.ingestion_id,
                ),
            )
            connection.commit()
            return FeatureRow(**{**row.__dict__, "sequence": int(cursor.lastrowid)})

    def write_many(self, rows: Iterable[FeatureRow]) -> list[FeatureRow]:
        return [self.write(row) for row in rows]

    def get_as_of(
        self,
        view_name: str,
        entity_id: str,
        timestamp: datetime,
        *,
        version: int | None = None,
        created_before: datetime | None = None,
    ) -> FeatureRow:
        view = self.get_view(view_name, version)
        params: list[Any] = [
            view.name,
            view.version,
            entity_id,
            self._time(timestamp),
        ]
        created_clause = ""
        if created_before is not None:
            created_clause = " AND created_at <= ?"
            params.append(self._time(created_before))
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM feature_rows
                WHERE view_name = ? AND view_version = ? AND entity_id = ? AND event_time <= ?
                {created_clause}
                ORDER BY event_time DESC, sequence DESC LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            raise KeyError(
                f"no {view.name}:{view.version} features for {entity_id} as of {self._time(timestamp)}"
            )
        return self._row_from_record(row)

    def point_in_time_join(
        self,
        view_name: str,
        examples: Iterable[tuple[str, datetime]],
        *,
        version: int | None = None,
        created_before: datetime | None = None,
    ) -> list[TrainingExample]:
        joined: list[TrainingExample] = []
        for entity_id, label_time in examples:
            try:
                row = self.get_as_of(
                    view_name,
                    entity_id,
                    label_time,
                    version=version,
                    created_before=created_before,
                )
            except KeyError:
                joined.append(TrainingExample(entity_id, utc(label_time), None, None, None))
            else:
                joined.append(
                    TrainingExample(
                        entity_id,
                        utc(label_time),
                        dict(row.values),
                        utc(row.event_time),
                        row.sequence,
                    )
                )
        return joined

    def materialize(
        self,
        *,
        job_name: str = "online",
        owner: str,
        limit: int = 1000,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = utc(now or self._now())
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM materialization_jobs WHERE name = ?", (job_name,)
            ).fetchone()
            if job and job["lease_owner"] not in (None, owner):
                held_until = datetime.fromisoformat(job["lease_until"])
                if held_until > now:
                    raise MaterializationLeaseError(
                        f"job {job_name!r} is leased by {job['lease_owner']!r} until {held_until.isoformat()}"
                    )
            cursor = int(job["cursor_sequence"]) if job else 0
            connection.execute(
                """
                INSERT INTO materialization_jobs(
                    name, cursor_sequence, lease_owner, lease_until, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    lease_until = excluded.lease_until,
                    updated_at = excluded.updated_at
                """,
                (job_name, cursor, owner, self._time(lease_until), self._time(now)),
            )
            rows = connection.execute(
                "SELECT * FROM feature_rows WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (cursor, max(1, min(limit, 10_000))),
            ).fetchall()
            for record in rows:
                connection.execute(
                    """
                    INSERT INTO online_features(
                        view_name, entity_id, view_version, event_time, created_at, row_sequence,
                        values_json, materialized_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(view_name, entity_id) DO UPDATE SET
                        view_version = excluded.view_version,
                        event_time = excluded.event_time,
                        created_at = excluded.created_at,
                        row_sequence = excluded.row_sequence,
                        values_json = excluded.values_json,
                        materialized_at = excluded.materialized_at
                    WHERE excluded.event_time > online_features.event_time
                       OR (excluded.event_time = online_features.event_time
                           AND excluded.row_sequence > online_features.row_sequence)
                    """,
                    (
                        record["view_name"],
                        record["entity_id"],
                        record["view_version"],
                        record["event_time"],
                        record["created_at"],
                        record["sequence"],
                        record["values_json"],
                        self._time(now),
                    ),
                )
            new_cursor = int(rows[-1]["sequence"]) if rows else cursor
            connection.execute(
                """
                UPDATE materialization_jobs
                SET cursor_sequence = ?, lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE name = ?
                """,
                (new_cursor, self._time(now), job_name),
            )
            connection.commit()
        return {
            "job_name": job_name,
            "rows_scanned": len(rows),
            "previous_cursor": cursor,
            "new_cursor": new_cursor,
        }

    def get_online(
        self, view_name: str, entity_id: str, *, now: datetime | None = None
    ) -> OnlineFeature:
        view = self.get_view(view_name)
        with self._connect() as connection:
            record = connection.execute(
                "SELECT * FROM online_features WHERE view_name = ? AND entity_id = ?",
                (view_name, entity_id),
            ).fetchone()
        if not record:
            raise KeyError(f"{view_name} features for {entity_id} are not materialized")
        observed_at = utc(now or self._now())
        event_time = datetime.fromisoformat(record["event_time"])
        age = max(0.0, (observed_at - event_time).total_seconds())
        row = FeatureRow(
            view_name=record["view_name"],
            view_version=record["view_version"],
            entity_id=record["entity_id"],
            event_time=event_time,
            created_at=datetime.fromisoformat(record["created_at"]),
            values=json.loads(record["values_json"]),
            sequence=record["row_sequence"],
        )
        return OnlineFeature(
            row=row,
            materialized_at=datetime.fromisoformat(record["materialized_at"]),
            stale=age > view.ttl_seconds,
            age_seconds=age,
        )

    def parity(self, view_name: str, entity_id: str) -> dict[str, Any]:
        online = self.get_online(view_name, entity_id)
        offline = self.get_as_of(view_name, entity_id, datetime.max.replace(tzinfo=UTC))
        matches = online.row.sequence == offline.sequence and online.row.values == offline.values
        return {
            "matches": matches,
            "online_sequence": online.row.sequence,
            "offline_sequence": offline.sequence,
        }

    @staticmethod
    def _view_from_json(value: str) -> FeatureView:
        payload = json.loads(value)
        features = tuple(
            FeatureDefinition(
                name=item["name"],
                dtype=FeatureType(item["dtype"]),
                nullable=item["nullable"],
                description=item.get("description", ""),
            )
            for item in payload["features"]
        )
        return FeatureView(
            name=payload["name"],
            version=payload["version"],
            entity_type=payload["entity_type"],
            features=features,
            ttl_seconds=payload["ttl_seconds"],
            description=payload.get("description", ""),
        )

    @staticmethod
    def _row_from_record(record: sqlite3.Row) -> FeatureRow:
        return FeatureRow(
            view_name=record["view_name"],
            view_version=record["view_version"],
            entity_id=record["entity_id"],
            event_time=datetime.fromisoformat(record["event_time"]),
            created_at=datetime.fromisoformat(record["created_at"]),
            values=json.loads(record["values_json"]),
            ingestion_id=record["ingestion_id"],
            sequence=record["sequence"],
        )
