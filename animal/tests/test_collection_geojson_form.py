import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from animal.forms import APIQueryForm


class APIQueryFormGeoJSONModeTests(TestCase):
    def _geojson_upload(self, name="id_mode.geojson"):
        payload = {
            "type": "FeatureCollection",
            "name": "23NOV15152207-S1BS-507980222010_02_P001_u08mr32619",
            "features": [],
        }
        return SimpleUploadedFile(
            name,
            json.dumps(payload).encode("utf-8"),
            content_type="application/geo+json",
        )

    def test_id_mode_requires_geojson_upload(self):
        form = APIQueryForm(
            data={
                "api": "ee",
                "search_mode": "id",
                "sensor": ["worldview_3"],
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("id_geojson_file", form.errors)

    def test_id_mode_accepts_geojson_upload(self):
        form = APIQueryForm(
            data={
                "api": "ee",
                "search_mode": "id",
                "sensor": ["worldview_3"],
            },
            files={"id_geojson_file": self._geojson_upload()},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data.get("id_tokens"), [])

    def test_id_mode_rejects_non_geojson_extension(self):
        form = APIQueryForm(
            data={
                "api": "ee",
                "search_mode": "id",
                "sensor": ["worldview_3"],
            },
            files={"id_geojson_file": self._geojson_upload(name="bad.txt")},
        )

        self.assertFalse(form.is_valid())
        self.assertIn("id_geojson_file", form.errors)
