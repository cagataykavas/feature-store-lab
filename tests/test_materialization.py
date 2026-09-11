from datetime import timedelta

import pytest

from feature_platform.errors import MaterializationLeaseError


def test_sequence_cursor_does_not_drop_rows_with_identical_event_times(platform, timestamp) -> None:
    for index in range(3):
        platform.ingest(
            "customer_risk",
            f"c-{index}",
            timestamp(1),
            {"txn_30d": index, "avg_amount": float(index)},
        )

    first = platform.repository.materialize(owner="worker-a", limit=2, now=timestamp(2))
    second = platform.repository.materialize(owner="worker-a", limit=2, now=timestamp(2))

    assert first["rows_scanned"] == 2
    assert second["rows_scanned"] == 1
    assert second["new_cursor"] == 3
    assert platform.repository.get_online("customer_risk", "c-2").row.values["txn_30d"] == 2


def test_late_older_event_cannot_roll_back_online_value(platform, timestamp) -> None:
    platform.ingest("customer_risk", "c-1", timestamp(3), {"txn_30d": 30, "avg_amount": 30.0})
    platform.repository.materialize(owner="worker", now=timestamp(4))
    platform.ingest("customer_risk", "c-1", timestamp(1), {"txn_30d": 10, "avg_amount": 10.0})
    platform.repository.materialize(owner="worker", now=timestamp(4))
    assert platform.repository.get_online("customer_risk", "c-1").row.values["txn_30d"] == 30


def test_freshness_is_derived_from_feature_view_ttl(platform, timestamp) -> None:
    platform.ingest("customer_risk", "c-1", timestamp(1), {"txn_30d": 3, "avg_amount": 10.0})
    platform.repository.materialize(owner="worker", now=timestamp(1))
    fresh = platform.repository.get_online(
        "customer_risk", "c-1", now=timestamp(1) + timedelta(seconds=3599)
    )
    stale = platform.repository.get_online(
        "customer_risk", "c-1", now=timestamp(1) + timedelta(seconds=3601)
    )
    assert fresh.stale is False
    assert stale.stale is True


def test_online_offline_parity_detects_unmaterialized_change(platform, timestamp) -> None:
    platform.ingest("customer_risk", "c-1", timestamp(1), {"txn_30d": 3, "avg_amount": 10.0})
    platform.repository.materialize(owner="worker", now=timestamp(2))
    assert platform.repository.parity("customer_risk", "c-1")["matches"] is True
    platform.ingest("customer_risk", "c-1", timestamp(3), {"txn_30d": 4, "avg_amount": 11.0})
    assert platform.repository.parity("customer_risk", "c-1")["matches"] is False


def test_active_lease_blocks_second_worker(platform, timestamp) -> None:
    connection = platform.repository._connect()
    with connection:
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
                (timestamp(2) + timedelta(seconds=30)).isoformat(),
                timestamp(2).isoformat(),
            ),
        )
    connection.close()

    with pytest.raises(MaterializationLeaseError, match="worker-a"):
        platform.repository.materialize(owner="worker-b", now=timestamp(2))
