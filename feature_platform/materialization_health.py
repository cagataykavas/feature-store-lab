from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class MaterializationSnapshot:
    job_name: str
    observed_at: datetime
    cursor_sequence: int
    source_max_sequence: int
    backlog_rows: int
    oldest_pending_created_at: datetime | None
    job_updated_at: datetime | None
    lease_owner: str | None
    lease_until: datetime | None

    @property
    def oldest_pending_age_seconds(self) -> float | None:
        if self.oldest_pending_created_at is None:
            return None
        return max(0.0, (self.observed_at - self.oldest_pending_created_at).total_seconds())

    @property
    def lease_active(self) -> bool:
        return bool(
            self.lease_owner
            and self.lease_until is not None
            and self.lease_until > self.observed_at
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "observed_at": self.observed_at.isoformat(),
            "cursor_sequence": self.cursor_sequence,
            "source_max_sequence": self.source_max_sequence,
            "backlog_rows": self.backlog_rows,
            "oldest_pending_created_at": (
                self.oldest_pending_created_at.isoformat()
                if self.oldest_pending_created_at
                else None
            ),
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
            "job_updated_at": self.job_updated_at.isoformat() if self.job_updated_at else None,
            "lease_owner": self.lease_owner,
            "lease_until": self.lease_until.isoformat() if self.lease_until else None,
            "lease_active": self.lease_active,
        }


@dataclass(frozen=True)
class MaterializationHealthPolicy:
    maximum_backlog_rows: int = 1_000
    maximum_oldest_pending_seconds: float = 300.0

    def __post_init__(self) -> None:
        if isinstance(self.maximum_backlog_rows, bool) or not isinstance(
            self.maximum_backlog_rows, int
        ):
            raise TypeError("maximum_backlog_rows must be an integer")
        if self.maximum_backlog_rows < 0:
            raise ValueError("maximum_backlog_rows must be non-negative")
        if isinstance(self.maximum_oldest_pending_seconds, bool) or not isinstance(
            self.maximum_oldest_pending_seconds, (int, float)
        ):
            raise TypeError("maximum_oldest_pending_seconds must be numeric")
        if not isfinite(self.maximum_oldest_pending_seconds):
            raise ValueError("maximum_oldest_pending_seconds must be finite")
        if self.maximum_oldest_pending_seconds < 0:
            raise ValueError("maximum_oldest_pending_seconds must be non-negative")


@dataclass(frozen=True)
class MaterializationHealthIssue:
    code: str
    message: str


@dataclass(frozen=True)
class MaterializationHealthReport:
    snapshot: MaterializationSnapshot
    policy: MaterializationHealthPolicy
    issues: tuple[MaterializationHealthIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot": self.snapshot.as_dict(),
            "policy": asdict(self.policy),
            "issues": [asdict(issue) for issue in self.issues],
        }


def evaluate_materialization_health(
    snapshot: MaterializationSnapshot,
    policy: MaterializationHealthPolicy,
) -> MaterializationHealthReport:
    issues: list[MaterializationHealthIssue] = []
    if snapshot.cursor_sequence > snapshot.source_max_sequence:
        issues.append(
            MaterializationHealthIssue(
                "cursor.ahead_of_source",
                f"cursor {snapshot.cursor_sequence} exceeds source maximum "
                f"{snapshot.source_max_sequence}",
            )
        )
    if snapshot.backlog_rows > policy.maximum_backlog_rows:
        issues.append(
            MaterializationHealthIssue(
                "backlog.rows_exceeded",
                f"backlog {snapshot.backlog_rows} exceeds {policy.maximum_backlog_rows}",
            )
        )
    pending_age = snapshot.oldest_pending_age_seconds
    if snapshot.backlog_rows and pending_age is None:
        issues.append(
            MaterializationHealthIssue(
                "backlog.oldest_timestamp_missing",
                "pending rows exist but their oldest creation time is unavailable",
            )
        )
    elif pending_age is not None and pending_age > policy.maximum_oldest_pending_seconds:
        issues.append(
            MaterializationHealthIssue(
                "backlog.age_exceeded",
                f"oldest pending age {pending_age:.3f}s exceeds "
                f"{policy.maximum_oldest_pending_seconds:.3f}s",
            )
        )
    if (snapshot.lease_owner is None) != (snapshot.lease_until is None):
        issues.append(
            MaterializationHealthIssue(
                "lease.incomplete",
                "lease owner and expiry must either both be present or both be absent",
            )
        )
    return MaterializationHealthReport(snapshot, policy, tuple(issues))
