# ------------------------------------------------------------------------------
# ----- create_footprints.py ---------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#
# ------------------------------------------------------------------------------

import os
from glob import glob
from functools import partial
from pathlib import Path
from multiprocessing import Pool

from django.core.management.base import BaseCommand

from animal.utils.spatial_ops import create_single_footprint
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)

class Command(BaseCommand):
    help = 'Creates vector footprints for a directory of COGs.'

    def add_arguments(self, parser):
        parser.add_argument("--input_dir",
            type=str,
            default="C:/gis/data/imagery/cogs",
            help="Directory containing the COG files to be processed."
        )
        parser.add_argument("--output-dir",
            type=str,
            default="C:/gis/data/geojson",
            help="Directory to save the fishnet outputs. Defaults to a 'fishnets' subdirectory in the input directory."
        )
        parser.add_argument("--processes", type=int, default=4, help="Number of parallel processes to use.")
        parser.add_argument(
            "--threads",
            type=int,
            default=os.cpu_count(), # Default to all available cores
            help="Number of threads for GDAL to use. Defaults to all available cores."
        )

    def handle(self, *args, **options):
        cogs = glob(os.path.join(options['input_dir'], '*.tif'))
        out_dir = Path(options['output_dir'])
        out_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"Creating footprints for {len(cogs)} files...")
        
        worker_func = partial(
            create_single_footprint, 
            out_dir=str(out_dir), 
            threads=options['threads'] # Pass the threads argument
        )

        with Pool(processes=options['processes']) as pool:
            results = pool.map(worker_func, cogs)

        successes = [r for r in results if r]
        self.stdout.write(self.style.SUCCESS(f"Complete. Created {len(successes)} footprints."))