from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from feature_platform.lineage import (
    ManifestPolicy,
    build_training_set_manifest,
    compare_training_set_manifests,
)
from feature_platform.models import TrainingExample


def _joined(platform, timestamp):
    platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 3, "avg_amount": 10.0},
        created_at=timestamp(1),
    )
    platform.ingest(
        "customer_risk",
        "c-2",
        timestamp(1),
        {"txn_30d": 8, "avg_amount": 40.0},
        created_at=timestamp(1),
    )
    return platform.training_set(
        "customer_risk",
        [("c-1", timestamp(1, 1)), ("c-2", timestamp(1, 1))],
        created_before=timestamp(1, 2),
    )


def _manifest(platform, timestamp, examples, **kwargs):
    return build_training_set_manifest(
        platform.repository.get_view("customer_risk"),
        examples,
        example_ids=kwargs.pop("example_ids", ["label-1", "label-2"]),
        dataset_id=kwargs.pop("dataset_id", "credit-train:v1"),
        created_before=kwargs.pop("created_before", timestamp(1, 2)),
        policy=kwargs.pop("policy", None),
        **kwargs,
    )


def test_exact_rebuild_has_same_manifest(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)

    first = _manifest(platform, timestamp, joined)
    second = _manifest(platform, timestamp, joined)
    comparison = compare_training_set_manifests(first, second)

    assert comparison.reproducible
    assert comparison.reasons == ()
    assert first.manifest_sha256 == second.manifest_sha256


def test_row_order_is_intentionally_bound(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)

    expected = _manifest(platform, timestamp, joined)
    candidate = _manifest(platform, timestamp, list(reversed(joined)))
    comparison = compare_training_set_manifests(expected, candidate)

    assert not comparison.reproducible
    assert "training_request_mismatch" in comparison.reasons
    assert "selected_rows_mismatch" in comparison.reasons


def test_changed_feature_selection_is_detected(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    changed = list(joined)
    changed[0] = replace(changed[0], features={"txn_30d": 4, "avg_amount": 10.0})

    comparison = compare_training_set_manifests(
        _manifest(platform, timestamp, joined),
        _manifest(platform, timestamp, changed),
    )

    assert comparison.reasons == ("selected_rows_mismatch",)


def test_cutoff_and_policy_changes_are_explained(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    expected = _manifest(platform, timestamp, joined)
    candidate = _manifest(
        platform,
        timestamp,
        joined,
        created_before=timestamp(1, 3),
        policy=ManifestPolicy(max_missing_fraction=0.1),
    )

    comparison = compare_training_set_manifests(expected, candidate)

    assert comparison.reasons == ("cutoff_mismatch", "manifest_policy_mismatch")


def test_missing_rows_are_explicit_and_policy_gated(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    joined[1] = TrainingExample("missing", timestamp(1, 1), None, None, None)

    with pytest.raises(ValueError, match="max_missing_fraction"):
        _manifest(platform, timestamp, joined)

    manifest = _manifest(
        platform,
        timestamp,
        joined,
        policy=ManifestPolicy(max_missing_fraction=0.5),
    )
    assert manifest.missing_count == 1
    assert manifest.missing_fraction == 0.5


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda row: replace(row, feature_event_time=row.label_time.replace(day=3)),
            "cannot be later",
        ),
        (lambda row: replace(row, row_sequence=0), "row_sequence must be positive"),
        (lambda row: replace(row, feature_event_time=None), "require event time"),
    ],
)
def test_invalid_selected_row_lineage_fails_closed(platform, timestamp, mutation, message) -> None:
    joined = _joined(platform, timestamp)
    joined[0] = mutation(joined[0])

    with pytest.raises(ValueError, match=message):
        _manifest(platform, timestamp, joined)


def test_manifest_requires_timezone_aware_cutoff(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)

    with pytest.raises(ValueError, match="timezone-aware"):
        _manifest(
            platform,
            timestamp,
            joined,
            created_before=datetime(2026, 1, 3),
        )


def test_duplicate_or_malformed_example_ids_fail_closed(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)

    with pytest.raises(ValueError, match="unique"):
        _manifest(platform, timestamp, joined, example_ids=["same", "same"])
    with pytest.raises(ValueError, match="bounded"):
        _manifest(platform, timestamp, joined, example_ids=["bad id", "valid"])


def test_resource_budget_is_enforced(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)

    with pytest.raises(ValueError, match="max_examples"):
        _manifest(platform, timestamp, joined, policy=ManifestPolicy(max_examples=1))


def test_manifest_exposes_digests_not_raw_training_values(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    manifest = _manifest(platform, timestamp, joined)
    payload = manifest.to_dict()

    assert "features" not in payload
    assert "entity_id" not in payload
    assert len(manifest.request_sha256) == 64
    assert len(manifest.selected_rows_sha256) == 64
    assert len(manifest.manifest_sha256) == 64


def test_naive_label_time_and_nonfinite_values_are_rejected(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    naive = replace(joined[0], label_time=datetime(2026, 1, 2))
    with pytest.raises(ValueError, match="label_time must be timezone-aware"):
        _manifest(platform, timestamp, [naive, joined[1]])

    invalid = replace(joined[0], features={"txn_30d": 3, "avg_amount": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        _manifest(platform, timestamp, [invalid, joined[1]])


def test_timezone_equivalent_inputs_have_same_identity(platform, timestamp) -> None:
    joined = _joined(platform, timestamp)
    utc_manifest = _manifest(platform, timestamp, joined)
    equivalent_cutoff = timestamp(1, 2).astimezone(UTC)
    equivalent_manifest = _manifest(
        platform,
        timestamp,
        joined,
        created_before=equivalent_cutoff,
    )

    assert utc_manifest.manifest_sha256 == equivalent_manifest.manifest_sha256
