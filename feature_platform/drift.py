"""Population Stability Index audit for numeric feature distributions."""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass

@dataclass(frozen=True)
class DriftBin:
    """One reference-derived bin in a PSI report."""

    lower: float | None
    upper: float | None
    reference_count: int
    current_count: int
    reference_share: float
    current_share: float
    psi_contribution: float
    missing: bool = False


@dataclass(frozen=True)
class DriftReport:
    """JSON-ready drift decision and its evidence."""

    psi: float
    severity: str
    reference_count: int
    current_count: int
    reference_missing_rate: float
    current_missing_rate: float
    missing_rate_delta: float
    bin_edges: tuple[float, ...]
    bins: tuple[DriftBin, ...]
    moderate_threshold: float
    significant_threshold: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_values(values: Iterable[float | None], name: str) -> list[float | None]:
    materialized = list(values)
    for index, value in enumerate(materialized):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name}[{index}] must be a finite number or None")
        if not math.isfinite(value):
            raise ValueError(f"{name}[{index}] must be finite")
    return materialized


def _quantile(values: list[float], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def _reference_edges(values: list[float], bin_count: int) -> tuple[float, ...]:
    ordered = sorted(values)
    if ordered[0] == ordered[-1]:
        value = ordered[0]
        return (math.nextafter(value, -math.inf), math.nextafter(value, math.inf))

    candidates = (_quantile(ordered, index / bin_count) for index in range(1, bin_count))
    return tuple(sorted(set(candidates)))


def audit_numeric_drift(
    reference: Iterable[float | None],
    current: Iterable[float | None],
    *,
    bin_count: int = 10,
    epsilon: float = 1e-6,
    minimum_samples: int = 20,
    moderate_threshold: float = 0.1,
    significant_threshold: float = 0.25,
) -> DriftReport:
    """Compare numeric distributions with reference-derived PSI bins.

    None is treated as an explicit missing-value bin. Bin edges are learned
    only from the reference sample so the audit is suitable for point-in-time
    monitoring and repeatable historical replay.
    """

    if not 2 <= bin_count <= 100:
        raise ValueError("bin_count must be between 2 and 100")
    if minimum_samples < 2:
        raise ValueError("minimum_samples must be at least 2")
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise ValueError("epsilon must be finite and greater than zero")
    if (
        not math.isfinite(moderate_threshold)
        or not math.isfinite(significant_threshold)
        or moderate_threshold <= 0
        or significant_threshold <= moderate_threshold
    ):
        raise ValueError("thresholds must be finite and 0 < moderate < significant")

    reference_values = _validate_values(reference, "reference")
    current_values = _validate_values(current, "current")
    if len(reference_values) < minimum_samples or len(current_values) < minimum_samples:
        raise ValueError(f"each sample must contain at least {minimum_samples} observations")

    reference_numeric = [float(value) for value in reference_values if value is not None]
    current_numeric = [float(value) for value in current_values if value is not None]
    if len(reference_numeric) < 2:
        raise ValueError("reference must contain at least two non-missing observations")
    if not current_numeric:
        raise ValueError("current must contain at least one non-missing observation")

    edges = _reference_edges(reference_numeric, bin_count)
    bucket_total = len(edges) + 2
    reference_counts = [0] * bucket_total
    current_counts = [0] * bucket_total

    for value in reference_values:
        index = len(edges) + 1 if value is None else bisect.bisect_right(edges, float(value))
        reference_counts[index] += 1
    for value in current_values:
        index = len(edges) + 1 if value is None else bisect.bisect_right(edges, float(value))
        current_counts[index] += 1

    reference_denominator = len(reference_values) + epsilon * bucket_total
    current_denominator = len(current_values) + epsilon * bucket_total
    bins: list[DriftBin] = []
    psi = 0.0

    for index, (reference_count, current_count) in enumerate(
        zip(reference_counts, current_counts, strict=True)
    ):
        reference_share = (reference_count + epsilon) / reference_denominator
        current_share = (current_count + epsilon) / current_denominator
        contribution = (current_share - reference_share) * math.log(
            current_share / reference_share
        )
        psi += contribution
        missing = index == bucket_total - 1
        lower = None if index == 0 or missing else edges[index - 1]
        upper = None if index >= len(edges) or missing else edges[index]
        bins.append(
            DriftBin(
                lower=lower,
                upper=upper,
                reference_count=reference_count,
                current_count=current_count,
                reference_share=reference_share,
                current_share=current_share,
                psi_contribution=contribution,
                missing=missing,
            )
        )

    severity = "stable"
    if psi >= significant_threshold:
        severity = "significant"
    elif psi >= moderate_threshold:
        severity = "moderate"

    reference_missing_rate = reference_counts[-1] / len(reference_values)
    current_missing_rate = current_counts[-1] / len(current_values)
    return DriftReport(
        psi=psi,
        severity=severity,
        reference_count=len(reference_values),
        current_count=len(current_values),
        reference_missing_rate=reference_missing_rate,
        current_missing_rate=current_missing_rate,
        missing_rate_delta=current_missing_rate - reference_missing_rate,
        bin_edges=edges,
        bins=tuple(bins),
        moderate_threshold=moderate_threshold,
        significant_threshold=significant_threshold,
    )
