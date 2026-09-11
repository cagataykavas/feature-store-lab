from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

from feature_platform.models import FeatureRow, FeatureView, TrainingExample
from feature_platform.store import SQLiteFeatureRepository


class FeaturePlatform:
    """Application service; transports do not own feature-store policy."""

    def __init__(self, repository: SQLiteFeatureRepository) -> None:
        self.repository = repository

    def register(self, view: FeatureView) -> FeatureView:
        return self.repository.register_view(view)

    def ingest(
        self,
        view_name: str,
        entity_id: str,
        event_time: datetime,
        values: dict[str, Any],
        *,
        view_version: int | None = None,
        ingestion_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FeatureRow:
        view = self.repository.get_view(view_name, view_version)
        kwargs: dict[str, Any] = {}
        if created_at is not None:
            kwargs["created_at"] = created_at
        return self.repository.write(
            FeatureRow(
                view_name=view.name,
                view_version=view.version,
                entity_id=entity_id,
                event_time=event_time,
                values=dict(values),
                ingestion_id=ingestion_id,
                **kwargs,
            )
        )

    def training_set(
        self,
        view_name: str,
        examples: Iterable[tuple[str, datetime]],
        *,
        view_version: int | None = None,
        created_before: datetime | None = None,
    ) -> list[TrainingExample]:
        return self.repository.point_in_time_join(
            view_name,
            examples,
            version=view_version,
            created_before=created_before,
        )

    def materialize(self, limit: int = 1000, *, owner: str | None = None) -> dict[str, Any]:
        return self.repository.materialize(owner=owner or f"worker-{uuid4()}", limit=limit)
