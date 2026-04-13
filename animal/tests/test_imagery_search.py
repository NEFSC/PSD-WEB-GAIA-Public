"""
Regression tests for search_imagery() and filter_wv3_swir_cavis().

Ticket:     GAIFAGP-558 (catalog_id search filter)
            GAIFAGP-563 (search consolidation, catalog_id-only tests)
Author:     John Wall
Created:    March 2026

Purpose
-------
Two test tiers:

1. **Application logic tests** (no network, no credentials):
   Verify filter_wv3_swir_cavis() SWIR/CAVIS removal using
   synthetic data, and search_imagery() error handling using
   mocked responses. These are the primary regression tests.

2. **Live API smoke tests** (require USGS credentials + network):
   Verify the USGS M2M API is reachable, returns parseable data,
   and search_imagery() processes it without error. These detect
   upstream API changes. If they fail and Tier 1 passes, the
   problem is USGS, not our code.

Tier 2 tests skip gracefully if credentials are missing.

Usage
-----
::

    python manage.py test animal.tests.test_imagery_search -v2
"""

import json
import logging
import unittest
from unittest.mock import MagicMock

import geopandas as gpd
import pandas as pd
import requests
from requests.structures import CaseInsensitiveDict
from django.conf import settings
from django.test import TestCase
from shapely.geometry import box

from animal.utils.api_utils import (
    filter_wv3_swir_cavis,
    search_imagery,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credential check — Tier 2 tests skip if missing
# ---------------------------------------------------------------------------
USGS_USERNAME = getattr(settings, "USGS_USERNAME", None)
USGS_TOKEN = getattr(settings, "USGS_TOKEN", None)
HAS_CREDS = bool(USGS_USERNAME and USGS_TOKEN)

SKIP_REASON = (
    "USGS credentials not configured "
    "(set USGS_USERNAME and USGS_TOKEN in secrets.json)"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CCB_BBOX = (-70.53, 41.75, -70.12, 42.11)
CCB_START = "2021-03-01"
CCB_END = "2021-04-30"
KNOWN_WV02_DATASET = "crssp_orderable_w2"
KNOWN_WV03_DATASET = "crssp_orderable_w3"
# Known catalog_id from the 563 backfill (3,425 POIs, CCB WV02)
KNOWN_WV02_CATALOG_ID = "10300100BBB08100"

EXPECTED_COLUMNS = {
    "Entity ID", "Catalog ID", "Vendor ID",
    "Acquisition Date", "Vendor", "Cloud Cover",
    "Satellite", "Sensor", "Number of Bands",
    "Map Projection", "Datum", "Processing Level",
    "File Format", "License ID", "Sun Azimuth",
    "Sun Elevation", "Pixel Size X", "Pixel Size Y",
    "thumbnail", "bounds",
}


def _get_session():
    from animal.utils.api_utils import ee_login
    session = requests.Session()
    return ee_login(session, USGS_USERNAME, USGS_TOKEN)


def _get_aoi_polygon():
    return box(*CCB_BBOX)


def _make_gdf(catalog_ids):
    """Build a synthetic GeoDataFrame matching search_imagery() output shape."""
    rows = []
    for i, cid in enumerate(catalog_ids):
        rows.append({
            "Catalog ID": cid,
            "Entity ID": f"ENT_{cid[:8]}_{i:02d}",
            "Vendor ID": f"VND_{cid[:8]}_{i:02d}",
            "Acquisition Date": "2021/03/21",
            "Vendor": "MAXAR TECHNOLOGIES",
            "Cloud Cover": "10",
            "Satellite": "WORLDVIEW-2",
            "Sensor": "MS",
            "Number of Bands": "8",
            "Map Projection": "UTM",
            "Datum": "WGS84",
            "Processing Level": "LV1",
            "File Format": "GEOTIFF",
            "License ID": "0",
            "Sun Azimuth": "155.0",
            "Sun Elevation": "42.0",
            "Pixel Size X": "1.85",
            "Pixel Size Y": "1.85",
            "thumbnail": f"https://example.com/{cid}.jpg",
        })
    df = pd.DataFrame(rows)
    df["bounds"] = gpd.GeoSeries(
        [box(-70, 41, -69, 42)] * len(rows),
        crs="EPSG:4326",
    )
    return gpd.GeoDataFrame(df, geometry="bounds")


# ===================================================================
# TIER 1: Application Logic (no network)
# ===================================================================


class TestFilterSwirCavisSynthetic(TestCase):
    """Unit tests for filter_wv3_swir_cavis() using synthetic data."""

    def test_swir_removed(self):
        """SWIR catalog_ids (104A prefix) are removed."""
        gdf = _make_gdf([
            "1040010012345600",
            "104A010012345600",
            "1040010099999900",
        ])
        result = filter_wv3_swir_cavis(gdf)
        remaining = list(result["Catalog ID"])
        self.assertEqual(len(remaining), 2)
        self.assertNotIn("104A010012345600", remaining)

    def test_cavis_removed(self):
        """CAVIS catalog_ids (104C prefix) are removed."""
        gdf = _make_gdf([
            "1040010012345600",
            "104C010012345600",
        ])
        result = filter_wv3_swir_cavis(gdf)
        remaining = list(result["Catalog ID"])
        self.assertEqual(len(remaining), 1)
        self.assertNotIn("104C010012345600", remaining)

    def test_both_swir_and_cavis_removed(self):
        """Both SWIR and CAVIS removed in one pass."""
        gdf = _make_gdf([
            "1040010012345600",
            "104A010012345600",
            "104C010012345600",
            "1030010012345600",
        ])
        result = filter_wv3_swir_cavis(gdf)
        remaining = set(result["Catalog ID"])
        self.assertEqual(remaining, {"1040010012345600", "1030010012345600"})

    def test_no_swir_cavis_unchanged(self):
        """GeoDataFrame with no SWIR/CAVIS passes through unchanged."""
        gdf = _make_gdf([
            "1040010012345600",
            "1030010012345600",
        ])
        result = filter_wv3_swir_cavis(gdf)
        self.assertEqual(len(result), 2)

    def test_all_swir_cavis_returns_empty(self):
        """GeoDataFrame with only SWIR/CAVIS returns empty."""
        gdf = _make_gdf([
            "104A010012345600",
            "104C010012345600",
        ])
        result = filter_wv3_swir_cavis(gdf)
        self.assertEqual(len(result), 0)

    def test_exclude_swir_only(self):
        """exclude_cavis=False keeps CAVIS, removes SWIR."""
        gdf = _make_gdf([
            "1040010012345600",
            "104A010012345600",
            "104C010012345600",
        ])
        result = filter_wv3_swir_cavis(
            gdf, exclude_swir=True, exclude_cavis=False
        )
        remaining = set(result["Catalog ID"])
        self.assertEqual(remaining, {"1040010012345600", "104C010012345600"})

    def test_exclude_cavis_only(self):
        """exclude_swir=False keeps SWIR, removes CAVIS."""
        gdf = _make_gdf([
            "1040010012345600",
            "104A010012345600",
            "104C010012345600",
        ])
        result = filter_wv3_swir_cavis(
            gdf, exclude_swir=False, exclude_cavis=True
        )
        remaining = set(result["Catalog ID"])
        self.assertEqual(remaining, {"1040010012345600", "104A010012345600"})

    def test_fallback_column_name(self):
        """Falls back to lowercase 'catalog_id' when 'Catalog ID' missing."""
        rows = [
            {"catalog_id": "1040010012345600"},
            {"catalog_id": "104A010012345600"},
        ]
        df = pd.DataFrame(rows)
        df["bounds"] = gpd.GeoSeries(
            [box(-70, 41, -69, 42)] * 2, crs="EPSG:4326",
        )
        gdf = gpd.GeoDataFrame(df, geometry="bounds")
        result = filter_wv3_swir_cavis(gdf)
        self.assertEqual(len(result), 1)

    def test_missing_column_raises_value_error(self):
        """ValueError when neither 'Catalog ID' nor 'catalog_id' exists."""
        rows = [{"some_other_field": "value"}]
        df = pd.DataFrame(rows)
        df["bounds"] = gpd.GeoSeries(
            [box(-70, 41, -69, 42)], crs="EPSG:4326",
        )
        gdf = gpd.GeoDataFrame(df, geometry="bounds")
        with self.assertRaises(ValueError):
            filter_wv3_swir_cavis(gdf)


class TestSearchImageryErrorHandling(TestCase):
    """Test search_imagery() error paths using mocked responses.

    Uses spec=requests.Session / spec=requests.Response so
    unexpected attribute access raises AttributeError.
    Session headers use CaseInsensitiveDict to match real behavior.
    """

    def _mock_session(self, status_code, json_body, content_type="application/json"):
        """Build a spec'd mock session whose .post() returns a canned response."""
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_body
        mock_resp.headers = {"content-type": content_type}
        session = MagicMock(spec=requests.Session)
        session.post.return_value = mock_resp
        session.headers = CaseInsensitiveDict({"X-Auth-Token": "fake"})
        return session

    def test_non_200_raises_runtime_error(self):
        """Non-200 status code raises RuntimeError with error code."""
        session = self._mock_session(
            500,
            {"errorCode": "INTERNAL_ERROR", "errorMessage": "Server error"},
        )
        with self.assertRaises(RuntimeError) as ctx:
            search_imagery(
                _get_aoi_polygon(), KNOWN_WV02_DATASET,
                CCB_START, CCB_END, session,
            )
        self.assertIn("INTERNAL_ERROR", str(ctx.exception))

    def test_401_raises_runtime_error(self):
        """401 with UNAUTHORIZED_USER enters the unauthorized branch.

        Asserts on 'Auth token present' — unique to the
        UNAUTHORIZED_USER branch in search_imagery(). Proves the
        branch was entered, not just that a RuntimeError was raised.
        """
        session = self._mock_session(
            401,
            {"errorCode": "UNAUTHORIZED_USER", "errorMessage": "Ignored by code"},
        )
        with self.assertRaises(RuntimeError) as ctx:
            search_imagery(
                _get_aoi_polygon(), KNOWN_WV02_DATASET,
                CCB_START, CCB_END, session,
            )
        self.assertIn("Auth token present", str(ctx.exception))

    def test_200_empty_data_returns_empty_gdf(self):
        """200 with no data object returns empty GeoDataFrame."""
        session = self._mock_session(200, {"data": None})
        gdf = search_imagery(
            _get_aoi_polygon(), KNOWN_WV02_DATASET,
            CCB_START, CCB_END, session,
        )
        self.assertEqual(len(gdf), 0)
        self.assertIn("geometry", gdf.columns)

    def test_200_empty_results_returns_empty_gdf(self):
        """200 with empty results list returns empty GeoDataFrame."""
        session = self._mock_session(
            200, {"data": {"results": []}}
        )
        gdf = search_imagery(
            _get_aoi_polygon(), KNOWN_WV02_DATASET,
            CCB_START, CCB_END, session,
        )
        self.assertEqual(len(gdf), 0)

    def test_no_criteria_raises_value_error(self):
        """search_imagery with no aoi, no dates, no catalog_id raises."""
        session = self._mock_session(200, {})
        with self.assertRaises(ValueError) as ctx:
            search_imagery(
                None, KNOWN_WV02_DATASET,
                None, None, session,
            )
        self.assertIn("requires at least one", str(ctx.exception))

    def test_200_malformed_json_raises(self):
        """200 with unparseable body raises JSONDecodeError."""
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("", "", 0)
        mock_resp.headers = {"content-type": "text/html"}
        session = MagicMock(spec=requests.Session)
        session.post.return_value = mock_resp
        session.headers = CaseInsensitiveDict({"X-Auth-Token": "fake"})
        with self.assertRaises(json.JSONDecodeError):
            search_imagery(
                _get_aoi_polygon(), KNOWN_WV02_DATASET,
                CCB_START, CCB_END, session,
            )


class TestCatalogIdPayloadConstruction(TestCase):
    """Verify build_ee_query_payload conditional filter logic."""

    def test_spatial_and_temporal_produces_three_filters(self):
        """AOI + dates produces spatial, acquisition, and cloud filters."""
        from animal.utils.api_utils import build_ee_query_payload
        payload = build_ee_query_payload(
            CCB_START, CCB_END, _get_aoi_polygon(),
        )
        sf = payload["sceneFilter"]
        self.assertIn("acquisitionFilter", sf)
        self.assertIn("spatialFilter", sf)
        self.assertIn("cloudCoverFilter", sf)

    def test_none_aoi_skips_spatial(self):
        """aoi=None omits spatialFilter."""
        from animal.utils.api_utils import build_ee_query_payload
        payload = build_ee_query_payload(
            CCB_START, CCB_END, None,
        )
        sf = payload["sceneFilter"]
        self.assertNotIn("spatialFilter", sf)
        self.assertIn("acquisitionFilter", sf)
        self.assertIn("cloudCoverFilter", sf)

    def test_none_dates_skips_acquisition(self):
        """start=None, end=None omits acquisitionFilter."""
        from animal.utils.api_utils import build_ee_query_payload
        payload = build_ee_query_payload(
            None, None, _get_aoi_polygon(),
        )
        sf = payload["sceneFilter"]
        self.assertNotIn("acquisitionFilter", sf)
        self.assertIn("spatialFilter", sf)
        self.assertIn("cloudCoverFilter", sf)

    def test_all_none_only_cloud_cover(self):
        """aoi=None, dates=None, no catalog_id → only cloudCoverFilter."""
        from animal.utils.api_utils import build_ee_query_payload
        payload = build_ee_query_payload(
            None, None, None,
        )
        sf = payload["sceneFilter"]
        self.assertEqual(set(sf.keys()), {"cloudCoverFilter"})

    def test_catalog_id_adds_metadata_filter(self):
        """catalog_id + session produces metadataFilter."""
        from unittest.mock import patch
        from animal.utils.api_utils import build_ee_query_payload
        with patch(
            "animal.utils.api_utils.get_catalog_field_id",
            return_value="5e83d14ed20e99b5",
        ):
            payload = build_ee_query_payload(
                None, None, None,
                catalog_id="10300100BBB08100",
                session=MagicMock(),
            )
        mf = payload["sceneFilter"]["metadataFilter"]
        self.assertEqual(mf["filterType"], "value")
        self.assertEqual(mf["value"], "10300100BBB08100")
        self.assertEqual(mf["filterId"], "5e83d14ed20e99b5")

    def test_catalog_id_without_session_raises(self):
        """catalog_id without session raises ValueError."""
        from animal.utils.api_utils import build_ee_query_payload
        with self.assertRaises(ValueError):
            build_ee_query_payload(
                None, None, None,
                catalog_id="10300100BBB08100",
                session=None,
            )

    def test_cloud_cover_always_present(self):
        """cloudCoverFilter is present regardless of other params."""
        from animal.utils.api_utils import build_ee_query_payload
        for args in [
            (CCB_START, CCB_END, _get_aoi_polygon()),
            (None, None, None),
            (CCB_START, CCB_END, None),
            (None, None, _get_aoi_polygon()),
        ]:
            payload = build_ee_query_payload(*args)
            self.assertIn(
                "cloudCoverFilter", payload["sceneFilter"],
                f"Missing cloudCoverFilter for args={args}"
            )


# ===================================================================
# TIER 2: Live API Smoke Tests (require credentials)
# ===================================================================

# Shared session for all Tier 2 tests — one login per test run.
_live_session = None


def setUpModule():
    global _live_session
    if HAS_CREDS:
        try:
            _live_session = _get_session()
        except Exception as e:
            logger.warning(
                "USGS login failed in setUpModule: %s. "
                "Tier 2 tests will skip.", e
            )
            _live_session = None


@unittest.skipUnless(HAS_CREDS, SKIP_REASON)
class TestLiveSearchContract(TestCase):
    """Verify search_imagery() processes live API data correctly.

    These test OUR code's handling of API responses — return type,
    column processing, geometry construction. If USGS changes their
    response format, these fail. That's intentional: it means our
    parsing needs updating.

    If these fail but Tier 1 passes, the problem is upstream.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session = _live_session
        if cls.session is None:
            raise unittest.SkipTest("No live session available")
        try:
            cls.gdf = search_imagery(
                _get_aoi_polygon(),
                KNOWN_WV02_DATASET,
                CCB_START, CCB_END,
                cls.session,
            )
        except RuntimeError as e:
            raise unittest.SkipTest(
                f"USGS API error (upstream). "
                f"Search: dataset={KNOWN_WV02_DATASET}, "
                f"dates={CCB_START}..{CCB_END}, spatial=CCB AOI. "
                f"Error: {e}"
            )
        try:
            cls.gdf_catalog = search_imagery(
                aoi=None, dataset=KNOWN_WV02_DATASET,
                start=None, end=None,
                session=cls.session,
                catalog_id=KNOWN_WV02_CATALOG_ID,
            )
        except RuntimeError as e:
            raise unittest.SkipTest(
                f"USGS API error (upstream). "
                f"Search: dataset={KNOWN_WV02_DATASET}, "
                f"catalog_id={KNOWN_WV02_CATALOG_ID}. "
                f"Error: {e}"
            )

    def test_returns_geodataframe(self):
        self.assertIsInstance(self.gdf, gpd.GeoDataFrame)

    def test_returns_results(self):
        """Known AOI + date range returns data. If this fails,
        either USGS is down or the test data assumptions changed."""
        self.assertGreater(
            len(self.gdf), 0,
            "Expected results for CCB WV02 Mar-Apr 2021. "
            "If USGS is up, test data assumptions may be stale."
        )

    def test_lv1_filter_applied(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        levels = self.gdf["Processing Level"].unique()
        self.assertEqual(list(levels), ["LV1"])

    def test_expected_columns_present(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        for col in EXPECTED_COLUMNS:
            self.assertIn(col, self.gdf.columns, f"Missing: {col}")

    def test_corner_columns_dropped(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        corner_cols = [c for c in self.gdf.columns if "Corner" in c]
        self.assertEqual(corner_cols, [])

    def test_center_columns_dropped(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        self.assertNotIn("Center Latitude", self.gdf.columns)
        self.assertNotIn("Center Longitude", self.gdf.columns)

    def test_bounds_are_valid_polygons(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        for geom in self.gdf["bounds"]:
            self.assertEqual(geom.geom_type, "Polygon")

    def test_crs_is_4326(self):
        if len(self.gdf) == 0:
            self.skipTest("No results")
        self.assertEqual(self.gdf.crs.to_epsg(), 4326)

    def test_required_fields_not_null(self):
        """Fields that map to non-nullable EE model columns are populated."""
        if len(self.gdf) == 0:
            self.skipTest("No results")
        required = [
            "Entity ID", "Catalog ID", "Vendor ID",
            "Satellite", "Sensor", "Vendor", "Datum",
            "File Format", "thumbnail",
        ]
        for col in required:
            for i, val in enumerate(self.gdf[col]):
                self.assertIsNotNone(
                    val, f"Row {i} {col} is None"
                )
                self.assertNotEqual(
                    str(val).strip(), "",
                    f"Row {i} {col} is empty string"
                )

    def test_numeric_fields_parseable(self):
        """Numeric EE model fields are actually numeric."""
        if len(self.gdf) == 0:
            self.skipTest("No results")
        numeric = [
            "Cloud Cover", "Sun Azimuth", "Sun Elevation",
            "Pixel Size X", "Pixel Size Y", "Number of Bands",
        ]
        for col in numeric:
            for i, val in enumerate(self.gdf[col]):
                try:
                    float(val)
                except (ValueError, TypeError):
                    self.fail(
                        f"Row {i} {col}={val!r} is not numeric"
                    )

    # --- catalog_id-only search (GAIFAGP-558) ---

    def test_catalog_id_only_returns_results(self):
        """search_imagery(aoi=None, start=None, end=None, catalog_id=X)
        returns results through the same GeoDataFrame pipeline."""
        self.assertIsInstance(self.gdf_catalog, gpd.GeoDataFrame)
        self.assertGreater(
            len(self.gdf_catalog), 0,
            f"Expected results for catalog_id {KNOWN_WV02_CATALOG_ID}"
        )

    def test_catalog_id_only_same_columns(self):
        """catalog_id-only search returns same columns as spatial search."""
        if len(self.gdf_catalog) == 0:
            self.skipTest("No results")
        for col in EXPECTED_COLUMNS:
            self.assertIn(col, self.gdf_catalog.columns, f"Missing: {col}")

    def test_catalog_id_only_all_match(self):
        """All returned rows have the requested Catalog ID."""
        if len(self.gdf_catalog) == 0:
            self.skipTest("No results")
        for cid in self.gdf_catalog["Catalog ID"]:
            self.assertEqual(str(cid), KNOWN_WV02_CATALOG_ID)

    def test_bogus_catalog_id_returns_empty(self):
        """Nonexistent catalog_id returns empty GeoDataFrame."""
        try:
            gdf = search_imagery(
                aoi=None, dataset=KNOWN_WV02_DATASET,
                start=None, end=None,
                session=self.session,
                catalog_id="0000000000000000",
            )
        except RuntimeError as e:
            self.skipTest(
                f"USGS API error (upstream). "
                f"Search: dataset={KNOWN_WV02_DATASET}, "
                f"catalog_id=0000000000000000. "
                f"Error: {e}")
        self.assertEqual(len(gdf), 0)


@unittest.skipUnless(HAS_CREDS, SKIP_REASON)
class TestNonRegression(TestCase):
    """Verify existing callers are unaffected by 558 changes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session = _live_session
        if cls.session is None:
            raise unittest.SkipTest("No live session available")

    def test_positional_args_still_work(self):
        try:
            gdf = search_imagery(
                _get_aoi_polygon(),
                KNOWN_WV02_DATASET,
                CCB_START,
                CCB_END,
                self.session,
            )
        except RuntimeError as e:
            self.skipTest(
                f"USGS API error (upstream). "
                f"Search: dataset={KNOWN_WV02_DATASET}, "
                f"dates={CCB_START}..{CCB_END}, spatial=CCB AOI. "
                f"Error: {e}")
        self.assertIsInstance(gdf, gpd.GeoDataFrame)

    def test_empty_result_returns_empty_geodataframe(self):
        """Search with future date range returns empty GeoDataFrame."""
        try:
            gdf = search_imagery(
                _get_aoi_polygon(),
                KNOWN_WV02_DATASET,
                "2099-01-01", "2099-01-02",
                self.session,
            )
        except RuntimeError as e:
            self.skipTest(
                f"USGS API error (upstream). "
                f"Search: dataset={KNOWN_WV02_DATASET}, "
                f"dates=2099-01-01..2099-01-02, spatial=CCB AOI. "
                f"Error: {e}")
        self.assertEqual(len(gdf), 0)
        self.assertIn("geometry", gdf.columns)