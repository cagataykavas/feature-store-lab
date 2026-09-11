from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from feature_platform.models import FeatureDefinition, FeatureType, FeatureView
from feature_platform.service import FeaturePlatform
from feature_platform.store import SQLiteFeatureRepository


def generate_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="feature-platform-") as directory:
        platform = FeaturePlatform(SQLiteFeatureRepository(Path(directory) / "evidence.db"))
        platform.register(
            FeatureView(
                name="customer_risk",
                version=1,
                entity_type="customer",
                ttl_seconds=3600,
                features=(
                    FeatureDefinition("txn_30d", FeatureType.INTEGER),
                    FeatureDefinition("avg_amount", FeatureType.FLOAT),
                ),
            )
        )
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            ("customer-1", base, {"txn_30d": 5, "avg_amount": 42.5}),
            ("customer-2", base, {"txn_30d": 8, "avg_amount": 75.0}),
            ("customer-3", base, {"txn_30d": 13, "avg_amount": 110.0}),
            (
                "customer-1",
                base + timedelta(days=2),
                {"txn_30d": 9, "avg_amount": 63.0},
            ),
        ]
        for index, (entity_id, event_time, values) in enumerate(rows):
            platform.ingest(
                "customer_risk",
                entity_id,
                event_time,
                values,
                ingestion_id=f"source-event-{index}",
            )

        first_batch = platform.repository.materialize(
            owner="evidence-worker", limit=2, now=base + timedelta(days=3)
        )
        second_batch = platform.repository.materialize(
            owner="evidence-worker", limit=2, now=base + timedelta(days=3)
        )
        joined = platform.training_set(
            "customer_risk",
            [
                ("customer-1", base + timedelta(days=1)),
                ("customer-1", base + timedelta(days=3)),
                ("missing", base + timedelta(days=3)),
            ],
        )
        parity = {
            entity_id: platform.repository.parity("customer_risk", entity_id)
            for entity_id in ("customer-1", "customer-2", "customer-3")
        }
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "registered_views": [view.as_dict() for view in platform.repository.list_views()],
            "materialization": [first_batch, second_batch],
            "training_join": [
                {
                    "entity_id": row.entity_id,
                    "label_time": row.label_time.isoformat(),
                    "feature_event_time": (
                        row.feature_event_time.isoformat() if row.feature_event_time else None
                    ),
                    "features": row.features,
                }
                for row in joined
            ],
            "parity": parity,
            "invariants": {
                "same_timestamp_rows_preserved": second_batch["new_cursor"] == 4,
                "point_in_time_join_uses_past_only": joined[0].features["txn_30d"] == 5,
                "missing_entities_are_explicit": joined[2].features is None,
                "online_offline_parity": all(item["matches"] for item in parity.values()),
            },
        }


def main() -> None:
    print(json.dumps(generate_evidence(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
