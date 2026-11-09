# Historical bootstrap workflow (1.0/1.1 only)

The native 1.2 packages do not require or install this helper. It remains available in the source tree for maintaining existing 1.0/1.1 installations. For new installations use [the README](../README.md).

# pg_awr

A native SQL/PL/pgSQL PostgreSQL extension for historical instance statistics and interval reports. PostgreSQL 15–18, a self-managed writable primary, one repository per cluster, database **awr**, schema **awr**. No background worker or third-party server library is added; scheduling is external. `pg_stat_statements` must be preloaded. SQL text capture and active-session sampling are off by default.

## Build and install

Requirements: matching PostgreSQL server/contrib and PGXS packages, Python 3.9+ and libpq 15+ for the installer, `psql` for jobs. The extension has no C compilation dependency. Tests additionally use Python `pexpect`.

```sh
make PG_CONFIG=/path/to/pg_config
sudo make install PG_CONFIG=/path/to/pg_config
/path/to/pg-awr-install --host localhost --port 5432 --username postgres --maintenance-db postgres -W
```

PGXS installs scripts in `pg_config --bindir`. Use that directory in PATH or symlink `pg-awr-install` and `pg-awr-job` into your executable path. Use the server's matching `pg_config`, not another major version's.

Before installing, inspect the existing value:

```sql
SHOW shared_preload_libraries;
SHOW compute_query_id;
```

Append `pg_stat_statements` to the existing list, **preserving existing modules** (for example `shared_preload_libraries = 'pg_cron,pg_stat_statements'`). Restart PostgreSQL; a reload is insufficient. Set `compute_query_id = auto` or `on`. Do not run a statistics reset to install the extension.

The installer requires both session and effective roles to be true superusers. `CREATEDB CREATEROLE` alone is rejected. It creates the runtime role, prompts twice on a controlling TTY with echo disabled, generates a SCRAM verifier using libpq, and only enables LOGIN after validation. There is no runtime password argument, environment variable, stdin automation option, or plaintext password file. Installer authentication can use peer auth, a protected libpq passfile, or `-W`. Avoid `PGPASSWORD` and password-bearing connection URIs.

The fixed maintenance database is `postgres`. Bootstrap holds a session advisory lock `(728301,0)` there and records protected provenance in `postgres.awr_bootstrap.state`. Connections are pinned to one endpoint, system identifier, postmaster, and timeline. Roles are created NOLOGIN with recorded OIDs and UUID comments. Database `awr` is created in a standalone statement with connections disabled; PUBLIC privileges are revoked before connections are enabled. The stats extension lives in locked schema `awr_stats`. Name collisions are never adopted automatically.

Normally, do **not** create `awr` yourself. If you already created an empty `awr` for this installation and receive `Conflicting names without verified bootstrap provenance`, use:

```sh
pg-awr-install --host localhost --username postgres -W --use-empty-database
```

This explicitly authorizes initialization of that database without dropping or recreating it. It must be owned by the installer account, contain no user objects, and have no other sessions, conflicting roles, custom database settings/grants, or prior bootstrap metadata. The installer verifies these conditions before recording ownership and provenance. Populated databases still require [operator reconciliation](legacy-operations.md#incomplete-installation-and-reconciliation).

A healthy repeat reports the installed version and preserves the password:

```sh
pg-awr-install --host localhost --username postgres -W
pg-awr-install --host localhost --username postgres -W --rotate-password
pg-awr-install --host localhost --username postgres -W --upgrade
```

Rotation disables new runtime logins before prompting. Cancellation leaves `awr_user NOLOGIN`; repeat the installer to finish. Existing sessions survive `NOLOGIN`; stop schedulers and disconnect sessions yourself when immediate credential revocation is required.

See [operations](legacy-operations.md) for recovery, exact grants, backup/restore, upgrades, failover, and uninstall.

## Collect and report

Use protected `.pgpass`/`PGPASSFILE` credentials, never passwords in arguments. Connect to `awr`, not an application database:

```sh
psql -X -h localhost -U awr_user -d awr -v ON_ERROR_STOP=1 -c 'SELECT current_database(), awr.take_snapshot();'
pg-awr-job snapshot
```

The defaults are 15-minute snapshots, 30-day snapshot retention, disabled 5-second activity sampling, and 7-day activity retention. Enable sampling as an administrator:

```sql
-- The singleton is initialized by the first collection, including disabled activity sampling.
SELECT awr.sample_activity();
UPDATE awr.config SET activity_enabled=true;
-- Optional and sensitive: normalized representative statement text, bounded to 1000 characters.
UPDATE awr.config SET capture_sql_text=true, sql_text_limit=1000;
```

Only the administrator/owner can configure the extension. Do not give runtime users owner membership. `statement_limit=0` collects all resident entries; a positive limit keeps the highest cumulative execution time entries and records truncation. No raw `pg_stat_activity.query`, bind values, plans, table statistics, or index statistics are collected.

```sql
SELECT * FROM awr.list_snapshots(now()-interval '1 day',now());
SELECT awr.report_json(1,2,20);
```

Clean, standalone HTML export (replace IDs with actual chronological snapshots):

```sh
psql -X -h localhost -U awr_user -d awr -At -v ON_ERROR_STOP=1 \
  -c 'SELECT awr.report_html(1,2,20)' > report.html
```

No external JavaScript, fonts, assets, or network requests are needed. All data is HTML escaped. JSON and HTML use the same interval computation. See [SQL/JSON contract and coverage](interface.md).

## Scheduling

Use either the [systemd units](../examples/systemd) or [cron example](../examples/crontab), under a dedicated OS account. Adjust executable paths and provision `/etc/pg-awr/pgpass` as owner-only mode 0600. Systemd's journal captures errors; monitor failed units. Create a protected cron log directory and rotate its log. Each job uses a fresh transaction, `ON_ERROR_STOP`, statement/lock/idle timeouts, and a transaction advisory lock. Overlap skips safely, returning SQL NULL. Activity is opt-in both in config and scheduler. Retention performs bounded batches; schedule it more frequently to clear a backlog.

## Tests and benchmark

Never run destructive acceptance tests on an existing cluster. The harness creates only dedicated local socket directories and ports 55415–55418, and always installs into a database named exactly `awr`. It refuses to overwrite a cluster directory. It drops/recreates only its own test `awr` for restore testing.

```sh
# Prepare user-owned PostgreSQL binaries in .local/pg/usr/{lib,share}/postgresql/<major>.
# On Ubuntu resolute amd64: python3 tests/prepare-local.py
# Or set PG_AWR_TEST_PREFIX to an equivalent private Debian/Ubuntu-style installation tree.
make
python3 tests/integration.py --versions 15 16 17 18 --reset
python3 tests/installer-empty-database.py --versions 15 16 17 18 --reset
python3 tests/benchmark.py --version 18 --seconds 15 --clients 3
```

[Recorded integration results](test-results.json) and [benchmark measurements](benchmark-results.json) identify the actual tested binaries. The benchmark compares identical write workloads with collection disabled/enabled, reporting transactions/sec, p95 latency, collection duration, and repository growth. It accelerates snapshots to 5 seconds and samples to 1 second so a short run exercises both collectors; it is a smoke benchmark, not a universal overhead guarantee.

See [the original specification](../pg_awr_technical_spec.md). Oracle catalog compatibility, execution-plan history, and managed services without true superuser access are outside v1 scope.
