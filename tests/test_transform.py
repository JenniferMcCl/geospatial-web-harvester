"""Verify date, location, domain and spatial rules using synthetic observations."""

from datetime import date

import pytest
from shapely.geometry import LineString, Point

from geospatial_web_harvester.models import Observation, PreparedBatch
from geospatial_web_harvester.transform import (
    Corridor,
    deduplicate,
    normalize,
    parse_date,
    prepare_batch,
    project_location,
)


def test_basic_pipeline(
    basic, projected_test_corridor, scan_time, projected_test_boundary
):
    unique, _ = deduplicate(basic.observations)
    rows = {
        o.values["source_id"]: normalize(
            o,
            projected_test_corridor,
            scan_time.date(),
            boundary=projected_test_boundary,
        )
        for o in unique
    }
    assert rows["SYN-Q001"]["screening_status"] == "accepted"
    assert rows["SYN-Q002"]["screening_status"] == "accepted"
    for source_id in ("SYN-Q003", "SYN-Q004", "SYN-Q007"):
        assert rows[source_id]["exclusion_reasons"]
    assert rows["SYN-Q005"]["corridor_status"] == "unknown"
    assert rows["SYN-Q005"]["geom"] is None
    assert rows["SYN-Q008"]["screening_status"] == "needs_review"
    assert rows["SYN-Q008"]["publication_date"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("03.10.2026", date(2026, 10, 3)),
        ("2026-10-04", date(2026, 10, 4)),
        ("2026-10-12T08:30:00+02:00", date(2026, 10, 12)),
        (None, None),
        ("31.02.2026", None),
        ("nonsense", None),
    ],
)
def test_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "coordinates",
    [
        {"crs": "EPSG:4326", "lat": "99", "lon": "10.7"},
        {"crs": "EPSG:4326", "lat": "50.9", "lon": "199"},
        {"crs": "EPSG:4326", "lat": "10.7", "lon": "50.9"},
        {"crs": "EPSG:4326", "lat": "nan", "lon": "10.7"},
        {"crs": "EPSG:4326", "lat": "50.9", "lon": "inf"},
        {"crs": "UNKNOWN", "easting": "640000", "northing": "5645000"},
        {"crs": "EPSG:25832", "easting": "0", "northing": "0"},
        {"crs": "EPSG:4326", "lat": "not-a-number"},
    ],
)
def test_invalid_locations(coordinates):
    point, flag = project_location(coordinates)
    assert point is None
    assert flag


def test_crs_roundtrip_and_missing_reference(basic, scan_time, projected_test_boundary):
    point, flag = project_location(
        {"crs": "EPSG:4326", "lat": "50.95", "lon": "11.037"}
    )
    assert flag is None
    assert 630000 < point.x < 650000
    assert 5600000 < point.y < 5700000
    projected, _ = project_location(
        {"crs": "EPSG:25832", "easting": str(point.x), "northing": str(point.y)}
    )
    assert projected.distance(point) < 1e-6
    row = normalize(
        basic.observations[0], None, scan_time.date(), boundary=projected_test_boundary
    )
    assert row["corridor_status"] == "unknown"
    assert row["distance_to_a4_m"] is None
    assert row["screening_status"] == "needs_review"


def test_exact_projected_distances():
    corridor = Corridor(LineString([(0, 0), (100, 0)]), {"synthetic": True}, 10000)
    assert corridor.measure(Point(50, 0)) == ("inside", 0)
    assert corridor.measure(Point(50, 10000)) == ("inside", 10000)
    assert corridor.measure(Point(50, 10001)) == ("outside", 10001)
    assert corridor.measure(None) == ("unknown", None)


def test_conflicting_duplicates_last_explicit_wins():
    a = Observation({"source_id": "X", "title": "First", "municipality": "Gotha"})
    b = Observation({"source_id": "X", "title": "Second"})
    unique, duplicates = deduplicate([a, b])
    assert duplicates == 1
    assert unique[0].values["title"] == "Second"
    assert unique[0].values["municipality"] == "Gotha"
    assert "conflicting_duplicate" in unique[0].flags


def test_preparation_retains_field_presence_order_namespaces_and_duplicate_counts():
    first = Observation({"source_id": "X", "title": "First", "municipality": "Gotha"})
    correction = Observation({"source_id": "X", "title": None})
    another_source = Observation(
        {"source_id": "X", "title": "Separate source"}, source_system="other_fixture"
    )
    batch = prepare_batch([first, correction, another_source, correction])
    assert isinstance(batch, PreparedBatch)
    assert batch.duplicate_count == 2
    assert len(batch.observations) == 2
    merged, separate = batch.observations
    assert merged.values == {"source_id": "X", "title": None, "municipality": "Gotha"}
    assert merged.flags == ["conflicting_duplicate", "duplicate_in_batch"]
    assert separate == another_source
    assert merged.source_system == first.source_system
    assert first.values["title"] == "First"
    assert first.flags == correction.flags == another_source.flags == []
    assert "municipality" not in correction.values


def test_preparation_accepts_empty_input():
    batch = prepare_batch([])
    assert batch.observations == []
    assert batch.duplicate_count == 0


def test_preparation_rejects_missing_identity():
    with pytest.raises(ValueError, match="stable identity"):
        prepare_batch([Observation({"title": "Unidentifiable"})])


def test_raw_record_does_not_contain_synthetic_marker(basic, scan_time):
    row = normalize(basic.observations[0], None, scan_time.date())
    assert "synthetic" not in row["raw_record"]
    assert row["source_system"] == "thueringen_planning_fixture"


def test_raw_record_contains_only_extracted_source_fields(
    basic,
    scan_time,
    projected_test_corridor,
    projected_test_boundary,
):
    observation = basic.observations[0]
    row = normalize(
        observation, projected_test_corridor, scan_time.date(), projected_test_boundary
    )
    assert row["raw_record"] == observation.values
    assert not {
        "synthetic",
        "observation_flags",
        "a4_reference",
        "state_reference",
    }.intersection(row["raw_record"])
    assert row["ingest_metadata"]["a4_reference"] == projected_test_corridor.provenance
    assert (
        row["ingest_metadata"]["state_reference"] == projected_test_boundary.provenance
    )
