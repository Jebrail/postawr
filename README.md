# PostAWR / pg_awr

A SQL/PL/pgSQL PostgreSQL extension for historical performance snapshots, activity sampling, and JSON/HTML interval reports. Supports PostgreSQL 15–18. Version 1.2 installs with an OS package and `CREATE EXTENSION`; it needs no Python installer, predefined roles, or specially named database.

## Install a binary package

Choose the package for your PostgreSQL **major version**. Examples for PostgreSQL 18:

```sh
# Debian / Ubuntu (distribution or PGDG PostgreSQL packages)
sudo apt install ./postgresql-18-pg-awr_1.2-1_all.deb

# RHEL / Rocky / AlmaLinux with the PGDG PostgreSQL repository enabled
sudo dnf install ./pg_awr_18-1.2-1.noarch.rpm
```

RPM packages target PGDG's `/usr/pgsql-18` layout and depend on `postgresql18-server`, `postgresql18-contrib`, and `postgresql18`. Debian packages depend on the matching `postgresql-18` and `postgresql-client-18` packages; the server package supplies `pg_stat_statements`. Packages for 15, 16, and 17 use the corresponding major number. They contain architecture-independent SQL and an optional shell job helper. Packages for different majors can coexist.

### PostgreSQL prerequisite

`pg_stat_statements` must be loaded in `shared_preload_libraries`, with `compute_query_id = auto` (the default) or `on`:

```sql
SHOW shared_preload_libraries;
SHOW compute_query_id;
```

If it is missing, append `pg_stat_statements` to the existing `shared_preload_libraries` value in your PostgreSQL configuration, preserving every existing entry, and restart the server. A reload is insufficient. For example, if you already use `pg_cron`, the resulting value is `'pg_cron,pg_stat_statements'`. No additional preload library is needed for pg_awr.

### Create the extension

Connect as a PostgreSQL superuser to the database where you want to keep history:

```sql
CREATE EXTENSION pg_awr CASCADE;
SELECT awr.take_snapshot();
```

`CASCADE` installs the required `pg_stat_statements` SQL objects if absent. If that extension is already installed, plain `CREATE EXTENSION pg_awr;` works. Its existing schema and grants are preserved. pg_awr's objects live in the fixed schema `awr`; the database may have any name. Use one repository database per cluster to avoid duplicate collection and storage.

The installing superuser owns the objects. The extension does not create logins, prompt for passwords, or change database permissions. Functions and history are private by default; the superuser can immediately collect and report. See [operations](docs/operations.md) for optional grants to existing collector and reader accounts.

## Collect and report

Take snapshots at two different times, then use their actual IDs:

```sql
SELECT awr.take_snapshot();
SELECT * FROM awr.list_snapshots(now() - interval '1 day', now());
SELECT awr.report_json(1, 2, 20);
SELECT awr.report_html(1, 2, 20);
```

Export a standalone HTML report:

```sh
psql -X -d your_database -At -v ON_ERROR_STOP=1 \
  -c 'SELECT awr.report_html(1,2,20)' > report.html
```

Configuration is initialized on the first collection. Defaults are 30-day snapshot retention, disabled activity sampling, 7-day activity retention, and no SQL text capture. To opt in as administrator:

```sql
SELECT awr.sample_activity();
UPDATE awr.config SET activity_enabled = true;
-- Optional: normalized representative SQL text, bounded to 1000 characters.
UPDATE awr.config SET capture_sql_text = true, sql_text_limit = 1000;
```

Collection is explicit: `CREATE EXTENSION` does not start a scheduler. Call `awr.take_snapshot()` periodically (the suggested cadence is 15 minutes), `awr.purge_expired()` daily, and optionally `awr.sample_activity()` every 5 seconds. Use an existing scheduler, the [cron example](examples/crontab), or [systemd examples](examples/systemd). Configure the same database and account for every job.

The packaged helper accepts libpq connection environment variables and protected passfiles:

```sh
# Debian / Ubuntu; RPM installs it in /usr/pgsql-18/bin instead.
PGDATABASE=your_database PGUSER=your_collector \
  /usr/lib/postgresql/18/bin/pg-awr-job snapshot
```

Set `PGHOST`, `PGPORT`, and `PGPASSFILE` as needed. The helper retains `awr` / `awr_user` defaults for older installations. It sets timeouts and returns a nonzero exit status on errors. SQL and JSON semantics, source coverage, and storage sizing are in [the interface reference](docs/interface.md).

## Upgrade or remove

Install the new OS package, then run in the repository database as superuser:

```sql
ALTER EXTENSION pg_awr UPDATE;
```

The included `1.0 → 1.1 → 1.2` migration preserves history and existing reader/collector grants. Version 1.2 transfers schema, history, and routine ownership to the extension's superuser owner. Existing database permissions and legacy roles remain; the old Python bootstrap installer is no longer used after this migration. See [upgrade and backup details](docs/operations.md).

`DROP EXTENSION pg_awr CASCADE;` deletes the repository's history and partitions. OS package removal only removes installed files; it does not delete database objects or history.

## Build packages or install from source

Package builds require Python 3.9+ and `dpkg-deb` (Debian) or `rpmbuild` (RPM). No PostgreSQL headers, compiler, root access, or running database is needed:

```sh
make package-deb PG_MAJOR=18
make package-rpm PG_MAJOR=18
# Or build both formats:
python3 packaging/build.py all --pg-major 18
```

Artifacts appear in `dist/`. Repeat for majors 15–17 as needed. RPM builds also emit a source RPM and source tarball. The spec is [packaging/pg_awr.spec](packaging/pg_awr.spec). Package installation has no database-changing hooks and does not start jobs or restart PostgreSQL.

For a custom PostgreSQL layout, use its matching PGXS installation:

```sh
make PG_CONFIG=/path/to/pg_config
sudo make install PG_CONFIG=/path/to/pg_config
# Then connect to the chosen database and CREATE EXTENSION pg_awr CASCADE.
```

## Tests

The package integration suite builds and extracts the actual packages into private PostgreSQL installations, then tests direct creation, dependency handling, privileges, collection, reports, backup/restore, upgrades, and removal. It uses only marked disposable clusters on ports 55615–55618. It never connects to an existing service.

```sh
# Prepare private PostgreSQL 15–18 binaries on Ubuntu resolute amd64:
python3 tests/prepare-local.py
# Or supply an equivalent private tree with PG_AWR_TEST_PREFIX.
python3 tests/native-extension.py --versions 15 16 17 18 --formats deb rpm --reset
```

[Native package results](docs/native-package-test-results.json) record the tested versions and formats. [Earlier validation](docs/validation.md) covers report correctness, concurrency, reset detection, standby behavior, and the historical bootstrap workflow. The [legacy installer](bin/pg-awr-install) remains in source for 1.0/1.1 maintenance only; it is not part of the new binary packages.
# postawr
