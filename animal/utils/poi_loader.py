"""
POI ingestion business logic for the ``poi load`` management command.

Implements the three-layer data contract from DL-022:
  Layer 1 — File-level metadata (vendor_id, epsg_code from filename)
  Layer 2 — Feature-level fields (sample_idx, area, deviation, geometry)
  Layer 3 — Resolved at ingest (catalog_id, entity_id, sensor,
            date_image_taken, project, generation_method)

All business logic lives here. The poi.py ``_action_load()`` handler
is a thin wrapper that delegates to ``load_pois()``.
"""
# ----------------------------------------------------------------------
# ----- poi_loader.py --------------------------------------------------
# ----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  POI ingestion from GeoJSON files. Parses file-level
#              and feature-level metadata per DL-022, resolves ETL
#              linkage at ingest, loads into PointsOfInterest table.
#
#    tickets:  GAIFAGP-451 (poi load action)
#              GAIFAGP-544 (SPIKE — workflow architecture)
#              GAIFAGP-428 (generation_method field)
#              GAIFAGP-573 (auto-provision EE records on load)
#
#    references:
#      DL-022 — POI Data Contract (three-layer contract)
#      DL-017 — Imagery Identifier Relationships (catalog_id prefix)
#      DL-019 — Soft-Link Governance (audit, not write-time enforcement)
#      DL-011 — CRS handling (reproject to WGS84 at ingest)
#
# ----------------------------------------------------------------------

import json
import logging
import re
from pathlib import Path
from typing import Optional

from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction

from animal.models import ExtractTransformLoad, PointsOfInterest, Project
from animal.utils.poi_utils import parse_geojson_filename
from animal.utils.poi_backfill import derive_sensor

logger = logging.getLogger(__name__)


def decode_geojson_payload(raw_data) -> dict:
    """Decode GeoJSON bytes/string/dict into a dictionary payload."""
    if isinstance(raw_data, dict):
        payload = raw_data
    else:
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")

        if not isinstance(raw_data, str):
            raise ValueError("Uploaded GeoJSON must decode to a JSON object.")

        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Uploaded file is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Uploaded GeoJSON must decode to a JSON object.")

    return payload


def extract_preview_points(raw_data) -> list[dict]:
    """Extract Point geometries from GeoJSON payload as lon/lat dictionaries."""
    payload = decode_geojson_payload(raw_data)

    if payload.get("type") != "FeatureCollection":
        raise ValueError("Expected GeoJSON FeatureCollection.")

    source_epsg = _extract_epsg_from_crs(payload.get("crs"))
    points = []

    for feature in payload.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue

        try:
            point = _reproject_geometry(geometry, source_epsg)
            points.append({"lon": point.x, "lat": point.y})
        except Exception:
            continue

    return points


def load_pois(
    filepath: str,
    project_identifier: str,
    dry_run: bool = True,
    replace_duplicates: bool = False,
    batch_size: int = 1000,
) -> dict:
    """
    Main entry point for POI ingestion from a GeoJSON file.

    Implements the full DL-022 contract: parses file-level metadata
    (Layer 1), extracts feature-level fields (Layer 2), resolves
    ETL linkage and derived fields (Layer 3), and loads records
    into PointsOfInterest.

    Args:
        filepath: Path to the GeoJSON file.
        project_identifier: Project ID (int-like) or Project.value
            string (e.g., ``narw_capecod_2020_2024``).
        dry_run: If True, report what would happen without writing.
        replace_duplicates: If True, replace existing records on
            sample_idx collision within the project. If False
            (default), skip duplicates.
        batch_size: Records per bulk_create batch.

    Returns:
        Dict with keys: loaded, skipped, duplicates, replaced,
        errors, etl_warnings, dry_run, total_features, project_label.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    if not path.suffix.lower() == '.geojson':
        raise ValueError(f"Expected .geojson file, got: {path.suffix}")

    result = {
        "loaded": 0,
        "skipped": 0,
        "duplicates": 0,
        "replaced": 0,
        "errors": [],
        "etl_warnings": [],
        "etl_match_found": False,
        "dry_run": dry_run,
        "total_features": 0,
        "project_label": "",
    }

    # --- Resolve project ---
    project = _resolve_project(project_identifier)
    result["project_label"] = str(project)

    # --- Layer 1: file-level metadata ---
    file_meta = _parse_filename(path)
    vendor_id = file_meta["vendor_id"]
    epsg_code = file_meta["epsg_code"]

    # --- Layer 3 (partial): ETL lookup ---
    etl_meta = _resolve_etl_fields(vendor_id)
    if etl_meta["catalog_id"] is None:
        result["etl_warnings"].append(
            f"No ETL record found for vendor_id '{vendor_id}'. "
            f"catalog_id, entity_id, sensor, date_image_taken will be NULL. "
            f"This is an audit finding per DL-019."
        )

    # --- Auto-provision EE record (GAIFAGP-573) ---
    if etl_meta["catalog_id"] and not _ee_record_exists(vendor_id):
        aoi_id = _get_etl_aoi_id(vendor_id)
        ee_result = _provision_ee_record(
            catalog_id=etl_meta["catalog_id"],
            vendor_id=vendor_id,
            aoi_id=aoi_id,
        )
        if ee_result:
            result["ee_provisioned"] = True
            logger.info(
                "Auto-provisioned EE record",
                extra={
                    "vendor_id": vendor_id,
                    "entity_id": ee_result.get("entity_id"),
                    "ticket": "GAIFAGP-573",
                },
            )

    # --- Layer 2: parse features ---
    features = _parse_geojson(path)
    result["total_features"] = len(features)

    if not features:
        result["errors"].append("GeoJSON contains no features.")
        return result

    # --- Validate each feature ---
    valid_records = []
    for i, feature in enumerate(features):
        is_valid, errors = _validate_record(feature)
        if is_valid:
            valid_records.append(feature)
        else:
            for err in errors:
                result["errors"].append(f"Feature {i}: {err}")
            result["skipped"] += 1

    if not valid_records:
        result["errors"].append("No valid features after validation.")
        return result

    # --- Duplicate detection ---
    new_records, duplicate_records = _detect_duplicates(
        valid_records, project.id
    )
    result["duplicates"] = len(duplicate_records)

    # --- Build POI objects ---
    pois_to_create = _build_poi_objects(
        new_records, project, file_meta, etl_meta
    )

    pois_to_replace = []
    if replace_duplicates and duplicate_records:
        pois_to_replace = _build_poi_objects(
            duplicate_records, project, file_meta, etl_meta
        )

    if not replace_duplicates:
        result["skipped"] += len(duplicate_records)

    # --- Dry run: report and return ---
    if dry_run:
        result["loaded"] = len(pois_to_create)
        result["replaced"] = len(pois_to_replace)
        return result

    # --- Execute: write to database ---
    with transaction.atomic():
        # Create new records
        if pois_to_create:
            PointsOfInterest.objects.bulk_create(
                pois_to_create, batch_size=batch_size
            )
            result["loaded"] = len(pois_to_create)

        # Replace duplicates
        if pois_to_replace:
            for poi in pois_to_replace:
                PointsOfInterest.objects.filter(
                    sample_idx=poi.sample_idx,
                    project_id=project.id,
                ).update(
                    catalog_id=poi.catalog_id,
                    vendor_id=poi.vendor_id,
                    entity_id=poi.entity_id,
                    date_image_taken=poi.date_image_taken,
                    sensor=poi.sensor,
                    area=poi.area,
                    deviation=poi.deviation,
                    epsg_code=poi.epsg_code,
                    point=poi.point,
                    generation_method=poi.generation_method,
                )
            result["replaced"] = len(pois_to_replace)

    logger.info(
        "POI load completed",
        extra={
            "file": str(path.name),
            "project": str(project),
            "loaded": result["loaded"],
            "replaced": result["replaced"],
            "skipped": result["skipped"],
            "errors": len(result["errors"]),
            "etl_warnings": len(result["etl_warnings"]),
            "ticket": "GAIFAGP-451",
        },
    )

    return result


def load_pois_from_geojson_upload(
    uploaded_file,
    project_identifier: str,
    id_type: str,
    target_id: str,
    dry_run: bool = False,
    batch_size: int = 1000,
) -> dict:
    """Load POIs from an uploaded GeoJSON file bound to one selected ID.

    This entrypoint is intended for the web "Load Points" flow where users
    provide one binding value per upload (vendor_id or catalog_id).

    Args:
        uploaded_file: Django uploaded file object.
        project_identifier: Project ID/value/label.
        id_type: Either ``vendor`` or ``catalog``.
        target_id: Selected vendor or catalog identifier.
        dry_run: If True, validates and reports without writes.
        batch_size: Records per bulk_create batch.

    Returns:
        Dict with keys: loaded, skipped, duplicates, replaced,
        errors, etl_warnings, dry_run, total_features, project_label.
    """
    normalized_id_type = (id_type or "").strip().lower()
    normalized_target_id = (target_id or "").strip()

    if normalized_id_type not in {"vendor", "catalog"}:
        raise ValueError("id_type must be either 'vendor' or 'catalog'.")
    if not normalized_target_id:
        raise ValueError("target_id is required.")

    result = {
        "loaded": 0,
        "skipped": 0,
        "duplicates": 0,
        "replaced": 0,
        "errors": [],
        "etl_warnings": [],
        "dry_run": dry_run,
        "total_features": 0,
        "project_label": "",
    }

    project = _resolve_project(project_identifier)
    result["project_label"] = str(project)

    data = _load_uploaded_geojson(uploaded_file)
    source_epsg = _extract_epsg_from_crs(data.get("crs"))

    records = _parse_geojson_payload(data)
    result["total_features"] = len(records)
    if not records:
        result["errors"].append("GeoJSON contains no features.")
        return result

    valid_records = []
    for i, record in enumerate(records):
        is_valid, errors = _validate_record(record)
        if is_valid:
            valid_records.append(record)
        else:
            for err in errors:
                result["errors"].append(f"Feature {i}: {err}")
            result["skipped"] += 1

    if not valid_records:
        result["errors"].append("No valid features after validation.")
        return result

    new_records, duplicate_records = _detect_duplicates(valid_records, project.id)
    result["duplicates"] = len(duplicate_records)
    result["skipped"] += len(duplicate_records)

    etl_meta = {
        "catalog_id": None,
        "vendor_id": None,
        "entity_id": None,
        "sensor": None,
        "date_image_taken": None,
    }

    try:
        if normalized_id_type == "vendor":
            etl_lookup = _resolve_etl_fields(normalized_target_id)
            etl_meta.update(etl_lookup)
            etl_meta["vendor_id"] = normalized_target_id
            if etl_meta["catalog_id"] is None:
                result["etl_warnings"].append(
                    f"No ETL record found for vendor_id '{normalized_target_id}'. "
                    "catalog_id/entity_id/sensor/date_image_taken may be NULL."
                )
            else:
                result["etl_match_found"] = True
        else:
            etl_lookup = _resolve_etl_fields_by_catalog_id(normalized_target_id)
            etl_meta.update(etl_lookup)
            etl_meta["catalog_id"] = normalized_target_id
            if etl_meta["vendor_id"] is None:
                result["etl_warnings"].append(
                    f"No ETL record found for catalog_id '{normalized_target_id}'. "
                    "vendor_id/entity_id/sensor/date_image_taken may be NULL."
                )
            else:
                result["etl_match_found"] = True
    except Exception as exc:
        result["etl_warnings"].append(
            f"ETL lookup failed for {normalized_id_type}_id '{normalized_target_id}': {exc}"
        )
        if normalized_id_type == "vendor":
            etl_meta["vendor_id"] = normalized_target_id
        else:
            etl_meta["catalog_id"] = normalized_target_id

    pois_to_create = []
    for record in new_records:
        point = _reproject_geometry(record["geometry"], source_epsg)
        pois_to_create.append(
            PointsOfInterest(
                catalog_id=etl_meta["catalog_id"],
                vendor_id=etl_meta["vendor_id"],
                entity_id=etl_meta["entity_id"],
                date_image_taken=etl_meta["date_image_taken"],
                sensor=etl_meta["sensor"],
                sample_idx=record["sample_idx"],
                area=float(record["area"]),
                deviation=float(record["deviation"]),
                epsg_code=source_epsg,
                project=project,
                generation_method="automated",
                point=point,
            )
        )

    if dry_run:
        result["loaded"] = len(pois_to_create)
        return result

    with transaction.atomic():
        if pois_to_create:
            PointsOfInterest.objects.bulk_create(
                pois_to_create,
                batch_size=batch_size,
            )
            result["loaded"] = len(pois_to_create)

    logger.info(
        "POI upload load completed",
        extra={
            "project": str(project),
            "loaded": result["loaded"],
            "skipped": result["skipped"],
            "duplicates": result["duplicates"],
            "errors": len(result["errors"]),
            "id_type": normalized_id_type,
        },
    )

    return result


def _resolve_project(project_identifier: str) -> Project:
    """
    Resolve a project from an ID or value string.

    Tries integer ID first, then Project.value exact match,
    then Project.label exact match.

    Args:
        project_identifier: Project ID (int-like) or value string.

    Returns:
        Project instance.

    Raises:
        ValueError: If project cannot be resolved.
    """
    # Try integer ID
    try:
        project_id = int(project_identifier)
        try:
            return Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            pass
    except (ValueError, TypeError):
        pass

    # Try value match
    try:
        return Project.objects.get(value=project_identifier)
    except Project.DoesNotExist:
        pass

    # Try label match
    try:
        return Project.objects.get(label=project_identifier)
    except Project.DoesNotExist:
        pass

    raise ValueError(
        f"Cannot resolve project '{project_identifier}'. "
        f"Pass a project ID, value, or label."
    )


def _parse_filename(path: Path) -> dict:
    """
    Extract Layer 1 metadata from the GeoJSON filename.

    Delegates to ``poi_utils.parse_geojson_filename()`` for
    vendor_id and epsg_code extraction.

    Args:
        path: Path to the GeoJSON file.

    Returns:
        Dict with 'vendor_id' and 'epsg_code'.

    Raises:
        ValueError: If filename doesn't match convention.
    """
    return parse_geojson_filename(path.name)


def _parse_geojson(path: Path) -> list:
    """
    Parse a GeoJSON FeatureCollection and extract Layer 2 fields.

    Reads the file as JSON (not GeoPandas) to avoid heavy
    dependencies in the management command path. Extracts
    sample_idx, area, deviation, and geometry per feature.

    Args:
        path: Path to the GeoJSON file.

    Returns:
        List of dicts, each with 'sample_idx', 'area',
        'deviation', 'geometry' (GeoJSON geometry dict).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return _parse_geojson_payload(data)


def _parse_geojson_payload(data: dict) -> list:
    """Parse a GeoJSON payload and extract Layer 2 feature fields."""

    if data.get("type") != "FeatureCollection":
        raise ValueError(
            f"Expected GeoJSON FeatureCollection, got: {data.get('type')}"
        )

    features_raw = data.get("features", [])
    records = []

    for feature in features_raw:
        props = feature.get("properties", {})
        geom = feature.get("geometry")

        # sample_idx: try 'id' first, then 'sample_idx' in properties
        sample_idx = props.get("id")
        if sample_idx is None:
            sample_idx = props.get("sample_idx")
        # Also check top-level GeoJSON 'id' field
        if sample_idx is None:
            sample_idx = feature.get("id")

        records.append({
            "sample_idx": str(sample_idx) if sample_idx is not None else None,
            "area": props.get("area"),
            "deviation": props.get("deviation"),
            "geometry": geom,
        })

    return records


def _load_uploaded_geojson(uploaded_file) -> dict:
    """Read and decode an uploaded GeoJSON file into a dictionary."""
    if not uploaded_file:
        raise ValueError("No upload provided.")

    file_name = (getattr(uploaded_file, "name", "") or "").lower()
    if file_name and not file_name.endswith(".geojson"):
        raise ValueError("Expected a .geojson upload.")

    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)

    raw_data = uploaded_file.read()
    return decode_geojson_payload(raw_data)


def _extract_epsg_from_crs(crs_obj: Optional[dict]) -> str:
    """Extract an EPSG code from a GeoJSON ``crs`` object.

    Defaults to EPSG:4326 when CRS metadata is missing.
    """
    if not crs_obj:
        return "4326"

    name = (
        crs_obj.get("properties", {}).get("name")
        if isinstance(crs_obj, dict)
        else None
    )
    if not name:
        return "4326"

    patterns = [
        r"EPSG::(\d+)$",
        r"EPSG:(\d+)$",
        r"/EPSG/(\d+)$",
        r"(\d+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, str(name), flags=re.IGNORECASE)
        if match:
            return match.group(1)

    raise ValueError(f"Unable to parse EPSG code from CRS name '{name}'.")


def _validate_record(record: dict) -> tuple:
    """
    Validate a single feature record against the DL-022 contract.

    Layer 2 required fields: sample_idx, area, deviation, geometry.

    Args:
        record: Dict from ``_parse_geojson()``.

    Returns:
        Tuple of (is_valid: bool, errors: list[str]).
    """
    errors = []

    if not record.get("sample_idx"):
        errors.append("Missing sample_idx (id field)")

    if record.get("area") is None:
        errors.append("Missing area")
    else:
        try:
            float(record["area"])
        except (ValueError, TypeError):
            errors.append(f"area is not numeric: {record['area']}")

    if record.get("deviation") is None:
        errors.append("Missing deviation")
    else:
        try:
            float(record["deviation"])
        except (ValueError, TypeError):
            errors.append(f"deviation is not numeric: {record['deviation']}")

    geom = record.get("geometry")
    if not geom:
        errors.append("Missing geometry")
    elif geom.get("type") != "Point":
        errors.append(f"Expected Point geometry, got: {geom.get('type')}")

    return (len(errors) == 0, errors)


def _reproject_geometry(geom_json: dict, source_epsg: str) -> GEOSGeometry:
    """
    Create a GEOSGeometry from GeoJSON and reproject to WGS84.

    Per DL-011, all POI geometry is stored in EPSG:4326. If the
    source CRS differs, the geometry is transformed.

    Args:
        geom_json: GeoJSON geometry dict (Point).
        source_epsg: EPSG code string (e.g., '32619').

    Returns:
        GEOSGeometry in EPSG:4326.
    """
    geom_str = json.dumps(geom_json)
    geom = GEOSGeometry(geom_str)

    source_srid = int(source_epsg)

    if source_srid != 4326:
        geom.srid = source_srid
        geom.transform(4326)
    else:
        geom.srid = 4326

    return geom


def _resolve_etl_fields(vendor_id: str) -> dict:
    """
    Look up ETL record by vendor_id and extract Layer 3 fields.

    Per DL-019 soft-link governance, a failed lookup is an audit
    finding, not a pipeline failure. Missing fields are set to None.

    Sensor is derived from catalog_id prefix per DL-017 using
    the existing ``derive_sensor()`` in poi_backfill.

    Args:
        vendor_id: Vendor ID parsed from filename (Layer 1).

    Returns:
        Dict with 'catalog_id', 'entity_id', 'sensor',
        'date_image_taken'. All None if lookup fails.
    """
    result = {
        "catalog_id": None,
        "entity_id": None,
        "sensor": None,
        "date_image_taken": None,
    }

    try:
        etl = ExtractTransformLoad.objects.get(vendor_id=vendor_id)
        result["catalog_id"] = str(etl.id)
        result["entity_id"] = etl.entity_id
        result["date_image_taken"] = etl.date
        # Sensor from catalog_id prefix (DL-017)
        result["sensor"] = derive_sensor(str(etl.id))
    except ExtractTransformLoad.DoesNotExist:
        logger.warning(
            "ETL lookup failed for vendor_id",
            extra={
                "vendor_id": vendor_id,
                "action": "audit_finding",
                "ref": "DL-019",
                "ticket": "GAIFAGP-451",
            },
        )
    except ExtractTransformLoad.MultipleObjectsReturned:
        # vendor_id is not unique in ETL — take the first match
        etl = ExtractTransformLoad.objects.filter(
            vendor_id=vendor_id
        ).first()
        if etl:
            result["catalog_id"] = str(etl.id)
            result["entity_id"] = etl.entity_id
            result["date_image_taken"] = etl.date
            result["sensor"] = derive_sensor(str(etl.id))
        logger.warning(
            "Multiple ETL records for vendor_id, using first match",
            extra={
                "vendor_id": vendor_id,
                "count": ExtractTransformLoad.objects.filter(
                    vendor_id=vendor_id
                ).count(),
                "ticket": "GAIFAGP-451",
            },
        )

    return result


def _resolve_etl_fields_by_catalog_id(catalog_id: str) -> dict:
    """Look up ETL record by catalog_id and extract Layer 3 fields."""
    result = {
        "catalog_id": None,
        "vendor_id": None,
        "entity_id": None,
        "sensor": None,
        "date_image_taken": None,
    }

    try:
        etl = ExtractTransformLoad.objects.get(id=catalog_id)
        result["catalog_id"] = str(etl.id)
        result["vendor_id"] = etl.vendor_id
        result["entity_id"] = etl.entity_id
        result["date_image_taken"] = etl.date
        result["sensor"] = derive_sensor(str(etl.id))
    except ExtractTransformLoad.DoesNotExist:
        logger.warning(
            "ETL lookup failed for catalog_id",
            extra={
                "catalog_id": catalog_id,
                "action": "audit_finding",
                "ref": "DL-019",
            },
        )

    return result


def _detect_duplicates(
    records: list,
    project_id: int,
) -> tuple:
    """
    Partition records into new and duplicate based on sample_idx.

    Duplicate key: sample_idx within a project (per DL-022).

    Args:
        records: List of validated feature dicts.
        project_id: Project primary key.

    Returns:
        Tuple of (new_records, duplicate_records).
    """
    sample_idxs = [r["sample_idx"] for r in records]

    existing = set(
        PointsOfInterest.objects.filter(
            project_id=project_id,
            sample_idx__in=sample_idxs,
        ).values_list("sample_idx", flat=True)
    )

    new_records = []
    duplicate_records = []

    for record in records:
        if record["sample_idx"] in existing:
            duplicate_records.append(record)
        else:
            new_records.append(record)

    return new_records, duplicate_records


def _build_poi_objects(
    records: list,
    project: Project,
    file_meta: dict,
    etl_meta: dict,
) -> list:
    """
    Construct PointsOfInterest model instances from validated records.

    Combines all three DL-022 layers:
      Layer 1 — vendor_id, epsg_code (from file_meta)
      Layer 2 — sample_idx, area, deviation, geometry (from records)
      Layer 3 — catalog_id, entity_id, sensor, date_image_taken
                (from etl_meta), project, generation_method

    Args:
        records: List of validated feature dicts.
        project: Resolved Project instance.
        file_meta: Dict from ``_parse_filename()``.
        etl_meta: Dict from ``_resolve_etl_fields()``.

    Returns:
        List of unsaved PointsOfInterest instances.
    """
    pois = []
    epsg_code = file_meta["epsg_code"]

    for record in records:
        point = _reproject_geometry(record["geometry"], epsg_code)

        poi = PointsOfInterest(
            catalog_id=etl_meta["catalog_id"],
            vendor_id=file_meta["vendor_id"],
            entity_id=etl_meta["entity_id"],
            date_image_taken=etl_meta["date_image_taken"],
            sensor=etl_meta["sensor"],
            sample_idx=record["sample_idx"],
            area=float(record["area"]),
            deviation=float(record["deviation"]),
            epsg_code=epsg_code,
            project=project,
            generation_method="automated",
            point=point,
        )
        pois.append(poi)

    return pois


# ------------------------------------------------------------------
# Auto-provisioning helpers (GAIFAGP-573)
# ------------------------------------------------------------------

def _ee_record_exists(vendor_id: str) -> bool:
    """Check if an EarthExplorer record exists for this vendor_id."""
    try:
        from animal.models import EarthExplorer
        return EarthExplorer.objects.filter(vendor_id=vendor_id).exists()
    except Exception:
        return False


def _get_etl_aoi_id(vendor_id: str) -> Optional[int]:
    """Fetch aoi_id from the ETL record for this vendor_id."""
    try:
        etl = ExtractTransformLoad.objects.filter(
            vendor_id=vendor_id,
        ).values_list("aoi_id", flat=True).first()
        return etl
    except Exception:
        return None


def _provision_ee_record(
    catalog_id: str,
    vendor_id: str,
    aoi_id: Optional[int],
) -> Optional[dict]:
    """
    Auto-provision an EarthExplorer record via USGS API.

    Searches USGS by catalog_id, matches result by vendor_id,
    creates EE record via update_or_create. Derives aoi_id from
    ETL record (no operator input needed).

    Graceful fallback: returns None on any failure (no credentials,
    API error, no results, no vendor match). Load continues regardless.

    Args:
        catalog_id: Catalog ID from ETL lookup.
        vendor_id: Vendor ID from filename.
        aoi_id: AOI ID from ETL record (may be None).

    Returns:
        Dict with created EE record info, or None on failure.
    """
    try:
        from django.conf import settings
        from animal.models import EarthExplorer
    except Exception:
        return None

    # Check credentials
    username = getattr(settings, "USGS_USERNAME", None)
    token = getattr(settings, "USGS_TOKEN", None)
    if not username or not token:
        logger.info(
            "EE auto-provision skipped: no USGS credentials configured",
            extra={"vendor_id": vendor_id, "ticket": "GAIFAGP-573"},
        )
        return None

    try:
        import requests
        from animal.utils.api_utils import ee_login, search_imagery

        session = requests.Session()
        ee_login(session, username, token)

        # Search by catalog_id only (GAIFAGP-558)
        gdf = search_imagery(
            aoi=None,
            dataset="crssp_orderable_w3",
            start=None,
            end=None,
            session=session,
            catalog_id=catalog_id,
        )

        if gdf.empty:
            logger.info(
                "EE auto-provision: no USGS results for catalog_id",
                extra={
                    "catalog_id": catalog_id,
                    "vendor_id": vendor_id,
                    "ticket": "GAIFAGP-573",
                },
            )
            return None

        # Match by vendor_id — column name in raw USGS response
        # search_imagery returns raw USGS column names (e.g., "Vendor ID")
        vid_col = None
        for col in gdf.columns:
            if col.lower().replace(" ", "_") == "vendor_id":
                vid_col = col
                break

        if not vid_col:
            logger.warning(
                "EE auto-provision: no Vendor ID column in search results",
                extra={"columns": list(gdf.columns), "ticket": "GAIFAGP-573"},
            )
            return None

        matched = gdf[gdf[vid_col] == vendor_id]
        if matched.empty:
            logger.info(
                "EE auto-provision: vendor_id not in search results",
                extra={
                    "catalog_id": catalog_id,
                    "vendor_id": vendor_id,
                    "result_count": len(gdf),
                    "ticket": "GAIFAGP-573",
                },
            )
            return None

        row = matched.iloc[0]

        # Map USGS columns to EE model fields
        # Normalize column names: "Vendor ID" -> "vendor_id"
        norm = {
            col.lower().replace(" ", "_"): col for col in gdf.columns
        }

        def _get(field, default=None):
            raw_col = norm.get(field)
            if raw_col and raw_col in row.index:
                val = row[raw_col]
                if val is not None and str(val).strip():
                    return val
            return default

        ee_defaults = {
            "catalog_id": _get("catalog_id", catalog_id),
            "vendor_id": vendor_id,
            "vendor": _get("vendor", ""),
            "satellite": _get("satellite", ""),
            "sensor": _get("sensor", ""),
            "cloud_cover": int(_get("cloud_cover", 0) or 0),
            "number_of_bands": int(_get("number_of_bands", 0) or 0),
            "map_projection": _get("map_projection", ""),
            "datum": _get("datum", ""),
            "processing_level": _get("processing_level", "LV1"),
            "file_format": _get("file_format", ""),
            "license_id": int(_get("license_id", 0) or 0),
            "sun_azimuth": float(_get("sun_azimuth", 0) or 0),
            "sun_elevation": float(_get("sun_elevation", 0) or 0),
            "pixel_size_x": float(_get("pixel_size_x", 0) or 0),
            "pixel_size_y": float(_get("pixel_size_y", 0) or 0),
            "center_latitude_dec": float(
                _get("center_latitude_dec", 0) or 0
            ),
            "center_longitude_dec": float(
                _get("center_longitude_dec", 0) or 0
            ),
            "event": _get("event", ""),
        }

        # Parse acquisition date
        acq_date_str = _get("acquisition_date")
        if acq_date_str:
            try:
                from datetime import datetime
                ee_defaults["acquisition_date"] = datetime.strptime(
                    str(acq_date_str)[:10], "%Y-%m-%d"
                ).date()
            except (ValueError, TypeError):
                ee_defaults["acquisition_date"] = None

        # aoi_id from ETL record
        if aoi_id is not None:
            from animal.models import AreaOfInterest
            ee_defaults["aoi_id"] = AreaOfInterest.objects.filter(
                id=aoi_id
            ).first()

        entity_id = _get("entity_id")
        if not entity_id:
            logger.warning(
                "EE auto-provision: no entity_id in search result",
                extra={"vendor_id": vendor_id, "ticket": "GAIFAGP-573"},
            )
            return None

        ee_obj, created = EarthExplorer.objects.update_or_create(
            entity_id=str(entity_id),
            defaults=ee_defaults,
        )

        logger.info(
            "EE record %s",
            "created" if created else "updated",
            extra={
                "entity_id": str(entity_id),
                "vendor_id": vendor_id,
                "catalog_id": catalog_id,
                "ticket": "GAIFAGP-573",
            },
        )

        return {
            "entity_id": str(entity_id),
            "vendor_id": vendor_id,
            "catalog_id": catalog_id,
            "created": created,
        }

    except Exception as e:
        logger.warning(
            "EE auto-provision failed, continuing load",
            extra={
                "vendor_id": vendor_id,
                "catalog_id": catalog_id,
                "error": str(e),
                "ticket": "GAIFAGP-573",
            },
        )
        return None
