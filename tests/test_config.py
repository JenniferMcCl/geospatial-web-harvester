"""Check environment settings, path resolution and fixture/live database isolation."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from geospatial_web_harvester.config import PROJECT_ROOT, Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in (
        "FIXTURE_MODE",
        "FIXTURE_PATHS",
        "FIXTURE_BASE_URL",
        "DATABASE_URL",
        "TEST_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_fixture_mode_defaults_to_basic_fixture_without_database():
    settings = Settings(_env_file=None)
    assert settings.fixture_mode is True
    assert settings.fixture_files() == [
        PROJECT_ROOT / "tests/fixtures/planning_basic.html"
    ]
    assert settings.fixture_base_url.startswith("https://example.invalid/")


def test_fixture_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("FIXTURE_MODE", "false")
    monkeypatch.setenv("FIXTURE_PATHS", '["tests/fixtures/planning_edge_cases.html"]')
    settings = Settings(_env_file=None)
    assert settings.fixture_mode is False
    assert settings.fixture_paths == [Path("tests/fixtures/planning_edge_cases.html")]


def test_fixture_database_is_selected_independently_of_live_database():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://role@localhost/real",
        test_database_url="postgresql+psycopg://role@localhost/harvester_test",
    )
    assert settings.selected_database_url().database == "harvester_test"
    settings.fixture_mode = False
    assert settings.selected_database_url().database == "real"


def test_fixture_mode_never_falls_back_to_live_database():
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://role@localhost/real"
    )
    with pytest.raises(ValueError, match="TEST_DATABASE_URL"):
        settings.selected_database_url()


@pytest.mark.parametrize("database", ["real", "postgres", "template1", ""])
def test_fixture_database_requires_dedicated_test_name(database):
    settings = Settings(
        _env_file=None,
        test_database_url=f"postgresql+psycopg://role@localhost/{database}",
    )
    with pytest.raises(ValueError):
        settings.selected_database_url()


def test_fixture_database_must_differ_from_real_database_even_with_host_aliases():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://live@localhost:5432/harvester_test",
        test_database_url="postgresql+psycopg://test@127.0.0.1:5432/harvester_test",
    )
    with pytest.raises(ValueError, match="different"):
        settings.selected_database_url()


def test_invalid_database_url_does_not_expose_password():
    settings = Settings(_env_file=None, test_database_url=SecretStr("secret-password"))
    with pytest.raises(ValueError) as exc:
        settings.selected_database_url()
    assert "secret-password" not in str(exc.value)
