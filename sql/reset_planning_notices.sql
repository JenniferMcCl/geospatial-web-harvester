-- Explicit destructive reset for one selected database. Never run at startup.
-- Execute separately against the dedicated fixture and live databases with psql.
-- Deletes legacy/current notice rows, then recreates web_ingest.planning_notices.
-- No CASCADE: unrelated dependent objects cause failure and transaction rollback.
\set ON_ERROR_STOP on
BEGIN;
DROP TABLE IF EXISTS iris_staging.signal_ingest;
DROP TABLE IF EXISTS web_ingest.planning_notices;

-- Remove the retired namespace only if it has no unrelated objects left.
DO $$
BEGIN
    IF to_regnamespace('iris_staging') IS NOT NULL THEN
        BEGIN
            DROP SCHEMA iris_staging;
        EXCEPTION WHEN dependent_objects_still_exist THEN
            RAISE NOTICE 'Retaining legacy schema because unrelated objects remain';
        END;
    END IF;
END $$;

\ir init.sql
COMMIT;
