-- Preserve history and existing grants while removing bootstrap role dependencies.
-- Use the extension's superuser owner for SECURITY DEFINER functions and storage.
DO $$
DECLARE r record; owner_name name;
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
 OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'upgrade requires session and effective superuser roles'; END IF;
 SELECT owner_role.rolname INTO STRICT owner_name FROM pg_extension e JOIN pg_roles owner_role ON owner_role.oid=e.extowner
 WHERE e.extname='pg_awr' AND owner_role.rolsuper;
 EXECUTE format('ALTER SCHEMA awr OWNER TO %I',owner_name);
 FOR r IN SELECT c.oid::regclass AS obj,c.relkind FROM pg_class c
  JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='awr' AND c.relkind IN ('r','p','S') ORDER BY c.relkind='S' LOOP
  EXECUTE format('ALTER %s %s OWNER TO %I',CASE WHEN r.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,r.obj,owner_name);
 END LOOP;
 FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
  JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='awr' LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',r.sig,owner_name);
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION awr._partition(parent text, ts timestamptz) RETURNS void LANGUAGE plpgsql
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
 EXECUTE format('REVOKE ALL ON awr.%I FROM PUBLIC',n);
 INSERT INTO awr.partitions VALUES(parent,n,lo,lo+interval '1 day');
END $$;

REVOKE ALL ON SCHEMA awr FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA awr FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA awr FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA awr FROM PUBLIC;
COMMENT ON EXTENSION pg_awr IS 'pg_awr 1.2: historical statistics with standard PostgreSQL extension installation';
