from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app


def test_api_supports_registry_ingestion_training_and_serving(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.db")) as client:
        view = {
            "name": "risk",
            "version": 1,
            "entity_type": "customer",
            "ttl_seconds": 3600,
            "features": [{"name": "score", "dtype": "float"}],
        }
        assert client.post("/v1/views", json=view).status_code == 201
        feature = {
            "view_name": "risk",
            "entity_id": "c-1",
            "event_time": "2026-01-01T00:00:00Z",
            "ingestion_id": "source-1",
            "values": {"score": 0.72},
        }
        written = client.post("/v1/features", json=feature)
        assert written.status_code == 201
        assert written.json()["sequence"] == 1

        training = client.post(
            "/v1/training-set",
            json={
                "view_name": "risk",
                "examples": [
                    {"entity_id": "c-1", "label_time": "2026-01-02T00:00:00Z"},
                    {"entity_id": "missing", "label_time": "2026-01-02T00:00:00Z"},
                ],
            },
        )
        assert training.status_code == 200
        assert training.json()["missing_count"] == 1
        assert training.json()["rows"][0]["features"]["score"] == 0.72

        materialized = client.post(
            "/v1/materializations/online", params={"owner": "api-worker", "limit": 10}
        )
        assert materialized.json()["new_cursor"] == 1
        online = client.get("/v1/online/risk/c-1")
        assert online.status_code == 200
        assert online.json()["values"]["score"] == 0.72
        assert client.get("/v1/parity/risk/c-1").json()["matches"] is True


def test_api_returns_typed_conflict_for_mutation(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post(
            "/v1/views",
            json={
                "name": "risk",
                "version": 1,
                "entity_type": "customer",
                "features": [{"name": "score", "dtype": "float"}],
            },
        )
        base = {
            "view_name": "risk",
            "entity_id": "c-1",
            "event_time": "2026-01-01T00:00:00Z",
            "values": {"score": 0.1},
        }
        client.post("/v1/features", json=base)
        base["values"] = {"score": 0.9}
        conflict = client.post("/v1/features", json=base)
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "FeatureMutationError"
