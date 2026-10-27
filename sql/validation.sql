-- Run with psql -v ON_ERROR_STOP=1 -f sql/validation.sql YOUR_DATABASE
SELECT version(), postgis_full_version();
SELECT count(*) AS total_rows FROM web_ingest.planning_notices;

-- Source fields and processing metadata use separate JSON objects.
SELECT count(*) FILTER (
    WHERE raw_record IS NOT NULL AND jsonb_typeof(raw_record) != 'object'
) AS invalid_raw_record_shape,
count(*) FILTER (
    WHERE jsonb_typeof(ingest_metadata) != 'object'
) AS invalid_ingest_metadata_shape
FROM web_ingest.planning_notices;

-- Must return no rows.
SELECT source_system, source_key, count(*)
FROM web_ingest.planning_notices
GROUP BY source_system, source_key HAVING count(*) > 1;

SELECT source_system, approval_status, industrial_status, state_status, corridor_status,
       screening_status, count(*)
FROM web_ingest.planning_notices
GROUP BY source_system, approval_status, industrial_status, state_status, corridor_status,
         screening_status
ORDER BY source_system, screening_status;

SELECT count(*) FILTER (WHERE publication_date IS NULL) AS null_dates,
       count(*) FILTER (WHERE geom IS NULL) AS null_geometries,
       count(*) FILTER (WHERE geom IS NOT NULL AND ST_SRID(geom) != 25832) AS wrong_srid,
       count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsValid(geom)) AS invalid_geometry,
       count(*) FILTER (WHERE geom IS NOT NULL AND ST_IsEmpty(geom)) AS empty_geometry,
       count(*) FILTER (WHERE distance_to_a4_m < 0) AS negative_distance
FROM web_ingest.planning_notices;

-- Both queries must return no rows.
SELECT id, source_system, source_key FROM web_ingest.planning_notices
WHERE screening_status = 'accepted'
  AND (geom IS NULL OR corridor_status != 'inside' OR distance_to_a4_m IS NULL
       OR state_status != 'inside'
       OR approval_status != 'approved' OR industrial_status != 'industrial');

SELECT id, source_key, first_seen_at, last_seen_at, content_updated_at
FROM web_ingest.planning_notices
WHERE first_seen_at > last_seen_at OR first_seen_at > content_updated_at;

-- Informational: data/schema revalidation may postdate the last source observation.
SELECT id, source_key, content_updated_at, last_seen_at
FROM web_ingest.planning_notices WHERE content_updated_at > last_seen_at;

SELECT source_key, ingest_metadata->'a4_reference'->>'sha256' AS a4_sha256,
       ingest_metadata->'state_reference'->>'sha256' AS state_sha256,
       ingest_metadata->'a4_reference'->>'buffer_meters' AS corridor_buffer_m
FROM web_ingest.planning_notices;

SELECT flag, count(*)
FROM web_ingest.planning_notices,
     LATERAL jsonb_array_elements_text(quality_flags) AS flag
GROUP BY flag ORDER BY flag;

SELECT source_key, first_seen_at, last_seen_at, content_updated_at
FROM web_ingest.planning_notices ORDER BY source_system, source_key;
