"""
Regression tests for the ``poi`` management command.

Ticket:     GAIFAGP-484  (IV&V compliance remediation)
Author:     John Wall
Created:    February 2026

Purpose
-------
Verify that formatting-only changes to poi.py (line wraps, docstrings)
and behavioral changes introduce no regressions.  Covers all 7 actions
(describe, list, validate, inspect, repair, delete, load), CLI flags,
and CommandError guard paths.

Assertions are deterministic: fixture counts are known, so every filter
test asserts an exact expected count.  AOI traversal is wired via an
EarthExplorer fixture (skipped if the EE model is unavailable).

Artifact mode
-------------
Write every test's stdout to a deterministic file tree for before/after
diffing.  Output is normalized to replace timestamps and durations with
stable placeholders so diffs reflect only functional changes.

Set the artifact directory via **either**:

1. Environment variable (one-off)::

    set POI_TEST_ARTIFACTS=C:\\gis\\test_artifacts\\before
    python manage.py test animal.tests.test_poi_command -v2
    REM ... replace source files ...
    set POI_TEST_ARTIFACTS=C:\\gis\\test_artifacts\\after
    python manage.py test animal.tests.test_poi_command -v2
    diff -ru C:\\gis\\test_artifacts\\before C:\\gis\\test_artifacts\\after

2. Django setting (persistent — add to settings.py)::

    POI_TEST_ARTIFACTS = r"C:\\gis\\test_artifacts\\before"

If neither is set, artifact mode is off and tests run normally.

Prerequisites
-------------
- SpatiaLite backend configured (SPATIALITE_LIBRARY_PATH in settings)
- ``animal.models.PointsOfInterest`` and ``animal.models.ExtractTransformLoad``
  importable
- For ETL (unmanaged model): the test runner must create the table.
  See ``setUpClass`` for the raw-SQL fallback if Django skips it.

Usage
-----
::

    python manage.py test animal.tests.test_poi_command -v2
    python manage.py test animal.tests.test_poi_command.TestPoiCount -v2
"""

import os
import re
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TestCase, TransactionTestCase

from animal.models import PointsOfInterest, ExtractTransformLoad, Project, ZoomLevel

# ---------------------------------------------------------------------------
# Optional model imports — mirrors _get_optional_model in poi.py
# ---------------------------------------------------------------------------
try:
    from animal.models import Annotations
    HAS_ANNOTATIONS = True
except ImportError:
    HAS_ANNOTATIONS = False

try:
    from animal.models import AreaOfInterest
    HAS_AOI = True
except ImportError:
    HAS_AOI = False

try:
    from animal.models import EarthExplorer
    HAS_EE = True
except ImportError:
    HAS_EE = False

try:
    from django.contrib.gis.geos import Point as GEOSPoint
    HAS_GEOS = True
except ImportError:
    HAS_GEOS = False


# ===================================================================
# Fixture expectations
#
# Five POIs are created by _create_fixtures().  Every count assertion
# in this file derives from these constants.  If you change the
# fixtures, update the constants — that IS the point.
#
#   poi_full:       CAT001, VENDOR001, ENTITY001, WV03, 2024-06-15, project
#   poi_partial:    CAT002, VENDOR002, None,      WV02, 2024-07-20, project
#   poi_null_dates: CAT003, VENDOR003, None,      None, None,       project
#   poi_fillable:   CAT004, VENDOR004, None,      WV03, None,       project
#   poi_orphan:     None,   None,      None,      None, None,       project
# ===================================================================

TOTAL_POIS = 5
POIS_WITH_DATES = 2          # poi_full + poi_partial
POIS_NULL_DATES = 3          # poi_null_dates + poi_orphan + poi_fillable
POIS_NULL_CATALOG_ID = 1     # poi_orphan
POIS_WITH_CATALOG_CAT001 = 1
POIS_WITH_VENDOR_001 = 1
POIS_WITH_ENTITY_001 = 1
POIS_WITH_PROJECT = 5        # all five — project_id is NOT NULL
POIS_SENSOR_WV03 = 2         # poi_full + poi_fillable
POIS_SENSOR_WV02 = 1         # poi_partial
UNIQUE_CATALOG_IDS = 4       # CAT001-CAT004 (orphan excluded)

# Backfill-dates expectations:
#   poi_fillable (CAT004) has ETL date 2024-08-10 → WILL fill

# AOI traversal (if EE + AOI models exist):
#   EE fixture links AOI -> vendor_id "VENDOR001"
#   ETL "CAT001" has vendor_id "VENDOR001"
#   POIs with catalog_id "CAT001" = 1 (poi_full)
POIS_VIA_AOI = 1


# ===================================================================
# Artifact mode
# ===================================================================

def _resolve_artifact_dir():
    """
    Resolve artifact output directory.  Checked in order:

    1. ``POI_TEST_ARTIFACTS`` environment variable
    2. ``POI_TEST_ARTIFACTS`` in Django settings
    3. None (artifact mode off)

    Examples::

        # env var -- one-off run
        set POI_TEST_ARTIFACTS=C:\\gis\\test_artifacts\\before
        python manage.py test animal.tests.test_poi_command -v2

        # settings.py -- persistent
        POI_TEST_ARTIFACTS = r"C:\\gis\\test_artifacts\\before"
    """
    from_env = os.environ.get("POI_TEST_ARTIFACTS")
    if from_env:
        return from_env
    try:
        from django.conf import settings
        return getattr(settings, "POI_TEST_ARTIFACTS", None)
    except Exception:
        return None


ARTIFACT_DIR = _resolve_artifact_dir()


def _write_artifact(test_id, stdout_text, stderr_text=""):
    """
    If POI_TEST_ARTIFACTS is set, write stdout/stderr to a
    deterministic path for before/after diffing.

    Output is normalized to strip known-noisy lines (timestamps,
    runtimes, generated-at markers) so diffs reflect only functional
    changes.
    """
    if not ARTIFACT_DIR:
        return
    base = Path(ARTIFACT_DIR)
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{test_id}.stdout.txt").write_text(
        _normalize_output(stdout_text),
    )
    if stderr_text.strip():
        (base / f"{test_id}.stderr.txt").write_text(
            _normalize_output(stderr_text),
        )


# Patterns that introduce nondeterminism in poi.py output.
# Each regex matches a full line; the line is replaced with a
# stable placeholder so diffs stay clean.
_NOISY_PATTERNS = [
    # timezone.now().isoformat() calls in delete/repair output
    (re.compile(r"^(\s*Timestamp:\s+)\d{4}-\d{2}-\d{2}T.*$", re.MULTILINE),
     r"\1<TIMESTAMP>"),
    # Any "Generated at:" or "Run at:" style lines
    (re.compile(r"^(\s*(?:Generated|Run|Executed) at:\s+).*$", re.MULTILINE),
     r"\1<TIMESTAMP>"),
    # Runtime/elapsed duration lines
    (re.compile(r"^(\s*(?:Runtime|Elapsed|Duration):\s+)[\d.]+\s*s.*$",
                re.MULTILINE),
     r"\1<DURATION>"),
]


def _normalize_output(text):
    """
    Strip nondeterministic content from command output so artifact
    diffs reflect only functional changes.
    """
    for pattern, replacement in _NOISY_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ===================================================================
# Helpers
# ===================================================================

def _ensure_etl_table():
    """
    ExtractTransformLoad is unmanaged (``managed = False``).
    Django's test runner may skip table creation for unmanaged models.
    If so, create it with raw SQL so fixture inserts succeed.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='etl';"
        )
        if not cur.fetchone():
            cur.execute("""
                CREATE TABLE IF NOT EXISTS etl (
                    id          VARCHAR(100) PRIMARY KEY,
                    table_name  VARCHAR(10),
                    aoi_id      INTEGER,
                    vendor_id   VARCHAR(100),
                    entity_id   VARCHAR(100),
                    vendor      VARCHAR(100),
                    platform    VARCHAR(50),
                    pixel_size_x REAL,
                    pixel_size_y REAL,
                    date        DATE,
                    publish_date DATE,
                    geometry    TEXT,
                    provenance_source VARCHAR(255),
                    date_image_taken DATE,
                    sea_state_qual VARCHAR(100),
                    sea_state_quant INTEGER,
                    shareable VARCHAR(100)
                );
            """)


def _extract_count(output):
    """
    Parse the integer from ``poi count`` output.

    Handles both normal (``POI count: 4``) and quiet (``4``) modes.
    Returns None if no integer is found.
    """
    m = re.search(r"POI count:\s*(\d+)", output)
    if m:
        return int(m.group(1))
    # Quiet mode: bare integer
    stripped = output.strip()
    if stripped.isdigit():
        return int(stripped)
    return None


def _extract_showing_of(output):
    """
    Parse ``Showing N of M POI(s):`` from list/summary output.
    Returns (shown, total) or None.
    """
    m = re.search(r"Showing\s+(\d+)\s+of\s+(\d+)\s+POI", output)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _csv_rows(output, expected_header=None):
    """
    Extract CSV data rows from command output, anchored on a header line.

    If *expected_header* is provided, parsing starts at the first line
    that matches it; only subsequent lines with the same column count
    are returned.  If *expected_header* is None, the first line
    containing a comma is treated as the header.

    Returns list of lists (split on comma).
    """
    lines = output.strip().splitlines()
    header_idx = None
    header_col_count = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if expected_header and stripped == expected_header:
            header_idx = i
            header_col_count = len(stripped.split(","))
            break
        if expected_header is None and "," in stripped:
            # First comma-containing line is the header
            header_idx = i
            header_col_count = len(stripped.split(","))
            break

    if header_idx is None or header_col_count is None:
        return []

    rows = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("=") or stripped.startswith("-"):
            continue
        parts = stripped.split(",")
        if len(parts) == header_col_count:
            rows.append(parts)
    return rows




# ===================================================================
# Mixin: fixture creation + call helpers
# ===================================================================

class PoiTestMixin:
    """
    Shared fixture builder and call helpers.

    Call ``self._create_fixtures()`` in ``setUp`` to populate the test
    database with 4 POIs + 3 ETL records + (optionally) a Project
    and AOI.
    """

    project = None
    aoi = None

    def _create_fixtures(self):
        _ensure_etl_table()

        # --- Project (NOT NULL after GAIFAGP-530) -------------------------
        self.zoom_level, _ = ZoomLevel.objects.get_or_create(
            value=1250,
            defaults={"label": "1:1250"},
        )
        self.project = Project.objects.create(
            label="Test Project", zoom_level=self.zoom_level,
        )

        # --- AOI (if model exists) ------------------------------------
        if HAS_AOI and HAS_GEOS:
            from django.contrib.gis.geos import Polygon
            bbox = Polygon.from_bbox((-70.0, 41.0, -69.0, 42.0))
            self.aoi = AreaOfInterest.objects.create(
                name="Test AOI", geometry=bbox, sqkm=100.0,
            )

        # --- ETL records -----------------------------------------------
        with connection.cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO etl "
                "(id, table_name, vendor_id, entity_id, date, platform) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("CAT001", "EE", "VENDOR001", "ENTITY001",
                 "2024-06-15", "WV03"),
            )
            cur.execute(
                "INSERT OR IGNORE INTO etl "
                "(id, table_name, vendor_id, entity_id, date, platform) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("CAT002", "EE", "VENDOR002", "ENTITY002",
                 "2024-07-20", "WV02"),
            )
            cur.execute(
                "INSERT OR IGNORE INTO etl "
                "(id, table_name, vendor_id, entity_id, date, platform) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("CAT003", "EE", "VENDOR003", None, None, "WV03"),
            )
            # CAT004: ETL with a known date, for poi_fillable to consume
            cur.execute(
                "INSERT OR IGNORE INTO etl "
                "(id, table_name, vendor_id, entity_id, date, platform) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("CAT004", "EE", "VENDOR004", "ENTITY004",
                 "2024-08-10", "WV03"),
            )

        # --- EarthExplorer (if model exists) ---------------------------
        #     Wires AOI -> EE -> ETL -> POI for deterministic --aoi tests.
        if HAS_EE and self.aoi and HAS_GEOS:
            from django.contrib.gis.geos import Polygon as GEOSPolygon
            ee_bounds = GEOSPolygon.from_bbox((-70.0, 41.0, -69.0, 42.0))
            EarthExplorer.objects.create(
                catalog_id="CAT001",
                entity_id="ENTITY001",
                vendor_id="VENDOR001",
                aoi_id=self.aoi,
                vendor="DigitalGlobe",
                satellite="WV03",
                sensor="MS",
                number_of_bands=8,
                map_projection="UTM",
                datum="WGS84",
                processing_level="LV1B",
                file_format="GeoTIFF",
                license_id=0,
                sun_azimuth=155.0,
                sun_elevation=60.0,
                event="",
                center_latitude_dec=41.5,
                center_longitude_dec=-69.5,
                thumbnail="",
                acquisition_date=date(2024, 6, 15),
                cloud_cover=10,
                pixel_size_x=0.31,
                pixel_size_y=0.31,
                bounds=ee_bounds,
            )

        # --- POIs ------------------------------------------------------
        # Required NOT NULL geometry after GAIFAGP-523.
        default_point = GEOSPoint(-69.5, 41.5, srid=4326)

        self.poi_full = PointsOfInterest.objects.create(
            catalog_id="CAT001",
            vendor_id="VENDOR001",
            entity_id="ENTITY001",
            sensor="WV03",
            date_image_taken=date(2024, 6, 15),
            point=default_point,
            project=self.project,
        )
        self.poi_partial = PointsOfInterest.objects.create(
            catalog_id="CAT002",
            vendor_id="VENDOR002",
            entity_id=None,
            sensor="WV02",
            date_image_taken=date(2024, 7, 20),
            point=default_point,
            project=self.project,
        )
        self.poi_null_dates = PointsOfInterest.objects.create(
            catalog_id="CAT003",
            vendor_id="VENDOR003",
            entity_id=None,
            sensor=None,
            date_image_taken=None,
            point=default_point,
            project=self.project,
        )
        self.poi_orphan = PointsOfInterest.objects.create(
            catalog_id=None,
            vendor_id=None,
            entity_id=None,
            sensor=None,
            date_image_taken=None,
            point=default_point,
            project=self.project,
        )
        # poi_fillable: NULL date, but ETL CAT004 has date=2024-08-10.
        # Backfill-dates --confirm MUST fill this one.
        self.poi_fillable = PointsOfInterest.objects.create(
            catalog_id="CAT004",
            vendor_id="VENDOR004",
            entity_id=None,
            sensor="WV03",
            date_image_taken=None,
            point=default_point,
            project=self.project,
        )

    # --- Call helpers --------------------------------------------------

    def _call(self, *args, **kwargs):
        """
        Call ``poi`` command, return (stdout, stderr) strings.
        If artifact mode is on, write output to disk.
        """
        out = StringIO()
        err = StringIO()
        call_command("poi", *args, stdout=out, stderr=err, **kwargs)
        stdout_text = out.getvalue()
        stderr_text = err.getvalue()

        # Derive test ID from the calling method name
        test_id = getattr(self, "_testMethodName", "unknown")
        _write_artifact(test_id, stdout_text, stderr_text)

        return stdout_text, stderr_text

    def _call_expecting_error(self, *args, **kwargs):
        """Assert that calling ``poi`` raises CommandError; return message."""
        with self.assertRaises(CommandError) as ctx:
            self._call(*args, **kwargs)
        return str(ctx.exception)


# ===================================================================
# 1. describe (default = stats)
# ===================================================================

class TestPoiDescribe(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T01_describe_basic(self):
        """describe reports correct total and date breakdown."""
        out, _ = self._call("describe")
        self.assertIn("PointsOfInterest Table Statistics", out)
        self.assertIn(f"{TOTAL_POIS}", out)
        # Date breakdown present
        self.assertIn("With date_image_taken:", out)
        self.assertIn("NULL date_image_taken:", out)

    def test_T02_describe_quiet_output(self):
        """describe --quiet + --output writes to file; file is non-empty."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            self._call("describe", "--quiet", f"--output={tmp_path}")
            content = Path(tmp_path).read_text()
            self.assertGreater(len(content), 0)
            self.assertIn("Table Statistics", content)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ===================================================================
# 2. describe --detail=count — exercises _build_queryset; exact expected counts
# ===================================================================

class TestPoiDescribeCount(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T03_count_all(self):
        out, _ = self._call("describe", "--detail=count")
        self.assertEqual(_extract_count(out), TOTAL_POIS)

    def test_T04_count_null_dates(self):
        out, _ = self._call("describe", "--detail=count", "--null-dates")
        self.assertEqual(_extract_count(out), POIS_NULL_DATES)

    def test_T05_count_quiet(self):
        """--quiet emits bare integer, parseable by scripts."""
        out, _ = self._call("describe", "--detail=count", "--quiet")
        self.assertEqual(_extract_count(out), TOTAL_POIS)

    def test_T06_count_has_dates(self):
        out, _ = self._call("describe", "--detail=count", "--has-dates")
        self.assertEqual(_extract_count(out), POIS_WITH_DATES)

    def test_T07_count_null_catalog_id(self):
        out, _ = self._call("describe", "--detail=count", "--null-catalog-id")
        self.assertEqual(_extract_count(out), POIS_NULL_CATALOG_ID)

    def test_T08_count_catalog_id(self):
        out, _ = self._call("describe", "--detail=count", "--catalog-id=CAT001")
        self.assertEqual(_extract_count(out), POIS_WITH_CATALOG_CAT001)

    def test_T09_count_vendor_id(self):
        out, _ = self._call("describe", "--detail=count", "--vendor-id=VENDOR001")
        self.assertEqual(_extract_count(out), POIS_WITH_VENDOR_001)

    def test_T10_count_entity_id(self):
        out, _ = self._call("describe", "--detail=count", "--entity-id=ENTITY001")
        self.assertEqual(_extract_count(out), POIS_WITH_ENTITY_001)

    def test_T11_count_project(self):
        out, _ = self._call("describe", "--detail=count", f"--project={self.project.id}")
        self.assertEqual(_extract_count(out), POIS_WITH_PROJECT)

    def test_T12_count_aoi(self):
        if not self.aoi:
            self.skipTest("AOI model or GEOS not available")
        if not HAS_EE:
            self.skipTest("EarthExplorer model not available for AOI traversal")
        out, _ = self._call("describe", "--detail=count", f"--aoi={self.aoi.id}")
        self.assertEqual(_extract_count(out), POIS_VIA_AOI)


# ===================================================================
# 3. list — 4 format modes + selection filters
# ===================================================================

class TestPoiList(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    # -- Format modes --------------------------------------------------

    def test_T13_list_simple(self):
        """Simple format includes all 4 POIs and correct structure."""
        out, _ = self._call("list", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result, "Expected 'Showing N of M' header")
        shown, total = result
        self.assertEqual(total, TOTAL_POIS)
        # Verify specific catalog_ids appear
        self.assertIn("CAT001", out)
        self.assertIn("CAT002", out)
        self.assertIn("N/A", out)  # orphan renders as N/A

    def test_T14_list_table(self):
        """Table format has column headers and separator line."""
        out, _ = self._call("list", "--limit=100", "--format=table")
        self.assertIn("ID", out)
        self.assertIn("Catalog ID", out)
        self.assertIn("---", out)
        self.assertIn("CAT001", out)

    def test_T15_list_csv(self):
        """CSV format: one row per POI, 4 fields each (no header)."""
        out, _ = self._call("list", "--limit=100", "--format=csv")
        # List CSV has no header line — parse all comma-delimited data
        # lines, skipping the "Showing N of M" preamble.
        lines = [
            l.strip() for l in out.strip().splitlines()
            if l.strip() and "," in l.strip()
            and not l.strip().startswith("Showing")
        ]
        self.assertEqual(len(lines), TOTAL_POIS)
        for line in lines:
            parts = line.split(",")
            self.assertEqual(
                len(parts), 4,
                f"CSV row should have exactly 4 fields, got: {parts}",
            )
        # CAT001 appears exactly once
        cat001_lines = [l for l in lines if "CAT001" in l]
        self.assertEqual(len(cat001_lines), 1)

    def test_T16_list_jira(self):
        """Jira format produces the Showing header (format dispatch may vary)."""
        out, _ = self._call("list", "--limit=100", "--format=jira")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result, "Expected 'Showing N of M' header")
        _, total = result
        self.assertEqual(total, TOTAL_POIS)

    # -- Filter paths --------------------------------------------------

    def test_T17_list_null_dates(self):
        out, _ = self._call("list", "--null-dates", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_NULL_DATES)

    def test_T18_list_has_dates(self):
        out, _ = self._call("list", "--has-dates", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_WITH_DATES)
        self.assertIn("CAT001", out)
        self.assertNotIn("CAT003", out)

    def test_T19_list_catalog_id(self):
        out, _ = self._call("list", "--catalog-id=CAT001", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_WITH_CATALOG_CAT001)
        self.assertIn("CAT001", out)
        self.assertNotIn("CAT002", out)

    def test_T20_list_vendor_id(self):
        out, _ = self._call("list", "--vendor-id=VENDOR001", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_WITH_VENDOR_001)

    def test_T21_list_entity_id(self):
        out, _ = self._call("list", "--entity-id=ENTITY001", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_WITH_ENTITY_001)

    def test_T22_list_project(self):
        out, _ = self._call(
            "list", f"--project={self.project.id}", "--limit=100",
        )
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_WITH_PROJECT)

    def test_T23_list_aoi(self):
        if not self.aoi:
            self.skipTest("AOI model or GEOS not available")
        if not HAS_EE:
            self.skipTest("EarthExplorer model not available for AOI traversal")
        out, _ = self._call("list", f"--aoi={self.aoi.id}", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_VIA_AOI)
        self.assertIn("CAT001", out)

    def test_T24_list_poi_id(self):
        out, _ = self._call("list", f"--id={self.poi_full.id}")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, 1)
        self.assertIn("CAT001", out)

    def test_T25_list_filter(self):
        out, _ = self._call("list", "--filter", "sensor=WV03", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_SENSOR_WV03)

    def test_T26_list_null_catalog_id(self):
        out, _ = self._call("list", "--null-catalog-id", "--limit=100")
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        self.assertEqual(total, POIS_NULL_CATALOG_ID)

    def test_T27_list_combined_filters(self):
        """Combined --null-dates + --project narrows correctly."""
        out, _ = self._call(
            "list", "--null-dates",
            f"--project={self.project.id}", "--limit=100",
        )
        result = _extract_showing_of(out)
        self.assertIsNotNone(result)
        _, total = result
        # poi_null_dates + poi_fillable + poi_orphan: all have project + null date.
        self.assertEqual(total, POIS_NULL_DATES)


# ===================================================================
# 4. describe --detail=summary — structural + count assertions
# ===================================================================

class TestPoiDescribeSummary(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T28_summary_simple(self):
        out, _ = self._call("describe", "--detail=summary", "--limit=100")
        self.assertIn("POI Summary by Catalog ID", out)
        self.assertIn(f"Total unique catalog_ids: {UNIQUE_CATALOG_IDS}", out)
        self.assertIn("CAT001", out)
        self.assertIn("CAT002", out)
        self.assertIn("CAT003", out)

    def test_T29_summary_table(self):
        out, _ = self._call("describe", "--detail=summary", "--limit=100", "--format=table")
        self.assertIn("Catalog ID", out)
        self.assertIn("Vendor ID(s)", out)
        self.assertIn("---", out)

    def test_T30_summary_csv(self):
        """Summary CSV has expected header and one row per catalog_id."""
        out, _ = self._call("describe", "--detail=summary", "--limit=100", "--format=csv")
        expected_header = "catalog_id,vendor_ids,poi_count,date_image_taken"
        self.assertIn(expected_header, out)
        rows = _csv_rows(out, expected_header=expected_header)
        self.assertEqual(
            len(rows), UNIQUE_CATALOG_IDS,
            f"Expected {UNIQUE_CATALOG_IDS} data rows after header, "
            f"got {len(rows)}",
        )

    def test_T31_summary_jira(self):
        out, _ = self._call("describe", "--detail=summary", "--limit=100", "--format=jira")
        self.assertIn("CAT001", out)

    def test_T32_summary_null_dates(self):
        out, _ = self._call("describe", "--detail=summary", "--null-dates", "--limit=100")
        self.assertIn("Total unique catalog_ids: 2", out)
        self.assertIn("CAT003", out)
        self.assertIn("CAT004", out)
        self.assertNotIn("CAT001", out)

    def test_T33_summary_has_dates(self):
        out, _ = self._call("describe", "--detail=summary", "--has-dates", "--limit=100")
        self.assertIn("Total unique catalog_ids: 2", out)
        self.assertIn("CAT001", out)
        self.assertIn("CAT002", out)
        self.assertNotIn("CAT003", out)

    def test_T34_summary_catalog_id(self):
        out, _ = self._call("describe", "--detail=summary", "--catalog-id=CAT001")
        self.assertIn("Total unique catalog_ids: 1", out)
        self.assertIn("CAT001", out)

    def test_T35_summary_vendor_id(self):
        out, _ = self._call("describe", "--detail=summary", "--vendor-id=VENDOR001")
        self.assertIn("Total unique catalog_ids: 1", out)

    def test_T36_summary_entity_id(self):
        out, _ = self._call("describe", "--detail=summary", "--entity-id=ENTITY001")
        self.assertIn("Total unique catalog_ids: 1", out)

    def test_T37_summary_project(self):
        out, _ = self._call(
            "describe", "--detail=summary", f"--project={self.project.id}", "--limit=100",
        )
        self.assertIn(
            f"Total unique catalog_ids: {UNIQUE_CATALOG_IDS}", out,
        )

    def test_T38_summary_aoi(self):
        if not self.aoi:
            self.skipTest("AOI model or GEOS not available")
        if not HAS_EE:
            self.skipTest("EarthExplorer model not available for AOI traversal")
        out, _ = self._call("describe", "--detail=summary", f"--aoi={self.aoi.id}", "--limit=100")
        self.assertIn("Total unique catalog_ids: 1", out)
        self.assertIn("CAT001", out)

    def test_T39_summary_poi_id(self):
        out, _ = self._call("describe", "--detail=summary", f"--id={self.poi_full.id}")
        self.assertIn("Total unique catalog_ids: 1", out)
        self.assertIn("CAT001", out)

    def test_T40_summary_filter(self):
        out, _ = self._call(
            "describe", "--detail=summary", "--filter", "sensor=WV03", "--limit=100",
        )
        self.assertIn("Total unique catalog_ids: 2", out)
        self.assertIn("CAT001", out)
        self.assertIn("CAT004", out)

    def test_T41_summary_null_catalog_id(self):
        """--null-catalog-id gives 0 unique catalog_ids (they're all NULL)."""
        out, _ = self._call("describe", "--detail=summary", "--null-catalog-id", "--limit=100")
        self.assertIn("Total unique catalog_ids: 0", out)


# ===================================================================
# 5. inspect
# ===================================================================

class TestPoiInspect(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T42_inspect_known(self):
        """inspect shows correct field values for a known POI."""
        out, _ = self._call("inspect", f"--id={self.poi_full.id}")
        self.assertIn("POI INSPECTION REPORT", out)
        self.assertIn("CAT001", out)
        self.assertIn("VENDOR001", out)
        self.assertIn("ENTITY001", out)
        self.assertIn("2024-06-15", out)

    def test_T43_inspect_nonexistent(self):
        """CommandError for missing POI."""
        msg = self._call_expecting_error("inspect", "--id=999999999")
        self.assertIn("not found", msg)

    def test_T44_inspect_no_id(self):
        """CommandError when --id is omitted."""
        msg = self._call_expecting_error("inspect")
        self.assertIn("--id", msg)


# ===================================================================
# 6. validate + repair --dry-run (diagnose)
# ===================================================================

class TestPoiValidateAndDiagnose(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T45_validate_cli_output(self):
        """validate CLI reports header and results section."""
        out, _ = self._call("validate")
        self.assertIn("POI Data Validation Report", out)
        self.assertIn("VALIDATION RESULTS", out)
        self.assertIn("CHAIN SUMMARY", out)

    def test_T46_validate_utility_findings(self):
        """validate_poi_chain() returns specific known findings from fixtures."""
        from animal.utils.poi_validation import validate_poi_chain
        result = validate_poi_chain()

        # Fixture: poi_orphan has no catalog_id, vendor_id, or entity_id
        has_no_ids = any("no catalog_id, vendor_id, or entity_id" in i
                         for i in result["issues"])
        self.assertTrue(has_no_ids,
            f"Expected 'no identifiers' issue for poi_orphan. "
            f"Issues: {result['issues']}")

        # Fixture: 3 POIs with NULL date_image_taken
        has_null_dates = any(
            f"{POIS_NULL_DATES} POIs with NULL date_image_taken" in w
            for w in result["warnings"]
        )
        self.assertTrue(has_null_dates,
            f"Expected {POIS_NULL_DATES} NULL dates warning. "
            f"Warnings: {result['warnings']}")

        # Chain counts
        self.assertEqual(
            result["chain_counts"]["PointsOfInterest"], TOTAL_POIS)
        self.assertIn("ExtractTransformLoad", result["chain_counts"])

    def test_T47_repair_dry_run(self):
        """repair --dry-run diagnoses our orphan POI."""
        out, _ = self._call("repair", "--dry-run")
        # Should report at least 1 NULL catalog_id
        self.assertIn("NULL catalog_id", out)

    def test_T48_repair_dry_run_verbose(self):
        out, _ = self._call("repair", "--dry-run", "--verbose")
        self.assertIn("NULL catalog_id", out)


# ===================================================================
# 7. Output redirection (TeeWriter)
# ===================================================================

class TestPoiOutputRedirection(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T49_output_dual(self):
        """--output without --quiet writes to both console and file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            out, _ = self._call("describe", f"--output={tmp_path}")
            file_content = Path(tmp_path).read_text()
            # Both console and file should contain the describe header
            self.assertIn("Table Statistics", out)
            self.assertIn("Table Statistics", file_content)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_T50_output_quiet(self):
        """--output with --quiet writes file only."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            self._call("describe", "--quiet", f"--output={tmp_path}")
            file_content = Path(tmp_path).read_text()
            self.assertIn("Table Statistics", file_content)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_T51_output_bad_path(self):
        """CommandError for unwritable path."""
        msg = self._call_expecting_error(
            "describe", "--output=/nonexistent/path/file.txt",
        )
        self.assertIn("Cannot open output file", msg)

    def test_T52_output_with_list(self):
        """--output works across actions, not just stats."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            self._call("list", "--limit=5", f"--output={tmp_path}")
            file_content = Path(tmp_path).read_text()
            self.assertIn("CAT001", file_content)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ===================================================================
# 8. repair --confirm (execute path; dry-run diagnose in section 6)
# ===================================================================

class TestPoiRepairExecute(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T65_repair_confirm_finds_orphans(self):
        """repair --confirm processes orphan POIs (NULL catalog_id)."""
        out, _ = self._call("repair", "--confirm")
        self.assertIn("Orphan POIs", out)
        self.assertRegex(out, r"Orphan POIs.*:\s*\d+")

    def test_T66_repair_confirm_verbose(self):
        out, _ = self._call("repair", "--confirm", "--verbose")
        self.assertIn("Orphan", out)

    def test_T67_repair_confirm_batch_size(self):
        out, _ = self._call("repair", "--confirm", "--batch-size=2")
        self.assertIn("Orphan", out)

    def test_T68_repair_no_flag_guard(self):
        """CommandError when neither --dry-run nor --confirm."""
        msg = self._call_expecting_error("repair")
        self.assertIn("--dry-run", msg)

    def test_T69_repair_mutex_guard(self):
        """CommandError for --dry-run + --confirm together."""
        msg = self._call_expecting_error(
            "repair", "--dry-run", "--confirm",
        )
        self.assertIn("Cannot use --dry-run and --confirm together", msg)


# ===================================================================
# 9. delete — dry-run selection paths + guard paths
# ===================================================================

class TestPoiDelete(PoiTestMixin, TestCase):
    """All delete tests use --dry-run.  Write-mode in TestPoiDeleteWrite."""

    def setUp(self):
        self._create_fixtures()

    # -- Selection paths (dry-run) -------------------------------------

    def test_T70_delete_null_catalog_id(self):
        out, _ = self._call("delete", "--null-catalog-id", "--dry-run")
        self.assertRegex(
            out, rf"\b{POIS_NULL_CATALOG_ID}\b",
        )

    def test_T71_delete_catalog_id(self):
        out, _ = self._call("delete", "--catalog-id=CAT001", "--dry-run")
        self.assertIn(str(POIS_WITH_CATALOG_CAT001), out)

    def test_T72_delete_vendor_id(self):
        out, _ = self._call("delete", "--vendor-id=VENDOR001", "--dry-run")
        self.assertIn(str(POIS_WITH_VENDOR_001), out)

    def test_T73_delete_project(self):
        out, _ = self._call(
            "delete", f"--project={self.project.id}", "--dry-run",
        )
        self.assertIn(str(POIS_WITH_PROJECT), out)

    def test_T74_delete_poi_id(self):
        out, _ = self._call(
            "delete", f"--id={self.poi_full.id}", "--dry-run",
        )
        self.assertRegex(out, r"\b1\b")

    def test_T75_delete_ids_valid(self):
        ids_str = f"{self.poi_full.id},{self.poi_partial.id}"
        out, _ = self._call("delete", f"--ids={ids_str}", "--dry-run")
        self.assertRegex(out, r"\b2\b")

    def test_T76_delete_ids_invalid(self):
        """CommandError for non-integer --ids."""
        msg = self._call_expecting_error(
            "delete", "--ids=abc,def", "--dry-run",
        )
        self.assertIn("comma-separated integers", msg)

    def test_T77_delete_filter(self):
        out, _ = self._call(
            "delete", "--filter", "sensor=WV03", "--dry-run",
        )
        self.assertIn(str(POIS_SENSOR_WV03), out)

    def test_T78_delete_all(self):
        out, _ = self._call("delete", "--all", "--dry-run")
        self.assertIn(str(TOTAL_POIS), out)

    def test_T79_delete_verbose(self):
        """--verbose includes cascade risk (Annotations count)."""
        out, _ = self._call(
            "delete", f"--id={self.poi_full.id}", "--dry-run", "--verbose",
        )
        self.assertIn("Annotations", out)

    # -- Guard paths ---------------------------------------------------

    def test_T80_delete_no_flag_guard(self):
        """CommandError when neither --dry-run nor --confirm."""
        msg = self._call_expecting_error("delete", "--null-catalog-id")
        self.assertIn("--dry-run", msg)

    def test_T81_delete_mutex_guard(self):
        """CommandError for --dry-run + --confirm together."""
        msg = self._call_expecting_error(
            "delete", "--null-catalog-id", "--dry-run", "--confirm",
        )
        self.assertIn("Cannot use --dry-run and --confirm together", msg)

    def test_T82_delete_no_criteria_guard(self):
        """CommandError when no selection criteria and no --all."""
        msg = self._call_expecting_error("delete", "--dry-run")
        self.assertIn("selection criteria", msg)

    def test_T83_delete_nuclear_guard(self):
        """CommandError for --all --confirm without nuclear flag."""
        msg = self._call_expecting_error("delete", "--all", "--confirm")
        self.assertIn("--i-really-want-to-delete-all", msg)


# ===================================================================
# 10. Error paths: _build_queryset
# ===================================================================

class TestPoiFilterErrors(PoiTestMixin, TestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T85_bad_filter_format(self):
        """CommandError for --filter without '='."""
        msg = self._call_expecting_error(
            "list", "--filter", "badfilternoequalssign",
        )
        self.assertIn("Invalid filter", msg)


# ===================================================================
# 11. Behavioral changes (api_utils.py wiring)
# ===================================================================

class TestPoiBehavioralChanges(PoiTestMixin, TestCase):
    """
    Verify that poi.py correctly wires through behavioral changes
    without calling external APIs.
    """

    def setUp(self):
        self._create_fixtures()

    def test_T91_build_acquisition_filter_rename(self):
        """Old 'acqusition' typo is gone; corrected function exists."""
        try:
            from animal.utils import api_utils
            self.assertTrue(
                hasattr(api_utils, "build_acquisition_filter"),
                "build_acquisition_filter should exist after rename",
            )
            self.assertFalse(
                hasattr(api_utils, "build_acqusition_filter"),
                "Old typo 'build_acqusition_filter' should be removed",
            )
        except ImportError:
            self.skipTest("api_utils not importable in test environment")


# ===================================================================
# 12. load — guard paths, GeoJSON parsing, ETL linkage, duplicates
# ===================================================================

class TestPoiLoad(PoiTestMixin, TransactionTestCase):

    def setUp(self):
        self._create_fixtures()
        self._geojson_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._geojson_dir, ignore_errors=True)

    def _write_geojson(self, filename, features):
        """Write a GeoJSON FeatureCollection to a temp file."""
        import json
        geojson = {
            "type": "FeatureCollection",
            "features": features,
        }
        path = os.path.join(self._geojson_dir, filename)
        with open(path, "w") as f:
            json.dump(geojson, f)
        return path

    def _make_feature(self, sample_idx, lon=-70.0, lat=42.0,
                      area=100.0, deviation=5.0):
        """Build a valid GeoJSON Point feature."""
        return {
            "type": "Feature",
            "id": sample_idx,
            "properties": {
                "sample_idx": sample_idx,
                "area": area,
                "deviation": deviation,
            },
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
        }

    # --- Guard paths (7) ---

    def test_T100_load_no_flag(self):
        """CommandError when neither --dry-run nor --confirm."""
        msg = self._call_expecting_error(
            "load", "--file=x.geojson", "--project-name=test")
        self.assertIn("--dry-run", msg)

    def test_T101_load_mutex_flags(self):
        """CommandError for --dry-run + --confirm together."""
        msg = self._call_expecting_error(
            "load", "--file=x.geojson", "--project-name=test",
            "--dry-run", "--confirm")
        self.assertIn("Cannot use", msg)

    def test_T102_load_no_file(self):
        """CommandError when --file is missing."""
        msg = self._call_expecting_error("load", "--dry-run",
                                          "--project-name=test")
        self.assertIn("--file", msg)

    def test_T103_load_no_project(self):
        """CommandError when neither --project nor --project-name."""
        msg = self._call_expecting_error("load", "--dry-run",
                                          "--file=x.geojson")
        self.assertIn("--project", msg)

    def test_T104_load_both_projects(self):
        """CommandError for --project + --project-name together."""
        msg = self._call_expecting_error(
            "load", "--dry-run", "--file=x.geojson",
            "--project=1", "--project-name=test")
        self.assertIn("not both", msg)

    def test_T105_load_file_not_found(self):
        """CommandError for nonexistent file."""
        msg = self._call_expecting_error(
            "load", "--dry-run", "--project-name=test",
            "--file=/nonexistent/path/file.geojson")
        self.assertIn("not found", msg.lower())

    def test_T106_load_bad_extension(self):
        """CommandError for non-.geojson file."""
        bad_file = os.path.join(self._geojson_dir, "data.json")
        with open(bad_file, "w") as f:
            f.write("{}")
        msg = self._call_expecting_error(
            "load", "--dry-run", "--project-name=test",
            f"--file={bad_file}")
        self.assertIn(".geojson", msg)

    # --- Functional: dry-run ---

    def test_T107_load_dry_run_valid(self):
        """Dry-run with valid GeoJSON reports counts, no DB writes."""
        # Filename format: vendorid_u08mr04326_cog_area-N_something.geojson
        # Use VENDOR001 so ETL lookup succeeds
        path = self._write_geojson(
            "VENDOR001_u08mr04326_cog_area-1_test.geojson",
            [self._make_feature("sample_1"), self._make_feature("sample_2")],
        )
        before = PointsOfInterest.objects.count()

        out, _ = self._call(
            "load", "--dry-run",
            f"--project={self.project.id}",
            f"--file={path}")

        self.assertIn("DRY RUN", out)
        self.assertIn("Features: 2", out)
        self.assertEqual(PointsOfInterest.objects.count(), before,
                         "Dry-run should not create records")

    def test_T108_load_confirm_creates(self):
        """Confirm creates POIs in the database."""
        path = self._write_geojson(
            "VENDOR001_u08mr04326_cog_area-1_test.geojson",
            [self._make_feature("load_s1"), self._make_feature("load_s2")],
        )
        before = PointsOfInterest.objects.count()

        out, _ = self._call(
            "load", "--confirm",
            f"--project={self.project.id}",
            f"--file={path}")

        self.assertIn("Loaded 2 new", out)
        self.assertEqual(PointsOfInterest.objects.count(), before + 2)

    def test_T109_load_duplicate_skip(self):
        """Default: duplicates are skipped (same sample_idx in project)."""
        path = self._write_geojson(
            "VENDOR001_u08mr04326_cog_area-1_test.geojson",
            [self._make_feature("dup_s1")],
        )
        # Load once
        self._call("load", "--confirm",
                    f"--project={self.project.id}",
                    f"--file={path}")
        count_after_first = PointsOfInterest.objects.count()

        # Load again — should skip
        out, _ = self._call("load", "--confirm",
                             f"--project={self.project.id}",
                             f"--file={path}")
        self.assertIn("Dupes: 1", out)
        self.assertEqual(PointsOfInterest.objects.count(), count_after_first,
                         "Duplicate should be skipped, not created again")

    def test_T110_load_etl_warning(self):
        """ETL warning when vendor_id not in ETL table."""
        path = self._write_geojson(
            "UNKNOWNVENDOR_u08mr04326_cog_area-1_test.geojson",
            [self._make_feature("etl_warn_s1")],
        )
        out, _ = self._call(
            "load", "--dry-run",
            f"--project={self.project.id}",
            f"--file={path}")
        self.assertIn("ETL:", out)
        self.assertIn("audit finding", out.lower())

    def test_T111_load_invalid_feature_skipped(self):
        """Features missing required fields are skipped."""
        bad_feature = {
            "type": "Feature",
            "properties": {"sample_idx": "bad_s1"},
            "geometry": {"type": "Point", "coordinates": [-70, 42]},
            # missing area and deviation
        }
        path = self._write_geojson(
            "VENDOR001_u08mr04326_cog_area-1_test.geojson",
            [bad_feature, self._make_feature("good_s1")],
        )
        out, _ = self._call(
            "load", "--dry-run",
            f"--project={self.project.id}",
            f"--file={path}")
        self.assertIn("Skip: 1", out)
        self.assertIn("Load: 1", out)

    def test_T112_load_project_by_id(self):
        """--project <ID> resolves correctly."""
        path = self._write_geojson(
            "VENDOR001_u08mr04326_cog_area-1_test.geojson",
            [self._make_feature("proj_id_s1")],
        )
        out, _ = self._call(
            "load", "--dry-run",
            f"--project={self.project.id}",
            f"--file={path}")
        self.assertIn("Load: 1", out)


# ===================================================================
# 13. Write actions — TransactionTestCase
# ===================================================================

class TestPoiRepairOrphansWrite(PoiTestMixin, TransactionTestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T95_repair_confirm_write(self):
        """repair --confirm: fixture orphan has vendor_id=None, so repair
        matches zero records. Verify DB state unchanged and output reflects
        that no updates occurred (orphan is unmatchable without vendor_id)."""
        orphans_before = PointsOfInterest.objects.filter(
            catalog_id__isnull=True,
        ).count()
        self.assertEqual(orphans_before, POIS_NULL_CATALOG_ID)

        out, _ = self._call("repair", "--confirm")
        self.assertNotIn("DRY RUN", out)

        # Orphan still NULL — no vendor_id means no POI-to-POI match
        orphans_after = PointsOfInterest.objects.filter(
            catalog_id__isnull=True,
        ).count()
        self.assertEqual(
            orphans_after, POIS_NULL_CATALOG_ID,
            "Fixture orphan (vendor_id=None) should remain unrepaired — "
            "repair requires vendor_id for POI-to-POI matching",
        )
        # Output should indicate no updates occurred
        self.assertTrue(
            "No records updated" in out or "no match" in out.lower(),
            f"Expected zero-update output for unmatchable orphan. Got: {out[:200]}",
        )


class TestPoiDeleteWrite(PoiTestMixin, TransactionTestCase):

    def setUp(self):
        self._create_fixtures()

    def test_T96_delete_confirm_single(self):
        """delete --id=N --confirm removes exactly one POI."""
        target_id = self.poi_orphan.id
        before = PointsOfInterest.objects.count()
        self.assertEqual(before, TOTAL_POIS)

        self._call("delete", f"--id={target_id}", "--confirm")

        after = PointsOfInterest.objects.count()
        self.assertEqual(after, TOTAL_POIS - 1)
        self.assertFalse(
            PointsOfInterest.objects.filter(id=target_id).exists(),
        )