"""Geospatial HTML harvesting prototype with optional PostGIS persistence.

The pipeline carries models.Observation from parser through batch preparation and
database reconciliation. models.PreparedBatch groups unique observations and the
duplicate count. transform.normalize returns classified dictionaries;
database persists eligible payloads and cli exports results and quality reports.
Real OSM reference geometry is distinct from synthetic announcement data. The
example source's HTML and legal status semantics remain unverified.

Importing the package performs no acquisition, file loading or database setup.
"""
