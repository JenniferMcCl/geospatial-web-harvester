-- Add nonunique indexes for bounded identity candidate reads on existing tables.
-- Run explicitly for an existing live database; fixture initialization adds these.
-- No source data, identities or timestamps are modified. Safe to rerun.
-- Ordinary index creation blocks writers while indexes are built; use a quiet
-- maintenance window for a populated database.
BEGIN;
-- Keep the whitespace expression identical to models.py and init.sql.
CREATE INDEX IF NOT EXISTS idx_planning_notices_source_id ON web_ingest.planning_notices (source_system, btrim(source_id, E'\u0009\u000A\u000B\u000C\u000D\u001C\u001D\u001E\u001F\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000'));
CREATE INDEX IF NOT EXISTS idx_planning_notices_source_url ON web_ingest.planning_notices (source_system, source_url);
COMMIT;
