"""Share deterministic source, clock and spatial inputs across the test suite.

Synthetic road/state shapes support exact rule tests only. Session-scoped real
OSM fixtures validate the supplied local reference files and skip if absent;
synthetic announcement points are not evidence of real project locations.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from shapely.geometry import LineString, box

from geospatial_web_harvester.config import FIXTURE_URL, PROJECT_ROOT
from geospatial_web_harvester.parser import parse_page
from geospatial_web_harvester.transform import (
    Corridor,
    StateBoundary,
    load_corridor,
    load_state_boundary,
    project_location,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def basic():
    """Parse the basic synthetic listing through the production parser."""
    return parse_page((FIXTURES / "planning_basic.html").read_text(), FIXTURE_URL)


@pytest.fixture
def scan_time():
    """Provide one aware UTC observation time without using the system clock."""
    return datetime(2026, 10, 27, 12, tzinfo=UTC)


@pytest.fixture
def projected_test_corridor():
    """Build a synthetic EPSG:25832 test road, isolated from production references."""
    # Explicitly synthetic test road; never written to data/reference or used by CLI.
    points = [
        project_location({"crs": "EPSG:4326", "lat": lat, "lon": lon})[0]
        for lon, lat in (
            ("10.704", "50.946"),
            ("11.037", "50.955"),
            ("11.59", "50.923"),
        )
    ]
    return Corridor(LineString(points), {"synthetic_test_road": True})


@pytest.fixture
def projected_test_boundary():
    """Build an intentionally rectangular synthetic area for deterministic tests."""
    return StateBoundary(
        box(550000, 5500000, 800000, 5800000), {"synthetic_test_state": True}
    )


@pytest.fixture(scope="session")
def real_boundary():
    """Validate and cache the local OSM state area, or skip when not supplied."""
    path = PROJECT_ROOT / "data/reference/thueringen_boundary.geojson"
    if not path.exists():
        pytest.skip("Real state boundary is not supplied")
    return load_state_boundary(path)


@pytest.fixture(scope="session")
def real_corridor():
    """Validate and cache the local OSM A4 extract, or skip when not supplied."""
    path = PROJECT_ROOT / "data/reference/a4_thueringen.geojson"
    if not path.exists():
        pytest.skip("Real A4 GeoJSON is not supplied")
    return load_corridor(path)
