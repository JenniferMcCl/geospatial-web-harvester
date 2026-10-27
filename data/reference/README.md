# Geographic Reference Data

This directory contains two OpenStreetMap (OSM) GeoJSON extracts used for spatial screening in the [geospatial-web-harvester](../../README.md). The reference files are independent: the A4 motorway geometry supports distance calculations, while the Thüringen administrative boundary determines state membership.

## Reference files

| File | Geographic content | Recorded OSM snapshot | Valid geometry |
| --- | --- | --- | --- |
| `a4_thueringen.geojson` | A4 motorway across the study region, including sections in Hessen | `2026-10-27T18:00:36Z` | 1,245 LineStrings |
| `thueringen_boundary.geojson` | Thüringen administrative area | `2026-10-27T18:09:51Z` | 1 MultiPolygon |

Both files were exported through [Overpass Turbo](https://overpass-turbo.eu/) in WGS84 longitude/latitude (EPSG:4326). The application reads the supplied GeoJSON without modifying the source files and projects the validated geometries to EPSG:25832 for spatial calculations.

### File integrity

The recorded SHA-256 fingerprints identify the particular reference versions documented here:

```text
a4_thueringen.geojson
9292b79391a03866d882d063a9c2144b946242c45a91bd4cd06bf5ab66366936

thueringen_boundary.geojson
406a9049b6ce9ab439b83d46e076b46bf4ff4442c733f0fb664ddb5ead538904
```

To verify local files on macOS, run from the repository root:

```sh
shasum -a 256 data/reference/*.geojson
```

### Geometry metadata

| Property | A4 motorway | Thüringen boundary |
| --- | --- | --- |
| OSM identifiers/tags | `highway=motorway`, `ref=A 4` | Relation `62366`, `ISO3166-2=DE-TH`, `admin_level=4` |
| WGS84 bounds (west, south, east, north) | `9.9345144, 50.8405463, 12.3562277, 51.0173454` | `9.8769608, 50.2043467, 12.6539368, 51.6492842` |
| EPSG:25832 bounds (min X, min Y, max X, max Y) | `565655.453, 5637461.746, 736283.568, 5652633.616` | `562009.043, 5562762.865, 756805.838, 5723503.534` |
| Projected measure | 382.872 km total line length, counting both carriageways | 16,198.113 km² area |
| Other exported features | None skipped | Two point members skipped; neither represents boundary area |

## Validation and coverage

**A4 motorway.** The supplied segments form two continuous carriageway chains of approximately 191.446 km and 191.426 km. Their nearest separation is approximately 10.955 m. The extract includes the section crossing Hessen near Wommen and Herleshausen; restricting road selection to Thüringen alone would omit approximately 6.6 km between longitudes `10.097534` and `10.1917413`. The motorway is therefore **not clipped to the state boundary**.

Validation checks found no longitudinal coverage gap in the supplied extract. A 1,000 m connectivity tolerance is used to flag significant discontinuities between geometry groups, not to join lines or define the industrial-site study corridor. No missing segments were constructed or snapped together.

**Thüringen boundary.** The usable boundary is a valid, nonempty MultiPolygon with plausible regional extent and area. Two additional point features are ignored with warnings. Boundary lines are not promoted to polygons, and invalid geometry is not silently repaired.

These are geometry and coverage sanity checks, **not independent verification of OSM completeness or survey-grade accuracy**. The motorway extract covers the regional study area, not the whole German A4. Reassess its endpoints and neighbouring sections if the area of interest changes.

## Reproducing or refreshing the extracts

The following Overpass QL queries are **reproduction starting points**, not authenticated copies of the exact original extraction queries. The original query text, Overpass endpoint and actual download times were not recorded. The embedded snapshot timestamps above must not be interpreted as download receipts.

**A4 motorway: include neighbouring states so the road remains continuous.**

```overpass
[out:json][timeout:180];
way["highway"="motorway"]["ref"~"^A ?4$"](50.7,9.7,51.2,12.8);
out geom;
```

**Thüringen: export the administrative relation as a complete area.**

```overpass
[out:json][timeout:180];
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"="DE-TH"];
out geom;
```

In [Overpass Turbo](https://overpass-turbo.eu/), inspect each result on the map and select **Export > Data > GeoJSON**. Save the files under their existing names in this directory. Before replacing the current references:

1. Confirm both carriageways and uninterrupted A4 coverage, including the Hessen section and the study-area endpoints.
2. Confirm that the boundary export contains a complete Polygon/MultiPolygon, not only boundary lines.
3. Re-run the application's reference validation and spatial tests; record feature counts, projected extents, geometry checks and new SHA-256 hashes.
4. Preserve the exact executed queries, Overpass endpoint, export/download UTC times and embedded snapshot timestamps for reproducibility.

## Licensing

Map and boundary data: **© OpenStreetMap contributors**, licensed under the [Open Database License (ODbL)](https://www.openstreetmap.org/copyright). OSM data licensing is separate from the application's code licensing. Assess applicable attribution and share-alike requirements before redistributing reference extracts or derived database content.

For the spatial eligibility rules, point-based corridor limitation and ingestion behavior, see the [main README](../../README.md).
