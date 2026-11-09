#!/usr/bin/env python3
"""Exercise opt-in initialization of pre-created awr on disposable clusters."""
import argparse
import json
import secrets
import time
from cluster import Cluster, ROOT, installer
from integration import check, failed_install, install


def run(version, reset):
    c = Cluster(version, 55500 + int(version))
    c.base = ROOT / f'.local/test-empty-{version}'
    c.data = c.base / 'data'
    c.socket = c.base / 'socket'
    c.params.update(host=str(c.socket))
    if reset:
        c.reset()
    c.setup()
    started = time.monotonic()
    cases = []
    option = ('--use-empty-database',)
    try:
        with c.db() as admin:
            admin.sql('CREATE DATABASE awr')
            database_state = "SELECT oid,datdba,datallowconn,ARRAY(SELECT acl FROM unnest(coalesce(datacl,acldefault('d',datdba))) AS a(acl) ORDER BY acl::text),shobj_description(oid,'pg_database') FROM pg_database WHERE datname='awr'"
            original = admin.sql(database_state)

        def untouched():
            with c.db() as admin:
                check(admin.sql(database_state) == original, 'rejected adoption changed database')
                check(admin.one("SELECT count(*) FROM pg_namespace WHERE nspname='awr_bootstrap'") == '0', 'rejected adoption created metadata')
                check(admin.one("SELECT count(*) FROM pg_roles WHERE rolname=ANY($1::text[])", '{'+','.join(installer.ROLES)+'}') == '0', 'rejected adoption created roles')

        failed_install(c, 'database awr')
        untouched()
        cases.append('pre-created database requires explicit opt-in')
        with c.db('awr'):
            failed_install(c, 'other sessions', extra=option)
        untouched()
        cases.append('active sessions rejected and connection access restored')

        fixtures = [
            ('table and data', 'CREATE TABLE public.keep_me(value text)', "INSERT INTO public.keep_me VALUES('preserve me')", 'DROP TABLE public.keep_me'),
            ('function in public', 'CREATE FUNCTION public.keep_me() RETURNS integer LANGUAGE sql AS $$SELECT 1$$', None, 'DROP FUNCTION public.keep_me()'),
            ('function in pg_catalog', 'CREATE FUNCTION pg_catalog.keep_me() RETURNS integer LANGUAGE sql AS $$SELECT 1$$', None, 'DROP FUNCTION pg_catalog.keep_me()'),
            ('type', "CREATE TYPE public.keep_me AS ENUM ('value')", None, 'DROP TYPE public.keep_me'),
            ('empty schema', 'CREATE SCHEMA keep_me', None, 'DROP SCHEMA keep_me'),
            ('extension', 'CREATE EXTENSION pg_stat_statements SCHEMA public', None, 'DROP EXTENSION pg_stat_statements'),
            ('large object with low OID', 'SELECT lo_create(1234)', None, 'SELECT lo_unlink(1234)'),
        ]
        for label, create, seed, cleanup in fixtures:
            with c.db('awr') as repo:
                repo.sql(create)
                if seed:
                    repo.sql(seed)
            failed_install(c, 'not empty', extra=option)
            untouched()
            with c.db('awr') as repo:
                if seed:
                    check(repo.one('SELECT value FROM public.keep_me') == 'preserve me', 'existing data changed')
                repo.sql(cleanup)
            cases.append(label + ' rejected and preserved')

        with c.db() as admin:
            admin.sql('CREATE ROLE different_owner')
            admin.sql('ALTER DATABASE awr OWNER TO different_owner')
        failed_install(c, 'owned by the installer', extra=option)
        with c.db() as admin:
            check(admin.one("SELECT datdba::regrole::text FROM pg_database WHERE datname='awr'") == 'different_owner', 'unrelated owner changed')
            admin.sql('ALTER DATABASE awr OWNER TO postgres')
            admin.sql('DROP ROLE different_owner')
            admin.sql('CREATE ROLE awr_reader')
        failed_install(c, 'existing awr roles', extra=option)
        with c.db() as admin:
            admin.sql('DROP ROLE awr_reader')
            admin.sql("ALTER DATABASE awr SET statement_timeout='1s'")
        failed_install(c, 'custom database settings', extra=option)
        with c.db() as admin:
            admin.sql('ALTER DATABASE awr RESET ALL')
            admin.sql('CREATE ROLE other_client')
            admin.sql('GRANT CONNECT ON DATABASE awr TO other_client')
        failed_install(c, 'grants to other roles', extra=option)
        with c.db() as admin:
            check(admin.one("SELECT has_database_privilege('other_client','awr','CONNECT')") == 't', 'rejection removed unrelated grant')
            admin.sql('REVOKE CONNECT ON DATABASE awr FROM other_client')
            admin.sql('DROP ROLE other_client')
        untouched()
        cases.append('unrelated owner, role collisions, database settings, and grants rejected')

        # Force a failure during the adoption transaction: metadata, roles, owner,
        # tag and PUBLIC ACL must roll back together, restoring connections.
        with c.db() as admin:
            ident = installer.identity(admin)
            admin.sql('SELECT pg_advisory_lock(728301,0)')
            sql = admin.sql

            def fail_comment(query, *params):
                if query.startswith('COMMENT ON DATABASE awr'):
                    raise installer.Error('injected adoption failure')
                return sql(query, *params)

            admin.sql = fail_comment
            try:
                with installer.empty_database(admin, ident, c.params, True) as oid:
                    installer.metadata(admin, ident, oid)
            except installer.Error as exc:
                check(str(exc) == 'injected adoption failure', 'unexpected adoption failure: '+str(exc))
            else:
                raise AssertionError('injected failure did not fire')
        untouched()
        cases.append('failed adoption rolls back ownership, roles, metadata, and privileges')

        with c.db() as admin:
            ident = installer.identity(admin)
            admin.sql('SELECT pg_advisory_lock(728301,0)')
            try:
                with installer.empty_database(admin, ident, c.params, True) as oid:
                    installer.metadata(admin, ident, oid)
                    raise installer.Error('injected interruption after adoption')
            except installer.Error as exc:
                check(str(exc) == 'injected interruption after adoption', str(exc))
            check(admin.one("SELECT datallowconn FROM pg_database WHERE datname='awr'") == 'f', 'incomplete adoption opened database')
            check(admin.one("SELECT count(*) FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a WHERE datname='awr' AND a.grantee=0") == '0', 'incomplete adoption retained PUBLIC privileges')
            check(admin.one('SELECT phase FROM awr_bootstrap.state') == 'database', 'adoption provenance missing')
        cases.append('interruption after adoption keeps database closed with resumable provenance')

        install(c, cancel=True, extra=option)
        with c.db() as admin:
            check(admin.one("SELECT oid FROM pg_database WHERE datname='awr'") == original[0][0], 'adoption recreated database')
            check(admin.one("SELECT rolcanlogin FROM pg_roles WHERE rolname='awr_user'") == 'f', 'cancel enabled login')
        password = secrets.token_urlsafe(24)
        install(c, password)  # Resume uses recorded provenance without the flag.
        check('preserved' in install(c, extra=option), 'repeat did not preserve password')
        with c.db('awr') as repo:
            installer.validate(repo)
        with c.db('awr', 'awr_user', password, tcp=True) as runtime:
            check(runtime.one('SELECT current_database()') == 'awr', 'SCRAM login failed')
            check(runtime.one('SELECT awr.take_snapshot()') is not None, 'adopted database cannot collect')
        cases.append('same database OID, cancellation/resume, repeat, SCRAM, privileges, and collection')
        with c.db() as admin:
            admin.sql("COMMENT ON DATABASE awr IS 'unexpected identity'")
        failed_install(c, 'Database ownership/provenance ambiguous', extra=option)
        cases.append('opt-in does not bypass existing provenance checks')
        result = dict(version=version, passed=len(cases), cases=cases, seconds=round(time.monotonic()-started, 2))
        print(json.dumps(result, indent=2), flush=True)
        return result
    finally:
        c.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions', nargs='+', default=['15', '16', '17', '18'])
    parser.add_argument('--reset', action='store_true', help='remove only marked disposable test-empty clusters')
    args = parser.parse_args()
    results = [run(version, args.reset) for version in args.versions]
    (ROOT/'docs/empty-database-test-results.json').write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    main()
