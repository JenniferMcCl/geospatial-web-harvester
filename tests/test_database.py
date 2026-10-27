"""Verify PostGIS batch identity, rescans, partial updates and transactional behavior.

Integration fixtures require a dedicated test destination. Strict xfail cases
record deferred exclusion removal and independent reference-only recalculation;
they must not be counted as successful acceptance checks.
"""

import os
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from conftest import FIXTURES
from shapely.affinity import translate
from shapely.geometry import box
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from geospatial_web_harvester import transform
from geospatial_web_harvester.config import FIXTURE_URL, PROJECT_ROOT, Settings
from geospatial_web_harvester.database import (
    _load_existing,
    initialize,
    make_engine,
    persist_batch,
    persist_prepared_batch,
    prepare_database,
)
from geospatial_web_harvester.models import (
    SOURCE_ID_INDEX,
    SOURCE_ID_WHITESPACE_SQL,
    SOURCE_URL_INDEX,
    Observation,
    PlanningNotice,
)
from geospatial_web_harvester.parser import parse_page
from geospatial_web_harvester.transform import Corridor, StateBoundary, prepare_batch

pytestmark = pytest.mark.integration
TABLE = PlanningNotice.__table__


@pytest.fixture
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL/PostGIS database")
    # Automatic provisioning is restricted to a dedicated test database.
    engine = prepare_database(
        Settings(_env_file=None, fixture_mode=True, test_database_url=url)
    )
    namespace = "thueringen_planning_fixture_test_" + uuid.uuid4().hex
    with engine.connect() as conn:
        version = conn.scalar(text("SHOW server_version"))
        postgis = conn.scalar(text("SELECT postgis_lib_version()"))
        assert version.startswith("16."), version
        assert postgis.startswith("3.4."), postgis
    yield engine, namespace
    with engine.begin() as conn:
        conn.execute(TABLE.delete().where(TABLE.c.source_system == namespace))
    engine.dispose()


def rows(database):
    engine, namespace = database
    with engine.connect() as conn:
        return {
            r["source_key"]: dict(r)
            for r in conn.execute(
                select(TABLE).where(TABLE.c.source_system == namespace)
            ).mappings()
        }


def test_candidate_reads_are_bounded_complete_and_namespace_scoped(
    database, basic, projected_test_corridor, projected_test_boundary, scan_time
):
    """Fetch only matches, retaining every shared-URL candidate across query chunks."""
    engine, namespace = database
    other_namespace = namespace + "_other"
    base_url = "https://example.invalid/candidates/"

    def observation(source_id, url, system=namespace):
        return Observation(
            {**basic.observations[0].values, "source_id": source_id, "source_url": url},
            source_system=system,
        )

    fallback = observation(None, base_url + "fallback")
    stored = [
        observation("key-only", base_url + "key"),
        fallback,
        observation("url-A", base_url + "shared"),
        observation("url-B", base_url + "shared"),
        *[
            observation(f"unrelated-{i}", base_url + f"unrelated/{i}")
            for i in range(20)
        ],
        observation("key-only", base_url + "foreign-key", other_namespace),
        observation("other-only", base_url + "other", other_namespace),
    ]
    try:
        persist_batch(
            engine, stored, projected_test_corridor, scan_time, projected_test_boundary
        )
        # A fallback identity may have acquired an ID without changing its key.
        with engine.begin() as conn:
            conn.execute(
                TABLE.update()
                .where(
                    TABLE.c.source_system == namespace,
                    TABLE.c.source_key == fallback.source_key,
                )
                .values(
                    source_id="\t\u00a0AB-42\u3000\n",
                    raw_record={
                        **fallback.values,
                        "source_id": "\t\u00a0AB-42\u3000\n",
                    },
                )
            )
        incoming = [
            Observation({"source_id": "key-only"}, source_system=namespace),
            Observation(
                {"source_id": "AB-42", "source_url": base_url + "corrected"},
                source_system=namespace,
            ),
            Observation({"source_url": base_url + "shared"}, source_system=namespace),
            Observation({"source_id": "other-only"}, source_system=other_namespace),
        ]
        parameter_counts = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            parameter_counts.append(len(parameters))

        with engine.begin() as conn:
            event.listen(conn, "before_cursor_execute", record_query)
            assert _load_existing(conn, []) == {}
            assert parameter_counts == []
            found = _load_existing(conn, incoming, chunk_size=1)
        assert len(parameter_counts) == 4
        assert max(parameter_counts) <= 4  # Namespace plus one key, ID and URL.
        assert set(found) == {
            (namespace, "id:key-only"),
            (namespace, fallback.source_key),
            (namespace, "id:url-A"),
            (namespace, "id:url-B"),
            (other_namespace, "id:other-only"),
        }
        assert all(row["last_seen_at"] == scan_time for row in found.values())
    finally:
        with engine.begin() as conn:
            conn.execute(TABLE.delete().where(TABLE.c.source_system == other_namespace))


def test_identity_indexes_upgrade_existing_table_without_changing_records(
    database, basic, projected_test_corridor, projected_test_boundary, scan_time
):
    """Fixture setup and the explicit migration add missing indexes idempotently."""
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine, [original], projected_test_corridor, scan_time, projected_test_boundary
    )
    before = rows(database)[original.source_key]
    indexes = (SOURCE_ID_INDEX, SOURCE_URL_INDEX)
    migration = (PROJECT_ROOT / "sql/migrate_identity_indexes.sql").read_text()
    with engine.begin() as conn:
        for index in indexes:
            index.drop(conn)
    try:
        initialize(engine)
        with engine.begin() as conn:
            for index in indexes:
                index.drop(conn)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(migration)
            conn.exec_driver_sql(migration)
        with engine.connect() as conn:
            for index in indexes:
                definition = conn.scalar(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname='web_ingest' "
                        "AND tablename='planning_notices' AND indexname=:name"
                    ),
                    {"name": index.name},
                )
                assert definition is not None
                assert "UNIQUE" not in definition
        after = rows(database)[original.source_key]
        assert after["id"] == before["id"]
        assert after["raw_record"] == before["raw_record"]
        for field in ("first_seen_at", "last_seen_at", "content_updated_at"):
            assert after[field] == before[field]
    finally:
        initialize(engine)


@pytest.mark.parametrize(
    "whitespace",
    [
        " ",
        "\t\n\r\v\f",
        "\x1c\x1d\x1e\x1f",
        "\u0085\u00a0\u1680",
        "\u2000\u200a\u2028\u2029\u202f\u205f\u3000",
    ],
)
def test_sql_id_trimming_matches_python(database, whitespace):
    """The expression used by lookup and index must preserve Python ID semantics."""
    engine, _ = database
    padded = whitespace + "Ab-42" + whitespace
    with engine.connect() as conn:
        trimmed = conn.scalar(select(func.btrim(padded, SOURCE_ID_WHITESPACE_SQL)))
    assert trimmed == padded.strip() == "Ab-42"


@pytest.mark.parametrize("use_prepared", [False, True], ids=["raw", "prepared"])
def test_basic_repeated_corrected_partial_and_absent(
    database,
    basic,
    projected_test_corridor,
    scan_time,
    projected_test_boundary,
    use_prepared,
    monkeypatch,
):
    engine, namespace = database
    deduplicate = Mock(wraps=transform.deduplicate)
    monkeypatch.setattr(transform, "deduplicate", deduplicate)

    def persist(observations, timestamp):
        writer = persist_prepared_batch if use_prepared else persist_batch
        batch = prepare_batch(observations) if use_prepared else observations
        return writer(
            engine, batch, projected_test_corridor, timestamp, projected_test_boundary
        )

    observations = [replace(o, source_system=namespace) for o in basic.observations]
    _, first = persist(observations, scan_time)
    assert first == {
        "inserted": 4,
        "updated": 0,
        "unchanged": 0,
        "source_updated": 0,
        "reference_updated": 0,
    }
    before = rows(database)
    later = scan_time + timedelta(hours=1)
    _, second = persist(observations, later)
    assert second == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 4,
        "source_updated": 0,
        "reference_updated": 0,
    }
    for key, row in rows(database).items():
        assert row["id"] == before[key]["id"]
        assert row["first_seen_at"] == scan_time
        assert row["last_seen_at"] == later
        assert row["content_updated_at"] == scan_time
    corrected = Observation(
        {
            "source_id": "SYN-Q001",
            "publication_date_raw": "02.10.2026",
            "publication_date_machine": "2026-10-02",
        },
        source_system=namespace,
    )
    third_time = later + timedelta(hours=1)
    _, third = persist([corrected], third_time)
    assert third == {
        "inserted": 0,
        "updated": 1,
        "unchanged": 0,
        "source_updated": 1,
        "reference_updated": 0,
    }
    after = rows(database)
    assert after["id:SYN-Q001"]["title"] == before["id:SYN-Q001"]["title"]
    assert after["id:SYN-Q001"]["screening_status"] == "accepted"
    assert after["id:SYN-Q001"]["content_updated_at"] == third_time
    assert after["id:SYN-Q002"]["last_seen_at"] == later
    _, partial = persist(
        [Observation({"source_id": "SYN-Q001"}, source_system=namespace)],
        third_time + timedelta(hours=1),
    )
    assert deduplicate.call_count == 4  # One pass per scan through either entry point.
    assert partial["unchanged"] == 1
    assert rows(database)["id:SYN-Q001"]["content_updated_at"] == third_time
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.ST_SRID(TABLE.c.geom)).where(
                    TABLE.c.source_system == namespace,
                    TABLE.c.source_key == "id:SYN-Q001",
                )
            )
            == 25832
        )


def test_explicit_clear_withdrawal_empty_and_replay(
    database, basic, projected_test_corridor, scan_time, projected_test_boundary
):
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine,
        [original],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    persist_batch(
        engine,
        [
            Observation(
                {
                    "source_id": "SYN-Q001",
                    "coordinates": {"crs": "UNKNOWN"},
                    "publication_date_raw": "31.02.2026",
                    "publication_date_machine": None,
                },
                source_system=namespace,
            )
        ],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    invalid = rows(database)["id:SYN-Q001"]
    assert invalid["geom"] is None
    assert invalid["publication_date"] is None
    assert invalid["distance_to_a4_m"] is None
    assert invalid["screening_status"] == "needs_review"
    withdrawn_time = scan_time + timedelta(hours=2)
    persist_batch(
        engine,
        [
            Observation(
                {
                    "source_id": "SYN-Q001",
                    "procedure_status": "Genehmigung widerrufen",
                },
                source_system=namespace,
            )
        ],
        projected_test_corridor,
        withdrawn_time,
        boundary=projected_test_boundary,
    )
    assert rows(database)["id:SYN-Q001"]["screening_status"] == "needs_review"
    persist_batch(
        engine,
        [],
        projected_test_corridor,
        withdrawn_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    persist_batch(
        engine,
        [original],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    final = rows(database)["id:SYN-Q001"]
    assert final["screening_status"] == "needs_review"
    assert "excluded_not_approved" in final["quality_flags"]
    assert final["last_seen_at"] == withdrawn_time
    assert final["content_updated_at"] == withdrawn_time


def test_identity_reconciliation_and_same_title(
    database, basic, projected_test_corridor, scan_time, projected_test_boundary
):
    engine, namespace = database
    values = dict(basic.observations[0].values)
    values.pop("source_id")
    fallback = Observation(values, source_system=namespace)
    persist_batch(
        engine,
        [fallback],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    with_id = Observation({**values, "source_id": "NEW-ID"}, source_system=namespace)
    persist_batch(
        engine,
        [with_id],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    assert len(rows(database)) == 1
    assert rows(database)[fallback.source_key]["source_id"] == "NEW-ID"
    changed_url = Observation(
        {"source_id": "NEW-ID", "source_url": "https://example.invalid/new"},
        source_system=namespace,
    )
    persist_batch(
        engine,
        [changed_url],
        projected_test_corridor,
        scan_time + timedelta(hours=2),
        boundary=projected_test_boundary,
    )
    # The retained URL key is found by its now-known ID even after URL changes.
    assert len(rows(database)) == 1
    second = Observation(
        {
            **values,
            "source_id": "OTHER-ID",
            "source_url": "https://example.invalid/other",
        },
        source_system=namespace,
    )
    persist_batch(
        engine,
        [second],
        projected_test_corridor,
        scan_time + timedelta(hours=3),
        boundary=projected_test_boundary,
    )
    assert len(rows(database)) == 2
    assert len({row["title"] for row in rows(database).values()}) == 1


def test_database_unique_constraint_and_transaction_rollback(
    database, basic, projected_test_corridor, scan_time, projected_test_boundary
):
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine,
        [original],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    existing = rows(database)["id:SYN-Q001"]
    values = {k: v for k, v in existing.items() if k != "id"}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            TABLE.update().where(TABLE.c.id == existing["id"]).values(title="rollback")
        )
        conn.execute(TABLE.insert().values(**values))
    assert rows(database)["id:SYN-Q001"]["title"] == existing["title"]


def test_ambiguous_url_fallback_is_not_merged(
    database, basic, projected_test_corridor, scan_time, projected_test_boundary
):
    engine, namespace = database
    values = dict(basic.observations[0].values)
    a = Observation({**values, "source_id": "A"}, source_system=namespace)
    b = Observation({**values, "source_id": "B"}, source_system=namespace)
    persist_batch(
        engine,
        [a, b],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    values.pop("source_id")
    persist_batch(
        engine,
        [Observation(values, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    stored = rows(database)
    assert len(stored) == 3
    fallback = stored["url:" + values["source_url"]]
    assert "ambiguous_identity" in fallback["quality_flags"]
    assert fallback["screening_status"] == "needs_review"


def test_application_batch_rolls_back_on_constraint_error(
    database,
    basic,
    projected_test_corridor,
    scan_time,
    monkeypatch,
    projected_test_boundary,
):
    from geospatial_web_harvester import database as module

    engine, namespace = database
    first = replace(basic.observations[0], source_system=namespace)
    second = replace(basic.observations[1], source_system=namespace)
    original = module.normalize

    def invalid_one(observation, corridor, today, boundary):
        row = original(observation, corridor, today, boundary)
        if observation.source_key == second.source_key:
            row["approval_status"] = "invalid_enum"
        return row

    monkeypatch.setattr(module, "normalize", invalid_one)
    with pytest.raises(IntegrityError):
        persist_batch(
            engine,
            [first, second],
            projected_test_corridor,
            scan_time,
            boundary=projected_test_boundary,
        )
    assert rows(database) == {}


def test_extended_fixture_idempotency(
    database, projected_test_corridor, scan_time, projected_test_boundary
):
    engine, namespace = database
    page = parse_page((FIXTURES / "planning_edge_cases.html").read_text(), FIXTURE_URL)
    observations = [replace(o, source_system=namespace) for o in page.observations]
    persist_batch(
        engine,
        observations,
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    before = rows(database)
    assert len(before) > 10
    assert before["id:SYN-E019"]["id"] != before["id:SYN-E020"]["id"]
    assert "aktualisiert" in before["id:SYN-E018"]["title"]
    _, counts = persist_batch(
        engine,
        observations,
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    assert counts == {
        "inserted": 0,
        "updated": 0,
        "unchanged": len(before),
        "source_updated": 0,
        "reference_updated": 0,
    }
    assert set(rows(database)) == set(before)


@pytest.mark.parametrize(
    "correction",
    [
        {"land_use": "Wohngebiet"},
        {"coordinates": {"crs": "EPSG:4326", "lat": "51.503", "lon": "10.791"}},
    ],
)
def test_previously_accepted_row_becomes_excluded(
    database,
    basic,
    projected_test_corridor,
    scan_time,
    correction,
    projected_test_boundary,
):
    engine, namespace = database
    persist_batch(
        engine,
        [replace(basic.observations[0], source_system=namespace)],
        projected_test_corridor,
        scan_time,
        boundary=projected_test_boundary,
    )
    assert rows(database)["id:SYN-Q001"]["screening_status"] == "accepted"
    persist_batch(
        engine,
        [Observation({"source_id": "SYN-Q001", **correction}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        boundary=projected_test_boundary,
    )
    assert len(rows(database)) == 1
    assert rows(database)["id:SYN-Q001"]["screening_status"] == "needs_review"
    assert any(
        flag.startswith("excluded_")
        for flag in rows(database)["id:SYN-Q001"]["quality_flags"]
    )


@pytest.mark.parametrize("changed_reference", ["state", "road"])
def test_reference_change_is_not_reported_as_source_change(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
    changed_reference,
):
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine, [original], projected_test_corridor, scan_time, projected_test_boundary
    )
    before = rows(database)[original.source_key]
    road, state = projected_test_corridor, projected_test_boundary
    if changed_reference == "state":
        state = StateBoundary(
            box(750000, 5500000, 800000, 5800000), {"synthetic_test_state": "v2"}
        )
    else:
        road = Corridor(
            translate(road.lines, yoff=50000), {"synthetic_test_road": "v2"}
        )
    later = scan_time + timedelta(hours=1)
    _, counts = persist_batch(engine, [original], road, later, state)
    assert counts == {
        "inserted": 0,
        "updated": 1,
        "unchanged": 0,
        "source_updated": 0,
        "reference_updated": 1,
    }
    after = rows(database)[original.source_key]
    assert len(rows(database)) == 1
    assert after["id"] == before["id"]
    assert after["raw_record"] == before["raw_record"]
    assert after["first_seen_at"] == scan_time
    assert after["last_seen_at"] == after["content_updated_at"] == later
    assert after["screening_status"] == "needs_review"


def test_actual_geographic_correction(
    database, basic, real_corridor, real_boundary, scan_time
):
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(engine, [original], real_corridor, scan_time, real_boundary)
    original_id = rows(database)[original.source_key]["id"]
    moved = Observation(
        {
            "source_id": "SYN-Q001",
            "coordinates": {
                "crs": "EPSG:4326",
                "lon": "10.14",
                "lat": "51.0",
            },
        },
        source_system=namespace,
    )
    _, counts = persist_batch(
        engine, [moved], real_corridor, scan_time + timedelta(hours=1), real_boundary
    )
    assert counts["source_updated"] == 1
    assert counts["reference_updated"] == 0
    corrected = rows(database)[original.source_key]
    assert corrected["id"] == original_id
    assert corrected["state_status"] == "outside"
    assert corrected["corridor_status"] == "inside"
    assert "excluded_outside_thueringen" in corrected["quality_flags"]
    persist_batch(
        engine, [original], real_corridor, scan_time + timedelta(hours=2), real_boundary
    )
    assert rows(database)[original.source_key]["screening_status"] == "accepted"


def test_revised_fixture_partial_and_explicit_empty_fields(
    database,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
):
    engine, namespace = database
    page = parse_page((FIXTURES / "planning_edge_cases.html").read_text(), FIXTURE_URL)
    selected = [
        replace(o, source_system=namespace)
        for o in page.observations
        if o.source_key in {"id:SYN-E012", "id:SYN-E024", "id:SYN-E030"}
    ]
    known = []
    for observation in selected:
        values = dict(observation.values)
        if observation.source_key == "id:SYN-E012":
            values["municipality"] = "Jena"
        elif observation.source_key == "id:SYN-E024":
            values["location_text"] = "Previous location wording"
        else:
            values["publication_date_raw"] = "01.10.2026"
        known.append(replace(observation, values=values))
    persist_batch(
        engine, known, projected_test_corridor, scan_time, projected_test_boundary
    )
    _, counts = persist_batch(
        engine,
        selected,
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    assert counts["unchanged"] == 1
    assert counts["source_updated"] == 2
    stored = rows(database)
    assert stored["id:SYN-E012"]["municipality"] == "Jena"
    assert stored["id:SYN-E024"]["location_text"] is None
    assert stored["id:SYN-E030"]["publication_date"] is None
    assert stored["id:SYN-E030"]["publication_date_raw"] is None


def test_cli_database_repeat_with_actual_references(database, tmp_path, monkeypatch):
    import json

    from geospatial_web_harvester import cli

    engine, namespace = database
    deduplicate = Mock(wraps=transform.deduplicate)
    monkeypatch.setattr(transform, "deduplicate", deduplicate)
    monkeypatch.setenv(
        "TEST_DATABASE_URL", engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        engine.url.set(database="harvester_live").render_as_string(hide_password=False),
    )
    original = cli.parse_page

    def isolated_fixture(*args, **kwargs):
        page = original(*args, **kwargs)
        page.observations = [
            replace(o, source_system=namespace) for o in page.observations
        ]
        return page

    monkeypatch.setattr(cli, "parse_page", isolated_fixture)
    report = tmp_path / "quality.json"
    args = [
        "--fixture",
        str(FIXTURES / "planning_basic.html"),
        "--quality-report",
        str(report),
        "--scan-time",
    ]
    assert cli.main([*args, "2026-10-27T12:00:00+00:00"]) == 0
    deduplicate.assert_called_once()
    assert json.loads(report.read_text())["database"]["inserted"] == 4
    assert cli.main([*args, "2026-10-27T13:00:00+00:00"]) == 0
    assert deduplicate.call_count == 2
    second = json.loads(report.read_text())
    assert second["database"]["unchanged"] == 4
    assert second["database"]["updated"] == second["database"]["inserted"] == 0
    assert len(rows(database)) == 4
    assert all("synthetic" not in row["raw_record"] for row in rows(database).values())


def test_separate_test_database_is_created_then_reused(database):
    engine, _ = database
    target = engine.url.set(database="thuringia_" + uuid.uuid4().hex + "_test")
    settings = Settings(
        _env_file=None,
        fixture_mode=True,
        database_url=target.set(database="postgres").render_as_string(
            hide_password=False
        ),
        test_database_url=target.render_as_string(hide_password=False),
    )
    admin = make_engine(target.set(database="postgres"))
    created = None
    reused = None
    try:
        with admin.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                    {"name": target.database},
                )
                == 0
            )
            prior_maintenance_table = conn.scalar(
                text("SELECT to_regclass('web_ingest.planning_notices')")
            )
        created = prepare_database(settings)
        with created.connect() as conn:
            assert conn.scalar(text("SELECT current_database()")) == target.database
            assert (
                conn.scalar(text("SELECT to_regclass('web_ingest.planning_notices')"))
                is not None
            )
            assert conn.scalar(text("SELECT postgis_lib_version()")).startswith("3.4.")
        reused = prepare_database(settings)
        with reused.connect() as conn:
            assert (
                conn.scalar(text("SELECT count(*) FROM web_ingest.planning_notices"))
                == 0
            )
        with admin.connect() as conn:
            assert (
                conn.scalar(text("SELECT to_regclass('web_ingest.planning_notices')"))
                == prior_maintenance_table
            )
    finally:
        if created:
            created.dispose()
        if reused:
            reused.dispose()
        if created:
            with admin.execution_options(
                isolation_level="AUTOCOMMIT"
            ).connect() as conn:
                quoted = conn.dialect.identifier_preparer.quote_identifier(
                    target.database
                )
                conn.exec_driver_sql(f"DROP DATABASE {quoted}")
        admin.dispose()


def test_idless_reused_url_does_not_overwrite_existing_url_identity(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
):
    engine, namespace = database
    values = dict(basic.observations[0].values)
    values.pop("source_id")
    fallback = Observation(values, source_system=namespace)
    persist_batch(
        engine, [fallback], projected_test_corridor, scan_time, projected_test_boundary
    )
    persist_batch(
        engine,
        [Observation({**values, "source_id": "A"}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    persist_batch(
        engine,
        [Observation({**values, "source_id": "B"}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=2),
        projected_test_boundary,
    )
    before = rows(database)
    untrusted = Observation(
        {**values, "title": "Cannot attribute this title to A or B"},
        source_system=namespace,
    )
    later = scan_time + timedelta(hours=3)
    persist_batch(
        engine, [untrusted], projected_test_corridor, later, projected_test_boundary
    )
    after = rows(database)
    assert len(after) == 2
    original = after[fallback.source_key]
    assert original["title"] == before[fallback.source_key]["title"]
    assert original["source_id"] == "A"
    assert original["last_seen_at"] == before[fallback.source_key]["last_seen_at"]
    assert original["screening_status"] == "needs_review"
    assert "ambiguous_identity" in original["quality_flags"]
    assert original["content_updated_at"] == later
    assert after["id:B"]["last_seen_at"] == before["id:B"]["last_seen_at"]


def test_fallback_gaining_id_with_multiple_url_candidates_requires_review(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
):
    engine, namespace = database
    values = dict(basic.observations[0].values)
    values.pop("source_id")
    fallback = Observation(values, source_system=namespace)
    second = Observation(
        {**values, "source_id": "B", "source_url": "https://example.invalid/other"},
        source_system=namespace,
    )
    persist_batch(
        engine,
        [fallback, second],
        projected_test_corridor,
        scan_time,
        projected_test_boundary,
    )
    persist_batch(
        engine,
        [
            Observation(
                {"source_id": "B", "source_url": values["source_url"]},
                source_system=namespace,
            )
        ],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    persist_batch(
        engine,
        [Observation({**values, "source_id": "A"}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=2),
        projected_test_boundary,
    )
    stored = rows(database)
    assert len(stored) == 3
    assert stored[fallback.source_key]["source_id"] is None
    assert stored[fallback.source_key]["last_seen_at"] == scan_time
    assert stored["id:A"]["screening_status"] == "needs_review"
    assert "ambiguous_identity" in stored["id:A"]["quality_flags"]


@pytest.mark.parametrize("strong_first", [True, False])
def test_identified_observation_wins_over_ambiguous_url_in_same_batch(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
    strong_first,
):
    engine, namespace = database
    values = dict(basic.observations[0].values)
    values.pop("source_id")
    fallback = Observation(values, source_system=namespace)
    persist_batch(
        engine, [fallback], projected_test_corridor, scan_time, projected_test_boundary
    )
    persist_batch(
        engine,
        [Observation({**values, "source_id": "A"}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    persist_batch(
        engine,
        [Observation({**values, "source_id": "B"}, source_system=namespace)],
        projected_test_corridor,
        scan_time + timedelta(hours=2),
        projected_test_boundary,
    )
    strong = Observation(
        {"source_id": "A", "title": "Trusted correction"}, source_system=namespace
    )
    ambiguous = Observation(
        {**values, "title": "Unattributable correction"}, source_system=namespace
    )
    observations = [strong, ambiguous] if strong_first else [ambiguous, strong]
    later = scan_time + timedelta(hours=3)
    persist_batch(
        engine, observations, projected_test_corridor, later, projected_test_boundary
    )
    stored = rows(database)[fallback.source_key]
    assert stored["title"] == "Trusted correction"
    assert stored["source_id"] == "A"
    assert stored["last_seen_at"] == later


def test_id_lookup_preserves_case_punctuation_and_fallback_keys(
    database, basic, projected_test_corridor, projected_test_boundary, scan_time
):
    """Trim ID whitespace without merging IDs that differ in case or punctuation."""
    engine, namespace = database
    source_ids = ["AB-42", "ab-42", "AB42"]
    originals = [
        Observation(
            {
                **basic.observations[0].values,
                "source_id": None,
                "source_url": f"https://example.invalid/item/{index}",
            },
            source_system=namespace,
        )
        for index in range(len(source_ids))
    ]
    persist_batch(
        engine, originals, projected_test_corridor, scan_time, projected_test_boundary
    )
    before = rows(database)
    identified = [
        replace(original, values={**original.values, "source_id": source_id})
        for original, source_id in zip(originals, source_ids, strict=True)
    ]
    persist_batch(
        engine,
        identified,
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    corrections = [
        Observation(
            {"source_id": f"  {source_id}\t", "title": f"Corrected {source_id}"},
            source_system=namespace,
        )
        for source_id in source_ids
    ]
    later = scan_time + timedelta(hours=2)
    _, counts = persist_batch(
        engine, corrections, projected_test_corridor, later, projected_test_boundary
    )
    after = rows(database)
    assert set(after) == set(before)
    assert counts["inserted"] == 0
    assert counts["updated"] == 3
    for original, source_id in zip(originals, source_ids, strict=True):
        stored = after[original.source_key]
        assert stored["id"] == before[original.source_key]["id"]
        assert stored["title"] == f"Corrected {source_id}"
        assert stored["source_id"] == f"  {source_id}\t"
        assert stored["first_seen_at"] == scan_time
        assert stored["last_seen_at"] == stored["content_updated_at"] == later
        assert "ambiguous_identity" not in stored["quality_flags"]


@pytest.mark.parametrize("use_stored_url", [False, True])
def test_url_lookup_tracks_effective_changes_and_preserves_stored_snapshot(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
    use_stored_url,
):
    """Remove superseded effective URLs without losing stored URL candidates."""
    engine, namespace = database
    stored_url = "https://example.invalid/stored"
    temporary_url = "https://example.invalid/temporary"
    fallback = Observation(
        {**basic.observations[0].values, "source_id": None, "source_url": stored_url},
        source_system=namespace,
    )
    persist_batch(
        engine, [fallback], projected_test_corridor, scan_time, projected_test_boundary
    )
    persist_batch(
        engine,
        [replace(fallback, values={**fallback.values, "source_id": "A"})],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    original_id = rows(database)[fallback.source_key]["id"]
    observations = [
        Observation(
            {"source_id": "A", "source_url": temporary_url}, source_system=namespace
        ),
        Observation({"source_url": stored_url}, source_system=namespace),
        Observation(
            {
                **basic.observations[0].values,
                "source_id": "B",
                "source_url": stored_url if use_stored_url else temporary_url,
            },
            source_system=namespace,
        ),
    ]
    persist_batch(
        engine,
        observations,
        projected_test_corridor,
        scan_time + timedelta(hours=2),
        projected_test_boundary,
    )
    stored = rows(database)
    assert set(stored) == {fallback.source_key, "id:B"}
    assert stored[fallback.source_key]["id"] == original_id
    assert stored[fallback.source_key]["source_id"] == "A"
    assert stored[fallback.source_key]["source_url"] == stored_url
    assert ("ambiguous_identity" in stored["id:B"]["quality_flags"]) == use_stored_url


def test_multiple_stored_id_matches_are_not_silently_collapsed(
    database, basic, projected_test_corridor, projected_test_boundary, scan_time
):
    """An imported duplicate source ID must remain ambiguous in the lookup."""
    engine, namespace = database
    originals = [
        Observation(
            {
                **basic.observations[0].values,
                "source_id": None,
                "source_url": f"https://example.invalid/imported/{index}",
            },
            source_system=namespace,
        )
        for index in range(2)
    ]
    persist_batch(
        engine, originals, projected_test_corridor, scan_time, projected_test_boundary
    )
    # Model legacy/external data with the same source ID on two fallback keys.
    with engine.begin() as conn:
        for original in originals:
            conn.execute(
                TABLE.update()
                .where(
                    TABLE.c.source_system == namespace,
                    TABLE.c.source_key == original.source_key,
                )
                .values(
                    source_id="AB-42",
                    raw_record={**original.values, "source_id": "AB-42"},
                )
            )
    before = rows(database)
    later = scan_time + timedelta(hours=1)
    persist_batch(
        engine,
        [
            Observation(
                {"source_id": " AB-42 ", "title": "Unattributable correction"},
                source_system=namespace,
            )
        ],
        projected_test_corridor,
        later,
        projected_test_boundary,
    )
    after = rows(database)
    assert set(after) == {*before, "id:AB-42"}
    assert after["id:AB-42"]["screening_status"] == "needs_review"
    assert "ambiguous_identity" in after["id:AB-42"]["quality_flags"]
    for key, old in before.items():
        assert after[key]["id"] == old["id"]
        assert after[key]["raw_record"] == old["raw_record"]
        assert after[key]["last_seen_at"] == scan_time


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Deferred requirement 1: remove conclusively excluded existing rows",
)
def test_deferred_existing_exclusion_is_removed(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
):
    engine, namespace = database
    original = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine, [original], projected_test_corridor, scan_time, projected_test_boundary
    )
    report, _ = persist_batch(
        engine,
        [
            Observation(
                {"source_id": "SYN-Q001", "procedure_status": "Genehmigung widerrufen"},
                source_system=namespace,
            )
        ],
        projected_test_corridor,
        scan_time + timedelta(hours=1),
        projected_test_boundary,
    )
    assert report[0]["exclusion_reasons"] == ["not_approved"]
    assert original.source_key not in rows(database)


@pytest.mark.xfail(
    strict=True,
    raises=AttributeError,
    reason="Deferred requirement 2: independent reference-only recalculation API",
)
def test_deferred_reference_only_recalculation_preserves_observation_time(
    database,
    basic,
    projected_test_corridor,
    projected_test_boundary,
    scan_time,
):
    from geospatial_web_harvester import database as module

    engine, namespace = database
    observation = replace(basic.observations[0], source_system=namespace)
    persist_batch(
        engine,
        [observation],
        projected_test_corridor,
        scan_time,
        projected_test_boundary,
    )
    later = scan_time + timedelta(hours=1)
    moved = Corridor(
        translate(projected_test_corridor.lines, yoff=1000),
        {"synthetic_test_road": "v2"},
    )
    module.reclassify_existing(
        engine,
        source_system=namespace,
        corridor=moved,
        boundary=projected_test_boundary,
        update_time=later,
    )
    stored = rows(database)[observation.source_key]
    assert stored["first_seen_at"] == stored["last_seen_at"] == scan_time
    assert stored["content_updated_at"] == later
    assert stored["distance_to_a4_m"] > 0
