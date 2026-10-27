"""Check mode routing, fixture isolation and dry-run behavior with injected settings."""

import json
from unittest.mock import Mock

import pytest
from conftest import FIXTURES

from geospatial_web_harvester import cli, transform
from geospatial_web_harvester.config import Settings
from geospatial_web_harvester.fetcher import FetchError


@pytest.fixture
def mode_environment(monkeypatch):
    # Explicit settings keep these tests independent of a developer's .env.
    settings = Settings(
        _env_file=None, fixture_mode=True, database_url=None, test_database_url=None
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    return settings


def test_configured_fixture_default_never_uses_network(
    mode_environment, monkeypatch, capsys
):
    fetch = Mock(side_effect=AssertionError("Fixture mode must not use HTTP"))
    monkeypatch.setattr("requests.Session.get", fetch)
    assert cli.main(["--dry-run", "--parser-only"]) == 0
    report = json.loads(capsys.readouterr().out)["quality_report"]
    assert report["source_mode"] == "fixture"
    assert report["fetched_cards"] == 8
    fetch.assert_not_called()


def test_fixture_path_overrides_config_only_in_fixture_mode(mode_environment, capsys):
    mode_environment.fixture_paths = [FIXTURES / "planning_basic.html"]
    assert (
        cli.main(
            [
                "--fixture",
                str(FIXTURES / "planning_edge_cases.html"),
                "--dry-run",
                "--parser-only",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)["quality_report"]
    assert report["fetched_cards"] == 32
    assert report["source_mode"] == "fixture"


def test_dry_run_prepares_once_across_fixture_pages(
    mode_environment, monkeypatch, capsys
):
    deduplicate = Mock(wraps=transform.deduplicate)
    monkeypatch.setattr(transform, "deduplicate", deduplicate)
    args = ["--dry-run", "--parser-only"]
    for _ in range(2):
        args.extend(["--fixture", str(FIXTURES / "planning_basic.html")])
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)["quality_report"]
    deduplicate.assert_called_once()
    assert report["parsed_observations"] == report["fetched_cards"] == 16
    assert report["unique_keys"] == 7
    assert report["duplicates"] == 9


def test_live_mode_ignores_fixture_argument_and_reports_fetch_failure(
    mode_environment,
    monkeypatch,
    tmp_path,
):
    mode_environment.fixture_mode = False
    fetch = Mock(side_effect=FetchError("Mocked unavailable source"))
    read = Mock(side_effect=AssertionError("Live mode must not read fixtures"))
    database = Mock(
        side_effect=AssertionError("Failed acquisition must not open a database")
    )
    monkeypatch.setattr("geospatial_web_harvester.fetcher.HttpFetcher.fetch", fetch)
    monkeypatch.setattr(cli, "read_fixture", read)
    monkeypatch.setattr("geospatial_web_harvester.database.prepare_database", database)
    report_path = tmp_path / "failure.json"
    assert (
        cli.main(
            [
                "--fixture",
                "does-not-exist.html",
                "--dry-run",
                "--parser-only",
                "--quality-report",
                str(report_path),
            ]
        )
        == 1
    )
    fetch.assert_called_once_with(mode_environment.source_url)
    read.assert_not_called()
    database.assert_not_called()
    report = json.loads(report_path.read_text())
    assert report["source_mode"] == "live"
    assert report["synthetic"] is False
    assert report["network_failures"] == 1


def test_live_switch_is_visible_and_never_falls_back_to_fixture(
    mode_environment,
    monkeypatch,
    tmp_path,
):
    fetch = Mock(side_effect=FetchError("Mocked inaccessible source"))
    read = Mock(side_effect=AssertionError("No automatic fixture fallback"))
    monkeypatch.setattr("geospatial_web_harvester.fetcher.HttpFetcher.fetch", fetch)
    monkeypatch.setattr(cli, "read_fixture", read)
    assert cli.main(["--live", "--dry-run", "--parser-only"]) == 1
    fetch.assert_called_once()
    read.assert_not_called()


def test_unrecognized_live_html_is_a_parse_failure_without_parser_adaptation(
    mode_environment,
    monkeypatch,
    tmp_path,
):
    mode_environment.fixture_mode = False
    monkeypatch.setattr(
        "geospatial_web_harvester.fetcher.HttpFetcher.fetch",
        Mock(return_value="<html><body>Unverified format</body></html>"),
    )
    report_path = tmp_path / "quality.json"
    assert (
        cli.main(["--dry-run", "--parser-only", "--quality-report", str(report_path)])
        == 1
    )
    report = json.loads(report_path.read_text())
    assert report["parse_failures"] == 1
    assert report["synthetic"] is False


def test_dry_run_does_not_create_a_database(mode_environment, monkeypatch):
    prepare = Mock(side_effect=AssertionError("Dry runs must not create databases"))
    monkeypatch.setattr("geospatial_web_harvester.database.prepare_database", prepare)
    assert cli.main(["--dry-run", "--parser-only"]) == 0
    prepare.assert_not_called()


def test_fixture_write_requires_test_database_even_if_real_url_is_available(
    mode_environment,
    monkeypatch,
):
    mode_environment.database_url = "postgresql+psycopg://role@localhost/real"
    engine = Mock(side_effect=AssertionError("Real DB must not be opened"))
    monkeypatch.setattr("geospatial_web_harvester.database.make_engine", engine)
    assert cli.main(["--parser-only"]) == 1
    engine.assert_not_called()
