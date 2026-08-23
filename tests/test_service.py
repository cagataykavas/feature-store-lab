from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app.api as api_module
from feature_store import OnlineFeatureStore
from storage.sqlite_store import SQLiteOfflineFeatureStore


def client_for(tmp_path: Path) -> TestClient:
    api_module.offline = SQLiteOfflineFeatureStore(tmp_path / "features.db")
    api_module.online = OnlineFeatureStore()
    return TestClient(api_module.app)


def test_point_in_time_read_prevents_future_leakage(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    rows = [
        {
            "entity_id": "customer-42",
            "event_time": "2026-01-01T00:00:00+00:00",
            "values": {"txn_30d": 8, "avg_amount": 74.5},
        },
        {
            "entity_id": "customer-42",
            "event_time": "2026-02-01T00:00:00+00:00",
            "values": {"txn_30d": 15, "avg_amount": 91.2},
        },
    ]
    for row in rows:
        assert client.post("/features", json=row).status_code == 201

    response = client.get(
        "/offline/customer-42",
        params={"as_of": "2026-01-15T00:00:00+00:00"},
    )
    assert response.status_code == 200
    assert response.json()["values"]["txn_30d"] == 8


def test_materialization_populates_online_store(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    client.post(
        "/features",
        json={
            "entity_id": "customer-7",
            "event_time": datetime.now(timezone.utc).isoformat(),
            "values": {"risk_score": 0.71},
        },
    )

    materialized = client.post("/materialize")
    assert materialized.status_code == 200
    assert materialized.json()["rows_materialized"] == 1

    online = client.get("/online/customer-7")
    assert online.status_code == 200
    assert online.json()["values"]["risk_score"] == 0.71
