"""Verify fixture field presence, URL identity, quarantine and duplicate handling."""

from geospatial_web_harvester.config import FIXTURE_URL
from geospatial_web_harvester.parser import canonical_url, parse_page
from geospatial_web_harvester.transform import deduplicate


def test_basic_parser(basic):
    assert basic.fetched_cards == 8
    assert len(basic.observations) == 8
    assert not basic.errors
    first = basic.observations[0]
    assert first.source_key == "id:SYN-Q001"
    assert first.values["source_url"] == "https://example.invalid/record/SYN-Q001"
    assert "Südachse" in first.values["title"]
    assert first.values["publication_date_raw"] == "03.10.2026"
    assert basic.next_url.endswith("?page=2")
    unique, duplicates = deduplicate(basic.observations)
    assert len(unique) == 7
    assert duplicates == 1
    assert unique[0].values["planning_type"] == "Gewerbliche Bauflächen"
    assert unique[0].flags == ["duplicate_in_batch"]


def test_canonical_identity_and_presence():
    assert canonical_url(
        "HTTPS://EXAMPLE.invalid/Record?id=X#fragment", FIXTURE_URL
    ) == ("https://example.invalid/Record?id=X")
    assert canonical_url("", FIXTURE_URL) is None
    html = (
        '<main class="notice-list"><article class="planning-notice" data-record-id="X">'
    )
    omitted = parse_page(html + "</article></main>", FIXTURE_URL).observations[0]
    explicit = parse_page(
        html + '<h2 class="notice-title"></h2></article></main>', FIXTURE_URL
    ).observations[0]
    assert "title" not in omitted.values
    assert explicit.values["title"] is None
