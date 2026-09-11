from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from feature_platform.errors import FeaturePlatformError
from feature_platform.models import FeatureDefinition, FeatureType, FeatureView
from feature_platform.service import FeaturePlatform
from feature_platform.store import SQLiteFeatureRepository


class FeatureDefinitionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    dtype: FeatureType
    nullable: bool = False
    description: str = Field(default="", max_length=500)


class FeatureViewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    entity_type: str = Field(min_length=1, max_length=100)
    features: list[FeatureDefinitionRequest] = Field(min_length=1)
    ttl_seconds: int = Field(default=3600, ge=1)
    description: str = Field(default="", max_length=1000)


class FeatureWriteRequest(BaseModel):
    view_name: str = Field(min_length=1, max_length=100)
    view_version: int | None = Field(default=None, ge=1)
    entity_id: str = Field(min_length=1, max_length=200)
    event_time: datetime
    created_at: datetime | None = None
    ingestion_id: str | None = Field(default=None, min_length=1, max_length=200)
    values: dict[str, Any]


class TrainingExampleRequest(BaseModel):
    entity_id: str = Field(min_length=1, max_length=200)
    label_time: datetime


class TrainingSetRequest(BaseModel):
    view_name: str = Field(min_length=1, max_length=100)
    view_version: int | None = Field(default=None, ge=1)
    created_before: datetime | None = None
    examples: list[TrainingExampleRequest] = Field(min_length=1, max_length=10_000)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    path = Path(database_path or os.getenv("FEATURE_STORE_DATABASE_PATH", "features.db"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.platform = FeaturePlatform(SQLiteFeatureRepository(path))
        yield

    application = FastAPI(
        title="Feature Platform Control Plane",
        version="1.0.0",
        description=(
            "Versioned schemas, immutable ingestion, point-in-time training joins, "
            "transactional materialization, freshness and online/offline parity."
        ),
        lifespan=lifespan,
    )

    @application.exception_handler(FeaturePlatformError)
    async def domain_error(_: Request, exc: FeaturePlatformError):
        return JSONResponse(
            status_code=409,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    @application.get("/health")
    def health(request: Request) -> dict[str, str]:
        _platform(request).repository.list_views()
        return {"status": "ok"}

    @application.post("/v1/views", status_code=201)
    def register_view(payload: FeatureViewRequest, request: Request) -> dict[str, Any]:
        view = FeatureView(
            name=payload.name,
            version=payload.version,
            entity_type=payload.entity_type,
            features=tuple(
                FeatureDefinition(
                    name=feature.name,
                    dtype=feature.dtype,
                    nullable=feature.nullable,
                    description=feature.description,
                )
                for feature in payload.features
            ),
            ttl_seconds=payload.ttl_seconds,
            description=payload.description,
        )
        return _platform(request).register(view).as_dict()

    @application.get("/v1/views")
    def list_views(request: Request) -> list[dict[str, Any]]:
        return [view.as_dict() for view in _platform(request).repository.list_views()]

    @application.post("/v1/features", status_code=201)
    def write_feature(payload: FeatureWriteRequest, request: Request) -> dict[str, Any]:
        row = _platform(request).ingest(
            payload.view_name,
            payload.entity_id,
            payload.event_time,
            payload.values,
            view_version=payload.view_version,
            ingestion_id=payload.ingestion_id,
            created_at=payload.created_at,
        )
        return _serialize_row(row)

    @application.get("/v1/offline/{view_name}/{entity_id}")
    def offline_features(
        view_name: str,
        entity_id: str,
        as_of: datetime,
        request: Request,
        version: int | None = Query(default=None, ge=1),
        created_before: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            row = _platform(request).repository.get_as_of(
                view_name,
                entity_id,
                as_of,
                version=version,
                created_before=created_before,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _serialize_row(row)

    @application.post("/v1/training-set")
    def training_set(payload: TrainingSetRequest, request: Request) -> dict[str, Any]:
        examples = _platform(request).training_set(
            payload.view_name,
            ((item.entity_id, item.label_time) for item in payload.examples),
            view_version=payload.view_version,
            created_before=payload.created_before,
        )
        return {
            "view_name": payload.view_name,
            "rows": [
                {
                    "entity_id": item.entity_id,
                    "label_time": item.label_time.isoformat(),
                    "feature_event_time": (
                        item.feature_event_time.isoformat() if item.feature_event_time else None
                    ),
                    "features": item.features,
                    "row_sequence": item.row_sequence,
                }
                for item in examples
            ],
            "missing_count": sum(item.features is None for item in examples),
        }

    @application.post("/v1/materializations/{job_name}")
    def materialize(
        job_name: str,
        request: Request,
        owner: str = Query(min_length=1, max_length=100),
        limit: int = Query(default=1000, ge=1, le=10_000),
    ) -> dict[str, Any]:
        return _platform(request).repository.materialize(
            job_name=job_name, owner=owner, limit=limit
        )

    @application.get("/v1/online/{view_name}/{entity_id}")
    def online_features(view_name: str, entity_id: str, request: Request) -> dict[str, Any]:
        try:
            result = _platform(request).repository.get_online(view_name, entity_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **_serialize_row(result.row),
            "materialized_at": result.materialized_at.isoformat(),
            "stale": result.stale,
            "age_seconds": result.age_seconds,
        }

    @application.get("/v1/parity/{view_name}/{entity_id}")
    def parity(view_name: str, entity_id: str, request: Request) -> dict[str, Any]:
        try:
            return _platform(request).repository.parity(view_name, entity_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return application


def _platform(request: Request) -> FeaturePlatform:
    return request.app.state.platform


def _serialize_row(row) -> dict[str, Any]:
    return {
        "view_name": row.view_name,
        "view_version": row.view_version,
        "entity_id": row.entity_id,
        "event_time": row.event_time.isoformat(),
        "created_at": row.created_at.isoformat(),
        "values": row.values,
        "ingestion_id": row.ingestion_id,
        "sequence": row.sequence,
    }


app = create_app()
