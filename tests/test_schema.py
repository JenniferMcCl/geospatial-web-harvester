"""Compare bootstrap SQL with the SQLAlchemy table contract without connecting."""

from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from geospatial_web_harvester.config import PROJECT_ROOT
from geospatial_web_harvester.models import (
    SOURCE_ID_INDEX,
    SOURCE_URL_INDEX,
    PlanningNotice,
)


def test_initialization_sql_matches_model():
    ddl = str(
        CreateTable(PlanningNotice.__table__, if_not_exists=True).compile(
            dialect=dialect()
        )
    )
    sql = (PROJECT_ROOT / "sql/init.sql").read_text()
    assert " ".join(ddl.split()) in " ".join(sql.split())


def test_lookup_indexes_match_bootstrap_and_upgrade_sql():
    """Keep the indexed ID expression identical to the production query contract."""
    scripts = [
        (PROJECT_ROOT / "sql" / name).read_text()
        for name in ("init.sql", "migrate_identity_indexes.sql")
    ]
    for index in (SOURCE_ID_INDEX, SOURCE_URL_INDEX):
        assert not index.unique
        ddl = str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect()))
        for script in scripts:
            assert " ".join(ddl.split()) in " ".join(script.split())
