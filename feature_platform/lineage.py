"""Deterministic lineage manifests for point-in-time training sets."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from feature_platform.models import FeatureView, TrainingExample

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class ManifestPolicy:
    """Resource and completeness limits fixed before dataset construction."""

    max_examples: int = 100_000
    max_missing_fraction: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_examples <= 1_000_000:
            raise ValueError("max_examples must be in [1, 1_000_000]")
        if not math.isfinite(self.max_missing_fraction) or not (
            0.0 <= self.max_missing_fraction <= 1.0
        ):
            raise ValueError("max_missing_fraction must be finite and in [0, 1]")


@dataclass(frozen=True, slots=True)
class TrainingSetManifest:
    schema_version: int
    dataset_id: str
    view_name: str
    view_version: int
    view_fingerprint: str
    created_before: str
    example_count: int
    matched_count: int
    missing_count: int
    missing_fraction: float
    request_sha256: str
    selected_rows_sha256: str
    policy_sha256: str
    manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ManifestComparison:
    reproducible: bool
    reasons: tuple[str, ...]
    expected_manifest_sha256: str
    candidate_manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_training_set_manifest(
    view: FeatureView,
    examples: Iterable[TrainingExample],
    *,
    example_ids: Sequence[str],
    dataset_id: str,
    created_before: datetime,
    policy: ManifestPolicy | None = None,
) -> TrainingSetManifest:
    """Bind a training request to its exact point-in-time feature selections.

    The manifest contains only aggregate counts and digests. Input ordering is
    intentionally significant because row order can affect model training.
    """

    active_policy = policy or ManifestPolicy()
    rows = list(examples)
    ids = list(example_ids)
    if not _IDENTIFIER.fullmatch(dataset_id):
        raise ValueError("dataset_id must be a bounded stable identifier")
    if not rows:
        raise ValueError("at least one training example is required")
    if len(rows) != len(ids):
        raise ValueError("example_ids and examples must have equal length")
    if len(rows) > active_policy.max_examples:
        raise ValueError("training set exceeds max_examples")
    if len(ids) != len(set(ids)):
        raise ValueError("example_ids must be unique")

    cutoff = _aware_utc(created_before, "created_before")
    request_records: list[dict[str, Any]] = []
    selection_records: list[dict[str, Any]] = []
    missing_count = 0

    for example_id, row in zip(ids, rows, strict=True):
        if not _IDENTIFIER.fullmatch(example_id):
            raise ValueError("example_ids must be bounded stable identifiers")
        if not row.entity_id:
            raise ValueError("training example entity_id must not be empty")
        label_time = _aware_utc(row.label_time, "label_time")
        request_records.append(
            {
                "example_id": example_id,
                "entity_id": row.entity_id,
                "label_time": label_time.isoformat(),
            }
        )

        if row.features is None:
            if row.feature_event_time is not None or row.row_sequence is not None:
                raise ValueError("missing features cannot reference a source row")
            missing_count += 1
            selection_records.append({"example_id": example_id, "missing": True})
            continue

        if row.feature_event_time is None or row.row_sequence is None:
            raise ValueError("matched features require event time and row sequence")
        feature_time = _aware_utc(row.feature_event_time, "feature_event_time")
        if feature_time > label_time:
            raise ValueError("feature_event_time cannot be later than label_time")
        if row.row_sequence < 1:
            raise ValueError("row_sequence must be positive")
        view.validate_values(row.features)
        feature_payload = _canonical_json(row.features)
        selection_records.append(
            {
                "example_id": example_id,
                "missing": False,
                "feature_event_time": feature_time.isoformat(),
                "row_sequence": row.row_sequence,
                "feature_values_sha256": _sha256(feature_payload),
            }
        )

    missing_fraction = missing_count / len(rows)
    if missing_fraction > active_policy.max_missing_fraction:
        raise ValueError("training set exceeds max_missing_fraction")

    policy_digest = _json_digest(asdict(active_policy))
    payload = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "view_name": view.name,
        "view_version": view.version,
        "view_fingerprint": view.fingerprint,
        "created_before": cutoff.isoformat(),
        "example_count": len(rows),
        "matched_count": len(rows) - missing_count,
        "missing_count": missing_count,
        "missing_fraction": missing_fraction,
        "request_sha256": _json_digest(request_records),
        "selected_rows_sha256": _json_digest(selection_records),
        "policy_sha256": policy_digest,
    }
    return TrainingSetManifest(**payload, manifest_sha256=_json_digest(payload))


def compare_training_set_manifests(
    expected: TrainingSetManifest, candidate: TrainingSetManifest
) -> ManifestComparison:
    """Explain why a rebuilt dataset does or does not reproduce its reference."""

    reasons: list[str] = []
    if expected.dataset_id != candidate.dataset_id:
        reasons.append("dataset_identity_mismatch")
    if (
        expected.view_name,
        expected.view_version,
        expected.view_fingerprint,
    ) != (
        candidate.view_name,
        candidate.view_version,
        candidate.view_fingerprint,
    ):
        reasons.append("view_contract_mismatch")
    if expected.created_before != candidate.created_before:
        reasons.append("cutoff_mismatch")
    if expected.request_sha256 != candidate.request_sha256:
        reasons.append("training_request_mismatch")
    if expected.selected_rows_sha256 != candidate.selected_rows_sha256:
        reasons.append("selected_rows_mismatch")
    if expected.policy_sha256 != candidate.policy_sha256:
        reasons.append("manifest_policy_mismatch")
    if expected.manifest_sha256 != candidate.manifest_sha256 and not reasons:
        reasons.append("manifest_identity_mismatch")
    return ManifestComparison(
        reproducible=not reasons,
        reasons=tuple(reasons),
        expected_manifest_sha256=expected.manifest_sha256,
        candidate_manifest_sha256=candidate.manifest_sha256,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("manifest values must be finite and JSON-serializable") from error


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_digest(payload: Any) -> str:
    return _sha256(_canonical_json(payload))
