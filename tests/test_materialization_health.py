from __future__ import annotations

from datetime import timedelta

import pytest

from feature_platform.materialization_health import MaterializationHealthPolicy


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_policy_rejects_non_finite_age_threshold(value: float) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        MaterializationHealthPolicy(maximum_oldest_pending_seconds=value)


def test_empty_pipeline_is_healthy(platform, timestamp) -> None:
    report = platform.materialization_health(
        now=timestamp(1),
        maximum_backlog_rows=0,
        maximum_oldest_pending_seconds=0,
    )

    assert report.ok
    assert report.snapshot.cursor_sequence == 0
    assert report.snapshot.source_max_sequence == 0
    assert report.snapshot.backlog_rows == 0


def test_backlog_rows_and_age_fail_independently(platform, timestamp) -> None:
    platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 1, "avg_amount": 10.0},
        created_at=timestamp(1),
    )

    report = platform.materialization_health(
        now=timestamp(1) + timedelta(seconds=20),
        maximum_backlog_rows=0,
        maximum_oldest_pending_seconds=10,
    )

    assert not report.ok
    assert {issue.code for issue in report.issues} == {
        "backlog.rows_exceeded",
        "backlog.age_exceeded",
    }
    assert report.snapshot.oldest_pending_age_seconds == 20


def test_partial_materialization_reduces_backlog(platform, timestamp) -> None:
    for index in range(3):
        platform.ingest(
            "customer_risk",
            f"c-{index}",
            timestamp(1),
            {"txn_30d": index, "avg_amount": float(index)},
            created_at=timestamp(1),
        )

    platform.repository.materialize(owner="worker", limit=2, now=timestamp(2))
    report = platform.materialization_health(
        now=timestamp(2),
        maximum_backlog_rows=1,
        maximum_oldest_pending_seconds=100_000,
    )

    assert report.ok
    assert report.snapshot.cursor_sequence == 2
    assert report.snapshot.source_max_sequence == 3
    assert report.snapshot.backlog_rows == 1


def test_complete_materialization_clears_pending_age(platform, timestamp) -> None:
    platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 1, "avg_amount": 1.0},
        created_at=timestamp(1),
    )
    platform.repository.materialize(owner="worker", now=timestamp(2))

    report = platform.materialization_health(
        now=timestamp(2),
        maximum_backlog_rows=0,
        maximum_oldest_pending_seconds=0,
    )

    assert report.ok
    assert report.snapshot.backlog_rows == 0
    assert report.snapshot.oldest_pending_age_seconds is None


def test_cursor_ahead_of_source_fails_closed(platform, timestamp) -> None:
    with platform.repository._connect() as connection:
        connection.execute(
            """
            INSERT INTO materialization_jobs(
                name, cursor_sequence, lease_owner, lease_until, updated_at
            ) VALUES (?, ?, NULL, NULL, ?)
            """,
            ("online", 99, timestamp(1).isoformat()),
        )

    report = platform.materialization_health(now=timestamp(1))

    assert not report.ok
    assert report.issues[0].code == "cursor.ahead_of_source"


def test_active_lease_is_observable_without_being_a_failure(platform, timestamp) -> None:
    with platform.repository._connect() as connection:
        connection.execute(
            """
            INSERT INTO materialization_jobs(
                name, cursor_sequence, lease_owner, lease_until, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "online",
                0,
                "worker-a",
                (timestamp(1) + timedelta(seconds=30)).isoformat(),
                timestamp(1).isoformat(),
            ),
        )

    report = platform.materialization_health(now=timestamp(1))

    assert report.ok
    assert report.snapshot.lease_active
    assert report.snapshot.lease_owner == "worker-a"
