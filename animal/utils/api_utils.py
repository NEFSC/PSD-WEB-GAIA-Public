"""
USGS EarthExplorer API utilities for GAIA.

Provides functions for USGS M2M API interaction: imagery
search, download orchestration, format conversion, metadata
field discovery, and GeoDataFrame transformation for EE,
GEGD, and MGP data sources.
"""
# -----------------------------------------------------------------------
# ----- api_utils.py ----------------------------------------------------
# -----------------------------------------------------------------------
#
#    author:  John Wall (john.wall@noaa.gov)
#
#    purpose: Utility functions for interacting with the United States
#             Geological Survey (USGS) EarthExplorer (EE) API and
#             related services, including downloading, unzipping,
#             standardizing, and transforming imagery data into
#             formats and schema compatible with the GAIA application.
#
#    tickets: GAIFAGP-484 (USGS metadata field discovery for
#             poi_backfill entity_id resolution)
#             GAIFAGP-479 (SWIR/CAVIS filter consolidation
#             from imagery.py — canonical location)
#             GAIFAGP-558 (catalog_id search filter)
#             GAIFAGP-563 (search consolidation — deleted
#             search_by_catalog_id, conditional payload)
#
#    notes:   Contains logic for preparing API payloads, retrieving
#             imagery data, converting formats (e.g., NTF to GeoTIFF),
#             transforming imagery metadata from EE, GEGD, and MGP
#             into GeoDataFrames with GAIA-standard schemas, and
#             discovering USGS metadata field IDs at runtime.
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - USGS M2M API is authoritative for scene metadata and
#        field definitions per dataset.
#      - ETL table is authoritative for catalog_id -> entity_id
#        mapping; USGS API is fallback.
#      - EarthExplorer dataset names (crssp_orderable_w2, _w3)
#        are the canonical identifiers for USGS scene-search.
#
#    references:
#      - https://m2m.cr.usgs.gov/api/docs/example/download_data-py
#      - https://m2m.cr.usgs.gov/api/docs/json (dataset-filters)
#      - https://github.com/yannforget/landsatxplore
#
#    ACCEPTED-RISK REMEDIATIONS:
#      - GAIFAGP-486: Eliminate None-return failure
#        paths in download pipeline functions
#        (download_imagery, get_product_id,
#        convert_ntf_to_tif). Replace with structured
#        error returns or typed exceptions.
#      - GAIFAGP-487: Decompose poi.py into thin-wrapper
#        command + poi_ops utility module per Section 3.8.
#
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# Import libraries, configure environment
# -----------------------------------------------------------------------
import os
import re
import time
import zipfile
from glob import glob
from pprint import pformat
from typing import Any, Iterable, Optional

import geopandas as gpd
import pandas as pd
import requests
from osgeo import gdal
from shapely.geometry import Polygon, box, shape

from animal.utils.config import settings
from animal.utils.logging import get_animal_logger
from animal.utils.utils import retry

logger = get_animal_logger(__name__)

# -----------------------------------------------------------------------
# Network timeout constants
# -----------------------------------------------------------------------
# All outbound requests MUST use these. A missing timeout
# means a hung socket can block the pipeline indefinitely.
# IV&V finding Feb 2026.
USGS_API_TIMEOUT = 30           # seconds, API calls
DOWNLOAD_CONNECT_TIMEOUT = 10   # seconds, connect phase
DOWNLOAD_READ_TIMEOUT = 300     # seconds, streaming read
DOWNLOAD_TIMEOUT = (             # tuple for requests lib
    DOWNLOAD_CONNECT_TIMEOUT,
    DOWNLOAD_READ_TIMEOUT,
)
MAX_POLL_ELAPSED = 600          # seconds (10 min) hard cap


# -----------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------
class TemporaryDataUnavailableError(Exception):
    """Raised when USGS data is staging / not yet available."""

    pass


# -----------------------------------------------------------------------
# Imagery Search
# -----------------------------------------------------------------------
def search_imagery(
    aoi: Optional[Polygon],
    dataset: str,
    start: Optional[str],
    end: Optional[str],
    session: requests.Session,
    catalog_id: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """
    Search for Maxar imagery in EarthExplorer.

    Supports three search modes:
      - Spatial+temporal: aoi + start + end (existing behavior)
      - Catalog ID only: catalog_id with aoi/start/end as None
      - Combined: all parameters provided (narrowest search)

    At least one of (aoi + start + end) or catalog_id must
    be provided. Passing only session with no search criteria
    raises ValueError.

    Assumptions:
        Session is pre-authenticated via ee_login() with a
        valid X-Auth-Token header. USGS scene-search
        endpoint returns JSON with 'data.results' list.
        Only LV1 processing level imagery is retained.

    Args:
        aoi: Area of interest (Polygon), or None.
        dataset: Dataset name (e.g. "crssp_orderable_w3").
        start: Start date (YYYY-MM-DD), or None.
        end: End date (YYYY-MM-DD), or None.
        session: Authenticated requests.Session.
        catalog_id: Optional catalog_id to filter by.
            Uses metadataFilter via get_catalog_field_id().
            GAIFAGP-558.

    Returns:
        GeoDataFrame of imagery search results.

    Raises:
        ValueError: If no search criteria provided (no aoi,
            no dates, no catalog_id).
        RuntimeError: On non-200 response or unauthorized
            access to the requested dataset.
    """
    has_spatial = aoi is not None
    has_temporal = start is not None and end is not None
    has_catalog = catalog_id is not None
    if not has_spatial and not has_temporal and not has_catalog:
        raise ValueError(
            "search_imagery() requires at least one of: "
            "aoi + start + end (spatial/temporal search), "
            "or catalog_id (catalog search). "
            "Cannot search with no criteria."
        )
    logger.info(
        "Initiating imagery search: dataset=%s, catalog_id=%s",
        dataset, catalog_id,
    )

    payload_dict = build_ee_query_payload(
        start, end, aoi, imagery_dataset=dataset,
        catalog_id=catalog_id, session=session,
    )
    logger.debug(
        "Sending search payload to USGS API:\n%s",
        pformat(payload_dict),
    )

    url = (
        "https://m2m.cr.usgs.gov"
        "/api/api/json/stable/scene-search"
    )
    response = session.post(
        url, json=payload_dict, timeout=USGS_API_TIMEOUT
    )

    if response.status_code != 200:
        ct = response.headers.get("content-type", "")
        response_data = (
            response.json()
            if "application/json" in ct
            else {}
        )
        error_code = response_data.get(
            "errorCode", "UNKNOWN_ERROR"
        )
        error_msg = response_data.get(
            "errorMessage", "No error message provided"
        )

        logger.error(
            "Search failed: HTTP %d, %s — %s (dataset=%s, catalog_id=%s)",
            response.status_code, error_code, error_msg,
            dataset, catalog_id,
        )

        has_token = bool(
            session.headers.get("X-Auth-Token")
        )
        if error_code == "UNAUTHORIZED_USER":
            raise RuntimeError(
                f"Not authorized for dataset "
                f"'{dataset}'. Auth token present: "
                f"{has_token}. May require special "
                f"permissions."
            )
        raise RuntimeError(
            f"Imagery search failed: "
            f"{error_code} - {error_msg}"
        )

    response_json = response.json()
    data = response_json.get("data")

    if not data:
        logger.warning(
            "Search returned no data object. "
            "Assuming zero results."
        )
        records = []
    else:
        records = data.get("results", [])

    logger.info(
        "Search results returned: %d scenes",
        len(records),
    )

    if not records:
        return gpd.GeoDataFrame([], columns=["geometry"])

    columns = [
        f["fieldName"] for f in records[0]["metadata"]
    ]

    df = pd.DataFrame(
        [
            {
                f["fieldName"]: f["value"]
                for f in r["metadata"]
            }
            for r in records
        ]
    )
    df["thumbnail"] = [
        r["browse"][0]["thumbnailPath"] for r in records
    ]
    df["bounds"] = gpd.GeoSeries(
        [
            Polygon(
                r["spatialBounds"]["coordinates"][0]
            )
            for r in records
        ],
        crs="EPSG:4326",
    )

    gdf = gpd.GeoDataFrame(df, geometry="bounds")
    gdf = gdf[gdf["Processing Level"] == "LV1"]

    drop_cols = [
        c for c in columns if "Corner" in c
    ] + ["Center Latitude", "Center Longitude"]
    gdf.drop(columns=drop_cols, inplace=True)

    logger.info(
        "Filtered to LV1 images",
        extra={"lv1_count": gdf.shape[0]},
    )
    return gdf


def _scene_records_to_gdf(
    scene_records: list[dict],
) -> gpd.GeoDataFrame:
    """Convert USGS scene-search records into the standard imagery GeoDataFrame."""
    if not scene_records:
        return gpd.GeoDataFrame([], columns=["geometry"])

    rows = []
    seen_keys = set()

    for record in scene_records:
        metadata_values = {
            field.get("fieldName"): field.get("value")
            for field in record.get("metadata", [])
            if field.get("fieldName")
        }

        dedupe_key = str(
            metadata_values.get("Entity ID")
            or metadata_values.get("Vendor ID")
            or metadata_values.get("Catalog ID")
            or record.get("entityId")
            or len(rows)
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        row = dict(metadata_values)
        browse = record.get("browse") or []
        row["thumbnail"] = (
            browse[0].get("thumbnailPath")
            if browse and isinstance(browse[0], dict)
            else None
        )

        bounds = None
        coords = (
            record.get("spatialBounds", {})
            .get("coordinates", [])
        )
        if coords:
            try:
                bounds = Polygon(coords[0])
            except Exception:
                bounds = None
        row["bounds"] = bounds

        rows.append(row)

    if not rows:
        return gpd.GeoDataFrame([], columns=["geometry"])

    df = pd.DataFrame(rows)
    if "bounds" not in df.columns:
        df["bounds"] = None

    gdf = gpd.GeoDataFrame(df, geometry="bounds", crs="EPSG:4326")

    if "Processing Level" in gdf.columns:
        gdf = gdf[gdf["Processing Level"] == "LV1"]

    drop_cols = [
        c for c in gdf.columns if "Corner" in c
    ]
    for col in ["Center Latitude", "Center Longitude"]:
        if col in gdf.columns:
            drop_cols.append(col)
    if drop_cols:
        gdf.drop(columns=drop_cols, inplace=True, errors="ignore")

    return gdf.reset_index(drop=True)


def _build_identifier_candidates(token: str) -> list[str]:
    """Build progressively looser metadata token candidates for scene-search."""
    raw = str(token).strip()
    if not raw:
        return []

    candidates = [raw]

    # Stereo products frequently need PAN/MSI variants to match USGS metadata.
    if "-S1BS-" in raw:
        candidates.append(raw.replace("-S1BS-", "-P1BS-"))
        candidates.append(raw.replace("-S1BS-", "-M1BS-"))

    # Try progressively shorter underscore-delimited prefixes.
    # Example: AAA_BBB_CCC -> AAA_BBB, AAA
    if "_" in raw:
        parts = raw.split("_")
        for end in range(len(parts) - 1, 0, -1):
            candidates.append("_".join(parts[:end]))

    # Case variants can help when metadata values differ by API version/source.
    candidates.append(raw.upper())

    # De-duplicate while preserving order.
    return list(dict.fromkeys(candidates))


def _scene_search_records_by_metadata(
    session: requests.Session,
    *,
    dataset: str,
    start: Optional[str],
    end: Optional[str],
    filter_id: str,
    value: str,
    max_results: int,
    include_acquisition_filter: bool,
) -> list[dict]:
    """Execute a USGS scene-search metadata query and return result records."""
    scene_filter = {
        "metadataFilter": {
            "filterType": "value",
            "filterId": filter_id,
            "value": value,
        }
    }
    if include_acquisition_filter and start and end:
        scene_filter["acquisitionFilter"] = build_acquisition_filter(
            start, end
        )

    payload = {
        "datasetName": dataset,
        "sceneFilter": scene_filter,
        "maxResults": max_results,
        "metadataType": "full",
    }
    url = "https://m2m.cr.usgs.gov/api/api/json/stable/scene-search"
    response = session.post(
        url,
        json=payload,
        timeout=USGS_API_TIMEOUT,
    )

    if response.status_code != 200:
        response_data = {}
        if "application/json" in response.headers.get("content-type", ""):
            response_data = response.json()
        raise RuntimeError(
            "Imagery search failed for token "
            f"'{value}' with filterId '{filter_id}': "
            f"{response_data.get('errorCode', response.status_code)} - "
            f"{response_data.get('errorMessage', 'No error message provided')}"
        )

    response_json = response.json()
    response_data = response_json.get("data") or {}
    return response_data.get("results", [])


def search_imagery_by_identifiers(
    identifier_values: Iterable[str],
    dataset: str,
    start: Optional[str],
    end: Optional[str],
    session: requests.Session,
    max_results: int = 500,
) -> gpd.GeoDataFrame:
    """Search EarthExplorer by vendor/catalog identifiers plus date range."""
    tokens = []
    seen_tokens = set()
    for raw_value in identifier_values:
        token = str(raw_value).strip()
        if not token:
            continue
        token_key = token.lower()
        if token_key in seen_tokens:
            continue
        seen_tokens.add(token_key)
        tokens.append(token)

    if not tokens:
        return gpd.GeoDataFrame([], columns=["geometry"])

    catalog_field_id = get_catalog_field_id(dataset, session)
    try:
        vendor_field_id = get_vendor_field_id(dataset, session)
    except ValueError as exc:
        vendor_field_id = None
        logger.warning(
            "Vendor metadata field not found; limiting ID search to catalog matches",
            extra={"dataset": dataset, "error": str(exc)},
        )

    all_records = []
    has_date_window = bool(start and end)

    for token in tokens:
        token_candidates = _build_identifier_candidates(token)
        filter_ids = [catalog_field_id]
        if vendor_field_id:
            filter_ids.append(vendor_field_id)

        for filter_id in filter_ids:
            matched_records = []

            # Pass 1: strict match using acquisition date filter.
            if has_date_window:
                for candidate in token_candidates:
                    records = _scene_search_records_by_metadata(
                        session,
                        dataset=dataset,
                        start=start,
                        end=end,
                        filter_id=filter_id,
                        value=candidate,
                        max_results=max_results,
                        include_acquisition_filter=True,
                    )
                    if records:
                        matched_records.extend(records)

            # Pass 2: if strict date-filtered match failed, retry without date filter.
            if not matched_records:
                logger.info(
                    "No strict ID matches found, retrying without acquisition filter",
                    extra={
                        "dataset": dataset,
                        "token": token,
                        "filter_id": filter_id,
                    },
                )
                for candidate in token_candidates:
                    records = _scene_search_records_by_metadata(
                        session,
                        dataset=dataset,
                        start=start,
                        end=end,
                        filter_id=filter_id,
                        value=candidate,
                        max_results=max_results,
                        include_acquisition_filter=False,
                    )
                    if records:
                        matched_records.extend(records)

            all_records.extend(matched_records)

    gdf = _scene_records_to_gdf(all_records)
    logger.info(
        "Identifier imagery search complete",
        extra={
            "dataset": dataset,
            "identifier_count": len(tokens),
            "result_count": len(gdf),
        },
    )
    return gdf


def filter_wv3_swir_cavis(
    gdf: gpd.GeoDataFrame,
    exclude_swir: bool = True,
    exclude_cavis: bool = True,
    catalog_id_column: str = "Catalog ID",
):
    """
    Filter WorldView-3 SWIR and/or CAVIS catalog IDs
    from search results.

    SWIR catalog IDs begin with '104A'.
    CAVIS catalog IDs begin with '104C'.

    Operates on GeoDataFrames returned by search_imagery()
    or gdf_from_ee(). Accepts both 'Catalog ID' (raw EE
    column name) and 'catalog_id' (normalized column name).

    Args:
        gdf: GeoDataFrame containing imagery search results.
        exclude_swir: If True, exclude SWIR catalog IDs.
        exclude_cavis: If True, exclude CAVIS catalog IDs.
        catalog_id_column: Column name containing catalog IDs.

    Returns:
        Filtered GeoDataFrame with excluded records removed.

    Raises:
        ValueError: If catalog_id_column not found.
    """
    if catalog_id_column not in gdf.columns:
        alt_column = "catalog_id"
        if alt_column in gdf.columns:
            catalog_id_column = alt_column
        else:
            raise ValueError(
                f"Column '{catalog_id_column}' not found "
                f"in GeoDataFrame. Available columns: "
                f"{list(gdf.columns)}"
            )

    initial_count = len(gdf)
    mask = pd.Series([True] * len(gdf), index=gdf.index)

    if exclude_swir:
        swir_mask = gdf[catalog_id_column].str.startswith(
            "104A", na=False
        )
        swir_count = swir_mask.sum()
        mask &= ~swir_mask
        if swir_count > 0:
            logger.info(
                "Excluded SWIR catalog IDs",
                extra={
                    "prefix": "104A",
                    "count": int(swir_count),
                },
            )

    if exclude_cavis:
        cavis_mask = gdf[catalog_id_column].str.startswith(
            "104C", na=False
        )
        cavis_count = cavis_mask.sum()
        mask &= ~cavis_mask
        if cavis_count > 0:
            logger.info(
                "Excluded CAVIS catalog IDs",
                extra={
                    "prefix": "104C",
                    "count": int(cavis_count),
                },
            )

    filtered_gdf = gdf[mask].copy()
    final_count = len(filtered_gdf)

    logger.info(
        "SWIR/CAVIS filtering complete",
        extra={
            "retained": final_count,
            "removed": initial_count - final_count,
            "initial": initial_count,
        },
    )
    return filtered_gdf


# -----------------------------------------------------------------------
# File Conversion & Naming
# -----------------------------------------------------------------------
def convert_ntf_to_tif(ntf: str) -> Optional[str]:
    """
    Convert NTF to GeoTIFF via GDAL.

    Assumptions:
        Input path ends with 'NTF' (case-sensitive
        replacement to 'TIF'). GDAL is available and
        can read the NTF format. Original NTF is deleted
        after successful conversion.

    Args:
        ntf: Path to .NTF file.

    Returns:
        Path to converted .TIF, or None on failure.
        NOTE: None-return is accepted tech debt
        (GAIFAGP-486). Should raise on failure.
    """
    try:
        outfile = ntf.replace("NTF", "TIF")
        ntf_data = gdal.Open(ntf)
        gdal.Translate(outfile, ntf_data, format="GTiff")
        del ntf_data
        logger.info(
            "Converted NTF to GeoTIFF",
            extra={"outfile": outfile},
        )
        os.remove(ntf)
        return outfile
    except Exception as e:
        logger.error(
            "NTF conversion failed",
            extra={"ntf": ntf, "error": str(e)},
        )
        return None


def standardize_names(
    imgdir: str,
) -> Optional[str | list[str]]:
    """
    Standardize imagery filenames in a directory.

    Finds GeoTIFF or NTF files, converts NTF if needed,
    and normalizes the 6-part hyphenated naming convention.

    Assumptions:
        Directory contains at most one GeoTIFF or NTF
        file matching the glob pattern. Filenames with
        6 hyphen-separated segments are non-standard and
        will be renamed. NTF files require GDAL.

    Args:
        imgdir: Path to image directory.

    Returns:
        Path string if already standardized, list of
        directory contents after rename, or None if NTF
        conversion fails.
    """
    glob_lo = imgdir + "/**/*.tif"
    glob_hi = imgdir + "/**/*.TIF"
    logger.info("Trying standardize name glob...")

    geotiff = (
        glob(glob_lo, recursive=True)
        + glob(glob_hi, recursive=True)
    )
    logger.info(
        "GeoTIFF results",
        extra={"count": len(geotiff)},
    )

    if not geotiff:
        glob_lo = imgdir + "/**/*.ntf"
        glob_hi = imgdir + "/**/*.NTF"
        geotiff = (
            glob(glob_lo, recursive=True)
            + glob(glob_hi, recursive=True)
        )
        geotiff = geotiff[0]
        logger.info(
            "NTF results", extra={"path": geotiff}
        )
        if len(geotiff) > 0:
            logger.info(
                "NTF files found. Converting to GeoTIFF"
            )
            geotiff = convert_ntf_to_tif(geotiff)
    else:
        geotiff = geotiff[0]

    logger.info(
        "Working geotiff", extra={"path": geotiff}
    )
    split_name = geotiff.split("-")
    if len(split_name) == 6:
        logger.info("Standardizing file name")
        new_name = "-".join(split_name[:-1]) + ".tif"
        os.rename(geotiff, new_name)
    else:
        logger.info("File name already standardized.")
        return geotiff
    return glob(imgdir, recursive=True)


# -----------------------------------------------------------------------
# Download Pipeline
# -----------------------------------------------------------------------
def unzip_download(zippedfile: str) -> str:
    """
    Unzip downloaded data from EarthExplorer.

    Assumptions:
        Input is a valid zip file from USGS download
        pipeline. Directory name derived from filename
        (minus extension). Original zip deleted after
        extraction.

    Args:
        zippedfile: Path to locally stored zip file.

    Returns:
        Absolute path to extracted directory.
    """
    root = "/".join(zippedfile.split("/")[:-1])
    dirname = zippedfile.split("/")[-1].split(".")[0]
    outdir = root + "/" + dirname
    with zipfile.ZipFile(zippedfile, "r") as z_obj:
        z_obj.extractall(outdir)
    logger.info("Unzipping complete")
    os.remove(zippedfile)
    return os.path.abspath(outdir)


def download_zip(
    session: requests.Session,
    url: str,
    out_dir: str,
) -> str:
    """
    Download zipped data from EarthExplorer.

    Assumptions:
        URL is a valid USGS download link with a
        content-disposition header containing filename.
        Output directory is created if it does not exist.

    Args:
        session: Authenticated requests.Session.
        url: EarthExplorer-supplied download URL.
        out_dir: Local output directory.

    Returns:
        Path to downloaded zip file.
    """
    response = session.get(
        url, stream=True, timeout=DOWNLOAD_TIMEOUT
    )
    headers = response.headers["content-disposition"]
    filename = re.findall(
        "filename=(.+)", headers
    )[0].replace('"', "")

    logger.info(
        "Downloading file",
        extra={
            "download_filename": filename,
            "out_dir": os.path.abspath(out_dir),
        },
    )
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    outname = os.path.join(out_dir, filename)
    with open(outname, "wb") as dst:
        for chunk in response.iter_content(
            chunk_size=8192
        ):
            dst.write(chunk)
    return outname


def retrieve_download(
    session: requests.Session,
    label: str,
) -> list[str]:
    """
    Retrieve download URLs for staged datasets.

    Assumptions:
        Label matches a previously submitted
        download-request. USGS staging may take minutes;
        caller is responsible for polling.

    Args:
        session: Authenticated requests.Session.
        label: Dataset label from download-request.

    Returns:
        List of download IDs.
    """
    url = (
        "https://m2m.cr.usgs.gov"
        "/api/api/json/stable/download-retrieve"
    )
    response = session.post(
        url=url,
        json={"label": label},
        timeout=USGS_API_TIMEOUT,
    )
    ra = response.json()["data"]["available"]
    logger.info("Data retrieved from staging.")
    return [ds["downloadId"] for ds in ra]


def request_download(
    session: requests.Session,
    entity_id: str,
    product_id: str,
) -> tuple[dict, str]:
    """
    Send initial download request to USGS API.

    Assumptions:
        entity_id and product_id are valid USGS
        identifiers. Response contains 'data' with
        'availableDownloads' and/or 'preparingDownloads'.

    Args:
        session: Authenticated requests.Session.
        entity_id: USGS entity identifier.
        product_id: USGS product identifier.

    Returns:
        Tuple of (API response dict, label string).
    """
    label = f"{entity_id}_{product_id}"
    payload = {
        "downloads": [
            {
                "entityId": entity_id,
                "productId": product_id,
            }
        ],
        "label": label,
    }
    url = (
        "https://m2m.cr.usgs.gov"
        "/api/api/json/stable/download-request"
    )
    response_data = session.post(
        url=url,
        json=payload,
        timeout=USGS_API_TIMEOUT,
    ).json()
    return response_data, label


def get_product_id(
    session: requests.Session,
    dataset_name: str,
    entity_id: str,
) -> Optional[str]:
    """
    Get product ID via download-options API endpoint.

    Assumptions:
        entity_id is a valid USGS entity identifier for
        the given dataset. API returns a list of download
        options; we take the first. Session is
        pre-authenticated via ee_login().

    Args:
        session: Authenticated requests.Session.
        dataset_name: USGS dataset name.
        entity_id: USGS entity identifier.

    Returns:
        Product ID string, or None on failure.
        NOTE: None-return is accepted tech debt
        (GAIFAGP-486). Should raise on failure.
    """
    data = {
        "datasetName": dataset_name,
        "entityIds": [entity_id],
    }
    url = (
        "https://m2m.cr.usgs.gov"
        "/api/api/json/stable/download-options"
    )
    response = session.post(
        url=url,
        json=data,
        timeout=USGS_API_TIMEOUT,
    )
    logger.info(
        "Download-options response",
        extra={"status_code": response.status_code},
    )

    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        response_body = response.text[:500]
        logger.error(
            "download-options request failed",
            extra={
                "status_code": response.status_code,
                "dataset": dataset_name,
                "entity_id": entity_id,
                "content_type": content_type,
                "response_body": response_body,
            },
        )
        if response.status_code == 403:
            logger.error(
                "USGS denied access to download options. "
                "This usually indicates missing dataset download permissions.",
                extra={
                    "dataset": dataset_name,
                    "entity_id": entity_id,
                },
            )
        return None

    if "application/json" not in content_type.lower():
        logger.error(
            "download-options returned non-JSON response",
            extra={
                "dataset": dataset_name,
                "entity_id": entity_id,
                "content_type": content_type,
                "response_body": response.text[:500],
            },
        )
        return None

    try:
        response_data = response.json()
    except ValueError:
        logger.error(
            "download-options returned invalid JSON",
            extra={
                "dataset": dataset_name,
                "entity_id": entity_id,
                "response_body": response.text[:500],
            },
        )
        return None

    data_list = response_data.get("data")

    if not data_list:
        logger.error(
            "No downloadable products returned",
            extra={
                "entity_id": entity_id,
                "dataset": dataset_name,
            },
        )
        logger.error(
            "Full API response: %s",
            pformat(response_data),
        )
        return None

    rd = data_list[0]
    logger.info(
        "Product size determined",
        extra={"filesize_bytes": rd["filesize"]},
    )
    return rd["id"]


@retry(max_retries=5, wait_seconds=10)
def robust_download(
    entity_id: str,
    session: requests.Session,
    datasetName: str,
    out_dir: str,
) -> Optional[str]:
    """
    Download with automatic retry (5x, 10s wait).

    Assumptions:
        Session is pre-authenticated. entity_id and
        datasetName are valid USGS identifiers. The
        @retry decorator handles transient network
        errors only; permanent failures propagate.

    Args:
        entity_id: USGS entity identifier.
        session: Authenticated requests.Session.
        datasetName: USGS dataset name.
        out_dir: Local output directory.

    Returns:
        Path to downloaded zip, or None on failure.

    Raises:
        Exception: Re-raised after logging to trigger
            @retry decorator on transient failures.
    """
    try:
        return download_imagery(
            entity_id,
            session=session,
            datasetName=datasetName,
            out_dir=out_dir,
        )
    except Exception as e:
        logger.error(
            "Download failed",
            extra={
                "entity_id": entity_id,
                "error": str(e),
            },
        )
        raise


def download_imagery(
    entity_id: str,
    *,
    session: requests.Session,
    datasetName: str,
    out_dir: str,
    max_retries: int = 5,
    poll_interval: int = 30,
) -> Optional[str]:
    """
    Orchestrate single image download with USGS polling.

    Handles the request-prepare-retrieve workflow with a
    polling mechanism for staged downloads. Polling is
    bounded by both max_retries AND MAX_POLL_ELAPSED
    (whichever triggers first).

    Assumptions:
        Session is pre-authenticated. entity_id is a
        valid USGS entity identifier. datasetName is a
        valid USGS dataset. USGS download-request returns
        either 'availableDownloads' or
        'preparingDownloads' in response data.

    Args:
        entity_id: USGS entity identifier.
        session: Authenticated requests.Session.
        datasetName: USGS dataset name.
        out_dir: Local output directory.
        max_retries: Polling attempts before giving up.
        poll_interval: Seconds between poll attempts.

    Returns:
        Path to downloaded zip, or None on failure.
        NOTE: None-return is accepted tech debt
        (GAIFAGP-486). Should raise on failure.
    """
    logger.debug(
        "Starting download process",
        extra={"entity_id": entity_id},
    )

    # 1. Get the product ID
    product_id = get_product_id(
        session, datasetName, entity_id
    )
    if not product_id:
        logger.error(
            "No product ID found. Aborting download.",
            extra={"entity_id": entity_id},
        )
        return None

    # 2. Send the initial download request
    response_data, label = request_download(
        session, entity_id, product_id
    )

    # 3. Check for immediately available downloads
    available = response_data["data"]["availableDownloads"]
    if available:
        download_url = available[0]["url"]
        logger.info(
            "Download immediately available",
            extra={"entity_id": entity_id},
        )
        zipped = download_zip(
            session, download_url, out_dir
        )
        logger.info(
            "Download saved",
            extra={"entity_id": entity_id},
        )
        return zipped

    # 4. If not available, poll until ready.
    #    Bounded by BOTH max_retries and elapsed time.
    preparing = response_data["data"]["preparingDownloads"]
    if preparing:
        logger.info(
            "Download being prepared, polling",
            extra={
                "entity_id": entity_id,
                "label": label,
                "max_retries": max_retries,
                "max_elapsed_s": MAX_POLL_ELAPSED,
            },
        )
        retr_url = (
            "https://m2m.cr.usgs.gov"
            "/api/api/json/stable/download-retrieve"
        )
        poll_start = time.monotonic()
        for i in range(max_retries):
            elapsed = time.monotonic() - poll_start
            if elapsed >= MAX_POLL_ELAPSED:
                logger.error(
                    "Polling exceeded elapsed limit",
                    extra={
                        "entity_id": entity_id,
                        "elapsed_s": round(elapsed, 1),
                        "limit_s": MAX_POLL_ELAPSED,
                    },
                )
                break
            time.sleep(poll_interval)
            logger.debug(
                "Polling for download",
                extra={
                    "label": label,
                    "attempt": i + 1,
                    "max_retries": max_retries,
                    "elapsed_s": round(
                        time.monotonic() - poll_start,
                        1,
                    ),
                },
            )
            retr_resp = session.post(
                retr_url,
                json={"label": label},
                timeout=USGS_API_TIMEOUT,
            ).json()

            if retr_resp["data"]["available"]:
                dl_url = (
                    retr_resp["data"]["available"][0][
                        "url"
                    ]
                )
                logger.info(
                    "Download now ready",
                    extra={"entity_id": entity_id},
                )
                zipped = download_zip(
                    session, dl_url, out_dir
                )
                logger.info(
                    "Download saved",
                    extra={"entity_id": entity_id},
                )
                return zipped

        logger.error(
            "Download not available after polling",
            extra={
                "entity_id": entity_id,
                "attempts": max_retries,
                "elapsed_s": round(
                    time.monotonic() - poll_start, 1
                ),
            },
        )
        return None

    logger.warning(
        "Unexpected USGS response: no available or "
        "preparing downloads",
        extra={"entity_id": entity_id},
    )
    return None


# -----------------------------------------------------------------------
# Payload Builders
# -----------------------------------------------------------------------
def geojson_for_ee(geojson: dict) -> dict:
    """
    Build geoJson value payload for build_spatial_filter.

    Args:
        geojson: GeoJSON dict with type and coordinates.

    Returns:
        EarthExplorer-formatted geoJson payload.

    Ref:
        landsatxplore/api.py L439-L469
    """
    geojson_payload = {"type": geojson["type"]}

    if geojson["type"] == "Polygon":
        geojson_payload["coordinates"] = (
            geojson["coordinates"]
        )
    else:
        logger.warning(
            "GeoJSON type is not a polygon. "
            "Only polygons supported at this time."
        )

    return geojson_payload


def build_cloud_cover_filter(
    minimum: int = 0,
    maximum: int = 100,
    include_unknown: bool = True,
) -> dict:
    """
    Build cloud cover bandpass filter.

    Args:
        minimum: Min cloud cover percentage.
        maximum: Max cloud cover percentage.
        include_unknown: Include unknown CC images.

    Returns:
        Cloud cover filter dict for EE API.

    Ref:
        landsatxplore/api.py L255-L259, L523-L539
    """
    return {
        "min": minimum,
        "max": maximum,
        "includeUnknown": include_unknown,
    }


def build_spatial_filter(geojson: Any) -> dict:
    """
    Build spatial filter from GeoJSON for EE API.

    Expects EPSG:4326. Only supports Polygons.

    Args:
        geojson: GeoJSON dict or geometry object with
            __geo_interface__.

    Returns:
        Spatial filter dict for EE API.

    Ref:
        landsatxplore/api.py L493-L504, L439-L469
    """
    spatial_payload = {"filterType": "geojson"}
    try:
        spatial_payload["geoJson"] = geojson_for_ee(
            geojson
        )
    except (TypeError, AttributeError, KeyError):
        logger.warning(
            "Non-GeoJSON input provided, "
            "falling back to __geo_interface__"
        )
        spatial_payload["geoJson"] = (
            geojson.__geo_interface__
        )
    return spatial_payload


def build_acquisition_filter(
    start: str, end: str
) -> dict:
    """
    Build temporal filter using ISO 8601 datetime format.

    Args:
        start: Start date (YYYY-MM-DD or full ISO 8601).
        end: End date (same format).

    Returns:
        Acquisition filter dict for EE API.

    Ref:
        landsatxplore/api.py L507-L520
    """
    return {"start": start, "end": end}


def build_dataset_filter(
    acquisition: dict, spatial: dict
) -> dict:
    """
    Build DATASET-SEARCH API query payload.

    Args:
        acquisition: Acquisition filter dict.
        spatial: Spatial filter dict.

    Returns:
        Combined filter dict for dataset-search.

    Ref:
        landsatxplore/api.py L563-L597
    """
    return {
        "acquisitionFilter": acquisition,
        "spatialFilter": spatial,
    }


def build_scene_filter(
    acquisition: dict, spatial: dict, cloud: dict
) -> dict:
    """
    Build SCENE-SEARCH API query payload.

    Args:
        acquisition: Acquisition filter dict.
        spatial: Spatial filter dict.
        cloud: Cloud cover filter dict.

    Returns:
        Combined filter dict for scene-search.

    Ref:
        landsatxplore/api.py L563-L597
    """
    return {
        "acquisitionFilter": acquisition,
        "spatialFilter": spatial,
        "cloudCoverFilter": cloud,
    }


def build_ee_query_payload(
    start_date: str,
    end_date: str,
    aoi: Polygon,
    max_cc_pct: int = 100,
    max_results: int = 500,
    imagery_dataset: str = "crssp_orderable_w3",
    catalog_id: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Build complete EarthExplorer scene-search payload.

    Conditionally includes filters based on which
    arguments are provided:
      - aoi → spatialFilter
      - start_date + end_date → acquisitionFilter
      - always → cloudCoverFilter
      - catalog_id + session → metadataFilter

    When called with catalog_id only (aoi=None, dates=None),
    produces a payload with only cloudCoverFilter and
    metadataFilter. GAIFAGP-558.

    Args:
        start_date: Start date (YYYY-MM-DD), or None.
        end_date: End date (YYYY-MM-DD), or None.
        aoi: Area of interest polygon, or None.
        max_cc_pct: Maximum cloud cover percentage.
        max_results: Maximum number of results.
            Default 500 for spatial searches. Catalog_id
            searches typically return <50 scenes per ID.
        imagery_dataset: USGS dataset name.
        catalog_id: Optional catalog_id to add as a
            metadataFilter. GAIFAGP-558.
        session: Authenticated session, required when
            catalog_id is provided (for field discovery).

    Returns:
        Complete payload dict for scene-search API.
    """
    data_filter = {
        "cloudCoverFilter": build_cloud_cover_filter(
            maximum=max_cc_pct
        ),
    }

    if start_date and end_date:
        data_filter["acquisitionFilter"] = (
            build_acquisition_filter(start_date, end_date)
        )

    if aoi is not None:
        data_filter["spatialFilter"] = (
            build_spatial_filter(aoi)
        )

    if catalog_id:
        if session is None:
            raise ValueError(
                "session is required when catalog_id is "
                "provided (needed for get_catalog_field_id "
                "API call)"
            )
        catalog_field_id = get_catalog_field_id(
            imagery_dataset, session
        )
        data_filter["metadataFilter"] = {
            "filterType": "value",
            "filterId": catalog_field_id,
            "value": catalog_id,
        }

    return {
        "datasetName": imagery_dataset,
        "sceneFilter": data_filter,
        "maxResults": max_results,
        "metadataType": "full",
    }


# -----------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------
def ee_login(
    session: requests.Session,
    username: str,
    token: str,
) -> requests.Session:
    """
    Log into EarthExplorer and embed auth token in session.

    Assumptions:
        USGS M2M login-token endpoint accepts JSON
        payload with 'username' and 'token' fields.
        Successful response returns auth token in
        'data' field. Token is embedded in session
        headers for all subsequent API calls.

    Args:
        session: requests.Session to authenticate.
        username: USGS username.
        token: USGS API token.

    Returns:
        Session with X-Auth-Token header set.

    Raises:
        requests.HTTPError: On non-200 response from
            USGS login endpoint (bad creds, outage).
        ValueError: If login succeeds but returns no
            auth token (API contract violation).

    Ref:
        landsatxplore/api.py L90-L104
    """
    payload = {"username": username, "token": token}
    url = (
        "https://m2m.cr.usgs.gov"
        "/api/api/json/stable/login-token"
    )
    r = session.post(
        url, json=payload, timeout=USGS_API_TIMEOUT
    )
    r.raise_for_status()

    auth_token = r.json().get("data")
    if not auth_token:
        raise ValueError(
            "USGS login returned 200 but no auth token "
            "in response data. Check credentials."
        )

    session.headers["X-Auth-Token"] = auth_token
    logger.info(
        "EarthExplorer login successful",
        extra={"username": username},
    )
    return session


# -----------------------------------------------------------------------
# USGS Metadata Field Discovery (GAIFAGP-484)
# -----------------------------------------------------------------------
USGS_DATASET_FIELDS_URL = (
    "https://m2m.cr.usgs.gov"
    "/api/api/json/stable/dataset-filters"
)


def discover_dataset_fields(
    dataset_name: str,
    session: requests.Session,
    timeout: int = 30,
) -> list[dict]:
    """
    Query USGS dataset-filters API for a given dataset.

    Returns the raw list of field definition dicts. Each
    dict contains at minimum an 'id' (the filterId used
    in scene-search) and a label field ('fieldLabel',
    'name', or 'label' depending on API version).

    SOURCE OF TRUTH ASSUMPTIONS:
      - USGS M2M dataset-filters endpoint is authoritative
        for metadata field definitions per dataset.
      - Field dict structure varies by API version;
        callers check 'fieldLabel', 'name', and 'label'.

    Args:
        dataset_name: USGS dataset name
            (e.g., "crssp_orderable_w2").
        session: Authenticated requests.Session.
        timeout: Request timeout in seconds.

    Returns:
        List of field definition dicts from USGS API.

    Raises:
        ValueError: If API returns an error or 0 fields.
        requests.HTTPError: On non-200 response.
    """
    resp = session.post(
        USGS_DATASET_FIELDS_URL,
        json={"datasetName": dataset_name},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errorCode"):
        raise ValueError(
            f"USGS dataset-filters error for "
            f"{dataset_name}: "
            f"{data.get('errorMessage', 'Unknown')}"
        )

    fields = data.get("data", [])
    if not fields:
        raise ValueError(
            f"dataset-filters returned 0 fields for "
            f"{dataset_name}. "
            f"Verify the dataset name is correct."
        )

    logger.info(
        "dataset-filters query complete",
        extra={
            "dataset": dataset_name,
            "field_count": len(fields),
        },
    )
    return fields


def get_catalog_field_id(
    dataset_name: str,
    session: requests.Session,
    timeout: int = 30,
) -> str:
    """
    Discover the USGS metadata field ID for catalog_id.

    Calls discover_dataset_fields(), searches for a field
    whose label contains "catalog" (case-insensitive),
    returns its ID for use as filterId in scene-search
    metadataFilter queries.

    Called by poi_backfill.resolve_via_usgs_api() on first
    API call per dataset. Result cached at the caller level
    (module-level dict in poi_backfill.py).

    SOURCE OF TRUTH ASSUMPTIONS:
      - Exactly one field per dataset has "catalog" in
        its label.
      - USGS field dicts may use 'fieldLabel', 'name',
        or 'label' for the display name.
      - If USGS renames the field, this function raises
        ValueError with the full field list.

    Args:
        dataset_name: USGS dataset name
            (e.g., "crssp_orderable_w2").
        session: Authenticated requests.Session.
        timeout: Request timeout in seconds.

    Returns:
        String field ID (filterId for scene-search).

    Raises:
        ValueError: If zero or multiple fields match
            "catalog". Includes full field list.
    """
    fields = discover_dataset_fields(
        dataset_name, session, timeout
    )

    # Match: field label containing "catalog"
    # (case-insensitive). Check all three known label keys
    # because USGS API versions differ.
    matches = []
    for f in fields:
        label = (
            f.get("fieldLabel", "")
            or f.get("name", "")
            or f.get("label", "")
        )
        if "catalog" in label.lower():
            matches.append(f)

    if len(matches) == 1:
        field_id = str(
            matches[0].get(
                "id", matches[0].get("fieldId", "")
            )
        )
        field_label = (
            matches[0].get("fieldLabel", "")
            or matches[0].get("name", "")
            or matches[0].get("label", "")
        )
        logger.info(
            "Catalog field discovered",
            extra={
                "dataset": dataset_name,
                "field_id": field_id,
                "field_label": field_label,
            },
        )
        return field_id

    # Diagnostic output for error
    field_list = "\n".join(
        f"  id={f.get('id', '?'):<20}  "
        f"label={f.get('fieldLabel', f.get('name', f.get('label', '?')))}"
        for f in fields
    )

    if len(matches) == 0:
        raise ValueError(
            f"No field containing 'catalog' found in "
            f"{dataset_name}. "
            f"Available fields:\n{field_list}\n"
            f"Update get_catalog_field_id() to match "
            f"the correct label."
        )

    match_labels = [
        f.get(
            "fieldLabel",
            f.get("name", f.get("label", "?")),
        )
        for f in matches
    ]
    raise ValueError(
        f"Multiple fields containing 'catalog' in "
        f"{dataset_name}: {match_labels}. "
        f"Available fields:\n{field_list}\n"
        f"Update get_catalog_field_id() to narrow "
        f"the match."
    )


def get_vendor_field_id(
    dataset_name: str,
    session: requests.Session,
    timeout: int = 30,
) -> str:
    """Discover the USGS metadata field ID for vendor_id style filters."""
    fields = discover_dataset_fields(
        dataset_name, session, timeout
    )

    candidates = []
    for f in fields:
        label = (
            f.get("fieldLabel", "")
            or f.get("name", "")
            or f.get("label", "")
        )
        normalized_label = " ".join(label.lower().split())
        score = 0
        if normalized_label == "vendor id":
            score = 100
        elif "vendor id" in normalized_label:
            score = 90
        elif normalized_label == "vendor":
            score = 80
        elif "vendor" in normalized_label:
            score = 70

        if score > 0:
            candidates.append((score, f, label))

    if candidates:
        candidates.sort(
            key=lambda item: (
                item[0],
                len(str(item[2] or "")),
            ),
            reverse=True,
        )
        best_match = candidates[0][1]
        best_label = candidates[0][2]
        field_id = str(
            best_match.get(
                "id", best_match.get("fieldId", "")
            )
        )
        logger.info(
            "Vendor field discovered",
            extra={
                "dataset": dataset_name,
                "field_id": field_id,
                "field_label": best_label,
                "candidate_count": len(candidates),
            },
        )
        return field_id

    field_list = "\n".join(
        f"  id={f.get('id', '?'):<20}  "
        f"label={f.get('fieldLabel', f.get('name', f.get('label', '?')))}"
        for f in fields
    )

    raise ValueError(
        f"No field containing 'vendor' found in "
        f"{dataset_name}. Available fields:\n{field_list}"
    )


# -----------------------------------------------------------------------
# GeoDataFrame Builders
# -----------------------------------------------------------------------
def gdf_from_gegd(
    results: dict, dar_id: str
) -> gpd.GeoDataFrame:
    """
    Create GeoDataFrame from GEGD API results.

    Args:
        results: GEGD API response dict.
        dar_id: DAR ID number.

    Returns:
        GeoDataFrame with CRS EPSG:4326.
    """
    df = pd.DataFrame()

    for i, record in enumerate(results["features"]):
        props = record["properties"]
        df.loc[i, "id"] = record["id"]
        df.loc[i, "aoi_id"] = dar_id
        df.loc[i, "legacy_id"] = props["legacyId"]
        df.loc[i, "factory_order_number"] = (
            props["factoryOrderNumber"]
        )
        df.loc[i, "acquisition_date"] = (
            props["acquisitionDate"]
        )
        df.loc[i, "source"] = props["source"]
        df.loc[i, "source_unit"] = props["sourceUnit"]
        df.loc[i, "product_type"] = props["productType"]
        df.loc[i, "cloud_cover"] = props["cloudCover"]
        df.loc[i, "off_nadir_angle"] = (
            props["offNadirAngle"]
        )
        df.loc[i, "sun_elevation"] = (
            props["sunElevation"]
        )
        df.loc[i, "sun_azimuth"] = props["sunAzimuth"]
        df.loc[i, "ground_sample_distance"] = (
            props["groundSampleDistance"]
        )
        df.loc[i, "data_layer"] = props["dataLayer"]
        df.loc[i, "legacy_description"] = (
            props["legacyDescription"]
        )
        df.loc[i, "color_band_order"] = (
            props["colorBandOrder"]
        )
        df.loc[i, "asset_name"] = props["assetName"]
        df.loc[i, "per_pixel_x"] = props["perPixelX"]
        df.loc[i, "per_pixel_y"] = props["perPixelY"]
        df.loc[i, "crs_from_pixels"] = (
            props["crsFromPixels"]
        )
        df.loc[i, "age_days"] = props["ageDays"]
        df.loc[i, "ingest_date"] = props["ingestDate"]
        df.loc[i, "company_name"] = props["companyName"]
        df.loc[i, "copyright"] = props["copyright"]
        df.loc[i, "niirs"] = props["niirs"]
        coords = record["geometry"]["coordinates"][0]
        df.loc[i, "geometry"] = Polygon(
            [list(reversed(pt)) for pt in coords]
        )

    gdf = gpd.GeoDataFrame(df, geometry="geometry")
    return gdf.set_crs(4326)


def gdf_from_mgp(
    results: requests.Response, dar_id: str
) -> gpd.GeoDataFrame:
    """
    Create GeoDataFrame from MGP API results.

    Args:
        results: MGP API response object.
        dar_id: DAR ID number.

    Returns:
        GeoDataFrame with CRS EPSG:4326.
    """
    columns = [
        "id", "platform", "instruments", "gsd",
        "pan_resolution_avg", "multi_resolution_avg",
        "datetime", "off_nadir", "azimuth",
        "sun_azimuth", "sun_elevation", "bbox",
    ]
    gdf = gpd.GeoDataFrame(columns=columns)
    gdf = gdf.set_geometry("bbox").set_crs("EPSG:4326")

    r = results.json()
    for i, feature in enumerate(r["features"]):
        props = feature["properties"]
        gdf.loc[i, "id"] = feature["id"]
        gdf.loc[i, "aoi_id"] = dar_id
        gdf.loc[i, "platform"] = props["platform"]
        gdf.loc[i, "instruments"] = ", ".join(
            props["instruments"]
        )
        gdf.loc[i, "gsd"] = props["gsd"]
        gdf.loc[i, "pan_resolution_avg"] = (
            props["pan_resolution_avg"]
        )
        gdf.loc[i, "multi_resolution_avg"] = (
            props["multi_resolution_avg"]
        )
        gdf.loc[i, "datetime"] = props["datetime"]
        gdf.loc[i, "off_nadir"] = (
            props["view:off_nadir"]
        )
        gdf.loc[i, "azimuth"] = props["view:azimuth"]
        gdf.loc[i, "sun_azimuth"] = (
            props["view:sun_azimuth"]
        )
        gdf.loc[i, "sun_elevation"] = (
            props["view:sun_elevation"]
        )
        gdf.loc[i, "bbox"] = box(*feature["bbox"])

    return gdf


def gdf_from_ee(
    results: requests.Response, dar_id: str
) -> gpd.GeoDataFrame:
    """
    Create GeoDataFrame from EarthExplorer API results.

    Args:
        results: EE API response object.
        dar_id: DAR ID number.

    Returns:
        GeoDataFrame with CRS EPSG:4326.
    """
    r = results.json()
    results_data = r["data"]["results"]
    columns = [
        f["fieldName"]
        for f in results_data[0]["metadata"]
    ]
    gdf = gpd.GeoDataFrame(columns=columns)

    for result in results_data:
        gdf.loc[gdf.shape[0]] = [
            f["value"] for f in result["metadata"]
        ]

    gdf["thumbnail"] = pd.Series(
        [
            r["browse"][0]["thumbnailPath"]
            for r in results_data
        ]
    )
    gdf["publish_date"] = pd.Series(
        [r["publishDate"] for r in results_data]
    )
    gdf["bounds"] = gpd.GeoSeries(
        [
            Polygon(
                r["spatialBounds"]["coordinates"][0]
            )
            for r in results_data
        ]
    )
    gdf = gdf.set_geometry("bounds").set_crs("EPSG:4326")

    drop_cols = [
        c for c in columns if "Corner" in c
    ] + ["Center Latitude", "Center Longitude"]
    gdf = gdf.drop(drop_cols, axis=1)

    # Normalize column names to database-safe format
    col_update = {
        col: col.lower().replace(" ", "_")
        for col in gdf.columns
    }
    gdf = gdf.rename(columns=col_update)

    # UTM Zone never contains useful data
    gdf = gdf.drop(columns=["utm_zone"])
    gdf.insert(loc=1, column="aoi_id", value=dar_id)

    return gdf