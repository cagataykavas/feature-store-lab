# Reproducible training-set lineage

A point-in-time join prevents obvious future leakage, but it does not by itself prove that a
later rebuild selected the same source rows. Backfills, schema activation, request reordering,
or a changed cutoff can silently produce a different model input while the dataset name stays
the same.

`feature_platform.lineage` creates a deterministic manifest that binds:

- the dataset identifier and ordered example identifiers;
- every entity/label-time request;
- the exact feature-view version and schema fingerprint;
- the required ingestion cutoff used to exclude later backfills;
- every selected source-row sequence, event time, and feature-value digest;
- missing-row counts and the pre-registered completeness policy.

```python
from feature_platform.lineage import (
    ManifestPolicy,
    build_training_set_manifest,
    compare_training_set_manifests,
)

reference = build_training_set_manifest(
    view,
    joined_examples,
    example_ids=label_ids,
    dataset_id="credit-training:2026-10",
    created_before=warehouse_cutoff,
    policy=ManifestPolicy(max_missing_fraction=0.001),
)

candidate = build_training_set_manifest(
    view,
    rebuilt_examples,
    example_ids=label_ids,
    dataset_id="credit-training:2026-10",
    created_before=warehouse_cutoff,
    policy=ManifestPolicy(max_missing_fraction=0.001),
)

comparison = compare_training_set_manifests(reference, candidate)
if not comparison.reproducible:
    block_model_promotion(comparison.reasons)
```

## Why ordering is bound

The request and selected-row digests are intentionally order-sensitive. Even when two datasets
contain the same rows, a different order can change stochastic batching, validation splits, and
training outcomes. Callers that require a canonical order should sort by a documented stable
sample key before building both the dataset and manifest.

## Privacy and trust boundary

The manifest exposes counts and aggregate SHA-256 identities, not entity IDs or feature values.
That does not make it anonymous: hashes do not authenticate the producer and can leak facts when
the possible input space is small. Store manifests under the same access controls as model and
dataset metadata, and sign or append-only persist them when producer authenticity matters.

## Operational limitations

- The manifest proves byte-level selection identity under this canonicalization contract; it
  does not prove label correctness, sampling validity, or model quality.
- `created_before` is mandatory because reproducibility without a backfill boundary is
  underspecified.
- The current aggregate digest requires rebuilding the selected examples. Large distributed
  datasets should compute compatible ordered partition manifests and a signed root manifest.
- Feature values must be finite and JSON-serializable. Binary/tensor features need an explicit
  content-addressed encoding contract before inclusion.

## Next integration step

Persist the manifest beside each trained model, require its digest at promotion time, and compare
it with a clean rebuild before retraining or rollback.
