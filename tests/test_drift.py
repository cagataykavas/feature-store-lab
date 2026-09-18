import json
import math

import pytest

from feature_platform.drift import audit_numeric_drift


def test_identical_distributions_are_stable_and_json_ready() -> None:
    values = [float(index) for index in range(100)]

    report = audit_numeric_drift(values, values)

    assert report.severity == "stable"
    assert report.psi == pytest.approx(0.0)
    assert sum(item.reference_count for item in report.bins) == 100
    assert json.loads(json.dumps(report.to_dict()))["severity"] == "stable"


def test_large_location_shift_is_significant() -> None:
    reference = [float(index) for index in range(100)]
    current = [float(index + 200) for index in range(100)]

    report = audit_numeric_drift(reference, current)

    assert report.severity == "significant"
    assert report.psi >= report.significant_threshold
    assert report.bins[-2].current_count == 100


def test_missing_values_have_an_explicit_drift_bin() -> None:
    reference = [float(index) for index in range(90)] + [None] * 10
    current = [float(index) for index in range(50)] + [None] * 50

    report = audit_numeric_drift(reference, current)

    missing_bin = report.bins[-1]
    assert missing_bin.missing is True
    assert missing_bin.reference_count == 10
    assert missing_bin.current_count == 50
    assert report.reference_missing_rate == pytest.approx(0.1)
    assert report.current_missing_rate == pytest.approx(0.5)
    assert report.missing_rate_delta == pytest.approx(0.4)
    assert report.severity == "significant"


def test_constant_reference_detects_shifts_in_both_directions() -> None:
    reference = [5.0] * 100
    current = [4.0] * 50 + [6.0] * 50

    report = audit_numeric_drift(reference, current)

    assert len(report.bin_edges) == 2
    assert report.bins[0].current_count == 50
    assert report.bins[1].reference_count == 100
    assert report.bins[2].current_count == 50
    assert report.severity == "significant"


@pytest.mark.parametrize(
    ("reference", "current", "error"),
    [
        ([1.0] * 19, [1.0] * 20, ValueError),
        ([1.0] * 20, [math.inf] * 20, ValueError),
        ([1.0] * 20, ["bad"] * 20, TypeError),
        ([None] * 20, [1.0] * 20, ValueError),
    ],
)
def test_invalid_samples_fail_closed(
    reference: list[object], current: list[object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        audit_numeric_drift(reference, current)  # type: ignore[arg-type]


def test_policy_parameters_are_validated() -> None:
    values = [float(index) for index in range(20)]

    with pytest.raises(ValueError, match="0 < moderate < significant"):
        audit_numeric_drift(values, values, moderate_threshold=0.3, significant_threshold=0.2)
    with pytest.raises(ValueError, match="epsilon"):
        audit_numeric_drift(values, values, epsilon=0.0)
    with pytest.raises(ValueError, match="bin_count"):
        audit_numeric_drift(values, values, bin_count=1)
