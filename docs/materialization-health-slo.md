# Materialization health SLO

An online feature can still be within its TTL while the materialization pipeline
is accumulating a backlog. Database liveness and per-entity freshness therefore
do not prove that warehouse-to-online synchronization is healthy.

The platform now reads a transactionally consistent operational snapshot of:

- source maximum sequence;
- materialization cursor;
- pending row count;
- oldest pending row creation time and age;
- job update time;
- lease owner, expiry, and active state.

`MaterializationHealthPolicy` gates both backlog size and oldest-pending age. It
also fails closed if the cursor is ahead of the source or the persisted lease is
structurally incomplete.

## Probe endpoint

```bash
curl --fail \
  'http://localhost:8000/v1/materializations/online/health?maximum_backlog_rows=1000&maximum_oldest_pending_seconds=300'
```

Healthy snapshots return `200`; policy violations return `503` with the same
machine-readable report. An active, internally consistent lease is observable
but not unhealthy by itself because it normally means a worker is processing.

Example failure:

```json
{
  "ok": false,
  "issues": [
    {
      "code": "backlog.age_exceeded",
      "message": "oldest pending age 420.000s exceeds 300.000s"
    }
  ]
}
```

## Operational interpretation

Backlog count protects finite online-store recovery capacity; oldest-pending age
protects the end-to-end latency objective. Alerting should normally require the
age condition to persist across multiple probes to avoid paging on a transient
batch boundary.

This SQLite reference provides a consistent local snapshot. A split warehouse /
stream / Redis deployment cannot read these values transactionally and should
attach source-watermark and consumer-checkpoint timestamps to a common snapshot
or bounded-skew contract. The next increment should export these fields as
low-cardinality Prometheus gauges and add multi-window burn-rate alerting.
