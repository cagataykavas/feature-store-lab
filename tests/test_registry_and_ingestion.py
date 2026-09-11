import pytest

from feature_platform.errors import (
    FeatureMutationError,
    FeatureValidationError,
    IncompatibleSchemaError,
)
from feature_platform.models import FeatureDefinition, FeatureType, FeatureView


def test_registering_same_schema_is_idempotent(platform) -> None:
    original = platform.repository.get_view("customer_risk")
    assert platform.register(original).fingerprint == original.fingerprint
    assert len(platform.repository.list_views()) == 1


def test_registered_schema_version_is_immutable(platform) -> None:
    with pytest.raises(FeatureMutationError, match="immutable"):
        platform.register(
            FeatureView(
                name="customer_risk",
                version=1,
                entity_type="customer",
                features=(FeatureDefinition("different", FeatureType.FLOAT),),
            )
        )


@pytest.mark.parametrize(
    "features, message",
    [
        ((FeatureDefinition("avg_amount", FeatureType.INTEGER),), "cannot be removed"),
        (
            (
                FeatureDefinition("txn_30d", FeatureType.FLOAT),
                FeatureDefinition("avg_amount", FeatureType.FLOAT),
                FeatureDefinition("segment", FeatureType.STRING, nullable=True),
            ),
            "cannot change type",
        ),
        (
            (
                FeatureDefinition("txn_30d", FeatureType.INTEGER),
                FeatureDefinition("avg_amount", FeatureType.FLOAT),
                FeatureDefinition("segment", FeatureType.STRING, nullable=True),
                FeatureDefinition("required_new", FeatureType.BOOLEAN),
            ),
            "must be nullable",
        ),
    ],
)
def test_breaking_schema_evolution_is_rejected(platform, features, message) -> None:
    with pytest.raises(IncompatibleSchemaError, match=message):
        platform.register(
            FeatureView(
                name="customer_risk",
                version=2,
                entity_type="customer",
                features=features,
            )
        )


def test_nullable_feature_can_be_added_compatibly(platform) -> None:
    current = platform.repository.get_view("customer_risk")
    successor = FeatureView(
        name=current.name,
        version=2,
        entity_type=current.entity_type,
        features=(
            *current.features,
            FeatureDefinition("is_vip", FeatureType.BOOLEAN, nullable=True),
        ),
    )
    platform.register(successor)
    assert platform.repository.get_view("customer_risk").version == 2


def test_values_are_validated_against_registered_schema(platform, timestamp) -> None:
    with pytest.raises(FeatureValidationError, match="expected integer"):
        platform.ingest(
            "customer_risk",
            "c-1",
            timestamp(1),
            {"txn_30d": 3.5, "avg_amount": 7.0},
        )
    with pytest.raises(FeatureValidationError, match="unknown features"):
        platform.ingest(
            "customer_risk",
            "c-1",
            timestamp(1),
            {"txn_30d": 3, "avg_amount": 7.0, "secret": 99},
        )


def test_ingestion_id_and_logical_key_are_idempotent_but_immutable(platform, timestamp) -> None:
    first = platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 3, "avg_amount": 7.0},
        ingestion_id="event-1",
    )
    replay = platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 3, "avg_amount": 7.0},
        ingestion_id="event-1",
    )
    assert replay.sequence == first.sequence

    with pytest.raises(FeatureMutationError, match="different content"):
        platform.ingest(
            "customer_risk",
            "c-2",
            timestamp(1),
            {"txn_30d": 4, "avg_amount": 9.0},
            ingestion_id="event-1",
        )
    with pytest.raises(FeatureMutationError, match="immutable"):
        platform.ingest(
            "customer_risk",
            "c-1",
            timestamp(1),
            {"txn_30d": 99, "avg_amount": 7.0},
        )
