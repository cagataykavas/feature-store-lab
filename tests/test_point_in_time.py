def test_point_in_time_join_never_reads_future_event(platform, timestamp) -> None:
    platform.ingest("customer_risk", "c-1", timestamp(1), {"txn_30d": 3, "avg_amount": 10.0})
    platform.ingest("customer_risk", "c-1", timestamp(3), {"txn_30d": 9, "avg_amount": 30.0})

    joined = platform.training_set("customer_risk", [("c-1", timestamp(2))])
    assert joined[0].features == {"txn_30d": 3, "avg_amount": 10.0}
    assert joined[0].feature_event_time == timestamp(1)


def test_created_before_cutoff_prevents_backfill_leakage(platform, timestamp) -> None:
    platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(1),
        {"txn_30d": 3, "avg_amount": 10.0},
        created_at=timestamp(1, 1),
    )
    platform.ingest(
        "customer_risk",
        "c-1",
        timestamp(2),
        {"txn_30d": 99, "avg_amount": 50.0},
        created_at=timestamp(5),
    )

    joined = platform.training_set(
        "customer_risk",
        [("c-1", timestamp(3))],
        created_before=timestamp(4),
    )
    assert joined[0].features["txn_30d"] == 3


def test_batch_join_preserves_order_and_marks_missing_entities(platform, timestamp) -> None:
    platform.ingest("customer_risk", "c-2", timestamp(1), {"txn_30d": 8, "avg_amount": 40.0})
    joined = platform.training_set(
        "customer_risk",
        [("missing", timestamp(2)), ("c-2", timestamp(2))],
    )
    assert [row.entity_id for row in joined] == ["missing", "c-2"]
    assert joined[0].features is None
    assert joined[1].features["txn_30d"] == 8
