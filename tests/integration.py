#!/usr/bin/env python3
"""Real PostgreSQL acceptance tests. Uses disposable local ports 55415–55418."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import pexpect
from cluster import Cluster, ROOT, DB, Error, installer

RESULTS = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def denied(db, sql):
    try:
        db.sql(sql)
    except Error:
        return
    raise AssertionError('Unexpectedly allowed: ' + sql)


def install(c, password=None, extra=(), cancel=False):
    child = pexpect.spawn(c.install_args(*extra)[0],c.install_args(*extra)[1:],encoding='utf-8',timeout=60,echo=False)
    if password is not None or cancel:
        child.expect('New password for awr_user: ')
        if cancel:
            child.sendcontrol('c')
        else:
            child.sendline(password)
            child.expect('Repeat password for awr_user: ')
            child.sendline(password)
    child.expect(pexpect.EOF)
    output=child.before
    child.close()
    if cancel:
        check(child.exitstatus != 0, 'cancel should fail')
    else:
        check(child.exitstatus == 0, 'installer failed: '+output)
    if password:
        check(password not in output,'password appeared in installer output')
    return output


def failed_install(c, fragment, extra=()):
    p = subprocess.run(c.install_args(*extra),text=True,capture_output=True)
    check(p.returncode != 0 and fragment in p.stderr, 'Expected rejection '+fragment+': '+p.stdout+p.stderr)


def run(c, reuse=False):
    started=time.monotonic()
    if not reuse:
        c.setup(default_version='1.1')
    class Cases(list):
        def append(self,case):
            super().append(case)
            print(f'[PostgreSQL {c.version}] PASS: {case}',flush=True)
    cases=Cases()
    with (c.data/'postgresql.conf').open('a') as f: f.write("\nshared_preload_libraries=''\n")
    c.stop(); c.start()
    failed_install(c,'not preloaded')
    with (c.data/'postgresql.conf').open('a') as f: f.write("\nshared_preload_libraries='pg_stat_statements'\n")
    c.stop(); c.start()
    cases.append('missing preload rejected before bootstrap')
    with c.db() as admin:
        admin.sql('CREATE ROLE impostor LOGIN CREATEDB CREATEROLE')
    failed_install(c,'true superusers',extra=('--username','impostor'))
    cases.append('CREATEDB/CREATEROLE rejected')
    with c.db() as admin:
        admin.sql('SELECT pg_advisory_lock(728301,0)')
        failed_install(c,'bootstrap lock')
    cases.append('concurrent bootstrap serialized')
    with c.db() as admin:
        admin.sql('CREATE ROLE awr_reader')
    failed_install(c,'Conflicting names')
    with c.db() as admin:
        admin.sql('DROP ROLE awr_reader')
    cases.append('name collision rejected')
    # The supported interactive installer performs the initial database creation.
    install(c,cancel=True,extra=('--version','1.0'))
    with c.db() as admin:
        check(admin.one("SELECT rolcanlogin FROM pg_roles WHERE rolname='awr_user'")=='f','cancel left LOGIN enabled')
        check(admin.one("SELECT datname FROM pg_database WHERE datname='awr'")=='awr','wrong repository database')
    password=secrets.token_urlsafe(24)
    install(c,password,extra=('--version','1.0'))
    with c.db() as admin:
        before=admin.one("SELECT rolpassword FROM pg_authid WHERE rolname='awr_user'")
        check(before.startswith('SCRAM-SHA-256$'),'not SCRAM')
    check('preserved' in install(c,extra=('--version','1.0')),'healthy repeat did not preserve password')
    with c.db() as admin:
        check(before==admin.one("SELECT rolpassword FROM pg_authid WHERE rolname='awr_user'"),'repeat changed password')
    with c.db('awr','awr_user',password,tcp=True) as runtime:
        check(runtime.one('select current_database()')=='awr','SCRAM runtime database')
    p=c.command('psql','-X','-h',c.socket,'-p',c.port,'-U','postgres','-d','awr','-At','-v','ON_ERROR_STOP=1','-c','SELECT current_database()')
    check(p.stdout.strip()=='awr','psql -X -d awr failed')
    cases.append('fresh install, TTY cancellation/resume, SCRAM, repeat install')
    with c.db() as admin:
        admin.sql('CREATE ROLE report_login LOGIN')
        admin.sql('GRANT awr_reader TO report_login')
        admin.sql('CREATE ROLE collect_login LOGIN')
        admin.sql('GRANT awr_executor TO collect_login')
        admin.sql('CREATE ROLE outsider LOGIN')
        check(admin.one("SELECT has_database_privilege('outsider','awr','CONNECT')")=='f','PUBLIC CONNECT')
    with c.db('awr','report_login') as reader:
        denied(reader,'SELECT awr.take_snapshot()')
        denied(reader,'SELECT * FROM awr.snapshots')
        denied(reader,'SELECT awr._config()')
        denied(reader,'SET ROLE awr_owner')
        denied(reader,'SELECT awr_stats.pg_stat_statements_reset()')
    with c.db('awr','collect_login') as collector:
        denied(collector,'SELECT awr.report_json(1,2)')
        denied(collector,'DELETE FROM awr.snapshots')
        denied(collector,'CREATE TABLE awr.injected(x int)')
        denied(collector,'SELECT pg_stat_reset()')
        check(collector.one('SELECT awr.sample_activity()')=='0','disabled sampling')
        first=int(collector.one('SELECT awr.take_snapshot()'))
    with c.db('awr') as admin:
        denied(admin,'SELECT awr.report_json(1,1)')
        denied(admin,'SELECT awr.report_json(-1,2)')
        denied(admin,'SELECT awr.report_json(1,2,0)')
        check(admin.one("SELECT count(*) FROM awr.statement_history WHERE query_text IS NOT NULL")=='0','SQL text enabled by default')
    with c.db('postgres') as admin:
        admin.sql('CREATE EXTENSION pg_stat_statements SCHEMA public')
        denied(admin,'CREATE EXTENSION pg_awr')
    cases.append('runtime/reader/PUBLIC least privilege, wrong database, input validation')
    with c.db() as admin:
        admin.sql('CREATE DATABASE workload_a')
        admin.sql('CREATE DATABASE workload_b')
    for name in ('workload_a','workload_b'):
        with c.db(name) as work:
            work.sql('CREATE TABLE public.load_rows(id int primary key, value text)')
            work.sql("INSERT INTO public.load_rows SELECT n,repeat('x',80) FROM generate_series(1,100) n")
            work.sql('SELECT sum(id) FROM public.load_rows')
            work.sql('SELECT pg_stat_force_next_flush()')
    with c.db('awr','awr_user') as runtime:
        baseline=int(runtime.one('SELECT awr.take_snapshot()'))
    for name in ('workload_a','workload_b'):
        with c.db(name) as work:
            for _ in range(4): work.sql('SELECT sum(id) FROM public.load_rows')
            work.sql("UPDATE public.load_rows SET value=repeat('y',80) WHERE id<20")
            work.sql('SELECT pg_stat_force_next_flush()')
    with c.db('awr') as admin:
        admin.sql('UPDATE awr.config SET activity_enabled=true,capture_sql_text=true,activity_interval_seconds=1')
    holder=c.db('workload_a')
    waiter=None
    try:
        holder.sql('BEGIN')
        holder.sql('UPDATE public.load_rows SET value=\'held\' WHERE id=1')
        waiter=subprocess.Popen([str(c.bindir/'psql'),'-X','-h',str(c.socket),'-p',str(c.port),'-U','postgres','-d','workload_a','-v','ON_ERROR_STOP=1','-c',"UPDATE public.load_rows SET value='waiting' WHERE id=1"],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        with c.db('awr') as admin:
            for _ in range(100):
                if int(admin.one("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'")):
                    break
                time.sleep(.02)
            else: raise AssertionError('no observed lock waiter')
        with c.db('awr','awr_user') as runtime:
            check(int(runtime.one('SELECT awr.sample_activity()'))>=1,'active waiter not sampled')
        with c.db('awr') as admin:
            check(int(admin.one("SELECT count(*) FROM awr.activity_history WHERE is_blocker AND state='idle in transaction'"))>=1,'idle blocker missing')
            check(admin.one("SELECT count(*) FROM information_schema.columns WHERE table_schema='awr' AND table_name='activity_history' AND column_name='query'")=='0','raw activity query stored')
    finally:
        holder.sql('ROLLBACK'); holder.close()
        if waiter: waiter.communicate(timeout=10)
    time.sleep(2.2)  # Deliberately miss scheduled observations; they must not become idle samples.
    with c.db('awr','awr_user') as runtime:
        runtime.sql('SELECT awr.sample_activity()')
        end=int(runtime.one('SELECT awr.take_snapshot()'))
    with c.db('awr','report_login') as reader:
        report=json.loads(reader.one('SELECT awr.report_json($1,$2)',baseline,end))
        check(report['schema_version']==1,'JSON schema')
        for name in ('workload_a','workload_b'):
            check(any(x['dbname']==name and x['delta'].get('calls',0)>=4 for x in report['top_sql']['calls']), 'cross-database calls missing: '+name)
        check(report['activity']['successful_samples']==2,'coverage denominator')
        check(report['activity']['aas_denominator']==2,'AAS denominator')
        check(report['activity']['coverage_ratio']<1,'missed samples hidden')
        check(report['activity']['largest_observed_gap_seconds']>=2,'missing-sample gap not shown')
        check(report['activity']['observed_blockers'],'blocker report missing')
        check(any(x['wait_event_type']=='Lock' for x in report['activity']['waits']),'wait report missing')
        check(any(x['source']=='pg_stat_wal' for x in report['metrics']),'WAL metrics missing')
        io_available=report['quality']['snapshots'][0]['sources']['pg_stat_io']
        check(io_available==(int(c.version)>=16),'pg_stat_io adapter')
        check(report['quality']['snapshots'][0]['sources']['pg_stat_checkpointer']==(int(c.version)>=17),'checkpoint adapter')
    cases.append('two-database workload, lock waits, idle blockers, sampling coverage, version adapters')
    with c.db('awr') as admin:
        admin.sql("UPDATE awr.statement_history SET query_text='<script>alert(1)</script>&\"' WHERE snap_id=$1",end)
    with c.db('awr','report_login') as reader:
        html=reader.one('SELECT awr.report_html($1,$2)',baseline,end)
        check('<script>' not in html and '&lt;script&gt;' in html,'HTML escaping')
    with c.db('awr') as admin:
        admin.sql('BEGIN'); admin.sql('SELECT pg_advisory_xact_lock(728301,1)')
        with c.db('awr','awr_user') as runtime:
            check(runtime.one('SELECT awr.take_snapshot()') is None,'overlap snapshot')
        admin.sql('ROLLBACK')
    with c.db('awr') as admin:
        old_count=admin.one('SELECT count(*) FROM awr.snapshots')
        admin.sql('BEGIN'); admin.sql('LOCK TABLE awr.statement_history IN ACCESS EXCLUSIVE MODE')
        with c.db('awr','awr_user') as runtime:
            runtime.sql("SET statement_timeout='100ms'")
            denied(runtime,'SELECT awr.take_snapshot()')
        admin.sql('ROLLBACK')
        check(admin.one('SELECT count(*) FROM awr.snapshots')==old_count,'timeout committed partial snapshot')
        for key,fn in ((2,'sample_activity'),(3,'purge_expired')):
            admin.sql('BEGIN'); admin.sql('SELECT pg_advisory_xact_lock(728301,$1::integer)',key)
            with c.db('awr','awr_user') as runtime:
                check(runtime.one('SELECT awr.'+fn+'()') is None,'overlap '+fn)
            admin.sql('ROLLBACK')
    cases.append('HTML escaping, overlapping jobs, timeout atomicity')
    # Intermediate reset evidence is sufficient even when endpoint counters recover.
    with c.db('awr') as admin:
        middle=int(admin.one('SELECT awr.take_snapshot()'))
        admin.sql("SELECT awr_stats.pg_stat_statements_reset()")
        reset_end=int(admin.one('SELECT awr.take_snapshot()'))
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',end,reset_end))
        check('pg_stat_statements reset' in report['quality']['statement_reasons'],'reset not detected')
        check(not report['top_sql']['calls'],'reset interval ranked')
        admin.sql("UPDATE awr.snapshots SET statement_info=jsonb_set(statement_info,'{stats_reset}',(SELECT statement_info->'stats_reset' FROM awr.snapshots WHERE snap_id=$1)) WHERE snap_id=$2",end,reset_end)
        admin.sql("UPDATE awr.snapshots SET statement_info=jsonb_set(statement_info,'{stats_reset}',to_jsonb(clock_timestamp())) WHERE snap_id=$1",middle)
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',end,reset_end))
        check('pg_stat_statements reset' in report['quality']['statement_reasons'],'intermediate reset not detected')
        admin.sql('UPDATE awr.config SET statement_limit=1')
        capped=int(admin.one('SELECT awr.take_snapshot()'))
        check(admin.one('SELECT statements_truncated FROM awr.snapshots WHERE snap_id=$1',capped)=='t','cap not recorded')
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',reset_end,capped))
        check('statement capture truncated' in report['quality']['statement_reasons'],'cap not reported')
        admin.sql('UPDATE awr.config SET statement_limit=0')
        evict_start=int(admin.one('SELECT awr.take_snapshot()'))
    with c.db('workload_b') as work:
        for n in range(650): work.sql('SELECT '+','.join(str(i) for i in range(n+1)))
    with c.db('awr') as admin:
        evict_end=int(admin.one('SELECT awr.take_snapshot()'))
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',evict_start,evict_end))
        check('statement eviction: continuity uncertain' in report['quality']['statement_reasons'],'eviction not detected')
        # Synthetic adversarial intermediate counter decrease; final value exceeds initial.
        points=[dict(counters={'calls':x},marker={},settings={}) for x in (10,2,20)]
        delta=json.loads(admin.one("SELECT awr._delta($1::jsonb,3,10,'{}')",json.dumps(points)))
        check(delta['delta']['calls'] is None and 'counter decreased' in delta['metric_reasons']['calls'],'counter decrease')
        delta=json.loads(admin.one("SELECT awr._delta($1::jsonb,4,10,'{}')",json.dumps(points)))
        check(delta['reasons'],'missing observation not marked')
    cases.append('reset and intermediate evidence, statement cap, eviction, counter decreases')
    before_restart=evict_end
    c.stop(); c.start()
    with c.db('awr') as admin:
        after_restart=int(admin.one('SELECT awr.take_snapshot()'))
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',before_restart,after_restart))
        check(report['quality']['interval_reasons'],'restart continuity not detected')
        admin.sql("SET track_io_timing=off")
        timing_start=int(admin.one('SELECT awr.take_snapshot()'))
        timing_end=int(admin.one('SELECT awr.take_snapshot()'))
        report=json.loads(admin.one('SELECT awr.report_json($1,$2)',timing_start,timing_end))
        check(any('blk_read_time' in x['metric_reasons'] for x in report['database_activity']),'disabled timing treated as zero')
        history_count=admin.one('SELECT count(*) FROM awr.snapshots')
    install(c,extra=('--upgrade',))
    with c.db('awr') as admin:
        check(admin.one("SELECT extversion FROM pg_extension WHERE extname='pg_awr'")=='1.1','upgrade version')
        check(admin.one('SELECT count(*) FROM awr.snapshots')==history_count,'upgrade lost history')
        installer.validate(admin)
    cases.append('restart discontinuity, disabled timing, 1.0 to 1.1 upgrade preserves history/privileges')
    install(c,cancel=True,extra=('--rotate-password',))
    with c.db() as admin:
        check(admin.one("SELECT rolcanlogin FROM pg_roles WHERE rolname='awr_user'")=='f','rotation cancellation left login enabled')
    rotated=secrets.token_urlsafe(24)
    install(c,rotated)
    with c.db('awr','awr_user',rotated,tcp=True) as runtime: runtime.sql('SELECT awr.sample_activity()')
    try:
        c.db('awr','awr_user',password,tcp=True)
    except Error:
        pass
    else: raise AssertionError('old password still works')
    cases.append('password rotation and cancelled rotation recovery')
    # Dump all history, including partitions, drop only the disposable test repository, restore exactly awr.
    dump=c.base/'history.dump'
    c.command('pg_dump','-h',c.socket,'-p',c.port,'-U','postgres','-Fc','-f',dump,'awr')
    with c.db('awr') as admin:
        before_counts=admin.sql('SELECT (SELECT count(*) FROM awr.snapshots),(SELECT count(*) FROM awr.statement_history),(SELECT count(*) FROM awr.activity_history)')
    with c.db() as admin:
        admin.sql('DROP DATABASE awr')
        admin.sql('CREATE DATABASE awr OWNER awr_owner TEMPLATE template0')
        admin.sql('REVOKE ALL ON DATABASE awr FROM PUBLIC')
        admin.sql('GRANT CONNECT ON DATABASE awr TO awr_executor,awr_reader')
    c.command('pg_restore','-h',c.socket,'-p',c.port,'-U','postgres','--exit-on-error','-d','awr',dump)
    with c.db('awr') as admin:
        check(before_counts==admin.sql('SELECT (SELECT count(*) FROM awr.snapshots),(SELECT count(*) FROM awr.statement_history),(SELECT count(*) FROM awr.activity_history)'),'dump/restore lost history')
        installer.validate(admin)
        admin.sql('SELECT awr.take_snapshot()')
        # Seed an old partition/header without modifying or waiting for current data.
        old=admin.one("INSERT INTO awr.snapshots(collected_at,server_version,system_identifier,postmaster_start,timeline_id,settings,sources) SELECT clock_timestamp()-interval '60 days',server_version,system_identifier,postmaster_start,timeline_id,settings,sources FROM awr.snapshots ORDER BY snap_id LIMIT 1 RETURNING snap_id")
        admin.sql("SELECT awr._partition('statement_history',clock_timestamp()-interval '60 days')")
        admin.sql("INSERT INTO awr.statement_history SELECT snap_id,collected_at,0,0,0,true,'old','old',NULL,'{}','{}' FROM awr.snapshots WHERE snap_id=$1",old)
        admin.sql('SELECT awr.purge_expired()')
        check(admin.one('SELECT count(*) FROM awr.snapshots WHERE snap_id=$1',old)=='0','retention failed')
        check(admin.one('SELECT count(*) FROM awr.snapshots') != '0','retention removed fresh history')
    failed_install(c,'Database ownership/provenance ambiguous')
    cases.append('dump/restore includes partitions and identities; bounded retention; ambiguous provenance rejected')
    # Physical hot standby from a clean copy: no network access to the real server.
    c.stop()
    standby=c.base/'standby-data'
    shutil.copytree(c.data,standby)
    (standby/'standby.signal').touch()
    with (standby/'postgresql.conf').open('a') as f:
        f.write(f"\nport={c.port+100}\nhot_standby=on\n")
    c.command('pg_ctl','-D',standby,'-l',c.base/'standby.log','-w','start')
    try:
        with DB(dict(c.params,port=c.port+100,dbname='awr')) as standby_db:
            check(standby_db.one('SELECT pg_is_in_recovery()')=='t','standby not in recovery')
            for fn in ('take_snapshot','sample_activity','purge_expired'):
                denied(standby_db,'SELECT awr.'+fn+'()')
            check(standby_db.one('SELECT awr.report_json($1,$2)',baseline,end) is not None,'standby historical report')
    finally:
        c.command('pg_ctl','-D',standby,'-m','fast','-w','stop')
        c.start()
    cases.append('real physical recovery rejects collectors and permits historical reports')
    log=(c.base/'postgres.log').read_text()
    check(password not in log and rotated not in log,'plaintext credentials in server log')
    with c.db() as admin: version=admin.one('SELECT version()')
    result=dict(version=version,passed=len(cases),cases=cases,seconds=round(time.monotonic()-started,2))
    RESULTS.append(result)
    print(json.dumps(result,indent=2),flush=True)
    c.stop()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--versions',nargs='+',default=['15','16','17','18'])
    p.add_argument('--reuse',action='store_true')
    p.add_argument('--reset',action='store_true',help='remove only marked, disposable private test clusters first')
    args=p.parse_args()
    for v in args.versions:
        c=Cluster(v)
        if args.reset: c.reset()
        run(c,args.reuse)
    (ROOT/'docs/test-results.json').write_text(json.dumps(RESULTS,indent=2)+'\n')


if __name__=='__main__': main()
