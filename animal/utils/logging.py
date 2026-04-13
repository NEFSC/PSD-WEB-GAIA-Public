# ------------------------------------------------------------------------------
# ----- logging.py -------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  John Wall (john.wall@noaa.gov)
#              
#    purpose:  Standardizes logging across the GAIA application by wrapping
#              Python’s built-in logging module to ensure consistency with the
#              existing 'animal' logger used by the broader development team.
#
#              Provides a convenient factory method for generating child loggers
#              scoped to specific modules or workflows that inherit the 'animal'
#              logger’s configuration, including output format and verbosity.
#
#    usage:    
#        from utils.logging import get_animal_logger
#        logger = get_animal_logger(__name__)
#        logger.info("Pipeline started successfully.")
#
#        Output to stdout:
#        2025-07-18 14:32:51,325 INFO animal.utils.pipelines Pipeline started successfully.
#
# ------------------------------------------------------------------------------


import os
import sys
import logging
from pathlib import Path

def get_animal_logger(name: str = "gaia.pipeline") -> logging.Logger:
    logger = logging.getLogger(name)

    if not getattr(logger, "_configured", False):
        logger.setLevel(logging.DEBUG)

        # Common formatter
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

        # --- Console (Stream) Handler ---
        log_to_stdout = os.getenv("LOG_TO_STDOUT", "1") == "1"
        if log_to_stdout:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setLevel(logging.INFO)  # Change to DEBUG if needed
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)

        # --- File Handler ---
        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gaia.log"

        file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Prevent handler duplication
        logger._configured = True

    return logger

# Optional preload logger
logger = get_animal_logger()