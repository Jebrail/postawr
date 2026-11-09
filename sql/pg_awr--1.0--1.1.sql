-- Upgrade adds snapshot primary identity lookup without rewriting or removing history.
DO $$ BEGIN
 IF current_database()<>'awr' OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
 OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
 RAISE EXCEPTION 'upgrade requires true superuser in database awr'; END IF;
END $$;
CREATE INDEX snapshot_primary_identity ON awr.snapshots(postmaster_start,timeline_id,collected_at);
COMMENT ON EXTENSION pg_awr IS 'pg_awr 1.1: historical statistics, conservative interval deltas, bounded activity sampling';
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA awr FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA awr FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA awr FROM PUBLIC;
