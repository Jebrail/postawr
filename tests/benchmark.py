#!/usr/bin/env python3
"""Repeatable local baseline/enabled benchmark; requires the disposable test cluster."""
import argparse
import concurrent.futures
import json
import statistics
import threading
import time
from cluster import Cluster,ROOT


def run(c,seconds,clients):
    with c.db('workload_b') as db:
        db.sql('CREATE TABLE IF NOT EXISTS public.bench_rows(id integer primary key, value bigint not null)')
        db.sql('INSERT INTO public.bench_rows SELECT n,0 FROM generate_series(1,1000) n ON CONFLICT DO NOTHING')
    result={}
    for enabled in (False,True):
        with c.db('awr') as db:
            db.sql('UPDATE awr.config SET activity_enabled=$1::boolean',enabled)
            size_start=int(db.one("SELECT pg_database_size('awr')"))
        stop=threading.Event()
        elapsed=[]
        collection=[]
        errors=[]
        def workload(worker):
            try:
                with c.db('workload_b') as db:
                    n=0
                    while not stop.is_set():
                        t=time.perf_counter()
                        db.sql('UPDATE public.bench_rows SET value=value+1 WHERE id=$1',1+(worker+n*clients)%1000)
                        elapsed.append((time.perf_counter()-t)*1000)
                        n+=1
            except Exception as e:
                errors.append(str(e));stop.set()
        def collector():
            try:
                with c.db('awr','awr_user') as db:
                    n=0
                    while not stop.is_set():
                        for fn in (['take_snapshot','sample_activity'] if n%5==0 else ['sample_activity']):
                            t=time.perf_counter(); db.sql('SELECT awr.'+fn+'()')
                            collection.append({'job':fn,'milliseconds':(time.perf_counter()-t)*1000})
                        n+=1
                        stop.wait(1)
            except Exception as e:
                errors.append(str(e));stop.set()
        t=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients+1) as pool:
            futures=[pool.submit(workload,i) for i in range(clients)]
            if enabled: futures.append(pool.submit(collector))
            stop.wait(seconds); stop.set()
            for f in futures:f.result()
        wall=time.perf_counter()-t
        if errors:raise RuntimeError(errors)
        elapsed.sort()
        with c.db('awr') as db:
            size_end=int(db.one("SELECT pg_database_size('awr')"))
        result['enabled' if enabled else 'baseline']={
            'duration_seconds':round(wall,3),'transactions':len(elapsed),'throughput_tps':round(len(elapsed)/wall,2),
            'p95_latency_ms':round(elapsed[min(len(elapsed)-1,int(len(elapsed)*.95))],4),
            'mean_latency_ms':round(statistics.mean(elapsed),4),'storage_growth_bytes':size_end-size_start,
            'collections':collection}
    result['method']={'clients':clients,'seconds_per_phase':seconds,'snapshot_period_seconds':5,'sample_period_seconds':1,
                      'note':'Accelerated collection cadence for a short repeatable smoke benchmark, not a production overhead guarantee. Both phases retain preloaded pg_stat_statements.'}
    with c.db() as db:result['postgresql']=db.one('select version()')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--version',default='18');p.add_argument('--seconds',type=int,default=15);p.add_argument('--clients',type=int,default=3)
    a=p.parse_args()
    if a.seconds<5 or a.clients<1:p.error('seconds >= 5 and clients >= 1 required')
    c=Cluster(a.version);c.start()
    try:r=run(c,a.seconds,a.clients)
    finally:c.stop()
    (ROOT/'docs/benchmark-results.json').write_text(json.dumps(r,indent=2)+'\n')
    print(json.dumps(r,indent=2))
