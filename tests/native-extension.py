#!/usr/bin/env python3
"""Exercise real package payloads in isolated PostgreSQL 15–18 clusters."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from cluster import Cluster, Error, ROOT


def check(value, message):
    if not value:
        raise AssertionError(message)


def denied(db, sql, message=None):
    try:
        db.sql(sql)
    except Error as error:
        if message:
            check(message in str(error), str(error))
        return
    raise AssertionError('Unexpected success: ' + sql)


def package_payload(version, fmt, target):
    subprocess.run(['python3', str(ROOT/'packaging/build.py'), fmt,
                    '--pg-major', str(version), '--output', str(target)], check=True,
                   stdout=subprocess.DEVNULL)
    extracted = target/'extracted'
    extracted.mkdir()
    if fmt == 'deb':
        package = next(target.glob('*.deb'))
        fields = subprocess.check_output(['dpkg-deb', '-f', str(package)], text=True)
        check(f'Depends: postgresql-{version}, postgresql-client-{version}' in fields, 'deb dependencies')
        subprocess.run(['dpkg-deb', '-x', str(package), str(extracted)], check=True)
        control = target/'control'
        subprocess.run(['dpkg-deb', '-e', str(package), str(control)], check=True)
        check(not any((control/name).exists() for name in ('preinst','postinst','prerm','postrm')), 'unexpected maintainer scripts')
        payload = extracted/f'usr/share/postgresql/{version}/extension'
        job = extracted/f'usr/lib/postgresql/{version}/bin/pg-awr-job'
    else:
        package = next(target.glob('*.noarch.rpm'))
        rpm = ['rpm', '--dbpath', str(target/'rpmdb')]
        subprocess.run([*rpm, '--initdb'], check=True)
        requires = subprocess.check_output([*rpm, '-qp', '--requires', str(package)], text=True)
        for dependency in (f'postgresql{version}', f'postgresql{version}-server', f'postgresql{version}-contrib'):
            check(dependency in requires.splitlines(), 'rpm dependencies: '+dependency)
        check(not subprocess.check_output([*rpm, '-qp', '--scripts', str(package)], text=True).strip(), 'unexpected RPM scriptlets')
        with tempfile.TemporaryFile() as archive:
            subprocess.run(['rpm2cpio', str(package)], stdout=archive, check=True)
            archive.seek(0)
            subprocess.run(['cpio', '-id', '--quiet'], cwd=extracted, stdin=archive, check=True)
        payload = extracted/f'usr/pgsql-{version}/share/extension'
        job = extracted/f'usr/pgsql-{version}/bin/pg-awr-job'
    check(payload.joinpath('pg_awr--1.2.sql').is_file(), 'missing native SQL')
    check(not list(extracted.rglob('pg-awr-install')), 'package requires legacy installer')
    return payload, job


def counts(db):
    return db.sql('SELECT (SELECT count(*) FROM awr.snapshots),'
                  '(SELECT count(*) FROM awr.statement_history),'
                  '(SELECT count(*) FROM awr.activity_batches),'
                  '(SELECT count(*) FROM awr.activity_history)')


def dump_restore(c, source, destination):
    dump = c.base/(source+'.dump')
    with c.db(source) as admin:
        before = counts(admin)
        config = admin.sql('SELECT * FROM awr.config')
    c.command('pg_dump', '-h', c.socket, '-p', c.port, '-U', 'postgres', '-Fc', '-f', dump, source)
    with c.db() as admin:
        admin.sql('CREATE DATABASE '+destination)
    c.command('pg_restore', '-h', c.socket, '-p', c.port, '-U', 'postgres', '--exit-on-error', '-d', destination, dump)
    with c.db(destination) as admin:
        check(before == counts(admin), 'restore lost history')
        check(config == admin.sql('SELECT * FROM awr.config'), 'restore lost configuration')
        check(int(admin.one('SELECT awr.take_snapshot()')) > int(before[0][0]), 'restored sequence collided')


def run(c, fmt):
    started = time.monotonic()
    cases = []
    def passed(message):
        cases.append(message)
        print(f'[PostgreSQL {c.version}, {fmt}] PASS: {message}', flush=True)

    with tempfile.TemporaryDirectory(prefix='pg-awr-test-package-') as tmp:
        payload, job = package_payload(c.version, fmt, Path(tmp))
        c.setup(extension_payload=payload)
        try:
            passed('native package paths, dependencies, no bootstrap or install hooks')
            with c.db() as admin:
                admin.sql('CREATE DATABASE monitoring')
                admin.sql('CREATE DATABASE cascade_install')
                admin.sql('CREATE ROLE native_reader LOGIN')
                admin.sql('CREATE ROLE native_collector LOGIN')
                admin.sql('CREATE ROLE native_outsider LOGIN')
                roles = admin.sql('SELECT oid,rolname FROM pg_roles ORDER BY oid')
                databases = admin.sql('SELECT oid,datname FROM pg_database ORDER BY oid')
            with c.db('monitoring') as admin:
                admin.sql('CREATE SCHEMA "stats source"')
                admin.sql('CREATE EXTENSION pg_stat_statements SCHEMA "stats source"')
                admin.sql('CREATE EXTENSION pg_awr')
                check(admin.one("SELECT extversion FROM pg_extension WHERE extname='pg_awr'") == '1.2', 'version')
                check(roles == admin.sql('SELECT oid,rolname FROM pg_roles ORDER BY oid'), 'created or changed roles')
                check(databases == admin.sql('SELECT oid,datname FROM pg_database ORDER BY oid'), 'created databases')
                check(admin.one("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='awr' AND p.proowner<>'postgres'::regrole") == '0', 'wrong function owners')
            passed('CREATE EXTENSION in arbitrary database, custom dependency schema, no global roles')
            with c.db('cascade_install') as admin:
                admin.sql('SET search_path=public')
                admin.sql('SET compute_query_id=off')
                denied(admin, 'CREATE EXTENSION pg_awr CASCADE', 'compute_query_id')
                check(admin.one("SELECT count(*) FROM pg_extension WHERE extname IN ('pg_awr','pg_stat_statements')") == '0', 'failed install was not atomic')
                admin.sql('SET compute_query_id=auto')
            with (c.data/'postgresql.conf').open('a') as config:
                config.write("\nshared_preload_libraries=''\n")
            c.stop(); c.start()
            with c.db('cascade_install') as admin:
                admin.sql('SET search_path=public')
                denied(admin, 'CREATE EXTENSION pg_awr CASCADE', 'must be preloaded')
                check(admin.one("SELECT count(*) FROM pg_namespace WHERE nspname='awr'") == '0', 'failed schema remained')
            with (c.data/'postgresql.conf').open('a') as config:
                config.write("\nshared_preload_libraries='pg_stat_statements'\n")
            c.stop(); c.start()
            with c.db('cascade_install') as admin:
                admin.sql('SET search_path=public')
                admin.sql('CREATE EXTENSION pg_awr CASCADE')
                admin.sql('SELECT awr.take_snapshot()')
                admin.sql('DROP EXTENSION pg_awr CASCADE')
                check(admin.one("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='awr'") == '0', 'drop left data')
                check(admin.one("SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'") == '1', 'drop removed prerequisite')
                admin.sql('CREATE SCHEMA IF NOT EXISTS awr AUTHORIZATION native_outsider')
                # DROP EXTENSION can leave a pre-existing namespace; explicitly set its owner.
                admin.sql('ALTER SCHEMA awr OWNER TO native_outsider')
                denied(admin, 'CREATE EXTENSION pg_awr', 'schema awr must be owned')
            passed('CASCADE, atomic prerequisite errors, unsafe schema rejection and DROP EXTENSION')
            with c.db('monitoring', 'native_outsider') as outsider:
                for sql in ('SELECT awr.take_snapshot()', 'SELECT awr._config()', 'SELECT * FROM awr.config'):
                    denied(outsider, sql)
            with c.db('monitoring') as admin:
                admin.sql('GRANT USAGE ON SCHEMA awr TO native_reader,native_collector')
                admin.sql('GRANT EXECUTE ON FUNCTION awr.take_snapshot(),awr.sample_activity(),awr.purge_expired() TO native_collector')
                admin.sql('GRANT EXECUTE ON FUNCTION awr.list_snapshots(timestamptz,timestamptz),awr.report_json(bigint,bigint,integer),awr.report_html(bigint,bigint,integer) TO native_reader')
            with c.db('monitoring', 'native_collector') as collector:
                denied(collector, 'SELECT * FROM awr.config')
                denied(collector, 'SELECT awr.report_json(1,2)')
                check(collector.one('SELECT awr.sample_activity()') == '0', 'sampling default')
                first = int(collector.one('SELECT awr.take_snapshot()'))
            with c.db('monitoring') as admin:
                admin.sql('UPDATE awr.config SET activity_enabled=true,capture_sql_text=true')
            with c.db('monitoring', 'native_collector') as collector:
                collector.sql('SELECT awr.sample_activity()')
                last = int(collector.one('SELECT awr.take_snapshot()'))
                collector.sql('SELECT awr.purge_expired()')
            with c.db('monitoring', 'native_reader') as reader:
                denied(reader, 'SELECT awr.take_snapshot()')
                denied(reader, 'SELECT awr._config()')
                report = json.loads(reader.one('SELECT awr.report_json($1,$2)', first, last))
                check(report['snapshot_observations'] == 2, 'report endpoints')
                check(reader.one('SELECT awr.report_html($1,$2)', first, last).startswith('<!doctype html>'), 'HTML report')
            with c.db('monitoring', 'native_outsider') as outsider:
                denied(outsider, 'SELECT * FROM awr.statement_history')
            passed('explicit collector/reader grants, private helpers/history, snapshots, sampling and reports')
            env = dict(os.environ, PATH=str(c.bindir)+os.pathsep+os.environ['PATH'],
                       PGHOST=str(c.socket), PGPORT=str(c.port), PGDATABASE='monitoring', PGUSER='native_collector')
            result = subprocess.run([str(job), 'snapshot'], env=env, text=True, capture_output=True)
            check(result.returncode == 0, result.stderr)
            passed('packaged job honors PGDATABASE/PGUSER and runs a snapshot')
            dump_restore(c, 'monitoring', 'monitoring_restore')
            passed('dump/restore to a different database preserves configuration, partition data and sequences')
            with c.db() as admin:
                for role in ('awr_owner','awr_reader','awr_executor','awr_user'):
                    admin.sql('CREATE ROLE '+role)
                admin.sql('GRANT pg_read_all_stats TO awr_owner')
                admin.sql('GRANT awr_reader,awr_executor TO awr_user')
                admin.sql('CREATE DATABASE awr OWNER awr_owner')
            with c.db('awr') as admin:
                admin.sql('CREATE EXTENSION pg_stat_statements SCHEMA public')
                admin.sql('GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system(),pg_catalog.pg_control_checkpoint() TO awr_owner')
                admin.sql("CREATE EXTENSION pg_awr VERSION '1.0'")
                admin.sql('SELECT awr.take_snapshot()')
                before = counts(admin)
                admin.sql('ALTER EXTENSION pg_awr UPDATE')
                check(admin.one("SELECT extversion FROM pg_extension WHERE extname='pg_awr'") == '1.2', 'upgrade chain')
                check(before == counts(admin), 'upgrade lost data')
                admin.sql('SET ROLE awr_user')
                admin.sql('SELECT awr.take_snapshot()')
                admin.sql("SELECT * FROM awr.list_snapshots(now()-interval '1 day',now())")
                admin.sql('RESET ROLE')
            dump_restore(c, 'awr', 'upgraded_restore')
            passed('1.0 → 1.1 → 1.2 upgrade retains history and runtime grants; upgraded dump restores')
            with c.db() as admin:
                version = admin.one('SELECT version()')
            return dict(version=version, package_format=fmt, passed=len(cases), cases=cases,
                        seconds=round(time.monotonic()-started, 2))
        finally:
            if (c.data/'postmaster.pid').exists():
                c.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions', nargs='+', choices=['15','16','17','18'], default=['15','16','17','18'])
    parser.add_argument('--formats', nargs='+', choices=['deb','rpm'], default=['deb'])
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    results = []
    for fmt in args.formats:
        for version in args.versions:
            c = Cluster(version, port=55600+int(version))
            c.base = ROOT/f'.local/test-native-{fmt}-{version}'
            c.data = c.base/'data'
            c.socket = c.base/'socket'
            c.params['host'] = str(c.socket)
            if args.reset:
                c.reset()
            results.append(run(c, fmt))
    (ROOT/'docs/native-package-test-results.json').write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    main()
