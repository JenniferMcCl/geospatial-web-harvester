"""Persist observations through field-aware reconciliation and atomic bulk upsert.

Engine construction is lazy; connections and schema setup happen only on explicit
calls. The table contract lives in models. This module does not parse HTML.
Stored raw_record source fields allow partial rescans to preserve omitted data.
Processing flags and spatial-reference provenance are stored in ingest_metadata.

Two requirements remain deferred: removing stored rows that become conclusively
excluded, and independent reference-only recalculation of unobserved records.
The strict expected-failure tests document those limits.
"""

from collections import defaultdict
from datetime import UTC, datetime

from geoalchemy2.shape import from_shape, to_shape
from sqlalchemy import Connection, Engine, case, create_engine, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from .config import Settings
from .models import (
    SOURCE_ID_INDEX,
    SOURCE_ID_WHITESPACE_SQL,
    SOURCE_URL_INDEX,
    Observation,
    PlanningNotice,
    PreparedBatch,
)
from .transform import Corridor, StateBoundary, normalize, prepare_batch

TABLE = PlanningNotice.__table__
# Compare meaningful content separately from identity and observation timestamps.
CONTENT_COLUMNS = [
    c.name
    for c in TABLE.columns
    if c.name
    not in {
        "id",
        "source_system",
        "source_key",
        "first_seen_at",
        "last_seen_at",
        "content_updated_at",
    }
]


def make_engine(url: str | URL) -> Engine:
    """Build a lazy PostgreSQL engine with connection timeout and pooled health checks.

    The caller owns disposal. Constructing the engine does not connect or migrate.
    """
    return create_engine(url, connect_args={"connect_timeout": 5}, pool_pre_ping=True)


def ensure_test_database(target: URL) -> None:
    """Provision a dedicated test database through a maintenance connection.

    Require a _test/_fixture suffix. CREATE DATABASE uses autocommit because it
    cannot run inside a transaction. Reuse existing databases and tolerate a
    concurrent creator; never reset contents. The caller must first validate
    separation from the live destination. Connection/permission errors propagate,
    and the maintenance engine is always disposed.
    """
    if not target.database or not target.database.endswith(("_test", "_fixture")):
        raise ValueError("A dedicated _test or _fixture database is required")
    admin = make_engine(target.set(database="postgres"))
    try:
        with admin.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                quoted = conn.dialect.identifier_preparer.quote_identifier(
                    target.database
                )
                try:
                    conn.exec_driver_sql(f"CREATE DATABASE {quoted}")
                except DBAPIError as exc:
                    # Another application instance may have created it concurrently.
                    if getattr(exc.orig, "sqlstate", None) != "42P04":
                        raise
    finally:
        admin.dispose()


def prepare_database(settings: Settings) -> Engine:
    """Select the mode-specific destination and return an engine owned by the caller.

    Validate destination isolation, provision a missing fixture database, check
    current_database(), then initialize fixture schema. Live schema is prepared
    manually. Dispose on setup failure and propagate the error. Dry runs must
    bypass this function to avoid connections and provisioning side effects.
    """
    target = settings.selected_database_url()
    if settings.fixture_mode:
        ensure_test_database(target)
    engine = make_engine(target)
    try:
        with engine.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != target.database:
                raise ValueError(
                    "Connected database does not match the selected database"
                )
        if settings.fixture_mode:
            initialize(engine)
    except Exception:
        engine.dispose()
        raise
    return engine


def initialize(engine: Engine) -> None:
    """Create missing PostGIS/schema/table objects in one setup transaction.

    Called explicitly or during fixture preparation. create_all does not migrate
    existing columns; apply the supplied SQL migrations for older prototype schemas.
    Missing identity lookup indexes are also created on existing fixture tables.
    """
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS web_ingest"))
        PlanningNotice.metadata.create_all(conn)
        SOURCE_ID_INDEX.create(conn, checkfirst=True)
        SOURCE_URL_INDEX.create(conn, checkfirst=True)


def content_changed(old: dict, new: dict) -> bool:
    """Compare persisted content, excluding identity and all scan timestamps.

    Compare old PostGIS geometry and new Shapely geometry as WKB; other fields
    use Python equality, including order-independent dictionary comparisons.
    This supports reporting; the upsert independently uses SQL IS DISTINCT FROM.
    """
    for name in CONTENT_COLUMNS:
        previous, current = old[name], new[name]
        if name == "geom":
            previous = to_shape(previous).wkb if previous is not None else None
            current = current.wkb if current is not None else None
        if previous != current:
            return True
    return False


def _load_existing(
    conn: Connection,
    observations: list[Observation],
    *,
    chunk_size: int = 500,
) -> dict[tuple[str, str], dict]:
    """Fetch all possible stored identity matches without loading whole namespaces.

    Collect keys, trimmed source IDs and exact canonical URLs from the entire
    batch before reconciliation. Query each namespace separately, limiting each
    IN list to chunk_size values (at most three lists per query). Never limit the
    number of matches: shared IDs/URLs must retain all ambiguity candidates.
    A row matched by multiple queries is returned once, keyed by its identity.

    ID trimming uses the same whitespace set as Python str.strip(), preserving
    case and punctuation. The caller holds the batch transaction/advisory lock.
    Empty input issues no reads. No rows or observation timestamps are modified.
    """
    if chunk_size < 1:
        raise ValueError("Identity lookup chunk size must be positive")
    by_system: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_system[observation.source_system].append(observation)
    existing = {}
    for system, incoming in by_system.items():
        lookups = [
            (TABLE.c.source_key, sorted({o.source_key for o in incoming})),
            (
                func.btrim(TABLE.c.source_id, SOURCE_ID_WHITESPACE_SQL),
                sorted(
                    {
                        o.values["source_id"].strip()
                        for o in incoming
                        if o.values.get("source_id")
                    }
                ),
            ),
            (
                TABLE.c.source_url,
                sorted(
                    {
                        o.values["source_url"]
                        for o in incoming
                        if o.values.get("source_url")
                    }
                ),
            ),
        ]
        for offset in range(0, max(len(values) for _, values in lookups), chunk_size):
            predicates = [
                column.in_(values[offset : offset + chunk_size])
                for column, values in lookups
                if offset < len(values)
            ]
            statement = select(TABLE).where(
                TABLE.c.source_system == system, or_(*predicates)
            )
            for row in conn.execute(statement).mappings():
                existing[row["source_system"], row["source_key"]] = dict(row)
    return existing


def persist_batch(
    engine: Engine,
    observations: list[Observation],
    corridor: Corridor | None,
    scan_time: datetime,
    boundary: StateBoundary | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Prepare raw observations and persist them through the shared transaction owner.

    Retain the original entry point for callers with potentially repeated source
    observations. Preparation handles deduplication and rejects missing identities.
    Call persist_prepared_batch directly when preparation has already completed,
    as in the CLI. Return values and persistence semantics are identical; see
    persist_prepared_batch for the transaction, timestamp and error contracts.
    """
    return persist_prepared_batch(
        engine, prepare_batch(observations), corridor, scan_time, boundary
    )


def persist_prepared_batch(
    engine: Engine,
    batch: PreparedBatch,
    corridor: Corridor | None,
    scan_time: datetime,
    boundary: StateBoundary | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Reconcile, normalize and persist an already prepared observation batch.

    Args:
        engine: Prepared PostgreSQL/PostGIS engine; disposal remains with caller.
        batch: Result of prepare_batch. Its observations must remain unique by
            namespace/key; this entry point does not repeat deduplication or
            mutate the batch. Duplicate counts remain available to reporting.
        corridor: Projected road reference, or None for unknown proximity.
        scan_time: One timezone-aware observation time, converted to UTC.
        boundary: Projected state reference, or None for unknown state membership.

    Reconcile IDs and trustworthy unique item URLs, retaining prior fallback keys.
    Bounded lookups fetch candidate rows by key, ID or URL. Batch-local indexes
    retain all matching identities, including ambiguous ones.
    Merge supplied fields over old raw_record values, preserving omissions and
    honoring explicit clears. Ambiguous shared URLs preserve known fields and
    cannot refresh last_seen_at without identifiable evidence. Older replays
    preserve newer content. Recompute classification from the effective values.

    One transaction and advisory lock cover reading, merging and bulk upsert.
    All application writers must respect the lock; the unique constraint remains
    authoritative. Identical rescans advance last_seen_at, material changes also
    advance content_updated_at, and first_seen_at remains immutable. Timestamps
    cannot regress. Unobserved rows are untouched, including after reference changes.

    Returns:
        Classified dictionaries (including exclusions for reports) and committed
        insert/update/unchanged counters with source/reference change subcounts.
        Newly excluded identities are skipped. Existing excluded rows currently
        reach upsert rather than removal; this explicitly deferred path can fail
        an accepted-row constraint and roll back the batch.

    Raises:
        ValueError: For naive scan times or missing/invalid stored source fields.
        SQLAlchemyError: On database failure; the entire batch rolls back and no
            successful counters are returned.
    """
    if scan_time.tzinfo is None or scan_time.utcoffset() is None:
        raise ValueError("scan_time must be timezone aware")
    scan_time = scan_time.astimezone(UTC)
    unique = batch.observations
    counts = {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "source_updated": 0,
        "reference_updated": 0,
    }
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(176910575, 10)"))
        existing = _load_existing(conn, unique)
        effective: dict[tuple[str, str], Observation] = {}
        existing_ids: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        existing_urls: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        effective_urls: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for stored_key, stored in existing.items():
            system = stored_key[0]
            if stored["source_id"]:
                existing_ids[system, stored["source_id"].strip()].add(stored_key)
            if stored["source_url"]:
                existing_urls[system, stored["source_url"]].add(stored_key)
        unconfirmed_keys: set[tuple[str, str]] = set()
        # Reconciliation is intentionally limited to trustworthy exact canonical URLs.
        for observation in unique:
            key = (observation.source_system, observation.source_key)
            url = observation.values.get("source_url")
            flags = list(observation.flags)
            incoming_id = observation.values.get("source_id")
            if key not in existing and incoming_id:
                id_matches = existing_ids.get(
                    (observation.source_system, incoming_id.strip()), set()
                )
                if len(id_matches) == 1:
                    key = next(iter(id_matches))
                elif len(id_matches) > 1:
                    flags.append("ambiguous_identity")
            url_key = (observation.source_system, url)
            candidates = (
                existing_urls.get(url_key, set()) | effective_urls.get(url_key, set())
                if url
                else set()
            )
            if key not in existing and key not in effective and candidates:
                candidate_key = next(iter(candidates))
                prior_id = (
                    effective[candidate_key].values.get("source_id")
                    if candidate_key in effective
                    else existing[candidate_key]["source_id"]
                )
                incoming_id = observation.values.get("source_id")
                if len(candidates) == 1 and (
                    not prior_id
                    or not incoming_id
                    or prior_id.strip() == incoming_id.strip()
                ):
                    key = candidate_key
                elif not incoming_id or any(
                    k[1].startswith("url:") for k in candidates
                ):
                    flags.append("ambiguous_identity")
            shared_url_identity = (
                not incoming_id
                and key[1].startswith("url:")
                and len(candidates) > 1
                and (key in existing or key in effective)
            )
            if shared_url_identity:
                flags.append("ambiguous_identity")
            old = existing.get(key)
            if key in effective:
                previous = effective[key]
                values = dict(previous.values)
                flags.extend(previous.flags)
            elif old:
                raw = old["raw_record"]
                if not isinstance(raw, dict) or not raw:
                    raise ValueError("Stored raw_record must contain source fields")
                values = dict(raw)
                flags.extend(old["ingest_metadata"].get("observation_flags", []))
            else:
                values = {}
            # An older replay must not resurrect a withdrawn or corrected record.
            if shared_url_identity:
                # A shared URL is not proof that this particular record was seen.
                # Keep known source fields and mark the existing candidate for review.
                if old and key not in effective:
                    unconfirmed_keys.add(key)
            elif old is None or scan_time >= old["last_seen_at"]:
                unconfirmed_keys.discard(key)
                supplied = dict(observation.values)
                # Identity is not a clearable content field; a blank ID is not a
                # withdrawal of the source identity previously reconciled by URL.
                if not supplied.get("source_id"):
                    supplied.pop("source_id", None)
                values.update(supplied)
            # Replace only the effective URL association. Stored URLs remain in
            # their immutable snapshot index until the batch is committed.
            previous_url = (
                effective[key].values.get("source_url") if key in effective else None
            )
            if previous_url:
                previous_url_key = (observation.source_system, previous_url)
                effective_urls[previous_url_key].discard(key)
                if not effective_urls[previous_url_key]:
                    del effective_urls[previous_url_key]
            if values.get("source_url"):
                effective_urls[observation.source_system, values["source_url"]].add(key)
            effective[key] = Observation(
                values,
                sorted(set(flags)),
                observation.source_system,
                observation.synthetic,
            )
        records = []
        payload = []
        for key, observation in effective.items():
            old = existing.get(key)
            if old and scan_time < old["last_seen_at"]:
                row = {name: old[name] for name in TABLE.columns.keys() if name != "id"}
                row["geom"] = to_shape(old["geom"]) if old["geom"] is not None else None
                row["exclusion_reasons"] = [
                    flag.removeprefix("excluded_")
                    for flag in old["quality_flags"]
                    if flag.startswith("excluded_")
                ]
            else:
                row = normalize(observation, corridor, scan_time.date(), boundary)
                row["source_key"] = key[1]  # Keep a reconciled URL-fallback key stable.
            records.append(row)
            if row["exclusion_reasons"] and old is None:
                continue
            if old is None:
                counts["inserted"] += 1
            elif content_changed(old, row):
                counts["updated"] += 1
                if old["raw_record"] != row["raw_record"]:
                    counts["source_updated"] += 1
                old_meta, new_meta = old["ingest_metadata"], row["ingest_metadata"]
                if any(
                    old_meta.get(name) != new_meta.get(name)
                    for name in ("a4_reference", "state_reference")
                ):
                    counts["reference_updated"] += 1
            else:
                counts["unchanged"] += 1
            values = {name: row[name] for name in CONTENT_COLUMNS}
            point = values["geom"]
            values["geom"] = (
                from_shape(point, srid=25832) if point is not None else None
            )
            values.update(
                source_system=key[0],
                source_key=key[1],
                first_seen_at=old["first_seen_at"] if old else scan_time,
                last_seen_at=old["last_seen_at"]
                if key in unconfirmed_keys
                else scan_time,
                content_updated_at=scan_time,
            )
            payload.append(values)
        if payload:
            statement = insert(TABLE).values(payload)
            excluded = statement.excluded
            changed = or_(
                *(
                    TABLE.c[name].is_distinct_from(excluded[name])
                    for name in CONTENT_COLUMNS
                )
            )
            statement = statement.on_conflict_do_update(
                index_elements=["source_system", "source_key"],
                set_={
                    **{name: excluded[name] for name in CONTENT_COLUMNS},
                    "last_seen_at": func.greatest(
                        TABLE.c.last_seen_at, excluded.last_seen_at
                    ),
                    "content_updated_at": case(
                        (
                            changed,
                            func.greatest(
                                TABLE.c.content_updated_at, excluded.content_updated_at
                            ),
                        ),
                        else_=TABLE.c.content_updated_at,
                    ),
                },
            )
            conn.execute(statement)
    # No success counters escape a transaction that failed to commit.
    return records, counts
