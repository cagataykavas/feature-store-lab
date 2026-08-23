from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from feature_store import FeatureRow, OnlineFeatureStore
from storage.sqlite_store import SQLiteOfflineFeatureStore


class FeatureWriteRequest(BaseModel):
    entity_id: str = Field(min_length=1, max_length=200)
    event_time: datetime
    values: dict[str, float]


DATABASE_PATH = Path(os.getenv("FEATURE_STORE_DATABASE_PATH", "features.db"))
offline = SQLiteOfflineFeatureStore(DATABASE_PATH)
online = OnlineFeatureStore()

app = FastAPI(
    title="Feature Store Service",
    version="0.2.0",
    description=(
        "Reference offline/online feature store with point-in-time correct reads, "
        "incremental materialization and explicit freshness metadata."
    ),
)


def serialize(row: FeatureRow) -> dict:
    return {
        "entity_id": row.entity_id,
        "event_time": row.event_time.isoformat(),
        "values": row.values,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/features", status_code=201)
def write_feature(request: FeatureWriteRequest) -> dict:
    row = FeatureRow(request.entity_id, request.event_time, dict(request.values))
    offline.write(row)
    return serialize(row)


@app.get("/offline/{entity_id}")
def offline_features(entity_id: str, as_of: datetime) -> dict:
    try:
        row = offline.get_as_of(entity_id, as_of)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize(row)


@app.get("/online/{entity_id}")
def online_features(entity_id: str) -> dict:
    try:
        values = online.get(entity_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="entity not materialized") from exc
    return {"entity_id": entity_id, "values": values}


@app.post("/materialize")
def materialize(limit: int = Query(default=1000, ge=1, le=10000)) -> dict:
    watermark = offline.get_watermark()
    changed = offline.changed_since(watermark, limit=limit)
    for row in changed:
        online.materialize(row)
    if changed:
        offline.set_watermark(max(row.event_time for row in changed))
    return {
        "rows_materialized": len(changed),
        "previous_watermark": watermark.isoformat() if watermark else None,
        "new_watermark": (
            max(row.event_time for row in changed).isoformat()
            if changed
            else (watermark.isoformat() if watermark else None)
        ),
    }


@app.get("/freshness")
def freshness() -> dict:
    watermark = offline.get_watermark()
    now = datetime.now(timezone.utc)
    lag_seconds = None
    if watermark is not None:
        normalized = watermark if watermark.tzinfo else watermark.replace(tzinfo=timezone.utc)
        lag_seconds = max(0.0, (now - normalized.astimezone(timezone.utc)).total_seconds())
    return {
        "materialization_watermark": watermark.isoformat() if watermark else None,
        "lag_seconds": lag_seconds,
    }
