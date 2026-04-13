import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from animal.models import Project, StagedImageryGeoJSONUpload, ZoomLevel
from animal.orchestration.workflow_launcher import launch_pipeline_from_payload
from animal.tasks.imagery_tasks import load_points_from_staged_geojson


class _ProjectFixtureMixin:
    def create_project(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="imagery_pipeline_user",
            password="testpass123",
        )
        self.zoom_level = ZoomLevel.objects.create(
            label="Pipeline Test Zoom",
            value=1100,
        )
        self.project = Project.objects.create(
            label="Pipeline Test Project",
            value="pipeline_test_project",
            owner=self.user,
            zoom_level=self.zoom_level,
        )


class TestLoadPointsFromStagedGeoJSONTask(TestCase, _ProjectFixtureMixin):
    def setUp(self):
        self.create_project()

    def _staged_upload(self, consumed=False):
        return StagedImageryGeoJSONUpload.objects.create(
            project=self.project,
            uploaded_by_user=self.user,
            source_filename="points.geojson",
            parsed_vendor_id="23NOV15152207-S1BS-507980222010_02_P001",
            geojson_payload=json.dumps(
                {
                    "type": "FeatureCollection",
                    "name": "23NOV15152207-S1BS-507980222010_02_P001_u08mr32619",
                    "features": [],
                }
            ),
            consumed=consumed,
        )

    def test_noop_when_points_args_missing(self):
        result = load_points_from_staged_geojson.run(
            None,
            points_upload_id=None,
            points_catalog_id=None,
            chain_id="chain-noop",
            project_id=self.project.id,
        )

        self.assertIn("No staged GeoJSON point load requested", result)

    @patch("animal.utils.poi_loader.load_pois_from_geojson_upload")
    def test_success_marks_staged_upload_consumed(self, mock_loader):
        mock_loader.return_value = {
            "loaded": 5,
            "skipped": 1,
            "duplicates": 0,
        }
        staged = self._staged_upload(consumed=False)

        result = load_points_from_staged_geojson.run(
            None,
            points_upload_id=staged.id,
            points_catalog_id="CAT-001",
            chain_id="chain-success",
            project_id=self.project.id,
        )

        staged.refresh_from_db()
        self.assertTrue(staged.consumed)
        self.assertIsNotNone(staged.consumed_at)
        self.assertIn("Loaded 5 POIs", result)

        mock_loader.assert_called_once()
        loader_kwargs = mock_loader.call_args.kwargs
        self.assertEqual(loader_kwargs["project_identifier"], str(self.project.id))
        self.assertEqual(loader_kwargs["id_type"], "catalog")
        self.assertEqual(loader_kwargs["target_id"], "CAT-001")
        self.assertFalse(loader_kwargs["dry_run"])

    @patch("animal.utils.poi_loader.load_pois_from_geojson_upload")
    def test_already_consumed_upload_is_skipped(self, mock_loader):
        staged = self._staged_upload(consumed=True)

        result = load_points_from_staged_geojson.run(
            None,
            points_upload_id=staged.id,
            points_catalog_id="CAT-001",
            chain_id="chain-consumed",
            project_id=self.project.id,
        )

        self.assertIn("already consumed", result)
        mock_loader.assert_not_called()

    def test_missing_staged_upload_raises(self):
        with self.assertRaises(ValueError):
            load_points_from_staged_geojson.run(
                None,
                points_upload_id=999999,
                points_catalog_id="CAT-001",
                chain_id="chain-missing",
                project_id=self.project.id,
            )


class TestWorkflowLauncherLoadPointsOrdering(TestCase):
    class _FakeChain:
        def __init__(self, signatures):
            self.tasks = list(signatures)
            self.apply_kwargs = None

        def apply_async(self, **kwargs):
            self.apply_kwargs = kwargs
            return SimpleNamespace(id="fake-chain-id")

    def test_payload_pipeline_appends_load_points_after_cleanup(self):
        captured = {}

        def fake_chain(*signatures):
            fake = self._FakeChain(signatures)
            captured["chain"] = fake
            return fake

        with patch("animal.orchestration.workflow_launcher.chain", side_effect=fake_chain):
            result = launch_pipeline_from_payload(
                results_payload_json=json.dumps(
                    {
                        "results": json.dumps(
                            {
                                "type": "FeatureCollection",
                                "features": [],
                            }
                        ),
                        "usgs_username": "tester",
                        "token": "fake-token",
                    }
                ),
                img_dir="/tmp/imagery",
                azure_credentials={
                    "account_name": "acc",
                    "account_key": "key",
                    "container_name": "container",
                },
                dem_path="/tmp/dem.tif",
                chain_id="chain-order",
                points_upload_id=321,
                points_catalog_id="CAT-999",
            )

        self.assertEqual(result.id, "fake-chain-id")
        workflow_chain = captured["chain"]
        task_ids = [sig.options.get("task_id") for sig in workflow_chain.tasks]

        self.assertTrue(task_ids[-2].endswith(":cleanup"))
        self.assertTrue(task_ids[-1].endswith(":load_points"))

        task_idx = {task_id.rsplit(":", 1)[1]: idx for idx, task_id in enumerate(task_ids)}
        self.assertLess(task_idx["cog"], task_idx["upload"])
        self.assertLess(task_idx["upload"], task_idx["cleanup"])
        self.assertLess(task_idx["cleanup"], task_idx["load_points"])

        final_sig = workflow_chain.tasks[-1]
        self.assertEqual(final_sig.kwargs.get("points_upload_id"), 321)
        self.assertEqual(final_sig.kwargs.get("points_catalog_id"), "CAT-999")

        self.assertEqual(workflow_chain.apply_kwargs.get("queue"), "imagery")
        self.assertEqual(workflow_chain.apply_kwargs.get("priority"), 5)


class TestCollectionPageGeoJSONPipelineHandoff(TestCase, _ProjectFixtureMixin):
    def setUp(self):
        self.create_project()
        self.client.force_login(self.user)
        self.url = reverse("collection_page", kwargs={"project_id": self.project.id})

    def _search_results_payload(self, catalog_id="CAT-001"):
        return json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "Catalog ID": catalog_id,
                            "MSI_Entity_ID": "MSI-ENTITY-1",
                            "PAN_Entity_ID": "PAN-ENTITY-1",
                            "Entity ID": "MSI-ENTITY-1",
                            "aoi": None,
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [0, 0],
                        },
                    }
                ],
            }
        )

    def _stage_upload(self):
        return StagedImageryGeoJSONUpload.objects.create(
            project=self.project,
            uploaded_by_user=self.user,
            source_filename="id_mode.geojson",
            parsed_vendor_id="23NOV15152207-S1BS-507980222010_02_P001",
            geojson_payload=self._search_results_payload(),
            consumed=False,
        )

    @patch("animal.views.collection_views.launch_imagery_processing_pipeline_with_search_data")
    def test_add_imagery_id_mode_passes_points_args_to_pipeline(self, mock_launch):
        staged = self._stage_upload()

        session = self.client.session
        session["search_results_gdf"] = self._search_results_payload("CAT-001")
        session.save()

        response = self.client.post(
            self.url,
            {
                "select_api": "ee",
                "search_mode": "id",
                "staged_geojson_upload_id": str(staged.id),
                "selected": ["CAT-001"],
            },
        )

        self.assertEqual(response.status_code, 302)
        mock_launch.assert_called_once()

        launch_args, launch_kwargs = mock_launch.call_args
        self.assertEqual(launch_args[0], "MSI-ENTITY-1")
        self.assertEqual(launch_args[1], self.project.id)
        self.assertEqual(launch_kwargs["catalog_ids"], "CAT-001")
        self.assertEqual(launch_kwargs["points_upload_id"], staged.id)
        self.assertEqual(launch_kwargs["points_catalog_id"], "CAT-001")

    @patch("animal.views.collection_views.launch_imagery_processing_pipeline_with_search_data")
    def test_add_imagery_id_mode_requires_staged_upload_id(self, mock_launch):
        session = self.client.session
        session["search_results_gdf"] = self._search_results_payload("CAT-001")
        session.save()

        response = self.client.post(
            self.url,
            {
                "select_api": "ee",
                "search_mode": "id",
                "selected": ["CAT-001"],
            },
        )

        self.assertEqual(response.status_code, 302)
        mock_launch.assert_not_called()

    @patch("animal.views.collection_views.launch_imagery_processing_pipeline_with_search_data")
    def test_add_imagery_id_mode_rejects_multiple_catalog_selection(self, mock_launch):
        staged = self._stage_upload()
        session = self.client.session
        session["search_results_gdf"] = self._search_results_payload("CAT-001")
        session.save()

        response = self.client.post(
            self.url,
            {
                "select_api": "ee",
                "search_mode": "id",
                "staged_geojson_upload_id": str(staged.id),
                "selected": ["CAT-001", "CAT-002"],
            },
        )

        self.assertEqual(response.status_code, 302)
        mock_launch.assert_not_called()
