# Historical validation (1.0/1.1)

For native 1.2 package validation see [native package results](native-package-test-results.json). The records below describe the earlier bootstrap-based implementation.

# Recorded validation

The integration suite ran against four real, disposable PostgreSQL clusters using private Unix sockets and loopback SCRAM authentication. Every supported installer run created the repository named exactly `awr`; `psql -X -d awr` verified the database name. No acceptance test targeted the existing service on port 5432.

| PostgreSQL | Acceptance groups | Elapsed |
|---|---:|---:|
| 15.19 | 13 passed | 67.33 s |
| 16.15 | 13 passed | 74.36 s |
| 17.11 | 13 passed | 73.85 s |
| 18.6 | 13 passed | 81.66 s |

[Machine-readable results](test-results.json) include the exact builds and individual acceptance groups. Coverage includes installer cancellation/resume and rotation, provenance collisions, preload checks, privilege boundaries, concurrent jobs, atomic timeout rollback, cross-database workload, observed blockers, missed samples, resets/eviction, restart discontinuities, version adapters, escaped HTML, upgrade, partition-aware dump/restore, sequence state, retention, and a real physical standby.

The [pre-created database regression results](empty-database-test-results.json) record 14 additional groups on each of PostgreSQL 15–18. They cover explicit opt-in, rejection and preservation of existing objects/data, active sessions, unrelated ownership/roles/settings/grants, atomic rollback, interruption after adoption, cancellation/resume, preserved database OIDs, SCRAM authentication, collection, and enforcement of existing provenance. These tests use separate disposable clusters on ports 55515–55518 and run with `python3 tests/installer-empty-database.py --versions 15 16 17 18 --reset`.

PGXS build and installation succeeded using the private PostgreSQL 18 installation prefix. Python compilation and shell syntax checks passed. The tests leave their private clusters stopped; they can be rebuilt with `python3 tests/integration.py --versions 15 16 17 18 --reset`.

The [PostgreSQL 18 benchmark](benchmark-results.json) ran three workload clients for 15 seconds per phase. Baseline: 136.27 transactions/s and 30.86 ms p95 latency. With accelerated collection: 142.17 transactions/s and 29.25 ms p95 latency. Snapshots took 452–520 ms; activity samples took 13–39 ms. Repository growth was 80 KiB baseline and 2176 KiB enabled. This single short trial is noisy and does not establish a throughput improvement or a production overhead bound. The saved result includes raw collection durations and cadence.

The implementation uses native SQL/PL/pgSQL extension objects. Optional source columns are represented by presence/absence rather than fabricated zeros. Logical restore uses local indexes on ordinary child partitions to avoid parent-index attachment conflicts with extension-owned objects, and explicitly dumps both ID sequences.

The existing PostgreSQL 18 service was verified as a primary with `pg_cron,pg_stat_statements` preloaded. Its database/role configuration was not modified by this implementation run. The native 1.2 installation now uses an OS package and `CREATE EXTENSION`; see the README.
