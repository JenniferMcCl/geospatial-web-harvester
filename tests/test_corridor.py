"""Exercise road validation with synthetic inputs and local OSM coverage checks.

Synthetic loader inputs test rejection rules; they are not production references.
"""

import json

import pytest

from geospatial_web_harvester.transform import load_corridor, project_location


@pytest.fixture
def road_json():
    # Synthetic loader input only: deliberately broad extent, no OSM provenance claim.
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "motorway", "ref": "A 4"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[10.03, 51.01], [12.35, 50.88]],
                },
            }
        ],
    }


def save(tmp_path, data):
    path = tmp_path / "synthetic_test_road.geojson"
    path.write_text(json.dumps(data))
    return path


def test_loader_multiline_and_non_line_warning(tmp_path, road_json, caplog):
    original = road_json["features"][0]
    original["geometry"] = {
        "type": "MultiLineString",
        "coordinates": [
            [[10.03, 51.01], [10.5, 50.7], [11, 50.95]],
            [[11, 50.95], [12.35, 50.88]],
        ],
    }
    road_json["features"].append(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [11, 51]},
        }
    )
    reference = load_corridor(save(tmp_path, road_json))
    assert reference.lines.is_valid
    assert reference.provenance["line_features"] == 1
    assert reference.provenance["skipped_features"] == 1
    assert 500000 < reference.lines.bounds[0] < 700000
    assert "Skipping non-line" in caplog.text


@pytest.mark.parametrize(
    "change",
    [
        "tainted",
        "empty",
        "wrong_route",
        "wrong_highway",
        "wrong_crs",
        "nan",
        "bad_extent",
        "missing_geometry",
        "no_lines",
        "corrupt_shape",
        "gap",
    ],
)
def test_reject_invalid_reference(tmp_path, road_json, change):
    feature = road_json["features"][0]
    if change == "tainted":
        feature["properties"]["@tainted"] = True
    elif change == "empty":
        feature["geometry"]["coordinates"] = []
    elif change == "wrong_route":
        feature["properties"]["ref"] = "A9"
    elif change == "wrong_highway":
        feature["properties"]["highway"] = "trunk"
    elif change == "wrong_crs":
        road_json["crs"] = {"properties": {"name": "EPSG:25832"}}
    elif change == "nan":
        feature["geometry"]["coordinates"][0][0] = float("nan")
    elif change == "bad_extent":
        feature["geometry"]["coordinates"] = [[11, 51], [11.1, 51]]
    elif change == "missing_geometry":
        feature["geometry"] = None
    elif change == "no_lines":
        road_json["features"] = []
    elif change == "corrupt_shape":
        feature["geometry"]["coordinates"] = [[11, 51]]
    elif change == "gap":
        feature["geometry"] = {
            "type": "MultiLineString",
            "coordinates": [
                [[10.03, 51.01], [10.8, 50.95]],
                [[11.5, 50.95], [12.35, 50.88]],
            ],
        }
    with pytest.raises(ValueError):
        load_corridor(save(tmp_path, road_json))


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"type":"Point"}'])
def test_corrupt_or_empty_file(tmp_path, text):
    path = tmp_path / "invalid.geojson"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_corridor(path)


def test_real_osm_reference_smoke(real_corridor):
    corridor = real_corridor
    assert corridor.lines.is_valid
    assert corridor.provenance["line_features"] > 1
    assert corridor.lines.bounds[0] > 500000
    assert corridor.provenance["max_longitude_gap_m"] == 0
    assert corridor.provenance["continuous_chains"] == 2
    # A road point from the supplied OSM export, never a claimed planning site.
    point, flag = project_location(
        {
            "crs": "EPSG:4326",
            "lon": "10.9550131",
            "lat": "50.8942108",
        }
    )
    assert flag is None
    status, distance = corridor.measure(point)
    assert status == "inside" and distance < 1000
