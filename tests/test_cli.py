"""Check CLI exports, quality reports and explicit acquisition/parser failures."""

import json
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from conftest import FIXTURES

from geospatial_web_harvester.cli import main


def test_installed_project_name_and_entry_points():
    """Check installed metadata and both supported entry points after a rename."""
    installed = distribution("geospatial-web-harvester")
    entry = next(
        entry
        for entry in installed.entry_points
        if entry.group == "console_scripts" and entry.name == "geospatial-web-harvester"
    )
    assert entry.value == "geospatial_web_harvester.cli:main"
    console = Path(sys.executable).with_name("geospatial-web-harvester")
    for command in (
        [str(console)],
        [sys.executable, "-m", "geospatial_web_harvester.cli"],
    ):
        result = subprocess.run(
            [*command, "--help"], check=True, capture_output=True, text=True
        )
        assert "--dry-run" in result.stdout
        assert "--fixture" in result.stdout


def test_installed_cli_missing_reference_mode(tmp_path, capsys):
    output = tmp_path / "output.json"
    report = tmp_path / "report.json"
    args = [
        "--fixture",
        str(FIXTURES / "planning_basic.html"),
        "--dry-run",
        "--a4-geojson",
        str(tmp_path / "missing.geojson"),
        "--allow-missing-a4",
        "--scan-time",
        "2026-10-27T12:00:00+00:00",
        "--output",
        str(output),
        "--quality-report",
        str(report),
    ]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["synthetic"] is True
    assert (
        len(result["records"]) == 5
    )  # Without road data the northern site is unknown.
    assert result["quality_report"]["accepted"] == 0
    assert result["quality_report"]["excluded"] == 2
    assert result["quality_report"]["database"]["inserted"] == 0
    assert all(r["corridor_status"] == "unknown" for r in result["records"])
    assert json.loads(capsys.readouterr().out) == result


def test_input_failure_does_not_report_database_success(tmp_path):
    fixture = tmp_path / "bad.html"
    fixture.write_text("Unavailable")
    report = tmp_path / "report.json"
    assert (
        main(
            [
                "--fixture",
                str(fixture),
                "--dry-run",
                "--allow-missing-a4",
                "--a4-geojson",
                str(tmp_path / "missing.geojson"),
                "--quality-report",
                str(report),
            ]
        )
        == 1
    )
    result = json.loads(report.read_text())
    assert result["success"] is False
    assert result["database"] == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "source_updated": 0,
        "reference_updated": 0,
    }


def test_naive_scan_time_rejected():
    assert (
        main(
            [
                "--fixture",
                str(FIXTURES / "planning_basic.html"),
                "--dry-run",
                "--scan-time",
                "2026-10-27T12:00:00",
            ]
        )
        == 1
    )


def test_missing_state_reference_fails_even_if_road_is_optional(tmp_path):
    assert (
        main(
            [
                "--fixture",
                str(FIXTURES / "planning_basic.html"),
                "--dry-run",
                "--allow-missing-a4",
                "--state-geojson",
                str(tmp_path / "missing.geojson"),
            ]
        )
        == 1
    )
