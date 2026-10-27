"""Validate references and normalize source observations without database access.

Reference loaders read local GeoJSON and cache dissolved EPSG:25832 geometries
in Corridor and StateBoundary objects supplied by the caller. Per-record
normalization uses these objects without file or network access. State membership
and motorway proximity are independent checks: the road is not clipped to the
state. Domain rules describe synthetic fixtures, not verified legal semantics.
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
from pyproj import Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates
from shapely.errors import ShapelyError
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union

from .config import SOURCE_CRS, TARGET_CRS
from .models import Observation, PreparedBatch

LOGGER = logging.getLogger(__name__)
TO_UTM = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)
TO_WGS84 = Transformer.from_crs(TARGET_CRS, SOURCE_CRS, always_xy=True)


@dataclass
class Corridor:
    """Hold projected motorway lines, reference provenance and a metre threshold.

    Production callers use load_corridor; direct construction supports synthetic
    unit-test geometry. The threshold applies on each side of the road and the
    line geometry must share EPSG:25832 with incoming points.
    """

    lines: BaseGeometry
    provenance: dict
    buffer_meters: float = 10000

    def measure(self, point: Point | None) -> tuple[str, float | None]:
        """Return proximity status and distance in metres, including the threshold.

        Missing points return (unknown, None). This does not test state membership.
        """
        if point is None:
            return "unknown", None
        distance = point.distance(self.lines)
        return ("inside" if distance <= self.buffer_meters else "outside"), distance


@dataclass
class StateBoundary:
    """Hold a projected state area and its reference-version metadata."""

    area: BaseGeometry
    provenance: dict

    def classify(self, point: Point | None) -> str:
        """Classify coverage, including the boundary; missing points are unknown."""
        if point is None:
            return "unknown"
        return "inside" if self.area.covers(point) else "outside"


def _read_reference(path: Path) -> tuple[dict, dict]:
    """Read a WGS84 FeatureCollection and fingerprint its exact file bytes.

    Reject empty collections, unsupported declared CRS and marked incomplete
    features. Absent CRS follows GeoJSON longitude/latitude convention. Preserve
    embedded generator/snapshot metadata without inventing download timestamps.
    Geometry-specific validation is performed by the loaders. File errors
    propagate; malformed structure and content raise ValueError.
    """
    content = path.read_bytes()
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise ValueError("Expected a GeoJSON FeatureCollection")
        crs = data.get("crs")
        if crs and crs.get("properties", {}).get("name") not in {
            "EPSG:4326",
            "urn:ogc:def:crs:OGC:1.3:CRS84",
            "urn:ogc:def:crs:EPSG::4326",
        }:
            raise ValueError("Reference GeoJSON must use WGS84 longitude/latitude")
        if not isinstance(data.get("features"), list) or not data["features"]:
            raise ValueError("Reference contains no features")
        for feature in data["features"]:
            properties = feature.get("properties") or {}
            if any(
                properties.get(k) or feature.get(k)
                for k in ("@tainted", "tainted", "incomplete")
            ):
                raise ValueError("Tainted/incomplete reference feature")
        return data, {
            "file": path.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "generator": data.get("generator"),
            "snapshot_timestamp": data.get("timestamp"),
            "license": "OpenStreetMap contributors, ODbL",
        }
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise ValueError(f"Malformed reference GeoJSON: {path.name}") from exc


def _reference_geometry(feature: dict) -> BaseGeometry:
    """Require valid, nonempty 2D geometry with finite regional WGS84 coordinates.

    The broad 9-13 E, 50-52 N bounds are sanity checks, not state membership.
    Invalid geometry is rejected with ValueError rather than silently repaired.
    """
    try:
        geometry = shape(feature["geometry"])
        if geometry.is_empty or not geometry.is_valid or geometry.has_z:
            raise ValueError("Empty, invalid or non-2D reference geometry")
        if any(
            not math.isfinite(x)
            or not math.isfinite(y)
            or not (9 <= x <= 13 and 50 <= y <= 52)
            for x, y in get_coordinates(geometry)
        ):
            raise ValueError("Implausible/non-finite WGS84 reference coordinates")
        return geometry
    except (KeyError, TypeError, AttributeError, ShapelyError) as exc:
        raise ValueError("Malformed reference geometry") from exc


def _project_reference(geometries: list[BaseGeometry]) -> BaseGeometry:
    """Reproject validated WGS84 geometries with GeoPandas and dissolve overlaps."""
    projected = gpd.GeoSeries(geometries, crs=SOURCE_CRS).to_crs(TARGET_CRS)
    return unary_union(projected.to_list())


def _road_coverage(lines: list[BaseGeometry], projected: BaseGeometry) -> dict:
    """Check coarse longitude coverage and projected proximity between road chains.

    WGS84 longitude intervals use an approximate 70,000 m/degree conversion only
    for gap detection. Projected chains must connect through separations of at
    most 1,000 m, allowing parallel carriageways. These checks neither reconstruct
    missing road nor independently prove OSM completeness. The tolerance is
    unrelated to the site corridor threshold. Raise ValueError on major gaps.
    """
    parts = [
        part
        for line in lines
        for part in (line.geoms if line.geom_type == "MultiLineString" else [line])
    ]
    intervals = sorted((part.bounds[0], part.bounds[2]) for part in parts)
    east = intervals[0][1]
    max_gap = 0.0
    for west, next_east in intervals[1:]:
        max_gap = max(max_gap, (west - east) * 70000)
        east = max(east, next_east)
    if max_gap > 1000:
        raise ValueError(
            f"Significant A4 longitude coverage gap: approximately {max_gap:.0f} m"
        )
    merged = (
        linemerge(projected) if projected.geom_type == "MultiLineString" else projected
    )
    chains = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
    # Validation only: do not snap, join or invent road coordinates.
    reached = {0}
    pending = [0]
    while pending:
        index = pending.pop()
        for other in range(len(chains)):
            if other not in reached and chains[index].distance(chains[other]) <= 1000:
                reached.add(other)
                pending.append(other)
    if len(reached) != len(chains):
        raise ValueError("Significantly disconnected A4 sections (>1000 m)")
    return {
        "continuous_chains": len(chains),
        "max_longitude_gap_m": max_gap,
        "connectivity_tolerance_m": 1000,
    }


def load_corridor(path: Path, buffer_meters: float = 10000) -> Corridor:
    """Load, validate and project the regional A4 extract once for a run.

    Require line features with motorway/A4 tags when supplied, plausible regional
    extent, length and coverage. Non-line features are skipped with warnings;
    invalid usable features fail validation. Retain the Hessen road sections:
    distance uses the whole extract before the independent state check.

    Return a Corridor with file fingerprint, available source metadata and
    validation measurements. Raise ValueError for invalid content or threshold;
    propagate file errors. No synthetic replacement is generated.
    """
    if not math.isfinite(buffer_meters) or buffer_meters <= 0:
        raise ValueError("A4 buffer must be finite and positive")
    data, provenance = _read_reference(path)
    lines = []
    skipped = 0
    for feature in data["features"]:
        raw_geometry = feature.get("geometry")
        if raw_geometry is None:
            raise ValueError("Missing A4 geometry")
        if raw_geometry.get("type") not in {"LineString", "MultiLineString"}:
            skipped += 1
            LOGGER.warning("Skipping non-line A4 feature")
            continue
        properties = feature.get("properties") or {}
        if properties.get("highway", "motorway") != "motorway":
            raise ValueError("A4 feature is not highway=motorway")
        if str(properties.get("ref", "A4")).replace(" ", "") != "A4":
            raise ValueError("A4 feature has an unexpected route reference")
        line = _reference_geometry(feature)
        if line.length == 0:
            raise ValueError("Zero-length A4 feature")
        lines.append(line)
    if not lines:
        raise ValueError("No usable A4 lines")
    merged = unary_union(lines)
    west, south, east, north = merged.bounds
    if not (west < 10.2 and east > 12.3 and south < 51.0 and north > 50.9):
        raise ValueError("Suspicious A4 regional coverage")
    projected = _project_reference(lines)
    if projected.length < 180000:
        raise ValueError("Suspiciously short A4 extract")
    provenance.update(
        line_features=len(lines),
        skipped_features=skipped,
        bounds_4326=list(merged.bounds),
        bounds_25832=list(projected.bounds),
        line_length_m=projected.length,
        buffer_meters=buffer_meters,
        **_road_coverage(lines, projected),
    )
    return Corridor(projected, provenance, buffer_meters)


def load_state_boundary(path: Path) -> StateBoundary:
    """Load polygonal state features, validate their union and project to metres.

    Check supplied administrative tags, geometry, regional extent and area.
    Polygon features are dissolved as parts of the state after those checks;
    unrelated polygons cannot be independently identified when tags are absent.
    Non-polygon members are skipped with warnings, never promoted to areas.
    Sanity checks do not independently certify completeness or boundary precision.

    Return StateBoundary with reference provenance; raise ValueError for unusable
    content and propagate file errors. No inferred or synthetic area is substituted.
    """
    data, provenance = _read_reference(path)
    polygons = []
    skipped = 0
    for feature in data["features"]:
        raw_geometry = feature.get("geometry")
        if raw_geometry is None:
            raise ValueError("Missing state boundary geometry")
        if raw_geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            skipped += 1
            LOGGER.warning("Skipping non-polygon state boundary member")
            continue
        properties = feature.get("properties") or {}
        if properties.get("ISO3166-2", "DE-TH") != "DE-TH":
            raise ValueError("Boundary is not the state of Thuringia")
        if (
            properties.get("boundary", "administrative") != "administrative"
            or str(properties.get("admin_level", "4")) != "4"
        ):
            raise ValueError("Expected an administrative state boundary (level 4)")
        polygons.append(_reference_geometry(feature))
    if not polygons:
        raise ValueError("No complete Polygon/MultiPolygon state area")
    merged = unary_union(polygons)
    west, south, east, north = merged.bounds
    if not (
        9.7 < west < 10.1
        and 12.4 < east < 12.9
        and 50.0 < south < 50.4
        and 51.5 < north < 51.8
    ):
        raise ValueError("Implausible Thuringia boundary extent")
    projected = _project_reference(polygons)
    if not projected.is_valid or not 15000e6 < projected.area < 18000e6:
        raise ValueError("Incomplete/implausible Thuringia polygon area")
    provenance.update(
        polygon_features=len(polygons),
        skipped_features=skipped,
        bounds_4326=list(merged.bounds),
        bounds_25832=list(projected.bounds),
        area_km2=projected.area / 1e6,
    )
    return StateBoundary(projected, provenance)


def deduplicate(observations: list[Observation]) -> tuple[list[Observation], int]:
    """Merge repeated namespace/key pairs and return unique observations plus count.

    Preserve first-identity order. Later supplied fields win, including explicit
    None; later omissions retain earlier fields. Add duplicate/conflict flags and
    combine incoming flags deterministically. Initial mappings/lists are copied,
    leaving caller observations unchanged. Raise ValueError for missing identity.
    """
    unique: dict[tuple[str, str], Observation] = {}
    duplicates = 0
    for observation in observations:
        if observation.source_key is None:
            raise ValueError("Observation has no stable identity")
        key = observation.source_system, observation.source_key
        old = unique.get(key)
        if old is None:
            unique[key] = Observation(
                dict(observation.values),
                list(observation.flags),
                observation.source_system,
                observation.synthetic,
            )
            continue
        duplicates += 1
        old.flags.append("duplicate_in_batch")
        if any(
            k in old.values and old.values[k] != v
            for k, v in observation.values.items()
        ):
            old.flags.append("conflicting_duplicate")
        old.values.update(observation.values)
        old.flags = sorted(set(old.flags + observation.flags))
    return list(unique.values()), duplicates


def prepare_batch(observations: list[Observation]) -> PreparedBatch:
    """Deduplicate a complete scan once for classification or database reconciliation.

    Preserve deduplicate's first-key order, last-explicit-value precedence, flags
    and omission/None distinction without mutating caller observations. Reject
    missing identities with ValueError. Empty input produces an empty batch with
    zero duplicates. No classification, I/O or database work is performed here.
    """
    unique, duplicates = deduplicate(observations)
    return PreparedBatch(unique, duplicates)


def parse_date(raw: str | None) -> date | None:
    """Parse German dotted dates or ISO dates/datetimes, returning None on failure.

    Datetimes contribute their written calendar date without timezone conversion.
    The caller retains raw text and flags errors; scan date is never substituted.
    """
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def project_location(coordinates: dict | None) -> tuple[Point | None, str | None]:
    """Validate supplied coordinates and return an EPSG:25832 point or quality flag.

    Accept declared EPSG:4326 lon/lat or EPSG:25832 easting/northing, validating
    numeric finiteness, geographic bounds and broad regional plausibility. pyproj
    uses longitude/easting first (always_xy). The regional check is not evidence
    of state membership or source accuracy. Never swap axes, geocode municipality
    names or invent centroids. Invalid/unknown locations return None with a reason.
    """
    if not coordinates:
        return None, "missing_coordinates"
    try:
        crs = coordinates.get("crs")
        if crs == SOURCE_CRS:
            lon, lat = float(coordinates["lon"]), float(coordinates["lat"])
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise ValueError
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError
            x, y = TO_UTM.transform(lon, lat, errcheck=True)
        elif crs == TARGET_CRS:
            x, y = float(coordinates["easting"]), float(coordinates["northing"])
            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError
            lon, lat = TO_WGS84.transform(x, y, errcheck=True)
        else:
            return None, "unknown_coordinate_crs"
        # Broad regional sanity check, not an administrative boundary test.
        if not (9 <= lon <= 13 and 50 <= lat <= 52):
            return None, "implausible_regional_coordinates"
        return Point(x, y), None
    except (KeyError, TypeError, ValueError, OverflowError, ProjError):
        return None, "invalid_coordinates"


def normalize(
    observation: Observation,
    corridor: Corridor | None,
    today: date,
    boundary: StateBoundary | None = None,
) -> dict:
    """Convert an effective Observation into classified source and derived fields.

    Args:
        observation: Parsed or reconciled source fields; not mutated here.
        corridor: Validated projected A4 reference, or None for unknown proximity.
        today: Scan calendar date used only to flag future publication dates.
        boundary: Validated projected state area, or None for unknown membership.

    Returns:
        A dictionary containing staging content plus report-only exclusion_reasons.
        raw_record copies effective source values; ingest_metadata holds processing
        flags and reference provenance. Geometry remains a Shapely point until
        persistence converts it. Missing/invalid coordinates yield null geometry
        and distance, with both geographic statuses unknown.

    Exact fixture status/land-use rules distinguish approval, planning adoption
    and refusal without inferring approval from industrial keywords. Accepted
    requires industrial approval, both spatial checks inside and no blocking
    completeness/conflict flags. Conclusive negative evidence adds exclusion
    reasons; persistence decides storage. Missing references cannot yield accepted.
    """
    values = observation.values
    flags = list(observation.flags)
    if observation.synthetic:
        flags.append("synthetic_fixture")
    raw_date = values.get("publication_date_raw")
    published = parse_date(raw_date)
    machine = values.get("publication_date_machine")
    if machine and parse_date(machine) != published:
        flags.append("conflicting_publication_dates")
        published = None
    if published is None:
        flags.append(
            "invalid_publication_date" if raw_date else "missing_publication_date"
        )
    elif published > today:
        flags.append("future_publication_date")
    if not values.get("title"):
        flags.append("missing_title")
    if not values.get("source_url"):
        flags.append("missing_item_url")
    if not values.get("municipality"):
        flags.append("missing_municipality")
    if not values.get("location_text"):
        flags.append("missing_location_text")
    stage = (values.get("procedure_status") or "").casefold()
    if any(
        word in stage
        for word in (
            "entwurf",
            "auslegung",
            "offenlage",
            "beteiligung",
            "abgelehnt",
            "widerruf",
            "zurückgenommen",
            "aufgehoben",
            "versagt",
            "nicht genehmigt",
        )
    ):
        approval, signal = "not_approved", "planning_or_refusal"
    elif any(
        word in stage for word in ("satzungsbeschluss", "inkrafttreten", "planfest")
    ):
        approval, signal = "unknown", "planning_adoption"
        flags.append("planning_adoption_not_site_permit")
    elif stage in {
        "genehmigung erteilt",
        "baugenehmigung erteilt",
        "zulassung erteilt",
    }:
        approval, signal = "approved", "approval_notice"
        # Exact fixture status is a prototype assumption, not legal verification.
    else:
        approval, signal = "unknown", None
        flags.append("unknown_approval_status")
    land_use = (values.get("land_use") or "").casefold()
    if any(term in land_use for term in ("wohn", "einzelhandel")):
        industrial = "non_industrial"
    elif land_use in {"industriegebiet", "gewerbegebiet", "gewerbliche bauflächen"}:
        industrial = "industrial"
    else:
        industrial = "unknown"
        flags.append("unknown_industrial_use")
    point, location_flag = project_location(values.get("coordinates"))
    if location_flag:
        flags.append(location_flag)
    status, distance = corridor.measure(point) if corridor else ("unknown", None)
    if corridor is None:
        flags.append("a4_reference_unavailable")
    state_status = boundary.classify(point) if boundary else "unknown"
    if boundary is None:
        flags.append("state_reference_unavailable")
    reasons = []
    if approval == "not_approved":
        reasons.append("not_approved")
    if industrial == "non_industrial":
        reasons.append("non_industrial")
    if status == "outside":
        reasons.append("outside_a4")
    if state_status == "outside":
        reasons.append("outside_thueringen")
    flags.extend("excluded_" + reason for reason in reasons)
    incomplete = any(
        flag in flags
        for flag in (
            "missing_title",
            "missing_publication_date",
            "invalid_publication_date",
            "future_publication_date",
            "conflicting_publication_dates",
            "ambiguous_identity",
            "conflicting_duplicate",
        )
    )
    screening = "needs_review"
    if (
        not reasons
        and approval == "approved"
        and industrial == "industrial"
        and status == "inside"
        and state_status == "inside"
    ):
        if not incomplete:
            screening = "accepted"
    result = {
        name: values.get(name)
        for name in (
            "source_id",
            "source_url",
            "title",
            "description",
            "municipality",
            "location_text",
            "procedure_status",
        )
    }
    result.update(
        source_system=observation.source_system,
        source_key=observation.source_key,
        publication_date=published,
        publication_date_raw=raw_date,
        approval_status=approval,
        industrial_status=industrial,
        signal_type=signal,
        geom=point,
        distance_to_a4_m=distance,
        corridor_status=status,
        state_status=state_status,
        screening_status=screening,
        quality_flags=sorted(set(flags)),
        raw_record=dict(values),
        ingest_metadata={
            "observation_flags": sorted(set(observation.flags)),
            "a4_reference": corridor.provenance if corridor else None,
            "state_reference": boundary.provenance if boundary else None,
        },
        exclusion_reasons=reasons,
    )
    return result
