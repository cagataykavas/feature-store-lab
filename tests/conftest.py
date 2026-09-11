from datetime import UTC, datetime
from pathlib import Path

import pytest

from feature_platform.models import FeatureDefinition, FeatureType, FeatureView
from feature_platform.service import FeaturePlatform
from feature_platform.store import SQLiteFeatureRepository


@pytest.fixture
def platform(tmp_path: Path) -> FeaturePlatform:
    service = FeaturePlatform(SQLiteFeatureRepository(tmp_path / "features.db"))
    service.register(
        FeatureView(
            name="customer_risk",
            version=1,
            entity_type="customer",
            ttl_seconds=3600,
            features=(
                FeatureDefinition("txn_30d", FeatureType.INTEGER),
                FeatureDefinition("avg_amount", FeatureType.FLOAT),
                FeatureDefinition("segment", FeatureType.STRING, nullable=True),
            ),
        )
    )
    return service


@pytest.fixture
def timestamp():
    def build(day: int, hour: int = 0) -> datetime:
        return datetime(2026, 1, day, hour, tzinfo=UTC)

    return build
