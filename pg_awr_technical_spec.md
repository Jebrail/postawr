# pg_awr technical specification for Codex

Build a native PostgreSQL extension for historical performance sampling and interval reporting modeled on Oracle AWR. Store samples, configuration, and report-generation functions in schema `awr` of a dedicated database named `awr`. Target self-managed PostgreSQL 15–18. Use one repository per cluster and collect from its writable primary.

## Packaging and installation

Ship a PGXS Makefile, `pg_awr.control`, versioned installation and upgrade SQL, an interactive `pg-awr-install` command, scheduler examples, documentation, and integration tests. Set `superuser = true`, `trusted = false`, `relocatable = false`, `schema = 'awr'`, and `requires = 'pg_stat_statements'`. Reject extension creation in any other database.

```bash
make PG_CONFIG=/path/to/pg_config
sudo make install PG_CONFIG=/path/to/pg_config
pg-awr-install --host localhost --port 5432 --username postgres --maintenance-db postgres
```

The supported installation command must create the database and runtime login, prompt for its password, and install the extension as one workflow. The client handles prompting. Create the database in a standalone command before connecting to it and issuing `CREATE EXTENSION`, because `CREATE DATABASE` cannot run inside a transaction block.

1. Verify that the session and effective roles are true superusers. Verify a writable primary, compatible extension files, and usable preloaded `pg_stat_statements`. Document how to append it to the existing `shared_preload_libraries` setting and restart PostgreSQL.
2. Serialize bootstrap with a session advisory lock in a fixed maintenance database. Pin all connections to the same cluster and primary. Record progress in protected metadata. Reject unrelated objects with conflicting names. Resume only when ownership and provenance can be verified. Require operator reconciliation for ambiguous states.
3. Create `awr_owner`, `awr_reader`, and `awr_executor` as `NOLOGIN` roles. Create `awr_user` initially as `NOLOGIN` with no password. Create database `awr` owned by `awr_owner` with connections disabled. Revoke `PUBLIC` database privileges before enabling connections for bootstrap.
4. Connect to `awr` as the installer superuser. Install `pg_stat_statements` in a locked schema and `pg_awr` in schema `awr`. Apply and verify the privilege model.
5. Prompt twice on a TTY for the new `awr_user` password with echo disabled. Set `password_encryption = 'scram-sha-256'` and use psql's `\password awr_user` or equivalent client-side SCRAM verifier generation. Keep plaintext out of SQL, arguments, environment variables, logs, history, and files. Enable `LOGIN` only after password setting and installation validation succeed. Leave the role disabled on cancellation or failure.
6. On a healthy repeat install, report the version and preserve the password. Provide explicit password rotation and a documented recovery procedure for incomplete installs.

## Privileges and ownership

| Principal | Required access |
| --- | --- |
| OS package installer | Write extension files into the PostgreSQL installation selected by the matching `pg_config`. |
| PostgreSQL installer | True `SUPERUSER` for bootstrap and upgrades. Owns the extension itself. |
| `awr_owner` | `NOLOGIN NOSUPERUSER`. Owns the database, managed schemas, tables, and privileged routines. Receives `pg_read_all_stats` and access to the required statistics sources. |
| `awr_user` | SCRAM-authenticated runtime login with inherited membership in `awr_executor` and `awr_reader`. |
| `awr_executor` | `CONNECT` on `awr`, schema `USAGE`, and `EXECUTE` on approved collection and bounded retention routines. |
| `awr_reader` | `CONNECT` on `awr`, schema `USAGE`, and `EXECUTE` on discovery and reporting routines. Can be granted to separate reporting logins. |

Document exact grants, ownership, host authentication requirements, and verification commands. Give runtime roles no administrative attributes, owner membership, direct repository writes, or statistics-reset privileges. Revoke `PUBLIC` access to routines in the same transaction that creates them, including upgrades. Use `SECURITY DEFINER` only where required, fix the search path to `pg_catalog, pg_temp`, and schema-qualify non-catalog references. Document that report readers can see instance-wide workload metadata and that `PUBLIC CONNECT` on other databases is managed separately.

## Sampling and storage

| Job | Default | Collection |
| --- | --- | --- |
| Cumulative snapshot | Every 15 minutes | Database and statement counters, plus supported WAL, checkpoint, and I/O metrics. |
| Active-session sampling | Initially off. Every 5 seconds when enabled. | Active client backends, query IDs, wait events, and observed blockers. |
| Retention | Daily | Keep snapshots for 30 days and active-session samples for 7 days. |

Ship systemd timer and cron examples. Connect as `awr_user` with protected libpq credentials. Use a fresh bounded transaction per invocation, explicit timeouts, and advisory locks to prevent overlapping jobs of the same type. Commit snapshot headers and details atomically. Log failures through the scheduler.

Record snapshot IDs, actual UTC collection timestamps, PostgreSQL version, source availability, reset and eviction markers, and enough primary and postmaster identity to detect known discontinuities. Keep database and role names alongside their identifiers for historical display after renames.

- Collect commits, rollbacks, block and tuple activity, temporary usage, and deadlocks from `pg_stat_database`.
- Collect `pg_stat_statements` keyed by `(dbid, userid, queryid, toplevel)`. Include calls, execution time, rows, shared and temporary block counters, and available I/O timing and WAL counters. Collect all resident entries by default. Record and report truncation when a cap is configured. Database-local table and index statistics are outside v1 scope.
- Sample `pg_stat_activity` and use bounded `pg_blocking_pids()` calls for lock waiters. Store backend start with PID to identify PID reuse. Exclude the sampler and idle sessions from active counts. Capture idle-in-transaction sessions when observed as blockers.
- Collect WAL, checkpoint, and I/O statistics through major-version-specific adapters. Document source and column coverage. Distinguish unavailable metrics, disabled timing, and measured zeros.

Use logged tables, foreign keys, and interval indexes. Partition high-volume details by UTC timestamp and prune in bounded work units. Provide storage estimates based on statement cardinality, active backend count, sample frequency, and retention. Disable SQL text capture by default. Allow administrators to opt in to bounded representative statement text. Omit bind values and raw `pg_stat_activity.query`.

## SQL interface and reports

```sql
awr.take_snapshot() RETURNS bigint
awr.sample_activity() RETURNS integer
awr.purge_expired() RETURNS bigint
awr.list_snapshots(from_time timestamptz, to_time timestamptz) RETURNS TABLE (...)
awr.report_html(begin_snap bigint, end_snap bigint, top_n integer DEFAULT 20) RETURNS text
awr.report_json(begin_snap bigint, end_snap bigint, top_n integer DEFAULT 20) RETURNS jsonb
```

Define exact discovery columns, return values, and behavior for overlap, disabled sampling, timeouts, unavailable sources, and recovery mode. Validate snapshot IDs, chronological order, and `top_n`. Share interval computation between both formats. Version the JSON schema, specify units, and produce self-contained HTML with all untrusted text escaped.

Include interval data quality, per-second load, database activity, top SQL by execution time, calls, shared blocks read, temporary I/O and WAL, sampled waits, estimated average active sessions, observed blockers, and supported WAL, checkpoint, and I/O summaries.

Calculate rates from valid counter deltas and elapsed time. Check resets, counter decreases, and known discontinuities, including intermediate snapshot evidence. Mark affected deltas unknown with reasons. Exclude endpoint-only statements from comparable rankings unless a baseline can be established. Surface eviction and reset uncertainty. Document that statement execution time sums across calls, samples without a wait event do not measure CPU time, and missed samples do not count as idle periods. Show sample coverage and the denominator used for average active sessions.

## Documentation and acceptance

Document prerequisites, installation, exact privileges, SCRAM and `pg_hba.conf` examples, scheduling, manual collection, reporting, storage sizing, rotation, failover, backup and restore, upgrades, and uninstall. Demonstrate clean HTML export with `psql -X -At` and `ON_ERROR_STOP`. Explain how to preserve extension-owned history on dump and restore, including partition data and separately provisioned roles.

### Required local PostgreSQL integration tests

Run the integration suite against real local PostgreSQL instances on versions 15, 16, 17, and 18. Use a local Unix socket or loopback address. On each instance, run the supported installer as a superuser and verify that it creates the test repository database named exactly `awr`. Connect with `psql -X -d awr` using the local instance's socket or port and verify that `current_database()` returns `awr`. Execute collection, repository, and report checks through that database. Use separate local databases only for workloads that exercise cross-database collection. Record the tested versions and results.

Verify the following.

- A fresh superuser install creates `awr` and `awr_user`, prompts for the password, and enables SCRAM login. A non-superuser with `CREATEDB` and `CREATEROLE` is rejected.
- Cancellation, concurrent bootstrap, name collisions, missing preload, partial failure, resume, and rotation behave as documented. Plaintext credentials stay out of process arguments and logs.
- Collector and report-reader privileges are limited to approved operations. `PUBLIC` cannot connect to the repository or execute its routines.
- Work generated in two application databases appears in a report generated entirely from `awr`. Tests cover lock waits, missing samples, resets, eviction, restarts, missing metrics, HTML escaping, concurrent collection, and retention.
- Dump and restore preserve history. At least one `ALTER EXTENSION pg_awr UPDATE` path preserves history and privileges. Provide a repeatable baseline-versus-enabled benchmark with collection time, throughput, p95 latency, and storage growth.

PostgreSQL 15–18, the external scheduler, and the initial cadence and retention are v1 design choices. Oracle catalog compatibility, execution-plan history, and managed services without true superuser access are outside scope.

## PostgreSQL references

- [CREATE EXTENSION](https://www.postgresql.org/docs/17/sql-createextension.html)
- [Packaging extensions](https://www.postgresql.org/docs/18/extend-extensions.html)
- [CREATE DATABASE](https://www.postgresql.org/docs/current/sql-createdatabase.html)
- [pg_stat_statements](https://www.postgresql.org/docs/16/pgstatstatements.html)
- [psql and password prompting](https://www.postgresql.org/docs/current/app-psql.html)
