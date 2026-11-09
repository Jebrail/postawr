-- pg_awr 1.0: extension objects and history are deliberately kept together.
DO $$
BEGIN
 IF current_database() <> 'awr' THEN RAISE EXCEPTION 'pg_awr requires database awr'; END IF;
 IF current_setting('server_version_num')::int NOT BETWEEN 150000 AND 189999 THEN
  RAISE EXCEPTION 'pg_awr supports PostgreSQL 15–18'; END IF;
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
 OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'session and effective roles must be true superusers'; END IF;
 IF pg_is_in_recovery() THEN RAISE EXCEPTION 'pg_awr requires a writable primary'; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='awr_owner' AND NOT rolcanlogin AND NOT rolsuper) THEN
  RAISE EXCEPTION 'run pg-awr-install first'; END IF;
END $$;
REVOKE ALL ON SCHEMA awr FROM PUBLIC;
ALTER SCHEMA awr OWNER TO awr_owner;

CREATE TABLE awr.config (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 activity_enabled boolean NOT NULL DEFAULT false,
 snapshot_retention_days integer NOT NULL DEFAULT 30 CHECK(snapshot_retention_days BETWEEN 1 AND 3650),
 activity_retention_days integer NOT NULL DEFAULT 7 CHECK(activity_retention_days BETWEEN 1 AND 365),
 statement_limit integer NOT NULL DEFAULT 0 CHECK(statement_limit BETWEEN 0 AND 1000000),
 capture_sql_text boolean NOT NULL DEFAULT false,
 sql_text_limit integer NOT NULL DEFAULT 1000 CHECK(sql_text_limit BETWEEN 0 AND 10000),
 blocker_limit integer NOT NULL DEFAULT 64 CHECK(blocker_limit BETWEEN 0 AND 1024),
 snapshot_interval_seconds integer NOT NULL DEFAULT 900 CHECK(snapshot_interval_seconds BETWEEN 1 AND 86400),
 activity_interval_seconds integer NOT NULL DEFAULT 5 CHECK(activity_interval_seconds BETWEEN 1 AND 3600)
);
-- Empty configuration during restore is filled lazily; avoids duplicate defaults in pg_dump.
CREATE SEQUENCE awr.snapshot_id_seq;
CREATE SEQUENCE awr.activity_id_seq;
CREATE TABLE awr.snapshots (
 snap_id bigint PRIMARY KEY DEFAULT nextval('awr.snapshot_id_seq'),
 collected_at timestamptz NOT NULL,
 server_version integer NOT NULL, system_identifier text NOT NULL,
 postmaster_start timestamptz NOT NULL, timeline_id integer NOT NULL,
 server_address text, server_port integer,
 settings jsonb NOT NULL, sources jsonb NOT NULL,
 statement_info jsonb, statement_count integer NOT NULL DEFAULT 0,
 statements_resident integer NOT NULL DEFAULT 0, statements_truncated boolean NOT NULL DEFAULT false,
 UNIQUE(snap_id,collected_at)
);
CREATE INDEX snapshots_time ON awr.snapshots(collected_at);
CREATE TABLE awr.database_history (
 snap_id bigint NOT NULL REFERENCES awr.snapshots ON DELETE CASCADE,
 dbid oid NOT NULL, dbname text, counters jsonb NOT NULL, marker jsonb,
 PRIMARY KEY(snap_id,dbid)
);
CREATE TABLE awr.statement_history (
 snap_id bigint NOT NULL, collected_at timestamptz NOT NULL,
 dbid oid NOT NULL, userid oid NOT NULL, queryid bigint NOT NULL, toplevel boolean NOT NULL,
 dbname text, username text, query_text text, counters jsonb NOT NULL, marker jsonb,
 FOREIGN KEY(snap_id,collected_at) REFERENCES awr.snapshots(snap_id,collected_at) ON DELETE CASCADE
) PARTITION BY RANGE(collected_at);
CREATE TABLE awr.metric_history (
 snap_id bigint NOT NULL REFERENCES awr.snapshots ON DELETE CASCADE,
 source text NOT NULL, dimensions jsonb NOT NULL, counters jsonb NOT NULL, marker jsonb,
 PRIMARY KEY(snap_id,source,dimensions)
);
CREATE TABLE awr.activity_batches (
 batch_id bigint PRIMARY KEY DEFAULT nextval('awr.activity_id_seq'),
 collected_at timestamptz NOT NULL, active_count integer NOT NULL DEFAULT 0,
 blocker_calls integer NOT NULL DEFAULT 0, lock_waiters integer NOT NULL DEFAULT 0,
 expected_interval_seconds integer NOT NULL,
 postmaster_start timestamptz NOT NULL, timeline_id integer NOT NULL,
 UNIQUE(batch_id,collected_at)
);
CREATE INDEX activity_batches_time ON awr.activity_batches(collected_at);
CREATE TABLE awr.activity_history (
 batch_id bigint NOT NULL, collected_at timestamptz NOT NULL,
 pid integer NOT NULL, backend_start timestamptz NOT NULL,
 dbid oid, dbname text, userid oid, username text, queryid bigint,
 state text, wait_event_type text, wait_event text, blockers integer[],
 is_active boolean NOT NULL, is_blocker boolean NOT NULL,
 FOREIGN KEY(batch_id,collected_at) REFERENCES awr.activity_batches(batch_id,collected_at) ON DELETE CASCADE
) PARTITION BY RANGE(collected_at);
CREATE TABLE awr.partitions (
 parent_name text NOT NULL CHECK(parent_name IN ('statement_history','activity_history')),
 partition_name text PRIMARY KEY, from_time timestamptz NOT NULL, to_time timestamptz NOT NULL
);

CREATE FUNCTION awr._config() RETURNS awr.config LANGUAGE plpgsql
SET search_path=pg_catalog,pg_temp AS $$
DECLARE c awr.config;
BEGIN
 INSERT INTO awr.config(singleton) VALUES(true) ON CONFLICT DO NOTHING;
 SELECT * INTO STRICT c FROM awr.config;
 RETURN c;
END $$;

CREATE FUNCTION awr._partition(parent text, ts timestamptz) RETURNS void LANGUAGE plpgsql
SET search_path=pg_catalog,pg_temp AS $$
DECLARE lo timestamptz := date_trunc('day',ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'; n text;
BEGIN
 IF parent NOT IN ('statement_history','activity_history') THEN RAISE EXCEPTION 'invalid partition parent'; END IF;
 n := parent || '_' || to_char(lo AT TIME ZONE 'UTC','YYYYMMDD');
 IF EXISTS(SELECT FROM awr.partitions WHERE partition_name=n) THEN RETURN; END IF;
 PERFORM pg_advisory_xact_lock(728301,4);
 IF EXISTS(SELECT FROM awr.partitions WHERE partition_name=n) THEN RETURN; END IF;
 EXECUTE format('CREATE TABLE awr.%I PARTITION OF awr.%I FOR VALUES FROM (%L) TO (%L)',n,parent,lo,lo+interval '1 day');
 -- Local partition indexes avoid pg_dump attaching a partition to extension-owned
 -- parent indexes before recreating its own indexes (duplicate-constraint failure).
 IF parent='statement_history' THEN
  EXECUTE format('ALTER TABLE awr.%I ADD PRIMARY KEY(collected_at,snap_id,dbid,userid,queryid,toplevel)',n);
  EXECUTE format('CREATE INDEX ON awr.%I(snap_id,collected_at)',n);
 ELSE
  EXECUTE format('ALTER TABLE awr.%I ADD PRIMARY KEY(collected_at,batch_id,pid,backend_start)',n);
  EXECUTE format('CREATE INDEX ON awr.%I(pid,backend_start,collected_at)',n);
 END IF;
 EXECUTE format('REVOKE ALL ON awr.%I FROM PUBLIC, awr_user, awr_reader, awr_executor',n);
 INSERT INTO awr.partitions VALUES(parent,n,lo,lo+interval '1 day');
END $$;

CREATE FUNCTION awr._numbers(j jsonb, keys text[]) RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_object_agg(key,value),'{}') FROM jsonb_each(j)
 WHERE key=ANY(keys) AND jsonb_typeof(value)='number'
$$;

-- Version adapters retain only cumulative counters. Gauges and averages are not subtracted.
CREATE FUNCTION awr._metric_sources() RETURNS TABLE(source text, available boolean) LANGUAGE sql STABLE
SET search_path=pg_catalog,pg_temp AS $$
 SELECT x, to_regclass('pg_catalog.'||x) IS NOT NULL
 FROM unnest(ARRAY['pg_stat_wal','pg_stat_bgwriter','pg_stat_checkpointer','pg_stat_io']) x
$$;

CREATE FUNCTION awr.take_snapshot() RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp SET timezone='UTC' SET lock_timeout='2s' AS $$
DECLARE c awr.config; sid bigint; ts timestamptz; ns text; info jsonb; src record;
 rowdata jsonb; resident integer; cap integer; source_status jsonb := '{}';
BEGIN
 IF pg_is_in_recovery() THEN RAISE EXCEPTION 'pg_awr requires a writable primary'; END IF;
 IF NOT pg_try_advisory_xact_lock(728301,1) THEN RETURN NULL; END IF;
 c:=awr._config(); ts:=clock_timestamp();
 PERFORM pg_stat_clear_snapshot();
 SELECT n.nspname INTO STRICT ns FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_stat_statements';
 -- Missing/broken pg_stat_statements aborts the whole snapshot; no misleading partial header.
 EXECUTE format('SELECT to_jsonb(i) FROM %I.pg_stat_statements_info i',ns) INTO info;
 EXECUTE format('SELECT count(*) FROM %I.pg_stat_statements(false)',ns) INTO resident;
 cap:=CASE WHEN c.statement_limit=0 THEN 2147483647 ELSE c.statement_limit END;
 SELECT jsonb_object_agg(source,available) INTO source_status FROM awr._metric_sources();
 source_status:=source_status||jsonb_build_object('pg_stat_database',true,'pg_stat_statements',true);
 INSERT INTO awr.snapshots(collected_at,server_version,system_identifier,postmaster_start,timeline_id,
 server_address,server_port,settings,sources,statement_info,statements_resident,statements_truncated)
 SELECT ts,current_setting('server_version_num')::int,system_identifier::text,pg_postmaster_start_time(),
 (pg_control_checkpoint()).timeline_id,inet_server_addr()::text,inet_server_port(),
 jsonb_build_object('track_io_timing',current_setting('track_io_timing'),
 'track_wal_io_timing',current_setting('track_wal_io_timing'),'track_counts',current_setting('track_counts'),
 'block_size',current_setting('block_size')::int,'snapshot_interval_seconds',c.snapshot_interval_seconds,
 'activity_interval_seconds',c.activity_interval_seconds,'activity_enabled',c.activity_enabled,
 'pg_stat_statements.track',current_setting('pg_stat_statements.track',true)),source_status,info,resident,resident>cap
 FROM pg_control_system() RETURNING snap_id INTO sid;
 INSERT INTO awr.database_history
 SELECT sid,datid,datname,awr._numbers(to_jsonb(d),ARRAY[
 'xact_commit','xact_rollback','blks_read','blks_hit','tup_returned','tup_fetched','tup_inserted','tup_updated','tup_deleted',
 'temp_files','temp_bytes','deadlocks','blk_read_time','blk_write_time','conflicts','sessions','sessions_abandoned',
 'sessions_fatal','sessions_killed','session_time','active_time','idle_in_transaction_time']),
 jsonb_build_object('stats_reset',stats_reset) FROM pg_stat_database d;
 PERFORM awr._partition('statement_history',ts);
 EXECUTE format($q$
 INSERT INTO awr.statement_history
 SELECT $1,$2,s.dbid,s.userid,s.queryid,s.toplevel,d.datname,r.rolname,
 CASE WHEN $3 THEN left(s.query,$4) ELSE NULL END,
 awr._numbers(to_jsonb(s),ARRAY['calls','total_exec_time','rows','shared_blks_hit','shared_blks_read',
 'shared_blks_dirtied','shared_blks_written','temp_blks_read','temp_blks_written','blk_read_time','blk_write_time',
 'shared_blk_read_time','shared_blk_write_time','local_blk_read_time','local_blk_write_time',
 'temp_blk_read_time','temp_blk_write_time','wal_records','wal_fpi','wal_bytes']),
 jsonb_build_object('stats_since',to_jsonb(s)->'stats_since')
 FROM %I.pg_stat_statements($3) s LEFT JOIN pg_database d ON d.oid=s.dbid
 LEFT JOIN pg_roles r ON r.oid=s.userid ORDER BY s.total_exec_time DESC,s.dbid,s.userid,s.queryid,s.toplevel LIMIT $5
 $q$,ns) USING sid,ts,c.capture_sql_text,c.sql_text_limit,cap;
 UPDATE awr.snapshots SET statement_count=(SELECT count(*) FROM awr.statement_history WHERE snap_id=sid AND collected_at=ts) WHERE snap_id=sid;
 FOR src IN SELECT * FROM awr._metric_sources() WHERE available LOOP
  FOR rowdata IN EXECUTE format('SELECT to_jsonb(t) FROM pg_catalog.%I t',src.source) LOOP
   INSERT INTO awr.metric_history VALUES(sid,src.source,
    CASE WHEN src.source='pg_stat_io' THEN jsonb_build_object('backend_type',rowdata->'backend_type','object',rowdata->'object','context',rowdata->'context') ELSE '{}' END,
    awr._numbers(rowdata,ARRAY['wal_records','wal_fpi','wal_bytes','wal_buffers_full','wal_write','wal_sync','wal_write_time','wal_sync_time',
    'checkpoints_timed','checkpoints_req','checkpoint_write_time','checkpoint_sync_time','buffers_checkpoint',
    'buffers_clean','maxwritten_clean','buffers_backend','buffers_backend_fsync','buffers_alloc',
    'num_timed','num_requested','num_done','write_time','sync_time','buffers_written',
    'restartpoints_timed','restartpoints_req','restartpoints_done','reads','read_time','read_bytes',
    'writes','write_bytes','writebacks','writeback_time','extends','extend_time','extend_bytes',
    'hits','evictions','reuses','fsyncs','fsync_time']),jsonb_build_object('stats_reset',rowdata->'stats_reset'));
  END LOOP;
 END LOOP;
 RETURN sid;
END $$;

CREATE FUNCTION awr.sample_activity() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp SET timezone='UTC' SET lock_timeout='2s' AS $$
DECLARE c awr.config; bid bigint; ts timestamptz; a record; blockers integer[];
 all_blockers integer[]:='{}'; n integer:=0; calls integer:=0; waiters integer:=0;
BEGIN
 IF pg_is_in_recovery() THEN RAISE EXCEPTION 'pg_awr requires a writable primary'; END IF;
 IF NOT pg_try_advisory_xact_lock(728301,2) THEN RETURN NULL; END IF;
 c:=awr._config(); IF NOT c.activity_enabled THEN RETURN 0; END IF;
 ts:=clock_timestamp(); PERFORM pg_stat_clear_snapshot();
 INSERT INTO awr.activity_batches(collected_at,expected_interval_seconds,postmaster_start,timeline_id)
 VALUES(ts,c.activity_interval_seconds,pg_postmaster_start_time(),(pg_control_checkpoint()).timeline_id) RETURNING batch_id INTO bid;
 PERFORM awr._partition('activity_history',ts);
 FOR a IN SELECT * FROM pg_stat_activity WHERE backend_type='client backend' AND state='active' AND pid<>pg_backend_pid() LOOP
  blockers:='{}';
  IF a.wait_event_type='Lock' THEN
   waiters:=waiters+1;
   IF calls<c.blocker_limit THEN blockers:=pg_blocking_pids(a.pid); calls:=calls+1; END IF;
  END IF;
  all_blockers:=all_blockers||blockers;
  INSERT INTO awr.activity_history VALUES(bid,ts,a.pid,a.backend_start,a.datid,a.datname,a.usesysid,a.usename,a.query_id,
    a.state,a.wait_event_type,a.wait_event,blockers,true,a.pid=ANY(all_blockers));
  n:=n+1;
 END LOOP;
 FOR a IN SELECT * FROM pg_stat_activity WHERE pid=ANY(all_blockers) AND pid<>pg_backend_pid() AND backend_start IS NOT NULL LOOP
  UPDATE awr.activity_history SET is_blocker=true
   WHERE collected_at=ts AND batch_id=bid AND pid=a.pid AND backend_start=a.backend_start;
  IF NOT FOUND THEN
   INSERT INTO awr.activity_history VALUES(bid,ts,a.pid,a.backend_start,a.datid,a.datname,a.usesysid,a.usename,a.query_id,
    a.state,a.wait_event_type,a.wait_event,'{}',a.state='active',true);
  END IF;
 END LOOP;
 UPDATE awr.activity_batches SET active_count=n,blocker_calls=calls,lock_waiters=waiters WHERE batch_id=bid;
 RETURN n;
END $$;

CREATE FUNCTION awr.purge_expired() RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp SET timezone='UTC' SET lock_timeout='2s' AS $$
DECLARE c awr.config; p record; n bigint:=0; deleted bigint;
BEGIN
 IF pg_is_in_recovery() THEN RAISE EXCEPTION 'pg_awr requires a writable primary'; END IF;
 IF NOT pg_try_advisory_xact_lock(728301,3) THEN RETURN NULL; END IF;
 c:=awr._config();
 PERFORM pg_advisory_xact_lock(728301,4);
 FOR p IN SELECT * FROM awr.partitions WHERE to_time<=clock_timestamp()-
  make_interval(days=>CASE WHEN parent_name='statement_history' THEN c.snapshot_retention_days ELSE c.activity_retention_days END)
  ORDER BY to_time,partition_name LIMIT 8 LOOP
  EXECUTE format('DROP TABLE awr.%I',p.partition_name);
  DELETE FROM awr.partitions WHERE partition_name=p.partition_name;
  n:=n+1;
 END LOOP;
 DELETE FROM awr.snapshots WHERE snap_id IN (SELECT s.snap_id FROM awr.snapshots s
  WHERE collected_at<clock_timestamp()-make_interval(days=>c.snapshot_retention_days)
  AND NOT EXISTS(SELECT FROM awr.partitions part WHERE part.parent_name='statement_history' AND s.collected_at>=part.from_time AND s.collected_at<part.to_time)
  ORDER BY collected_at LIMIT 100);
 GET DIAGNOSTICS deleted=ROW_COUNT; n:=n+deleted;
 DELETE FROM awr.activity_batches WHERE batch_id IN (SELECT b.batch_id FROM awr.activity_batches b
  WHERE collected_at<clock_timestamp()-make_interval(days=>c.activity_retention_days)
  AND NOT EXISTS(SELECT FROM awr.partitions part WHERE part.parent_name='activity_history' AND b.collected_at>=part.from_time AND b.collected_at<part.to_time)
  ORDER BY collected_at LIMIT 5000);
 GET DIAGNOSTICS deleted=ROW_COUNT;
 RETURN n+deleted;
END $$;

CREATE FUNCTION awr.list_snapshots(from_time timestamptz,to_time timestamptz)
RETURNS TABLE(snap_id bigint,collected_at timestamptz,server_version integer,postmaster_start timestamptz,
 timeline_id integer,statement_count integer,statements_resident integer,statements_truncated boolean,sources jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF from_time IS NULL OR to_time IS NULL OR from_time>to_time THEN RAISE EXCEPTION 'invalid time interval'; END IF;
 RETURN QUERY SELECT s.snap_id,s.collected_at,s.server_version,s.postmaster_start,s.timeline_id,
 s.statement_count,s.statements_resident,s.statements_truncated,s.sources FROM awr.snapshots s
 WHERE s.collected_at>=from_time AND s.collected_at<=to_time ORDER BY s.collected_at,s.snap_id;
END $$;

-- Every observed intermediate point participates in validation, not just endpoints.
CREATE FUNCTION awr._delta(points jsonb, expected integer, elapsed double precision, initial_reasons text[])
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE firstp jsonb:=points->0; lastp jsonb:=points->-1; p jsonb; prev jsonb;
 k text; v numeric; diffs jsonb:='{}'; rates jsonb:='{}'; reasons text[]:=initial_reasons;
 bad text[]; field_reasons jsonb:='{}';
BEGIN
 IF jsonb_array_length(points)<>expected THEN reasons:=array_append(reasons,'missing baseline, endpoint, or intermediate observation'); END IF;
 FOR p IN SELECT value FROM jsonb_array_elements(points) LOOP
  IF p->'marker' IS DISTINCT FROM firstp->'marker' THEN reasons:=array_append(reasons,'statistics reset or entry replacement'); EXIT; END IF;
 END LOOP;
 FOR k IN SELECT DISTINCT key FROM jsonb_array_elements(points) a CROSS JOIN LATERAL jsonb_object_keys(a->'counters') key LOOP
  bad:=reasons; prev:=NULL;
  FOR p IN SELECT value FROM jsonb_array_elements(points) LOOP
   IF p->'counters'->k IS NULL THEN bad:=array_append(bad,'metric unavailable');
   ELSIF prev IS NOT NULL AND (p->'counters'->>k)::numeric<(prev->'counters'->>k)::numeric THEN bad:=array_append(bad,'counter decreased'); END IF;
   IF k ~ '(read|write|extend|fsync|writeback|sync)_time$' AND k NOT IN ('checkpoint_write_time','checkpoint_sync_time') THEN
    IF (p->>'source'='pg_stat_wal' AND p->'settings'->>'track_wal_io_timing'<>'on')
    OR (coalesce(p->>'source','') NOT IN ('pg_stat_wal','pg_stat_checkpointer') AND p->'settings'->>'track_io_timing'<>'on') THEN
     bad:=array_append(bad,'timing disabled in collector session; workload timing may be mixed'); END IF;
   END IF;
   IF p->>'source'='pg_stat_database' AND p->'settings'->>'track_counts'<>'on' THEN bad:=array_append(bad,'track_counts disabled'); END IF;
   prev:=p;
  END LOOP;
  IF cardinality(bad)>0 THEN
   v:=NULL; SELECT array_agg(DISTINCT x) INTO bad FROM unnest(bad) x;
   field_reasons:=field_reasons||jsonb_build_object(k,bad);
  ELSE v:=(lastp->'counters'->>k)::numeric-(firstp->'counters'->>k)::numeric; END IF;
  diffs:=diffs||jsonb_build_object(k,v); rates:=rates||jsonb_build_object(k,v/elapsed);
 END LOOP;
 SELECT coalesce(array_agg(DISTINCT x),'{}') INTO reasons FROM unnest(reasons) x;
 RETURN jsonb_build_object('delta',diffs,'per_second',rates,'reasons',reasons,'metric_reasons',field_reasons,
  'comparable',cardinality(reasons)=0 AND field_reasons='{}','observations',jsonb_array_length(points));
END $$;

CREATE FUNCTION awr.report_json(begin_snap bigint,end_snap bigint,top_n integer DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE b awr.snapshots; e awr.snapshots; secs double precision; expected integer; r record;
 common text[]:='{}'; stmt_reasons text[]:='{}'; reasons text[]; result jsonb; dbs jsonb:='[]'; stmts jsonb:='[]';
 metrics jsonb:='[]'; rankings jsonb:='{}'; item jsonb; k text; batches bigint; active bigint; waits jsonb; blockers jsonb;
 expected_samples numeric; avg_cadence numeric; coverage jsonb; source_quality jsonb;
BEGIN
 IF top_n IS NULL OR top_n NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'top_n must be between 1 and 1000'; END IF;
 SELECT * INTO b FROM awr.snapshots WHERE snap_id=begin_snap;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown begin snapshot %',begin_snap; END IF;
 SELECT * INTO e FROM awr.snapshots WHERE snap_id=end_snap;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown end snapshot %',end_snap; END IF;
 IF b.collected_at>=e.collected_at OR begin_snap>=end_snap THEN RAISE EXCEPTION 'snapshots must be distinct and chronological'; END IF;
 secs:=extract(epoch FROM e.collected_at-b.collected_at);
 SELECT count(*) INTO expected FROM awr.snapshots WHERE collected_at BETWEEN b.collected_at AND e.collected_at;
 IF EXISTS(SELECT FROM awr.snapshots s WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at AND
  (s.system_identifier<>b.system_identifier OR s.postmaster_start<>b.postmaster_start OR s.timeline_id<>b.timeline_id
   OR s.server_version<>b.server_version OR s.server_address IS DISTINCT FROM b.server_address OR s.server_port IS DISTINCT FROM b.server_port)) THEN
  common:=array_append(common,'primary, postmaster, or version discontinuity'); END IF;
 IF EXISTS(SELECT FROM awr.snapshots s WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at
  AND s.statement_info->'stats_reset' IS DISTINCT FROM b.statement_info->'stats_reset') THEN
  stmt_reasons:=array_append(stmt_reasons,'pg_stat_statements reset'); END IF;
 IF EXISTS(SELECT FROM awr.snapshots s WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at
  AND s.statement_info->'dealloc' IS DISTINCT FROM b.statement_info->'dealloc') THEN
  stmt_reasons:=array_append(stmt_reasons,'statement eviction: continuity uncertain'); END IF;
 IF EXISTS(SELECT FROM awr.snapshots s WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at AND s.settings->>'pg_stat_statements.track'='none') THEN
  stmt_reasons:=array_append(stmt_reasons,'statement tracking disabled'); END IF;
 IF EXISTS(SELECT FROM awr.snapshots s WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at AND s.statements_truncated) THEN
  stmt_reasons:=array_append(stmt_reasons,'statement capture truncated'); END IF;
 FOR r IN SELECT d.dbid,(array_agg(d.dbname ORDER BY s.collected_at DESC))[1] dbname,
  jsonb_agg(jsonb_build_object('counters',d.counters,'marker',d.marker,'settings',s.settings,'source','pg_stat_database') ORDER BY s.collected_at) points
  FROM awr.database_history d JOIN awr.snapshots s USING(snap_id)
  WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at GROUP BY d.dbid LOOP
  dbs:=dbs||jsonb_build_array(jsonb_build_object('dbid',r.dbid,'dbname',r.dbname)||awr._delta(r.points,expected,secs,common));
 END LOOP;
 FOR r IN SELECT d.dbid,d.userid,d.queryid,d.toplevel,
  (array_agg(d.dbname ORDER BY s.collected_at DESC))[1] dbname,
  (array_agg(d.username ORDER BY s.collected_at DESC))[1] username,
  (array_agg(d.query_text ORDER BY s.collected_at DESC))[1] query_text,
  jsonb_agg(jsonb_build_object('counters',d.counters,'marker',d.marker,'settings',s.settings,'source','pg_stat_statements') ORDER BY s.collected_at) points
  FROM awr.statement_history d JOIN awr.snapshots s USING(snap_id)
  WHERE d.collected_at BETWEEN b.collected_at AND e.collected_at GROUP BY d.dbid,d.userid,d.queryid,d.toplevel LOOP
  stmts:=stmts||jsonb_build_array(jsonb_build_object('dbid',r.dbid,'userid',r.userid,'queryid',r.queryid::text,
  'toplevel',r.toplevel,'dbname',r.dbname,'username',r.username,'query_text',r.query_text)||awr._delta(r.points,expected,secs,common||stmt_reasons));
 END LOOP;
 FOREACH k IN ARRAY ARRAY['total_exec_time','calls','shared_blks_read','temp_blks_read','temp_blks_written','wal_bytes'] LOOP
  SELECT coalesce(jsonb_agg(x),'[]') INTO item FROM
   (SELECT value x FROM jsonb_array_elements(stmts) WHERE value->'delta'->>k IS NOT NULL
    ORDER BY (value->'delta'->>k)::numeric DESC,value->>'queryid',value->>'dbid',value->>'userid' LIMIT top_n) q;
  rankings:=rankings||jsonb_build_object(k,item);
 END LOOP;
 FOR r IN SELECT d.source,d.dimensions,
  jsonb_agg(jsonb_build_object('counters',d.counters,'marker',d.marker,'settings',s.settings,'source',d.source) ORDER BY s.collected_at) points
  FROM awr.metric_history d JOIN awr.snapshots s USING(snap_id)
  WHERE s.collected_at BETWEEN b.collected_at AND e.collected_at GROUP BY d.source,d.dimensions LOOP
  metrics:=metrics||jsonb_build_array(jsonb_build_object('source',r.source,'dimensions',r.dimensions)||awr._delta(r.points,expected,secs,common));
 END LOOP;
 SELECT count(*),coalesce(sum(active_count),0),avg(expected_interval_seconds) INTO batches,active,avg_cadence
 FROM awr.activity_batches WHERE collected_at>b.collected_at AND collected_at<=e.collected_at;
 expected_samples:=secs/coalesce(avg_cadence,(b.settings->>'activity_interval_seconds')::numeric);
 SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO waits FROM (
  SELECT coalesce(wait_event_type,'No wait event') wait_event_type,coalesce(wait_event,'No wait event') wait_event,count(*) samples
  FROM awr.activity_history WHERE collected_at>b.collected_at AND collected_at<=e.collected_at AND is_active
  GROUP BY 1,2 ORDER BY count(*) DESC LIMIT top_n) q;
 SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO blockers FROM (
  SELECT pid,backend_start,dbname,username,state,count(*) samples
  FROM awr.activity_history WHERE collected_at>b.collected_at AND collected_at<=e.collected_at AND is_blocker
  GROUP BY 1,2,3,4,5 ORDER BY count(*) DESC LIMIT top_n) q;
 coverage:=jsonb_build_object('successful_samples',batches,'expected_samples',expected_samples,
  'coverage_ratio',least(1,batches/nullif(expected_samples,0)), 'average_active_sessions',active::numeric/nullif(batches,0),
  'active_observations',active,'aas_denominator',batches,'denominator_description','successful samples, including observed zero-active samples',
  'first_sample',(SELECT min(collected_at) FROM awr.activity_batches WHERE collected_at>b.collected_at AND collected_at<=e.collected_at),
  'last_sample',(SELECT max(collected_at) FROM awr.activity_batches WHERE collected_at>b.collected_at AND collected_at<=e.collected_at),
  'largest_observed_gap_seconds',(SELECT max(extract(epoch FROM ts-prev)) FROM (
   SELECT ts,lag(ts) OVER(ORDER BY ts) prev FROM (
    SELECT b.collected_at ts UNION ALL SELECT collected_at FROM awr.activity_batches WHERE collected_at>b.collected_at AND collected_at<=e.collected_at UNION ALL SELECT e.collected_at) x) y),
  'uninspected_lock_waiters',(SELECT coalesce(sum(lock_waiters-blocker_calls),0) FROM awr.activity_batches WHERE collected_at>b.collected_at AND collected_at<=e.collected_at),
  'waits',waits,'observed_blockers',blockers);
 SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO source_quality FROM (
  SELECT snap_id,sources,settings,statements_resident,statement_count,statements_truncated,statement_info
  FROM awr.snapshots WHERE collected_at BETWEEN b.collected_at AND e.collected_at ORDER BY collected_at) q;
 RETURN jsonb_build_object('schema_version',1,'begin_snap',begin_snap,'end_snap',end_snap,
 'begin_time',b.collected_at,'end_time',e.collected_at,'elapsed_seconds',secs,'snapshot_observations',expected,
 'units',jsonb_build_object('time','milliseconds except elapsed_seconds and gap seconds','bytes','bytes',
 'blocks','PostgreSQL blocks','block_size',b.settings->'block_size','rates','counter units per second'),
 'quality',jsonb_build_object('interval_reasons',common,'statement_reasons',stmt_reasons,'snapshots',source_quality,
 'notes',jsonb_build_array('Statement execution time sums across calls and concurrent sessions.',
 'No wait event does not measure CPU time. Missed samples are not idle periods.',
 'Timing switches are observed in the collector; other sessions may use different settings.',
 'Undetected resets between observations remain possible, especially targeted statement resets before PostgreSQL 17.',
 'Statements require both endpoints and every intermediate observation; eviction invalidates statement deltas conservatively.')),
 'database_activity',dbs,'top_sql',rankings,'statement_quality',
 (SELECT coalesce(jsonb_agg(value-'query_text'-'delta'-'per_second'),'[]') FROM jsonb_array_elements(stmts)
  WHERE jsonb_array_length(value->'reasons')>0 OR value->'metric_reasons'<>'{}'),
 'load_per_second', (SELECT jsonb_build_object('commits',CASE WHEN count(*) FILTER(WHERE value->'per_second'->>'xact_commit' IS NULL)>0 THEN NULL ELSE sum((value->'per_second'->>'xact_commit')::numeric) END,
 'rollbacks',CASE WHEN count(*) FILTER(WHERE value->'per_second'->>'xact_rollback' IS NULL)>0 THEN NULL ELSE sum((value->'per_second'->>'xact_rollback')::numeric) END) FROM jsonb_array_elements(dbs)),
 'activity',coverage,'metrics',metrics);
END $$;

CREATE FUNCTION awr._html_escape(t text) RETURNS text LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,pg_temp AS $$
 SELECT replace(replace(replace(replace(replace(coalesce(t,''),'&','&amp;'),'<','&lt;'),'>','&gt;'),'"','&quot;'),'''','&#39;')
$$;
CREATE FUNCTION awr.report_html(begin_snap bigint,end_snap bigint,top_n integer DEFAULT 20)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE j jsonb; h text; section text; title text;
BEGIN
 j:=awr.report_json(begin_snap,end_snap,top_n);
 h:='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>pg_awr interval report</title><style>body{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#18242c;background:#f6f8fa}h1,h2{color:#143d59}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:1rem;border:1px solid #ccd7de;border-radius:8px}nav a{margin-right:1rem}p{line-height:1.6}</style></head><body><h1>pg_awr interval report</h1><p>'||
 awr._html_escape(j->>'begin_time')||' → '||awr._html_escape(j->>'end_time')||' · '||awr._html_escape(j->>'elapsed_seconds')||' seconds</p><nav>';
 FOREACH section IN ARRAY ARRAY['quality','load_per_second','database_activity','top_sql','statement_quality','activity','metrics','units'] LOOP
  h:=h||'<a href="#'||section||'">'||initcap(replace(section,'_',' '))||'</a>';
 END LOOP;
 h:=h||'</nav><p>Unknown deltas are null and carry reasons. Active-session estimates use successful samples; no wait event is not a CPU measurement.</p>';
 FOREACH section IN ARRAY ARRAY['quality','load_per_second','database_activity','top_sql','statement_quality','activity','metrics','units'] LOOP
  h:=h||'<section id="'||section||'"><h2>'||initcap(replace(section,'_',' '))||'</h2><pre>'||awr._html_escape(jsonb_pretty(j->section))||'</pre></section>';
 END LOOP;
 RETURN h||'</body></html>';
END $$;

-- No PUBLIC exposure, including helper functions, at any commit boundary.
REVOKE ALL ON ALL TABLES IN SCHEMA awr FROM PUBLIC,awr_user,awr_executor,awr_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA awr FROM PUBLIC,awr_user,awr_executor,awr_reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA awr FROM PUBLIC,awr_user,awr_executor,awr_reader;
DO $$ DECLARE r record;
BEGIN
 FOR r IN SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='awr' AND c.relkind IN ('r','p','S') ORDER BY c.relkind='S' LOOP
  IF r.relkind='S' THEN EXECUTE format('ALTER SEQUENCE awr.%I OWNER TO awr_owner',r.relname);
  ELSE EXECUTE format('ALTER TABLE awr.%I OWNER TO awr_owner',r.relname); END IF;
 END LOOP;
 FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='awr' LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO awr_owner',r.sig);
 END LOOP;
END $$;
GRANT USAGE ON SCHEMA awr TO awr_executor,awr_reader;
GRANT EXECUTE ON FUNCTION awr.take_snapshot(),awr.sample_activity(),awr.purge_expired() TO awr_executor;
GRANT EXECUTE ON FUNCTION awr.list_snapshots(timestamptz,timestamptz),awr.report_json(bigint,bigint,integer),awr.report_html(bigint,bigint,integer) TO awr_reader;
-- Dump extension-member data explicitly; runtime-created partitions are ordinary tables.
SELECT pg_catalog.pg_extension_config_dump('awr.config','');
SELECT pg_catalog.pg_extension_config_dump('awr.snapshots','');
SELECT pg_catalog.pg_extension_config_dump('awr.database_history','');
SELECT pg_catalog.pg_extension_config_dump('awr.metric_history','');
SELECT pg_catalog.pg_extension_config_dump('awr.activity_batches','');
SELECT pg_catalog.pg_extension_config_dump('awr.partitions','');
SELECT pg_catalog.pg_extension_config_dump('awr.snapshot_id_seq','');
SELECT pg_catalog.pg_extension_config_dump('awr.activity_id_seq','');
