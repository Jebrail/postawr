# SQL and JSON contract

All historical timestamps use `timestamptz`; collectors, installer and reports use UTC. Session display timezone can change presentation in `list_snapshots`. Snapshot time is the actual collection start, not a nominal timer boundary. The statistics sources are read sequentially, so they are not a perfectly simultaneous global cut.

| Routine | Contract |
|---|---|
| `awr.take_snapshot() → bigint` | Atomically commits a header with database, statements and available metric details; returns its ID. NULL means same-job overlap. Required source failure raises an error and rolls the entire invocation back. |
| `awr.sample_activity() → integer` | Returns observed active client backend count. Excludes sampler and idle sessions. 0 when disabled, with no batch written; 0 when enabled and idle, with a batch written. NULL on overlap. Blocker probes are bounded by `blocker_limit`; observed idle-in-transaction blockers are retained but excluded from active counts. PID plus backend start identifies a backend. |
| `awr.purge_expired() → bigint` | Returns the sum of partitions dropped and header rows deleted (not all cascaded detail rows). NULL on overlap. Maximum 8 daily partitions, 100 snapshot headers and 5000 sample headers per invocation. |
| `awr.list_snapshots(from_time timestamptz,to_time timestamptz)` | Inclusive time range, ascending actual timestamp/ID. NULL bounds or reversed bounds raise an error. Empty ranges return no rows. |
| `awr.report_json(begin_snap bigint,end_snap bigint,top_n integer=20) → jsonb` | Schema version 1. Requires existing increasing IDs and increasing timestamps; `top_n` 1–1000, non-NULL. |
| `awr.report_html(begin_snap bigint,end_snap bigint,top_n integer=20) → text` | Self-contained HTML containing exactly the JSON report's sections, escaped including SQL text and names. |

Discovery columns, in order: `snap_id bigint`, `collected_at timestamptz`, `server_version integer` (server_version_num), `postmaster_start timestamptz`, `timeline_id integer`, `statement_count integer`, `statements_resident integer`, `statements_truncated boolean`, `sources jsonb`.

Collectors and retention reject recovery mode. Read-only reports can run on a physical standby. Advisory locks are `(728301,1)` snapshots, `(728301,2)` activity, `(728301,3)` retention; partition creation/pruning shares `(728301,4)`. Locks last to transaction end. Always commit promptly. Overlap is not a failed scheduler run. SQL errors and timeouts abort all writes in the invocation; schedulers surface a nonzero status. `pg-awr-job` bounds snapshots/retention at 120 seconds and samples at 4 seconds, with 2-second lock timeouts. Direct callers should use an explicit `statement_timeout` before calling; setting that parameter inside a function does not establish a timer for an already-running command.

## JSON v1

Top-level fields: `schema_version`, `begin_snap`, `end_snap`, `begin_time`, `end_time`, `elapsed_seconds`, `snapshot_observations`, `units`, `quality`, `database_activity`, `top_sql`, `statement_quality`, `load_per_second`, `activity`, `metrics`.

Database/statement/metric series include `delta`, `per_second`, `reasons`, `metric_reasons`, `comparable`, `observations`, plus identifiers and historical names. A missing column is **unavailable**, a JSON null delta is **unknown** with reasons, and numeric zero is a measured zero. A series may have valid calls but disabled I/O timing; each ranking uses the validity of its own ranking metric. `comparable` is true only when every recorded field is valid. Counter names match their upstream views. Times are milliseconds, WAL/temp usage is bytes where named bytes, block counters use `units.block_size`, and rates use counter units/second. Query IDs are strings to avoid JavaScript integer precision loss.

`top_sql` contains lists keyed by `total_exec_time`, `calls`, `shared_blks_read`, `temp_blks_read`, `temp_blks_written`, `wal_bytes`. SQL identities are `(dbid,userid,queryid,toplevel)`; SQL present at only one endpoint is excluded. `statement_quality` lists invalid series and explanations. All resident statements are captured by default. Truncation invalidates statement comparisons conservatively; statement churn, eviction and resets cannot become negative or fabricated throughput.

Every collected snapshot between endpoints participates. Primary/system identifier, postmaster, server version, address/port and timeline discontinuities invalidate counter deltas. Per-source/entry reset markers, absent observations/columns, and **any intermediate decrease** invalidate affected deltas. Statement eviction increments invalidate statement comparisons even if endpoint values look plausible. Before per-entry `stats_since` is available, targeted resets that reset and overtake a previous value between observations can be undetectable. No sampling system can prove the absence of every between-sample reset; reports state that limitation.

`activity` contains successful/expected sample counts, coverage ratio, active observation sum, AAS, its explicit successful-sample denominator, first/last sample timestamps, largest observed gap including interval boundaries, uninspected lock waiters, grouped waits, and observed blocker identities. The interval is `(begin_time,end_time]`. Expected samples use elapsed seconds divided by the mean configured cadence among successful batches, falling back to the beginning snapshot setting. Coverage is a cadence-based estimate capped at 1, not proof of continuous observation. Missing samples never count as idle. AAS is the mean over **successful observations**, including recorded zero-active batches; it is NULL with no successful samples. This can be biased during gaps or cadence changes. No wait event is not a CPU measurement. Blocker lists are observations, not a complete historical wait graph; prepared-transaction blockers can be PID 0 without a backend row.

`quality.snapshots` shows source availability, settings, statement residency, capture count, truncation, global reset/eviction markers for each point. Session timing settings describe the collector's view; workloads may have session-local overrides, so timing remains potentially partial even when enabled. Execution time sums over calls and concurrent sessions, and can exceed wall-clock elapsed time.

## Version adapters and source coverage

Collectors inspect catalog presence and extract allowlisted numeric counters from each major version's row JSON; no nonexistent column is referenced in a parsed SQL statement.

| Source | Versions | Captured counters |
|---|---|---|
| `pg_stat_database` | 15–18 | commits/rollbacks, block hits/reads, returned/fetched/inserted/updated/deleted tuples, temp files/bytes, deadlocks, conflicts, block timing, sessions and session/active/idle-in-transaction time and termination counters |
| `pg_stat_statements` | 15–18 | calls, execution ms, rows, shared hit/read/dirtied/written blocks, temp read/written blocks, available shared/local/temp I/O timing, WAL records/full-page images/bytes; global reset/dealloc, per-entry stats_since when available |
| `pg_stat_wal` | 15–18 | records, FPI, bytes, buffers full; write/sync counts and timing where present (18 moves those I/O details to pg_stat_io) |
| `pg_stat_bgwriter` | 15–18 | clean/maxwritten/allocated buffers; 15–16 also backend buffers/fsync and checkpoint counts/buffers/timing |
| `pg_stat_checkpointer` | 17–18 | timed/requested/completed checkpoints where present, buffers written, write/sync timing, timed/requested/completed restartpoints |
| `pg_stat_io` | 16–18 | grouped by backend_type/object/context; read/write/writeback/extend counts/timing, hits, evictions, reuses, fsync counts/timing; byte counters where present in 18 |

Absent sources are explicitly false in `sources`. Absent columns are omitted, not zero-filled. `stats_reset` is retained with each source. Disabled block/WAL timing becomes null with a reason, rather than measured zero. Checkpointer timing is independent of track_io_timing and remains usable. `pg_stat_activity` excludes background workers from the active count; blocker capture may include other backend types if directly observed blocking a client.

## Storage sizing

High-volume statement and session details are logged, daily UTC range partitions, with foreign keys to atomic headers and interval indexes. Lower-volume headers, database and dimensioned system statistics are logged normal tables. Dynamic partitions are owned by the collector function owner (the installing superuser in 1.2). No PUBLIC table privileges are granted; collector and reader roles receive only explicitly granted routine access. Retention drops only fully expired partitions, so detail retention can exceed the target by up to one UTC day plus backlog. Orphan-free header cleanup follows partition removal in bounded batches.

Approximate uncompressed storage before indexes, WAL and free space:

- Statements: `resident_entries × (86400 / snapshot_seconds) × retention_days × average_row_bytes`. At 5000 entries, 900 seconds, 30 days, 600 bytes: about 8.0 GiB of row payload. JSON, optional text and indexes can materially increase this.
- Activity: `mean_active_backends × (86400 / sample_seconds) × retention_days × average_row_bytes`, plus blocker-only observations. At 10 active backends, 5 seconds, 7 days, 200 bytes: about 231 MiB plus headers/indexes.
- Sample headers: `(86400 / sample_seconds) × retention_days` rows, even during observed idle periods. Snapshot metadata scales with database count and I/O dimensions.

Measure your actual rows using `pg_column_size` and `pg_total_relation_size`, budget WAL/backups separately, and monitor disk usage. All-resident snapshots can be large; assess cardinality before choosing cadence and retention.
