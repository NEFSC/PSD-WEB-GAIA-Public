# ------------------------------------------------------------------------------
# ----- config.py --------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  John Wall (john.wall@noaa.gov)
#              
#    purpose:  Centralized application configuration using pydantic.BaseSettings
#              with support for local .env files and environment variable overrides.
#
#              This enables a single source of configuration truth for use across
#              manual scripts, Celery tasks, and Django integrations.
#
#    usage:
#        from core.config import settings
#        print(settings.usgs_username)
#
# ------------------------------------------------------------------------------


import os
import json
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
from animal.utils.logging import get_animal_logger

# Setup logger for this module
logger = get_animal_logger("gaia.config")

# Load .env manually and log status
env_path = find_dotenv(usecwd=True)
if load_dotenv(dotenv_path=env_path):
    logger.info(f".env loaded from {env_path}")
else:
    logger.warning(".env file not found. Attempting to load secrets.json...")

    # This path is guaranteed by your Dockerfile symlink logic
    secrets_path = Path("/app/gaia/secrets.json")
    if secrets_path.exists():
        try:
            with open(secrets_path, "r") as f:
                secrets = json.load(f)

            for key, value in secrets.items():
                os.environ.setdefault(key, str(value))

            logger.info("Secrets loaded from /app/gaia/secrets.json")
        except Exception as e:
            logger.error(f"Failed to load secrets.json: {e}")
    else:
        logger.error("Expected secrets.json not found at /app/gaia/secrets.json")

class GaiaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding='utf-8', extra='ignore')

    # Secrets
    usgs_username: str = Field(..., alias="USGS_USERNAME")
    token: str = Field(..., alias="USGS_TOKEN")
    azure_account_name: str = Field(..., alias="AZURE_STORAGE_ACCOUNT_NAME")
    azure_account_key: str = Field(..., alias="AZURE_STORAGE_ACCOUNT_KEY")
    azure_container_name: str = Field(..., alias="AZURE_CONTAINER_NAME")

    # Data directories (local fallback)
    data_dir: Path = Field(default=Path("../../../gis/data/"), env="DATA_DIR")
    aoi_shp: Path | None = None
    dem_file: Path | None = None
    img_dir: Path | None = None
    geojson_dir: Path | None = None

    # Cloud blobs
    aoi_blob_uri: Optional[str] = ''
    dem_blob_uri: Optional[str] = ''

    # Pipeline job values
    imagery_dataset: str = Field(default="crssp_orderable_w3")
    start_date: str = Field(default="2010-06-01")
    end_date: str = Field(default="2012-06-30")
    project_id: int = Field(default="2")

    def model_post_init(self, __context):
        # Resolve local fallback paths
        base = Path(self.data_dir)
        # If a relative path was provided (e.g. ../../../gis/data), make it
        # absolute relative to the project root so runtime workers (web/
        # celery) can reliably locate files inside the mounted /app tree.
        if not base.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            # If the provided path contains leading '..' components (common in
            # development defaults), strip them so the path is resolved inside
            # the project root (e.g. /app/gis/data instead of /gis/data).
            parts = list(base.parts)
            while parts and parts[0] == '..':
                parts.pop(0)
            if parts:
                base = (project_root / Path(*parts)).resolve()
            else:
                base = project_root.resolve()

        self.aoi_shp = self.aoi_shp or (base / "shapefiles" / "UCIPlus.shp")
        self.dem_file = self.dem_file or (base / "rasters" / "output_hh.tif")
        self.img_dir = self.img_dir or (base / "imagery" )
        self.geojson_dir = self.geojson_dir or (base / "geojson")

        # Log resolution
        logger.info(f"AOI Path: {self.aoi_blob_uri or self.aoi_shp}")
        logger.info(f"DEM Path: {self.dem_blob_uri or self.dem_file}")
        logger.info(f"Imagery Path: {self.img_dir}")
        logger.info(f"GeoJSON Path: {self.geojson_dir}")

# Instantiate the settings object
settings = GaiaSettings()