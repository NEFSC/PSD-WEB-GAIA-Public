"""
POI lineage inspection — traces a single POI through the full data chain.

Returns structured results for CLI formatting by poi.py inspect.
"""
# -----------------------------------------------------------------------
# ----- poi_inspection.py -----------------------------------------------
# -----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Extracted from poi.py _action_inspect (GAIFAGP-573).
#              Full lineage tracing: POI → ETL → EE/GEGD/MGP → AOI.
#    tickets:  GAIFAGP-573 (poi.py consolidation)
#
# -----------------------------------------------------------------------

import logging

from animal.models import ExtractTransformLoad, PointsOfInterest
from animal.utils.model_helpers import get_optional_model

logger = logging.getLogger(__name__)


def inspect_poi(poi_id: int) -> dict:
    """
    Inspect a single POI with full data lineage tracing.

    Traces: POI → ETL → EE/GEGD/MGP → AOI

    Args:
        poi_id: PointsOfInterest primary key.

    Returns:
        dict with keys:
          - poi: dict of POI record fields
          - adjudication: dict or None
          - annotation_count: int or None
          - etl: dict with found, match_type, record, date_status
          - source_catalogs: dict with ee, gegd, mgp sub-dicts
          - issues: list[str]

    Raises:
        PointsOfInterest.DoesNotExist: if poi_id not found.
    """
    poi = PointsOfInterest.objects.select_related(
        "project",
        "final_classification",
        "final_species",
        "final_confidence",
    ).get(id=poi_id)

    result = {
        "poi": _extract_poi_fields(poi),
        "adjudication": _extract_adjudication(poi),
        "annotation_count": _count_annotations(poi),
        "etl": None,
        "source_catalogs": {},
        "issues": [],
    }

    # ETL linkage
    etl_record, match_type = _resolve_etl(poi)
    result["etl"] = _build_etl_result(poi, etl_record, match_type)

    # Source catalog linkage
    result["source_catalogs"] = _resolve_source_catalogs(
        poi, etl_record
    )

    # Collect issues
    if not etl_record:
        result["issues"].append("No ETL linkage")

    has_any_source = any(
        v.get("found") for v in result["source_catalogs"].values()
    )
    if not has_any_source and result["source_catalogs"]:
        result["issues"].append("No source catalog linkage")

    if poi.date_image_taken is None:
        result["issues"].append("NULL date_image_taken")

    return result


def _extract_poi_fields(poi) -> dict:
    """Extract display fields from a POI instance."""
    fields = {
        "id": poi.id,
        "catalog_id": poi.catalog_id,
        "vendor_id": poi.vendor_id,
        "entity_id": poi.entity_id,
        "date_image_taken": poi.date_image_taken,
        "project": poi.project.label if poi.project else None,
        "sensor": getattr(poi, "sensor", None),
        "epsg_code": poi.epsg_code,
        "area": poi.area,
    }
    if poi.point:
        fields["location"] = (poi.point.x, poi.point.y)
    return fields


def _extract_adjudication(poi) -> dict:
    """Extract adjudication fields if any are populated."""
    if not any([
        poi.final_classification,
        poi.final_species,
        poi.final_confidence,
    ]):
        return None
    return {
        "classification": (
            poi.final_classification.label
            if poi.final_classification else None
        ),
        "species": (
            poi.final_species.label if poi.final_species else None
        ),
        "confidence": (
            poi.final_confidence.label if poi.final_confidence else None
        ),
        "review_date": getattr(poi, "final_review_date", None),
    }


def _count_annotations(poi):
    """Count annotations for this POI if the model exists."""
    Annotations, has_ann = get_optional_model("Annotations")
    if has_ann:
        return Annotations.objects.filter(poi=poi).count()
    return None


def _resolve_etl(poi):
    """
    Resolve ETL record via priority chain:
    catalog_id → vendor_id → entity_id.

    Returns (etl_record_or_None, match_type_str_or_None).
    """
    if poi.catalog_id:
        rec = ExtractTransformLoad.objects.filter(
            id=str(poi.catalog_id)
        ).first()
        if rec:
            return rec, "catalog_id -> ETL.id"

    if poi.vendor_id:
        rec = ExtractTransformLoad.objects.filter(
            vendor_id=str(poi.vendor_id)
        ).first()
        if rec:
            return rec, "vendor_id -> ETL.vendor_id"

    if poi.entity_id:
        rec = ExtractTransformLoad.objects.filter(
            entity_id=str(poi.entity_id)
        ).first()
        if rec:
            return rec, "entity_id -> ETL.entity_id"

    return None, None


def _build_etl_result(poi, etl_record, match_type) -> dict:
    """Build structured ETL result dict."""
    if not etl_record:
        return {
            "found": False,
            "match_type": None,
            "record": None,
            "date_status": None,
        }

    # Date consistency
    date_status = None
    if poi.date_image_taken and etl_record.date:
        if poi.date_image_taken != etl_record.date:
            date_status = "mismatch"
        else:
            date_status = "match"
    elif not poi.date_image_taken and etl_record.date:
        date_status = "poi_null"

    return {
        "found": True,
        "match_type": match_type,
        "record": {
            "id": etl_record.id,
            "table_name": etl_record.table_name,
            "vendor_id": etl_record.vendor_id,
            "entity_id": etl_record.entity_id,
            "date": etl_record.date,
            "aoi_id": etl_record.aoi_id,
        },
        "date_status": date_status,
    }


def _resolve_source_catalogs(poi, etl_record) -> dict:
    """Resolve EE, GEGD, and MGP source catalog records."""
    catalogs = {}

    # EarthExplorer
    EarthExplorer, has_ee = get_optional_model("EarthExplorer")
    if has_ee:
        catalogs["ee"] = _resolve_ee(poi, EarthExplorer)
    else:
        catalogs["ee"] = {"found": False, "available": False}

    # GEOINTDiscovery
    GEOINTDiscovery, has_gegd = get_optional_model("GEOINTDiscovery")
    if has_gegd:
        catalogs["gegd"] = _resolve_gegd(etl_record, GEOINTDiscovery)
    else:
        catalogs["gegd"] = {"found": False, "available": False}

    # MaxarGeospatialPlatform
    MaxarGeospatialPlatform, has_mgp = get_optional_model(
        "MaxarGeospatialPlatform"
    )
    if has_mgp:
        catalogs["mgp"] = _resolve_mgp(etl_record, MaxarGeospatialPlatform)
    else:
        catalogs["mgp"] = {"found": False, "available": False}

    return catalogs


def _resolve_ee(poi, EarthExplorer) -> dict:
    """Search EarthExplorer by vendor_id then entity_id."""
    ee_record = None
    if poi.vendor_id:
        ee_record = EarthExplorer.objects.filter(
            vendor_id=str(poi.vendor_id)
        ).first()
    if not ee_record and poi.entity_id:
        ee_record = EarthExplorer.objects.filter(
            entity_id=str(poi.entity_id)
        ).first()

    if not ee_record:
        return {"found": False, "available": True}

    aoi_id_val = (
        ee_record.aoi_id_id
        if hasattr(ee_record, "aoi_id_id")
        else ee_record.aoi_id
    )

    result = {
        "found": True,
        "available": True,
        "record": {
            "pk": ee_record.pk,
            "vendor_id": ee_record.vendor_id,
            "entity_id": ee_record.entity_id,
            "aoi_id": aoi_id_val,
        },
        "aoi_name": None,
    }

    # Resolve AOI name
    if aoi_id_val:
        AreaOfInterest, has_aoi = get_optional_model("AreaOfInterest")
        if has_aoi:
            try:
                aoi = AreaOfInterest.objects.get(id=aoi_id_val)
                result["aoi_name"] = aoi.name
            except AreaOfInterest.DoesNotExist:
                result["aoi_name"] = "[NOT FOUND]"

    return result


def _resolve_gegd(etl_record, GEOINTDiscovery) -> dict:
    """Search GEGD by legacy_id = ETL.id (only for GEGD-sourced ETL)."""
    if etl_record and etl_record.table_name == "GEGD":
        rec = GEOINTDiscovery.objects.filter(
            legacy_id=str(etl_record.id)
        ).first()
        if rec:
            return {
                "found": True,
                "available": True,
                "record": {
                    "id": rec.id,
                    "legacy_id": rec.legacy_id,
                },
            }
    return {"found": False, "available": True}


def _resolve_mgp(etl_record, MaxarGeospatialPlatform) -> dict:
    """Search MGP by id = ETL.id (only for MGP-sourced ETL)."""
    if etl_record and etl_record.table_name == "MGP":
        try:
            mgp_id = int(etl_record.id)
            rec = MaxarGeospatialPlatform.objects.filter(
                id=mgp_id
            ).first()
            if rec:
                return {
                    "found": True,
                    "available": True,
                    "record": {
                        "id": rec.id,
                        "platform": rec.platform,
                    },
                }
        except (ValueError, TypeError):
            pass
    return {"found": False, "available": True}
