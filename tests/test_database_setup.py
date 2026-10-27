"""Mock maintenance connections to check safe, conditional database provisioning."""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from geospatial_web_harvester import database
from geospatial_web_harvester.config import Settings


@pytest.mark.parametrize("exists", [False, True])
def test_test_database_creation_is_conditional_and_uses_autocommit(monkeypatch, exists):
    connection = Mock()
    connection.scalar.return_value = exists
    connection.dialect.identifier_preparer.quote_identifier.return_value = (
        '"harvester_test"'
    )
    admin = Mock()
    admin.execution_options.return_value.connect.return_value = nullcontext(connection)
    factory = Mock(return_value=admin)
    monkeypatch.setattr(database, "make_engine", factory)
    target = make_url("postgresql+psycopg://role@localhost:5432/harvester_test")
    database.ensure_test_database(target)
    assert factory.call_args.args[0].database == "postgres"
    admin.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
    if exists:
        connection.exec_driver_sql.assert_not_called()
    else:
        connection.exec_driver_sql.assert_called_once_with(
            'CREATE DATABASE "harvester_test"'
        )
        connection.dialect.identifier_preparer.quote_identifier.assert_called_once_with(
            "harvester_test"
        )
    admin.dispose.assert_called_once()


def test_fixture_database_is_verified_before_schema_initialization(monkeypatch):
    settings = Settings(
        _env_file=None,
        test_database_url="postgresql+psycopg://role@localhost/harvester_test",
    )
    provision = Mock()
    initialize = Mock()
    connection = Mock()
    connection.scalar.return_value = "real_database"
    engine = Mock()
    engine.connect.return_value = nullcontext(connection)
    monkeypatch.setattr(database, "ensure_test_database", provision)
    monkeypatch.setattr(database, "make_engine", Mock(return_value=engine))
    monkeypatch.setattr(database, "initialize", initialize)
    with pytest.raises(ValueError, match="database"):
        database.prepare_database(settings)
    initialize.assert_not_called()
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("sqlstate", ["42P04", "42501"])
def test_create_race_is_allowed_but_permission_errors_are_not_hidden(
    monkeypatch, sqlstate
):
    connection = Mock()
    connection.scalar.return_value = False
    error = Exception("test error")
    error.sqlstate = sqlstate
    connection.exec_driver_sql.side_effect = DBAPIError("CREATE DATABASE", {}, error)
    admin = Mock()
    admin.execution_options.return_value.connect.return_value = nullcontext(connection)
    monkeypatch.setattr(database, "make_engine", Mock(return_value=admin))
    target = make_url("postgresql+psycopg://role@localhost/harvester_test")
    if sqlstate == "42501":
        with pytest.raises(DBAPIError):
            database.ensure_test_database(target)
    else:
        database.ensure_test_database(target)
    admin.dispose.assert_called_once()
