# pg_awr 1.2 operations

## Installation and ownership

Install the package matching PostgreSQL 15, 16, 17, or 18. Debian/Ubuntu packages use `/usr/share/postgresql/<major>/extension`; PGDG RPMs use `/usr/pgsql-<major>/share/extension`. Then run `CREATE EXTENSION pg_awr CASCADE` as a superuser in the chosen repository database. Both the session and effective role must be superusers. A writable primary and preloaded `pg_stat_statements` are required.

Objects live in schema `awr`, owned by the installing superuser. An existing schema must be owned by a superuser and must not grant CREATE to other roles or PUBLIC. Reserve this schema for pg_awr. No roles, database, bootstrap metadata, passwords, scheduler, or OS service are created. The existing `pg_stat_statements` installation is reused in its current schema. Avoid schemas writable by untrusted users for extension prerequisites.

Install one repository per cluster. Statistics and statement history cover the cluster, even when the repository is in an ordinary database. Collection uses database-local advisory locks, so installations in different databases do not coordinate with one another.

## Optional access for existing accounts

All history, helpers, and reporting/collection routines are private by default. The extension owner can use them immediately. To delegate access, substitute your existing role names:

```sql
GRANT USAGE ON SCHEMA awr TO your_collector, your_reader;
GRANT EXECUTE ON FUNCTION awr.take_snapshot(), awr.sample_activity(),
  awr.purge_expired() TO your_collector;
GRANT EXECUTE ON FUNCTION awr.list_snapshots(timestamptz,timestamptz),
  awr.report_json(bigint,bigint,integer), awr.report_html(bigint,bigint,integer)
  TO your_reader;
```

Ensure these accounts can connect to the repository database under your existing database/HBA policy. They do not need superuser, schema CREATE, table access, owner membership, or `pg_read_all_stats`. Public routines use SECURITY DEFINER with a fixed `pg_catalog, pg_temp` search path. Configure retention and sampling through the owner account. Reporting exposes cluster-wide performance details and, if explicitly enabled, normalized SQL text; grant it only to appropriate readers.

The extension revokes PUBLIC privileges. Owner-defined default privileges still apply to other named roles; review such defaults before installation. Do not grant direct table writes or access to internal helpers. Avoid changing object ownership individually, since collectors and runtime-created partitions need a consistent privileged owner.

## Scheduling

`CREATE EXTENSION` makes the SQL API available; it does not collect automatically. Schedule snapshot and retention calls externally. Activity sampling is opt-in both in `awr.config` and in the scheduler. The cadence values in configuration describe expected scheduling; they do not start timers.

`pg-awr-job snapshot|activity|retention` honors `PGDATABASE`, `PGUSER`, `PGHOST`, `PGPORT`, and `PGPASSFILE`. Defaults `awr` and `awr_user` remain for old deployments. Each job opens a fresh transaction with statement, lock, and idle timeouts. Same-job overlap returns SQL NULL safely. Retention works in bounded batches; run it more often to clear a backlog.

The package installs examples under its documentation directory, not as active services. Set the executable path, database, account, and owner-only mode-0600 passfile before enabling them. Monitor scheduler failures and storage growth.

## Backup and restore

Use a full database dump that includes pg_awr and its runtime-created partitions:

```sh
pg_dump -Fc -d your_database -f repository.dump
# Install matching extension files and preload pg_stat_statements on the destination.
createdb restored_database
pg_restore --exit-on-error -d restored_database repository.dump
```

Restore as a superuser to an empty database. pg_restore creates the extensions. Recreate any ordinary application/collector/reader roles referenced by ownership or grants first, as with any PostgreSQL database restore. Fresh 1.2 installs need no predefined awr roles, and the destination database can have a different name. Keep the installing superuser role available for restored ownership.

Configuration, snapshot headers, database/system histories, activity headers, partition registry, and ID sequence values are registered for dumping. Runtime-created partitions are ordinary tables and their data/indexes are dumped normally. Do not use schema-only or filtered dumps to back up repository history. Existing collection should be stopped during a restore. A physical backup/restore follows PostgreSQL's ordinary cluster backup process.

## Upgrade from 1.0 or 1.1

Back up the repository and stop scheduled jobs, install the new package, and run:

```sql
ALTER EXTENSION pg_awr UPDATE TO '1.2';
```

The extension's owner must remain a superuser. Run this as a session/effective superuser. The `1.0 → 1.1 → 1.2` chain preserves configuration, history, sequence state, and explicit routine grants. Version 1.2 transfers schema, relation, and routine ownership from the legacy `awr_owner` to the extension owner and removes the partition function's dependency on fixed role names. The original dedicated database, connection restrictions, roles, memberships, and bootstrap metadata remain in place. Existing jobs can continue using `awr_user` and database `awr`.

Do not run `pg-awr-install` after upgrading: its older ownership/provenance model applies only to 1.0/1.1. Use SQL for future upgrades, configuration, grants, and password management. Upgrade a legacy repository before making a dump for restoration with the 1.2 default extension; older dumps recreate objects according to the old ownership model. For older recovery procedures see [legacy operations](legacy-operations.md).

## Failover and removal

Collectors reject recovery mode; historical reports can run on a physical standby. Move scheduling to the new writable primary after failover. Reports spanning a restart or failover flag continuity problems.

Stop jobs and back up any required history before removing the database extension:

```sql
DROP EXTENSION pg_awr CASCADE;
```

This removes managed data, including ordinary partitions that depend on the extension's parent tables. It leaves `pg_stat_statements`, cluster roles, and the database itself in place. Removing a `.deb` or RPM removes only OS files; database objects are untouched. Drop the database extension before removing its package if it will no longer be used. Package upgrades require an explicit `ALTER EXTENSION` in each repository database.

## References

See PostgreSQL's [extension lifecycle and configuration tables](https://www.postgresql.org/docs/18/extend-extensions.html), [CREATE EXTENSION](https://www.postgresql.org/docs/18/sql-createextension.html), and [pg_stat_statements setup](https://www.postgresql.org/docs/18/pgstatstatements.html).
