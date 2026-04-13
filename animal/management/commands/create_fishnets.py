# ------------------------------------------------------------------------------
# ----- create_fishnets.py -----------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#
# ------------------------------------------------------------------------------


import os
from glob import glob
from pathlib import Path
from functools import partial
from multiprocessing import Pool

from django.core.management.base import BaseCommand
from django.conf import settings

from animal.utils.spatial_ops import generate_grid_from_footprint
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)

class Command(BaseCommand):
    help = 'Creates fishnet grids for a directory of Cloud Optimized GeoTIFFs (COGs).'

    def add_arguments(self, parser):
        parser.add_argument("--input_dir",
            type=str,
            default="C:/gis/data/geojson",
            help="Directory containing the footprint files to be processed."
        )
        parser.add_argument("--output-dir",
            type=str,
            default="C:/gis/data/geojson/fishnets",
            help="Directory to save the fishnet outputs. Defaults to a 'fishnets' subdirectory in the input directory."
        )
        parser.add_argument("--cell-width", type=float, default=600.0, help="Width of the fishnet cells in meters.")
        parser.add_argument("--cell-height", type=float, default=400.0, help="Height of the fishnet cells in meters.")
        parser.add_argument(
            "--shape",
            choices=['rectangle', 'hex'],
            default='rectangle',
            help="Shape of the fishnet cells."
        )
        parser.add_argument("--processes", type=int, default=4, help="Number of parallel processes to use.")

    def handle(self, *args, **options):
        input_dir = Path(options['input_dir'])
        output_dir = Path(options['output_dir']) if options['output_dir'] else input_dir
        
        # Unpack the arguments for the processing function
        cell_width = options['cell_width']
        cell_height = options['cell_height']
        shape = options['shape']
        num_processes = options['processes']

        if not input_dir.is_dir():
            self.stderr.write(self.style.ERROR(f"Input directory not found: {input_dir}"))
            return

        self.stdout.write(self.style.SUCCESS(f"--- Starting Fishnet Creation ---"))

        footprints = glob(os.path.join(input_dir, '**', '*.geojson'), recursive=True)

        if not footprints:
            self.stderr.write(self.style.ERROR(f"No footprint files found in: {input_dir}"))
            return

        # The call to your adapted function is already correct
        worker_func = partial(
            generate_grid_from_footprint,
            out_dir=options['output_dir'],
            cell_width=options['cell_width'],
            cell_height=options['cell_height'],
            shape=options['shape']
        )

        with Pool(processes=options['processes']) as pool:
            results = pool.map(worker_func, footprints)

        successes = [r for r in results if r]
        failures = [f for f, r in zip(footprints, results) if not r]

        self.stdout.write(self.style.SUCCESS(f"\n--- Fishnet Creation Complete ---"))
        self.stdout.write(f"Successfully created {len(successes)} fishnets.")
        if failures:
            self.stdout.write(self.style.WARNING(f"{len(failures)} files failed to process."))