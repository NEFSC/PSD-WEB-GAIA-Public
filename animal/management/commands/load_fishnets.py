# ------------------------------------------------------------------------------
# ----- load_fishnets.py -------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Load fishnet GeoJSONs into the database.
#
#    tickets:  GAIFAGP-475 (debug block removal)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - Fishnet GeoJSONs are authoritative geometry source
#      - project_id links fishnets to their project context
#      - import_fishnet (db_utils) handles ORM insertion
#
#    usage:    python manage.py load_fishnets --help
#
#    NOTE: Default paths (--input_dir, --dbase) are hardcoded to Windows
#          dev environment. Will need config-based defaults for Docker.
#
# ------------------------------------------------------------------------------

import os
from glob import glob
from pathlib import Path
import geopandas as gpd
from django.db import transaction

from django.core.management.base import BaseCommand

# Import your custom settings for the default project_id
from animal.utils.config import settings
from animal.utils.db_utils import import_fishnet
from animal.models import Fishnet as FN


class Command(BaseCommand):
    help = 'Load fishnet geometries into the database based on project ID.'

    def add_arguments(self, parser):
        parser.add_argument("--input_dir",
            type=str,
            default="C:/gis/data/geojson/fishnets",
            help="Directory for the fishnet inputs. Defaults to a 'fishnets' subdirectory in the input directory."
        )
        parser.add_argument("--dbase",
            type=str,
            default="C:/gis/PSD-WEB-GAIA/db.sqlite3",
            help="Path to the SQLite database file. Defaults to 'C:/gis/PSD-WEB-GAIA/db.sqlite3'."
        )
        parser.add_argument("--project_id",
            type=str,
            default=settings.project_id,
            help="ID of the project to associate with the fishnets."
        )

    def handle(self, *args, **options):
        """
        Load all fishnet GeoJSONs from input directory into the database.

        Reads each .geojson file via GeoPandas, imports features within a
        transaction boundary. Empty files are skipped. Errors on individual
        files do not halt the batch.

        Assumptions:
            - input_dir contains .geojson files (searched recursively)
            - import_fishnet handles geometry insertion via ORM
            - project_id is valid and exists in the database
        """
        input_dir = Path(options['input_dir'])
        dbase = Path(options['dbase'])
        project_id = options['project_id']

        if not input_dir.is_dir():
            self.stderr.write(self.style.ERROR(f"Input directory not found: {input_dir}"))
            return

        if not dbase.is_file():
            self.stderr.write(self.style.ERROR(f"Database file not found: {dbase}"))
            return

        self.stdout.write(self.style.SUCCESS(f"--- Starting Fishnet Loading ---"))

        fishnets = glob(os.path.join(input_dir, '**', '*.geojson'), recursive=True)

        if not fishnets:
            self.stderr.write(self.style.ERROR(f"No fishnet files found in: {input_dir}"))
            return

        self.stdout.write(f"Found {len(fishnets)} fishnet files to process.")

        loaded = 0
        skipped = 0
        errors = 0

        for filepath in fishnets:
            filename = os.path.basename(filepath)
            try:
                gdf = gpd.read_file(filepath)

                if gdf.empty:
                    self.stdout.write(self.style.WARNING(f"  Skipped (empty): {filename}"))
                    skipped += 1
                    continue

                with transaction.atomic():
                    import_fishnet(gdf, FN, project_id=project_id)

                loaded += 1
                self.stdout.write(f"  Loaded: {filename} ({len(gdf)} features)")

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Error: {filename} — {e}"))
                errors += 1

        count = FN.objects.filter(project_id=project_id).count()
        self.stdout.write(f"\nResults: {loaded} loaded, {skipped} skipped, {errors} errors")
        self.stdout.write(f"Total fishnets in DB for project {project_id}: {count}")

        self.stdout.write(self.style.SUCCESS(f"--- Fishnet Loading Complete ---"))