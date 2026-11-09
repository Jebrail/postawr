# Legacy 1.0/1.1 installation

These instructions describe the optional historical Python bootstrap installer. For 1.2 and later, use [standard extension operations](operations.md). Do not run the legacy installer after upgrading to 1.2.

# Operations and security

## Ownership and exact privileges

The OS installer needs write access to the installation selected by `pg_config`. The SQL installer must be a true superuser (session and effective role), owns both extensions and the protected `postgres.awr_bootstrap` metadata, and performs upgrades. Cluster roles and databases cannot be extension members.

All four roles have `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT`. `awr_owner`, `awr_reader`, `awr_executor` are NOLOGIN. `awr_user` starts NOLOGIN with a NULL password and becomes LOGIN only after a SCRAM verifier and validation. Neither runtime role receives owner membership.

The installer applies the following grants and restrictions:

```sql
GRANT pg_read_all_stats TO awr_owner;
GRANT awr_executor, awr_reader TO awr_user;
REVOKE ALL ON DATABASE awr FROM PUBLIC;
GRANT CONNECT ON DATABASE awr TO awr_executor, awr_reader;
-- Inside awr:
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA awr, awr_stats FROM PUBLIC;
GRANT USAGE ON SCHEMA awr TO awr_executor, awr_reader;
GRANT USAGE ON SCHEMA awr_stats TO awr_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA awr_stats TO awr_owner;
GRANT EXECUTE ON FUNCTION awr_stats.pg_stat_statements(boolean),
  awr_stats.pg_stat_statements_info() TO awr_owner;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system(),
  pg_catalog.pg_control_checkpoint() TO awr_owner;
GRANT EXECUTE ON FUNCTION awr.take_snapshot(), awr.sample_activity(),
  awr.purge_expired() TO awr_executor;
GRANT EXECUTE ON FUNCTION awr.list_snapshots(timestamptz,timestamptz),
  awr.report_json(bigint,bigint,integer), awr.report_html(bigint,bigint,integer)
  TO awr_reader;
```

All tables, partitions, ID sequences and routines in `awr`, both managed schemas, and the database belong to `awr_owner`. The `pg_stat_statements` extension's internal views/functions retain their superuser extension ownership. PUBLIC execution on *all* functions in both managed schemas and PUBLIC table privileges are revoked within the creating transaction. The extension's helper functions are not callable by runtime roles. Repository data is readable only through approved reporting functions; runtime roles have no direct SELECT or writes. Default PUBLIC function execution for objects subsequently created by awr_owner is revoked. Privileged routines are SECURITY DEFINER with fixed `search_path=pg_catalog,pg_temp`; helpers use invoker rights, the same fixed search path, and schema-qualified repository references.

A report reader can see **instance-wide workload metadata**, historical database/role names and opted-in statement text, across application databases. Grant `awr_reader` only to trusted reporting logins. `PUBLIC CONNECT` on application databases is separate and is not changed by the installer.

Verify as an administrator:

```sql
SELECT current_database(); -- awr
SELECT extname,extversion,extowner::regrole FROM pg_extension;
SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin
FROM pg_roles WHERE rolname LIKE 'awr_%';
SELECT member::regrole,roleid::regrole,admin_option FROM pg_auth_members
WHERE member IN (SELECT oid FROM pg_roles WHERE rolname LIKE 'awr_%');
SELECT has_function_privilege('awr_reader','awr.take_snapshot()','EXECUTE'); -- false
SELECT has_table_privilege('awr_user','awr.snapshots','INSERT'); -- false
SELECT has_table_privilege('awr_reader','awr.snapshots','SELECT'); -- false
SELECT n.nspname,p.proname,p.proowner::regrole,p.prosecdef,p.proconfig
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='awr';
-- Do not print the verifier itself into a shared terminal/log:
SELECT rolname,rolpassword LIKE 'SCRAM-SHA-256$%' AS scram
FROM pg_authid WHERE rolname='awr_user';
```

## Authentication

Place explicit rules before broader matching rules in `pg_hba.conf`, adjusted for your scheduler/report hosts:

```conf
host  awr  awr_user     127.0.0.1/32  scram-sha-256
host  awr  +awr_reader  127.0.0.1/32  scram-sha-256
host  awr  awr_user     ::1/128       scram-sha-256
host  awr  +awr_reader  ::1/128       scram-sha-256
# For remote use, require TLS and narrow trusted network ranges:
# hostssl awr awr_user 192.0.2.10/32 scram-sha-256
```

Reload HBA configuration after editing. A SCRAM verifier does not force an HBA rule to use passwords; `trust` and `peer` rules still behave as configured. Verify a TCP login with `psql -X -h 127.0.0.1 -U awr_user -d awr -W`. Use a mode-0600 passfile owned by the scheduler account with `host:port:awr:awr_user:<password>`, escaping `:` and `\` according to libpq rules. Provision it privately; never commit it. SQL text capture is off by default because even normalized statements can disclose identifiers or literals in utility commands. Bind values and activity query text are not collected.

## Pre-created empty database

`Conflicting names without verified bootstrap provenance: database awr` means `awr` already exists but the installer has no protected record of creating it. The normal workflow creates the database itself; do not pre-create it for a fresh install.

If you created an empty `awr` specifically for this installation, disconnect other clients from it and run the updated installer:

```sh
./bin/pg-awr-install --host localhost --username postgres -W --use-empty-database
```

Run this source-tree command from the project directory, or reinstall with `sudo make install PG_CONFIG=/path/to/pg_config` before using the installed script. An older installed copy will not recognize the option.

The option preserves the database OID and verifies that it is owned by the current installer, allows connections, is not a template, has the default unlimited connection limit, and has no comment, custom database settings, or database grants to other roles. Existing managed roles are rejected. The emptiness check examines local catalogs for objects created after initdb, including functions/types in system schemas, and separately checks large objects and subscriptions. A database made from a customized template can therefore be rejected even if it has no application tables.

The installer temporarily disables new connections and refuses to proceed while other sessions or prepared transactions remain. It creates roles, changes database ownership, revokes PUBLIC privileges, and records provenance in one transaction. It then follows the normal extension installation and interactive runtime-password workflow. Ordinary failures before adoption restore connection access; after adoption, rerun normally to resume from protected metadata. Healthy repeats may retain the option and still preserve the runtime password.

If the installer process or server is killed before that transaction commits, the database may remain closed with no bootstrap metadata. After verifying the database still has its original owner and no managed roles/metadata were created, restore access from `postgres` with `ALTER DATABASE awr ALLOW_CONNECTIONS true`, then retry the option. Do not use this procedure for a database with existing installation provenance; follow reconciliation below.

## Incomplete installation and reconciliation

Rerun the same installer as the same original superuser against the same primary after an ordinary error or cancellation. It verifies protected metadata ownership/ACLs, cluster identifier, exact object OIDs and provenance comments, role attributes and memberships before resuming. A postmaster change during one invocation aborts; rerunning against a stable primary is allowed. Healthy repeats validate and preserve the password.

For a state it cannot prove, the installer intentionally refuses to infer ownership from names alone. Examples: server failure after standalone `CREATE DATABASE` but before the database is tagged/recorded; failure after repository installation commits but before maintenance metadata records it; a dropped/recreated role, database, or copied stale bootstrap record. Do not delete unrelated objects to make installation succeed.

Administrator recovery steps:

1. Stop competing installers/schedulers and take backups. Inspect `SELECT * FROM awr_bootstrap.state` in `postgres` (no passwords stored) and `pg_shdescription` comments, role/database OIDs and owners.
2. Verify that `awr` and all roles belong to this bootstrap, that the database is primary, that the extension is owned by the recorded installer, and that repository/privilege checks pass. Keep `awr_user NOLOGIN` while reconciling.
3. If an empty database exists from the untagged CREATE DATABASE window, and you have independently verified it is solely the failed bootstrap's database, either drop **that empty database only** and clear `db_oid`/set `phase='roles'`, or tag it using `pg_awr bootstrap v1 <token>` and record its verified OID/`phase='database'`. Do not infer provenance solely from `datdba`.
4. For a verified successful repository commit with lagging metadata, set `phase='installed'` only after auditing the extension owners and grants. Rerun; a password is still required before LOGIN.
5. For restore/failover into a new cluster, independently provision roles, restore data, validate grants, and reconcile system/role/database OIDs and UUID comments in protected metadata. Do not copy old metadata and blindly mark it ready. If provenance cannot be established, retain backups and investigate instead of enabling the runtime login.

The tool never offers an unsafe `--force` adoption switch. `--use-empty-database` applies only to a verified empty database explicitly prepared for a new installation; it does not bypass existing provenance or role checks. A failed password rotation stays disabled; rerun or use `--rotate-password` to finish. An already healthy installation is not disabled merely because an unrelated preflight check failed.

## Backup and restore

Use a consistent full-database custom-format dump as superuser and back up cluster globals separately:

```sh
pg_dump -h localhost -U postgres -Fc -d awr -f awr.dump
pg_dumpall -h localhost -U postgres --globals-only --no-role-passwords > globals.sql
```

Treat backups as sensitive workload metadata. Protect files and encryption keys. `--no-role-passwords` intentionally requires reprovisioning SCRAM credentials. Never commit dumps or globals. Preserve extension SQL/control files for the dumped version.

The extension registers its configuration, snapshot/database/system histories, activity headers, and partition registry with `pg_extension_config_dump`; ordinary runtime-created partitions are dumped with their data and inherited indexes/FKs. Do not use schema-only dumps, filter out partitions, or assume the default extension behavior includes history. PostgreSQL restores ID sequence state so the next snapshot ID does not collide. A complete custom-format dump/restore is exercised by the integration suite.

Provision `awr_owner`, `awr_reader`, `awr_executor` and `awr_user` (NOLOGIN), their memberships and the two pg_control function grants first, on the target. Create **database named exactly awr**, owned by awr_owner with PUBLIC privileges revoked before enabling connections. Make the matching extension packages and preloaded pg_stat_statements available. Then:

```sh
pg_restore -h localhost -U postgres --exit-on-error -d awr awr.dump
```

Do not bootstrap an already populated `awr` before restoring into it; the installer deliberately rejects unrelated existing names. On a different cluster, restore into an empty, independently provisioned `awr`, reconcile bootstrap provenance as above, audit ownership/ACLs, and set a new password. Re-grant `CONNECT` to awr_reader/awr_executor if creating the database without restoring its ACL. `pg_dump` alone does not contain cluster roles or maintenance-database bootstrap metadata. Keep a separate protected backup of the provenance metadata for investigation; do not assume its OIDs are portable.

## Upgrades, failover, uninstall

Upgrade files as the OS package administrator using the matching pg_config, back up history, stop jobs, then run `pg-awr-install --upgrade` as the original true superuser. The shipped `1.0 → 1.1` migration adds a snapshot primary identity index, preserves all history and grants, and revokes PUBLIC privileges in the same transaction. The installer does not silently downgrade or automatically upgrade a healthy installation without `--upgrade`. Restart jobs after validation.

A physical failover or restart changes identity/reset evidence; reports spanning it mark affected deltas unknown. Pin jobs to the new writable primary. Never run independent collectors on both sides of a split brain. Collection on a standby is rejected; historical reports can be served there. Retain old history and choose snapshot endpoints after the transition when you need comparable rates.

To uninstall, stop timers/cron, disable `awr_user` login and back up history first. `DROP EXTENSION pg_awr CASCADE` destroys managed data, including partitions; only run it when intentionally discarding the repository. Dropping `awr` also destroys history and the local pg_stat_statements SQL objects. Remove the dedicated roles, their memberships, default privileges and pg_control grants only after auditing dependencies. Remove protected bootstrap metadata last after verifying that no recovery is needed. Do not remove pg_stat_statements from shared_preload_libraries if other tools use it. No uninstall command automatically deletes databases, roles or shared preloads.

## References

Implementation and behavior follow the primary PostgreSQL documentation: [extension packaging and configuration tables](https://www.postgresql.org/docs/18/extend-extensions.html), [CREATE DATABASE](https://www.postgresql.org/docs/18/sql-createdatabase.html), [statistics system](https://www.postgresql.org/docs/18/monitoring-stats.html), [pg_stat_statements](https://www.postgresql.org/docs/18/pgstatstatements.html), [psql password handling](https://www.postgresql.org/docs/18/app-psql.html), [libpq password encryption](https://www.postgresql.org/docs/18/libpq-misc.html).
