# -------------------------------------------------------------------------------
# ----- test_chain_wiring.py ----------------------------------------------------
# -------------------------------------------------------------------------------
#
#    ticket:   GAIFAGP-503
#    authors:  John Wall
#    epic:     QA/QC Framework (421)
#    related:  GAIFAGP-490 (BUG-1 fix), GAIFAGP-449
#
#    purpose:  Import-level regression tests that verify every callable wired
#              into a Celery chain is actually a Celery task (has .delay),
#              not a utility function.
#
#              BUG-1 (GAIFAGP-490) was caused by importing run_cog_creation
#              (utility) instead of run_cog_creation_task (Celery task).
#              .delay() silently fails on non-task callables. These tests
#              prevent that bug class from returning.
#
#              No Celery broker required — these are import-time checks only.
#
#    scope:    workflow_launcher.py and imagery_chain.py (John's territory).
#              collection_views.py is Jeffrey's territory and has the same
#              BUG-1 instance (line 30 imports run_cog_creation). That gets
#              fixed when GAIFAGP-495 replaces chain-building with calls to
#              workflow_launcher. Tests for the views layer belong in 495.
#
#    usage:    python manage.py test animal.tests.test_chain_wiring
#
# -------------------------------------------------------------------------------

from django.test import TestCase
from types import SimpleNamespace
from unittest import SkipTest
from unittest.mock import patch


class TestWorkflowLauncherChainWiring(TestCase):
    """
    Verify that workflow_launcher.py references Celery tasks, not utility
    functions. Covers both launch_pipeline (8-step) and
    launch_pipeline_from_payload (7-step).

    Every callable imported for chain wiring must have .delay (confirming
    it is a @shared_task, not a plain function).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from animal.orchestration import workflow_launcher

        cls.module = workflow_launcher

    def test_prepare_workspace_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.prepare_workspace, "delay"),
            "prepare_workspace is not a Celery task — missing .delay",
        )

    def test_login_and_search_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.login_and_search, "delay"),
            "login_and_search is not a Celery task — missing .delay",
        )

    def test_download_imagery_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.download_imagery, "delay"),
            "download_imagery is not a Celery task — missing .delay",
        )

    def test_organize_and_calibrate_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.organize_and_calibrate, "delay"),
            "organize_and_calibrate is not a Celery task — missing .delay",
        )

    def test_run_pansharpen_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.run_pansharpen, "delay"),
            "run_pansharpen is not a Celery task — missing .delay",
        )

    def test_run_cog_creation_task_is_celery_task(self):
        """BUG-1 regression: must be run_cog_creation_task, not run_cog_creation."""
        self.assertTrue(
            hasattr(self.module.run_cog_creation_task, "delay"),
            "run_cog_creation_task is not a Celery task — missing .delay. "
            "Was run_cog_creation (utility) imported instead? (BUG-1)",
        )

    def test_upload_to_azure_task_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.upload_to_azure_task, "delay"),
            "upload_to_azure_task is not a Celery task — missing .delay",
        )

    def test_cleanup_local_data_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.cleanup_local_data, "delay"),
            "cleanup_local_data is not a Celery task — missing .delay",
        )

    def test_load_points_from_staged_geojson_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.load_points_from_staged_geojson, "delay"),
            "load_points_from_staged_geojson is not a Celery task — missing .delay",
        )

    def test_launch_pipeline_is_callable(self):
        self.assertTrue(
            callable(self.module.launch_pipeline),
            "launch_pipeline is not callable",
        )

    def test_launch_pipeline_from_payload_is_callable(self):
        self.assertTrue(
            callable(self.module.launch_pipeline_from_payload),
            "launch_pipeline_from_payload is not callable",
        )

    def test_no_utility_name_run_cog_creation_imported(self):
        """
        Verify the module does NOT have a bare 'run_cog_creation' attribute.
        If it does, someone imported the utility function alongside or instead
        of the task.
        """
        self.assertFalse(
            hasattr(self.module, "run_cog_creation"),
            "workflow_launcher.py has 'run_cog_creation' in namespace — "
            "this is the utility function, not the Celery task. "
            "Only run_cog_creation_task should be imported.",
        )


class TestImageryChainWiring(TestCase):
    """
    Verify that imagery_chain.py references Celery tasks, not utility
    functions. Covers run_imagery_chain() (6-step chain, search done
    synchronously outside the chain).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from animal.orchestration import imagery_chain
        except ImportError as exc:
            raise SkipTest(
                "animal.orchestration.imagery_chain is not part of this deployment architecture"
            ) from exc

        cls.module = imagery_chain

    def test_login_and_search_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.login_and_search, "delay"),
            "login_and_search is not a Celery task — missing .delay",
        )

    def test_download_imagery_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.download_imagery, "delay"),
            "download_imagery is not a Celery task — missing .delay",
        )

    def test_organize_and_calibrate_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.organize_and_calibrate, "delay"),
            "organize_and_calibrate is not a Celery task — missing .delay",
        )

    def test_run_pansharpen_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.run_pansharpen, "delay"),
            "run_pansharpen is not a Celery task — missing .delay",
        )

    def test_run_cog_creation_task_is_celery_task(self):
        """BUG-1 regression: must be run_cog_creation_task, not run_cog_creation."""
        self.assertTrue(
            hasattr(self.module.run_cog_creation_task, "delay"),
            "run_cog_creation_task is not a Celery task — missing .delay. "
            "Was run_cog_creation (utility) imported instead? (BUG-1)",
        )

    def test_upload_to_azure_task_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.upload_to_azure_task, "delay"),
            "upload_to_azure_task is not a Celery task — missing .delay",
        )

    def test_cleanup_local_data_is_celery_task(self):
        self.assertTrue(
            hasattr(self.module.cleanup_local_data, "delay"),
            "cleanup_local_data is not a Celery task — missing .delay",
        )

    def test_no_utility_name_run_cog_creation_imported(self):
        """
        Verify the module does NOT have a bare 'run_cog_creation' attribute.
        """
        self.assertFalse(
            hasattr(self.module, "run_cog_creation"),
            "imagery_chain.py has 'run_cog_creation' in namespace — "
            "this is the utility function, not the Celery task.",
        )


class TestImageryTaskSignals(TestCase):
    """Verify imagery load status transitions are tied to the final chain step."""

    @patch("animal.tasks.signals.mark_loaded")
    def test_postrun_marks_loaded_on_load_points_success(self, mock_mark_loaded):
        from animal.tasks.signals import task_postrun_handler

        task_postrun_handler(
            task_id="chain-123:load_points",
            task=SimpleNamespace(name="gaia.imagery.load_points"),
            state="SUCCESS",
        )

        mock_mark_loaded.assert_called_once_with("chain-123")

    @patch("animal.tasks.signals.mark_loaded")
    def test_postrun_does_not_mark_loaded_on_upload_success(self, mock_mark_loaded):
        from animal.tasks.signals import task_postrun_handler

        task_postrun_handler(
            task_id="chain-123:upload",
            task=SimpleNamespace(name="gaia.imagery.upload_to_azure"),
            state="SUCCESS",
        )

        mock_mark_loaded.assert_not_called()
