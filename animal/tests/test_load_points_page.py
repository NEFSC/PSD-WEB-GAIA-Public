import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from animal.models import PointsOfInterest, Project, ZoomLevel


class LoadPointsPageTests(TestCase):
    VALID_VENDOR_ID = "21SEP28215129-505662347010_01_P020"
    OVERRIDE_VENDOR_ID = "25JAN01120000-S1BS-000000000000_01_P001"

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="loadpoints_user",
            password="testpass123",
        )
        self.zoom_level = ZoomLevel.objects.create(
            label="Test Zoom",
            value=1000,
        )
        self.project = Project.objects.create(
            label="Load Points Project",
            value="load_points_project",
            owner=self.user,
            zoom_level=self.zoom_level,
        )
        self.client.force_login(self.user)
        self.url = reverse(
            "load_points_page",
            kwargs={"project_id": self.project.id},
        )

    def _geojson_upload(self, payload, name=None):
        file_name = name or "preview_points.geojson"
        return SimpleUploadedFile(
            file_name,
            json.dumps(payload).encode("utf-8"),
            content_type="application/geo+json",
        )

    def _valid_payload(self):
        return {
            "type": "FeatureCollection",
            "name": f"{self.VALID_VENDOR_ID}_u08mr32605",
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:EPSG::32605"},
            },
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "id": 1,
                        "area": 2.3,
                        "deviation": 81.0,
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [630286.0581619825, 6798666.031672513],
                    },
                }
            ],
        }

    def _preview(self, payload, file_name=None, vendor_id_select="", follow=True):
        uploads = []
        if isinstance(payload, list):
            file_names = file_name if isinstance(file_name, list) else []
            for index, item in enumerate(payload):
                name = file_names[index] if index < len(file_names) else None
                uploads.append(self._geojson_upload(item, name=name))
        else:
            uploads.append(self._geojson_upload(payload, name=file_name))

        return self.client.post(
            self.url,
            {
                "action": "preview_points",
                "geojson_files": uploads,
                "vendor_id_select": vendor_id_select,
            },
            follow=follow,
        )

    def _load(self, follow=True):
        return self.client.post(
            self.url,
            {
                "action": "load_points",
            },
            follow=follow,
        )

    def test_get_renders_load_points_page(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Load Points")
        self.assertContains(response, "Preview Points")
        self.assertContains(response, "Vendor ID (optional override)")
        self.assertNotContains(response, "Vendor or Catalog ID")

    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_preview_then_load_inserts_records(self, _mock_find_cog):
        preview_response = self._preview(self._valid_payload())

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 0)
        self.assertContains(preview_response, "Preview Map")
        self.assertContains(preview_response, self.VALID_VENDOR_ID)

        load_response = self._load()

        self.assertEqual(load_response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 1)

        poi = PointsOfInterest.objects.get(project=self.project)
        self.assertEqual(poi.vendor_id, self.VALID_VENDOR_ID)
        self.assertEqual(poi.sample_idx, "1")
        self.assertEqual(poi.generation_method, "automated")
        self.assertEqual(poi.epsg_code, "32605")
        self.assertEqual(poi.point.srid, 4326)

    @patch("animal.views.load_points_views._find_cog_blob", side_effect=["cogs/fake1.tif", "cogs/fake2.tif"])
    def test_preview_then_load_multiple_geojson_files(self, _mock_find_cog):
        payload_one = self._valid_payload()
        payload_two = self._valid_payload()
        payload_two["name"] = f"{self.VALID_VENDOR_ID}_u08mr32605"
        payload_two["features"][0]["properties"]["id"] = 2

        preview_response = self._preview(
            [payload_one, payload_two],
            file_name=["first.geojson", "second.geojson"],
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "from <strong>2</strong> file(s)")
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 0)

        load_response = self._load()

        self.assertEqual(load_response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 2)

    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_preview_blocks_multi_file_upload_with_mixed_vendors(self, _mock_find_cog):
        payload_one = self._valid_payload()
        payload_two = self._valid_payload()
        payload_two["name"] = f"{self.OVERRIDE_VENDOR_ID}_u08mr32605"
        payload_two["features"][0]["properties"]["id"] = 2

        preview_response = self._preview(
            [payload_one, payload_two],
            file_name=["first.geojson", "second.geojson"],
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "Please upload one vendor at a time")
        self.assertNotContains(preview_response, "Preview Map")

        session_key = f"load_points_preview_{self.project.id}"
        self.assertNotIn(session_key, self.client.session)

    @patch(
        "animal.views.load_points_views._get_available_cog_vendor_ids",
        return_value=["21SEP28215129-505662347010_01_P020"],
    )
    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_preview_auto_selects_matching_vendor_id(self, _mock_find_cog, _mock_vendor_options):
        preview_response = self._preview(self._valid_payload())

        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "Auto-selected vendor ID")

        session_key = f"load_points_preview_{self.project.id}"
        pending = self.client.session.get(session_key)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.get("selected_vendor_id"), self.VALID_VENDOR_ID)
        self.assertTrue(pending.get("auto_selected"))

    @patch(
        "animal.views.load_points_views._get_available_cog_vendor_ids",
        return_value=[
            "21SEP28215129-505662347010_01_P020",
            "25JAN01120000-S1BS-000000000000_01_P001",
        ],
    )
    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_manual_vendor_override_is_used_on_load(self, _mock_find_cog, _mock_vendor_options):
        preview_response = self._preview(
            self._valid_payload(),
            vendor_id_select=self.OVERRIDE_VENDOR_ID,
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "Manual vendor override selected")

        load_response = self._load()

        self.assertEqual(load_response.status_code, 200)
        self.assertContains(load_response, "Manual vendor override is active")
        poi = PointsOfInterest.objects.get(project=self.project)
        self.assertEqual(poi.vendor_id, self.OVERRIDE_VENDOR_ID)

    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_duplicate_sample_idx_is_skipped_on_second_load(self, _mock_find_cog):
        payload = self._valid_payload()

        self._preview(payload, file_name=f"{self.VALID_VENDOR_ID}_u08mr32605_first.geojson")
        self._load()

        self._preview(payload, file_name=f"{self.VALID_VENDOR_ID}_u08mr32605_second.geojson")
        second_load_response = self._load()

        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 1)
        self.assertContains(second_load_response, "No POIs were loaded")

    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_preview_non_point_geometry_shows_zero_preview_points(self, _mock_find_cog):
        payload = self._valid_payload()
        payload["features"][0]["geometry"] = {
            "type": "LineString",
            "coordinates": [
                [630286.0, 6798666.0],
                [630287.0, 6798667.0],
            ],
        }

        response = self._preview(
            payload,
            file_name=f"{self.VALID_VENDOR_ID}_u08mr32605_invalid-geom.geojson",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 0)
        self.assertContains(response, "Previewing")
        self.assertContains(response, "0")

    def test_invalid_geojson_name_is_rejected(self):
        payload = self._valid_payload()
        payload["name"] = "invalid_name"
        response = self._preview(payload, file_name="anything.geojson")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 0)
        self.assertContains(response, "Cannot parse vendor_id from GeoJSON name")

    @patch("animal.views.load_points_views._find_cog_blob", return_value="cogs/fake.tif")
    def test_get_clears_previous_preview_state(self, _mock_find_cog):
        self._preview(self._valid_payload())

        session_key = f"load_points_preview_{self.project.id}"
        self.assertIn(session_key, self.client.session)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Preview Map")
        self.assertNotIn(session_key, self.client.session)

    @patch("animal.views.load_points_views._find_cog_blob", return_value=None)
    def test_no_cog_shows_warning_but_preview_and_load_still_work(self, _mock_find_cog):
        preview_response = self._preview(self._valid_payload())

        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "No COG found")
        self.assertContains(preview_response, "Preview Map")

        load_response = self._load()

        self.assertEqual(load_response.status_code, 200)
        self.assertEqual(PointsOfInterest.objects.filter(project=self.project).count(), 1)
