"""Test state coverage independently of A4 proximity, including boundary points.

Synthetic areas support exact cases; local OSM smoke checks cover real references.
"""

import json
from datetime import date

import pytest
from shapely.geometry import LineString, box

from geospatial_web_harvester.models import Observation
from geospatial_web_harvester.transform import (
    Corridor,
    StateBoundary,
    load_state_boundary,
    normalize,
    project_location,
)


@pytest.mark.parametrize(
    ("xy", "state", "corridor", "excluded"),
    [
        ((645000, 5650000), "inside", "inside", None),
        ((635000, 5645000), "outside", "inside", "outside_thueringen"),
        ((645000, 5660000), "inside", "outside", "outside_a4"),
        ((640000, 5645000), "inside", "inside", None),  # Exact state boundary.
        ((645000, 5655000), "inside", "inside", None),  # Exact 10 km threshold.
        (None, "unknown", "unknown", None),
    ],
)
def test_independent_spatial_checks(basic, xy, state, corridor, excluded):
    road = Corridor(
        LineString([(630000, 5645000), (660000, 5645000)]), {"synthetic": True}
    )
    boundary = StateBoundary(box(640000, 5640000, 660000, 5660000), {"synthetic": True})
    values = {
        **basic.observations[0].values,
        "coordinates": None
        if xy is None
        else {
            "crs": "EPSG:25832",
            "easting": xy[0],
            "northing": xy[1],
        },
    }
    row = normalize(Observation(values), road, date(2026, 10, 27), boundary)
    assert row["state_status"] == state
    assert row["corridor_status"] == corridor
    if excluded:
        assert excluded in row["exclusion_reasons"]
        assert row["screening_status"] == "needs_review"
    elif xy is not None:
        assert row["screening_status"] == "accepted"
    else:
        assert row["geom"] is None
        assert row["distance_to_a4_m"] is None
        assert row["screening_status"] == "needs_review"


@pytest.mark.parametrize(
    ("lon", "lat", "state", "corridor"),
    [
        (10.704, 50.946, "inside", "inside"),
        (10.14, 51.0, "outside", "inside"),  # Test location in Hessen near the A4.
        (10.791, 51.503, "inside", "outside"),
    ],
)
def test_actual_references(lon, lat, state, corridor, real_boundary, real_corridor):
    point, flag = project_location({"crs": "EPSG:4326", "lon": lon, "lat": lat})
    assert flag is None
    assert real_boundary.classify(point) == state
    assert real_corridor.measure(point)[0] == corridor


def test_real_boundary_smoke(real_boundary):
    assert real_boundary.area.geom_type in {"Polygon", "MultiPolygon"}
    assert real_boundary.area.is_valid
    assert 15000 < real_boundary.provenance["area_km2"] < 18000
    assert real_boundary.provenance["polygon_features"] == 1
    assert real_boundary.provenance["skipped_features"] == 2


@pytest.mark.parametrize(
    "kind", ["missing", "empty", "line", "small", "invalid", "wrong_state", "wrong_crs"]
)
def test_unusable_boundary_fails(tmp_path, kind):
    path = tmp_path / "boundary.geojson"
    if kind == "missing":
        with pytest.raises(FileNotFoundError):
            load_state_boundary(path)
        return
    feature = {
        "type": "Feature",
        "properties": {"ISO3166-2": "DE-TH"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[10, 51], [11, 51], [11, 50.5], [10, 51]]],
        },
    }
    data = {"type": "FeatureCollection", "features": [feature]}
    if kind == "empty":
        data["features"] = []
    elif kind == "line":
        feature["geometry"] = {
            "type": "LineString",
            "coordinates": [[10, 51], [12, 51]],
        }
    elif kind == "invalid":
        feature["geometry"]["coordinates"] = [
            [[10, 51], [11, 50], [10, 50], [11, 51], [10, 51]]
        ]
    elif kind == "wrong_state":
        feature["properties"]["ISO3166-2"] = "DE-HE"
    elif kind == "wrong_crs":
        data["crs"] = {"properties": {"name": "EPSG:25832"}}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_state_boundary(path)


def test_road_never_substitutes_for_missing_state(basic, real_corridor):
    row = normalize(basic.observations[0], real_corridor, date(2026, 10, 27))
    assert row["corridor_status"] == "inside"
    assert row["state_status"] == "unknown"
    assert row["screening_status"] == "needs_review"
