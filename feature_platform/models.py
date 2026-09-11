from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from feature_platform.errors import FeatureValidationError, IncompatibleSchemaError


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FeatureType(StrEnum):
    FLOAT = "float"
    INTEGER = "integer"
    STRING = "string"
    BOOLEAN = "boolean"

    def accepts(self, value: Any) -> bool:
        if self is FeatureType.FLOAT:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self is FeatureType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if self is FeatureType.STRING:
            return isinstance(value, str)
        return isinstance(value, bool)


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    dtype: FeatureType
    nullable: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise FeatureValidationError(f"invalid feature name: {self.name!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype.value,
            "nullable": self.nullable,
            "description": self.description,
        }


@dataclass(frozen=True)
class FeatureView:
    name: str
    version: int
    entity_type: str
    features: tuple[FeatureDefinition, ...]
    ttl_seconds: int = 3600
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or self.version < 1 or not self.entity_type:
            raise FeatureValidationError(
                "view name, entity type, and positive version are required"
            )
        if not self.features:
            raise FeatureValidationError("a feature view must define at least one feature")
        names = [feature.name for feature in self.features]
        if len(names) != len(set(names)):
            raise FeatureValidationError("feature names must be unique")
        if self.ttl_seconds < 1:
            raise FeatureValidationError("ttl_seconds must be positive")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entity_type": self.entity_type,
            "features": [feature.as_dict() for feature in self.features],
            "ttl_seconds": self.ttl_seconds,
            "description": self.description,
        }

    def validate_values(self, values: dict[str, Any]) -> None:
        definitions = {feature.name: feature for feature in self.features}
        unknown = sorted(set(values) - set(definitions))
        missing = sorted(
            name
            for name, definition in definitions.items()
            if name not in values and not definition.nullable
        )
        if unknown:
            raise FeatureValidationError(f"unknown features: {', '.join(unknown)}")
        if missing:
            raise FeatureValidationError(f"missing required features: {', '.join(missing)}")
        for name, value in values.items():
            definition = definitions[name]
            if value is None and definition.nullable:
                continue
            if not definition.dtype.accepts(value):
                raise FeatureValidationError(
                    f"feature {name!r} expected {definition.dtype.value}, got {type(value).__name__}"
                )

    def assert_compatible_successor(self, successor: FeatureView) -> None:
        if successor.name != self.name or successor.version <= self.version:
            raise IncompatibleSchemaError("successor must keep the name and increase the version")
        if successor.entity_type != self.entity_type:
            raise IncompatibleSchemaError("entity type cannot change")
        previous = {feature.name: feature for feature in self.features}
        current = {feature.name: feature for feature in successor.features}
        for name, definition in previous.items():
            candidate = current.get(name)
            if candidate is None:
                raise IncompatibleSchemaError(f"existing feature {name!r} cannot be removed")
            if candidate.dtype is not definition.dtype:
                raise IncompatibleSchemaError(f"feature {name!r} cannot change type")
            if definition.nullable and not candidate.nullable:
                raise IncompatibleSchemaError(f"feature {name!r} cannot become required")
        new_required = sorted(
            name
            for name, definition in current.items()
            if name not in previous and not definition.nullable
        )
        if new_required:
            raise IncompatibleSchemaError(
                "new features must be nullable for compatibility: " + ", ".join(new_required)
            )


@dataclass(frozen=True)
class FeatureRow:
    view_name: str
    view_version: int
    entity_id: str
    event_time: datetime
    values: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ingestion_id: str | None = None
    sequence: int | None = None

    @property
    def content_hash(self) -> str:
        payload = {
            "view_name": self.view_name,
            "view_version": self.view_version,
            "entity_id": self.entity_id,
            "event_time": utc(self.event_time).isoformat(),
            "values": self.values,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class OnlineFeature:
    row: FeatureRow
    materialized_at: datetime
    stale: bool
    age_seconds: float


@dataclass(frozen=True)
class TrainingExample:
    entity_id: str
    label_time: datetime
    features: dict[str, Any] | None
    feature_event_time: datetime | None
    row_sequence: int | None
