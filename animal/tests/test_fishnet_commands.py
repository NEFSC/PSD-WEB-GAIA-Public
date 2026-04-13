"""
Regression tests for fishnet management commands.

Ticket:     GAIFAGP-505  (load_fishnets loop regression)
Author:     John Wall
Created:    April 2026

Purpose
-------
GAIFAGP-475 fixed a bug where load_fishnets had a debug block that only
processed ``fishnets[0]``, silently dropping every subsequent GeoJSON file.
These tests prevent that regression by feeding multiple GeoJSON files and
asserting all are ingested.

Prerequisites
-------------
- SpatiaLite backend configured (SPATIALITE_LIBRARY_PATH in settings)
- ``animal.models.Fishnet`` and ``animal.models.Project`` importable

Usage
-----
::

    python manage.py test animal.tests.test_fishnet_commands -v2
"""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TransactionTestCase

from animal.models import Fishnet, Project, ZoomLevel


def _write_fishnet_geojson(directory: Path, vendor_id: str) -> Path:
    """Write a minimal GeoJSON with one polygon feature for a given vendor_id."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"vendor_id": vendor_id},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-70.0, 41.0],
                        [-70.0, 42.0],
                        [-69.0, 42.0],
                        [-69.0, 41.0],
                        [-70.0, 41.0],
                    ]],
                },
            }
        ],
    }
    filepath = directory / f"{vendor_id}_fishnet.geojson"
    filepath.write_text(json.dumps(geojson))
    return filepath


# ===================================================================
# load_fishnets — loop regression (GAIFAGP-475 / GAIFAGP-505)
# ===================================================================

class TestLoadFishnetsLoop(TransactionTestCase):
    """
    Verify load_fishnets processes ALL GeoJSON files in the input
    directory, not just the first one.
    """

    def setUp(self):
        self.zoom_level, _ = ZoomLevel.objects.get_or_create(
            value=1250,
            defaults={"label": "1:1250"},
        )
        self.project = Project.objects.create(
            label="Fishnet Test Project",
            value="fishnet_test",
            zoom_level=self.zoom_level,
        )

    def _call(self, *args, **kwargs):
        out = StringIO()
        err = StringIO()
        call_command("load_fishnets", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_T200_all_files_loaded(self):
        """Regression: all GeoJSON files are processed, not just the first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            _write_fishnet_geojson(tmppath, "VENDOR_ALPHA")
            _write_fishnet_geojson(tmppath, "VENDOR_BRAVO")
            _write_fishnet_geojson(tmppath, "VENDOR_CHARLIE")

            out, err = self._call(
                f"--input_dir={tmppath}",
                f"--project_id={self.project.id}",
            )

            total = Fishnet.objects.filter(project_id=self.project.id).count()
            self.assertEqual(
                total, 3,
                f"Expected 3 fishnets, got {total}. Output:\n{out}",
            )

            # Each vendor_id should appear exactly once
            for vid in ("VENDOR_ALPHA", "VENDOR_BRAVO", "VENDOR_CHARLIE"):
                count = Fishnet.objects.filter(
                    project_id=self.project.id, vendor_id=vid,
                ).count()
                self.assertEqual(count, 1, f"Expected 1 fishnet for {vid}, got {count}")

    def test_T201_empty_file_skipped(self):
        """Empty GeoJSON files are skipped without crashing the loop."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # One valid file, one empty FeatureCollection
            _write_fishnet_geojson(tmppath, "VENDOR_VALID")
            empty_geojson = {
                "type": "FeatureCollection",
                "features": [],
            }
            (tmppath / "empty_fishnet.geojson").write_text(json.dumps(empty_geojson))

            out, err = self._call(
                f"--input_dir={tmppath}",
                f"--project_id={self.project.id}",
            )

            total = Fishnet.objects.filter(project_id=self.project.id).count()
            self.assertEqual(
                total, 1,
                f"Expected 1 fishnet (empty skipped), got {total}",
            )
            self.assertIn("Skipped", out)

    def test_T202_multi_feature_file(self):
        """A single GeoJSON with multiple features creates multiple records."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"vendor_id": "VENDOR_MULTI"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-70.0, 41.0], [-70.0, 41.5],
                            [-69.5, 41.5], [-69.5, 41.0],
                            [-70.0, 41.0],
                        ]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"vendor_id": "VENDOR_MULTI"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-69.5, 41.0], [-69.5, 41.5],
                            [-69.0, 41.5], [-69.0, 41.0],
                            [-69.5, 41.0],
                        ]],
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "multi_feature.geojson"
            filepath.write_text(json.dumps(geojson))

            self._call(
                f"--input_dir={tmpdir}",
                f"--project_id={self.project.id}",
            )

            total = Fishnet.objects.filter(
                project_id=self.project.id, vendor_id="VENDOR_MULTI",
            ).count()
            self.assertEqual(
                total, 2,
                f"Expected 2 cells from multi-feature file, got {total}",
            )
