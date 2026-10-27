"""Check extended synthetic announcements for identity and classification outcomes."""

from datetime import date

from conftest import FIXTURES

from geospatial_web_harvester.config import FIXTURE_URL
from geospatial_web_harvester.parser import parse_page
from geospatial_web_harvester.transform import deduplicate, normalize


def test_extended_fixture(projected_test_corridor, projected_test_boundary):
    page = parse_page((FIXTURES / "planning_edge_cases.html").read_text(), FIXTURE_URL)
    assert page.fetched_cards == 32
    assert len(page.observations) == 31
    assert len(page.errors) == 1  # Identity is required; title alone is not.
    unique, duplicates = deduplicate(page.observations)
    assert len(unique) == 30
    assert duplicates == 1
    rows = {
        o.source_key: normalize(
            o,
            projected_test_corridor,
            date(2026, 10, 27),
            boundary=projected_test_boundary,
        )
        for o in unique
    }
    assert rows["id:SYN-E002"]["screening_status"] == "needs_review"
    assert rows["id:SYN-E002"]["signal_type"] == "planning_adoption"
    for source_id in ("SYN-E003", "SYN-E004", "SYN-E005", "SYN-E006", "SYN-E007"):
        assert rows["id:" + source_id]["exclusion_reasons"]
    assert rows["id:SYN-E008"]["industrial_status"] == "unknown"
    assert rows["id:SYN-E009"]["publication_date"] is None
    assert rows["id:SYN-E010"]["publication_date_raw"] == "31.02.2026"
    assert rows["id:SYN-E010"]["publication_date"] is None
    assert rows["id:SYN-E011"]["publication_date"] == date(2026, 10, 12)
    assert "Kühlsysteme & Lüftung" in rows["id:SYN-E012"]["title"]
    assert rows["id:SYN-E012"]["municipality"] is None
    assert "missing_municipality" in rows["id:SYN-E012"]["quality_flags"]
    for source_id in ("SYN-E013", "SYN-E014", "SYN-E015", "SYN-E016", "SYN-E030"):
        assert rows["id:" + source_id]["geom"] is None
        assert rows["id:" + source_id]["distance_to_a4_m"] is None
        assert rows["id:" + source_id]["corridor_status"] == "unknown"
    assert rows["id:SYN-E017"]["geom"].x == 640000
    assert "aktualisiert" in rows["id:SYN-E018"]["title"]
    assert "conflicting_duplicate" in rows["id:SYN-E018"]["quality_flags"]
    assert rows["id:SYN-E019"]["title"] == rows["id:SYN-E020"]["title"]
    assert "url:https://example.invalid/record/stable-url-identifier-001" in rows
    assert rows["id:SYN-E023"]["source_url"].endswith(
        "/plan/details?record=SYN-E023&lang=de"
    )
    assert rows["id:SYN-E024"]["source_url"] is None
    assert rows["id:SYN-E024"]["location_text"] is None
    assert rows["id:SYN-E025"]["approval_status"] == "unknown"
    assert rows["id:SYN-E026"]["title"] is None
    assert rows["id:SYN-E026"]["screening_status"] == "needs_review"
    for source_id in ("SYN-E027", "SYN-E028", "SYN-E031"):
        assert rows["id:" + source_id]["geom"] is not None
    assert "future_publication_date" in rows["id:SYN-E029"]["quality_flags"]
    assert rows["id:SYN-E030"]["publication_date_raw"] is None
    assert "missing_publication_date" in rows["id:SYN-E030"]["quality_flags"]
    assert rows["id:SYN-E027"]["municipality"] == "Gotha"
    assert rows["id:SYN-E027"]["publication_date"] == date(2026, 10, 2)
    assert "Produktionshalle" in rows["id:SYN-E001"]["description"]


def test_expectation_attributes_are_ignored():
    html = (FIXTURES / "planning_basic.html").read_text()
    altered = html.replace(
        'data-test-expected-signal="candidate"', 'data-test-expected-signal="exclude"'
    )
    assert (
        parse_page(html, FIXTURE_URL).observations
        == parse_page(altered, FIXTURE_URL).observations
    )
