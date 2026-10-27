"""Define the source observation and PostgreSQL staging contracts.

``Observation`` carries source fields from parsing through preparation and
field-aware reconciliation. ``PreparedBatch`` groups deduplicated observations
and their duplicate count. Normalization converts each observation to a dictionary
for bulk SQLAlchemy Core persistence; ``PlanningNotice`` declares the table, rather than
serving as an ORM object passed through the pipeline. Importing this module
declares metadata only and does not connect to PostgreSQL.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Match Python 3.12 str.strip(), including Unicode whitespace, in indexed SQL.
# Keep this literal identical in init.sql and migrate_identity_indexes.sql.
SOURCE_ID_WHITESPACE_SQL = literal_column(
    r"E'\u0009\u000A\u000B\u000C\u000D\u001C\u001D\u001E\u001F\u0020"
    r"\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    r"\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000'"
)


@dataclass
class Observation:
    """Carry extracted fields without losing omission versus explicit-empty intent.

    Attributes:
        values: Source-field mapping. An absent key means not supplied; a present
            key with ``None`` means explicitly empty. Raw coordinate strings and
            date text remain available for validation and subsequent rescans.
        flags: Observation warnings, including duplicate and identity conflicts.
        source_system: Identity namespace, separate for fixture and live records.
        synthetic: Processing marker for fixture origin, never a raw source field.

    The container is mutable. Deduplication copies the initial mapping and flag
    list before merging observations; nested field values are not deep-copied.
    """

    values: dict[str, Any]
    flags: list[str] = field(default_factory=list)
    source_system: str = "thueringen_planning_fixture"
    synthetic: bool = True

    @property
    def source_key(self) -> str | None:
        """Prefer a whitespace-trimmed source ID, otherwise use the parsed item URL.

        URL canonicalization and rejection of listing URLs belong to the parser.
        Return ``None`` when neither identity is present. Persistence may retain
        a prior URL-based key when a later observation acquires a source ID.
        """
        source_id = self.values.get("source_id")
        if source_id and source_id.strip():
            return "id:" + source_id.strip()
        url = self.values.get("source_url")
        return "url:" + url if url else None


@dataclass
class PreparedBatch:
    """Carry unique observations and duplicate counts after batch preparation.

    Construct through transform.prepare_batch and treat the contents as read-only.
    Observations are unique by source namespace/key before database reconciliation;
    reconciliation may still resolve different incoming keys to the same identity.
    This container does not imply that dates, geometry or domain status have been
    validated. Neither this container nor its duplicate count is persisted as
    source data. The observations remain the existing Observation transfer type.
    """

    observations: list[Observation]
    duplicate_count: int


class Base(DeclarativeBase):
    """Own SQLAlchemy metadata for the prototype's single staging table."""

    pass


class PlanningNotice(Base):
    """Declare the prototype staging schema and database-enforced invariants.

    Identity is unique on ``(source_system, source_key)``. Geometry is an optional
    EPSG:25832 point; unknown dates and locations use SQL NULL. ``raw_record``
    retains source fields, while ``ingest_metadata`` records processing flags and
    spatial-reference versions. Both JSONB columns are part of initial table creation.

    Persistence owns UTC timestamps: first observation is immutable, last seen
    advances for identifiable observations, and content updated advances for
    material changes. Only accepted/reviewable statuses are allowed. Removal of
    existing rows that become conclusively excluded is currently deferred.
    """

    __tablename__ = "planning_notices"
    __table_args__ = (
        UniqueConstraint(
            "source_system", "source_key", name="uq_planning_notice_source"
        ),
        CheckConstraint("length(source_key) > 0", name="ck_source_key"),
        CheckConstraint(
            "approval_status IN ('approved', 'not_approved', 'unknown')",
            name="ck_approval_status",
        ),
        CheckConstraint(
            "industrial_status IN ('industrial', 'non_industrial', 'unknown')",
            name="ck_industrial_status",
        ),
        CheckConstraint(
            "corridor_status IN ('inside', 'outside', 'unknown')",
            name="ck_corridor_status",
        ),
        CheckConstraint(
            "state_status IN ('inside', 'outside', 'unknown')",
            name="ck_state_status",
        ),
        CheckConstraint(
            "state_status = 'unknown' OR geom IS NOT NULL",
            name="ck_state_geometry",
        ),
        CheckConstraint(
            "screening_status IN ('accepted', 'needs_review')",
            name="ck_screening_status",
        ),
        CheckConstraint(
            "distance_to_a4_m IS NULL OR "
            "(distance_to_a4_m >= 0 AND distance_to_a4_m < 'Infinity'::float8)",
            name="ck_distance",
        ),
        CheckConstraint(
            "(corridor_status = 'unknown' AND distance_to_a4_m IS NULL) OR "
            "(corridor_status != 'unknown' AND distance_to_a4_m IS NOT NULL "
            "AND geom IS NOT NULL)",
            name="ck_corridor_geometry",
        ),
        CheckConstraint(
            "screening_status != 'accepted' OR "
            "(approval_status = 'approved' AND industrial_status = 'industrial' "
            "AND corridor_status = 'inside' AND state_status = 'inside' "
            "AND geom IS NOT NULL)",
            name="ck_accepted",
        ),
        CheckConstraint("jsonb_typeof(quality_flags) = 'array'", name="ck_flags"),
        CheckConstraint(
            "jsonb_typeof(ingest_metadata) = 'object'", name="ck_ingest_metadata"
        ),
        CheckConstraint(
            "first_seen_at <= last_seen_at AND first_seen_at <= content_updated_at",
            name="ck_chronology",
        ),
        {"schema": "web_ingest"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_system: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[date | None]
    publication_date_raw: Mapped[str | None] = mapped_column(Text)
    municipality: Mapped[str | None] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(Text)
    procedure_status: Mapped[str | None] = mapped_column(Text)
    approval_status: Mapped[str] = mapped_column(Text)
    industrial_status: Mapped[str] = mapped_column(Text)
    signal_type: Mapped[str | None] = mapped_column(Text)
    geom: Mapped[Any | None] = mapped_column(
        Geometry("POINT", srid=25832, spatial_index=True)
    )
    distance_to_a4_m: Mapped[float | None]
    corridor_status: Mapped[str] = mapped_column(Text)
    state_status: Mapped[str] = mapped_column(Text, server_default="unknown")
    screening_status: Mapped[str] = mapped_column(Text)
    quality_flags: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    raw_record: Mapped[dict | None] = mapped_column(JSONB)
    ingest_metadata: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Nonunique: shared source IDs/URLs must remain visible as ambiguous candidates.
SOURCE_ID_INDEX = Index(
    "idx_planning_notices_source_id",
    PlanningNotice.source_system,
    func.btrim(PlanningNotice.source_id, SOURCE_ID_WHITESPACE_SQL),
)
SOURCE_URL_INDEX = Index(
    "idx_planning_notices_source_url",
    PlanningNotice.source_system,
    PlanningNotice.source_url,
)
