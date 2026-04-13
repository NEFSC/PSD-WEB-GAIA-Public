"""
POI data integrity validation across the full AOI→EE→ETL→POI chain.

Returns structured results for use by both CLI (poi.py validate)
and test suite (GAIFAGP-542).
"""
# -----------------------------------------------------------------------
# ----- poi_validation.py -----------------------------------------------
# -----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Extracted from poi.py _action_validate (GAIFAGP-573).
#              Validates POI data integrity with structured return types
#              callable from CLI and test suite.
#    tickets:  GAIFAGP-573 (poi.py consolidation)
#              GAIFAGP-542 (test tier — unblocked by this extraction)
#
# -----------------------------------------------------------------------

import logging

from animal.models import ExtractTransformLoad, PointsOfInterest
from animal.utils.model_helpers import get_optional_model

logger = logging.getLogger(__name__)


def validate_poi_chain() -> dict:
    """
    Validate POI data integrity across the full chain.

    Checks:
      1. POIs with NULL date_image_taken
      2. POIs without any identifier (catalog_id, vendor_id, entity_id)
      3. POIs without geometry
      4. POI → ETL linkage (catalog_id in ETL.id)
      5. Date consistency (POI.date_image_taken vs ETL.date, sampled)
      6. ETL → Source catalog linkage (ETL.vendor_id in EE.vendor_id)
      7. Source catalog → AOI linkage (EE.aoi_id references valid AOI)

    Returns:
        dict with keys:
          - issues: list[str] — hard errors
          - warnings: list[str] — soft warnings
          - chain_counts: dict[str, int] — record counts per entity
    """
    issues = []
    warnings = []
    chain_counts = {}

    # 1. POIs with NULL date_image_taken
    null_dates = (
        PointsOfInterest.objects.filter(date_image_taken__isnull=True).count()
    )
    if null_dates > 0:
        warnings.append(f"{null_dates} POIs with NULL date_image_taken")

    # 2. POIs without any identifier
    no_ids = PointsOfInterest.objects.filter(
        catalog_id__isnull=True,
        vendor_id__isnull=True,
        entity_id__isnull=True,
    ).count()
    if no_ids > 0:
        issues.append(
            f"{no_ids} POIs with no catalog_id, vendor_id, or entity_id"
        )

    # 3. POIs without geometry
    no_geom = PointsOfInterest.objects.filter(point__isnull=True).count()
    if no_geom > 0:
        issues.append(f"{no_geom} POIs with NULL point geometry")

    # 4. POI → ETL linkage
    pois_with_catalog = set(
        str(cid) for cid in
        PointsOfInterest.objects.filter(catalog_id__isnull=False)
        .values_list("catalog_id", flat=True).distinct()
    )

    etl_ids = set(
        str(eid) for eid in
        ExtractTransformLoad.objects.values_list("id", flat=True)
    )

    orphaned_pois = pois_with_catalog - etl_ids
    if orphaned_pois:
        warnings.append(
            f"{len(orphaned_pois)} POIs have catalog_id not found in ETL table"
        )

    # 5. Date consistency (POI vs ETL, sampled)
    etl_dates = {
        str(row["id"]): row["date"]
        for row in ExtractTransformLoad.objects.values("id", "date")
    }

    date_mismatches = 0
    pois_with_dates = PointsOfInterest.objects.filter(
        date_image_taken__isnull=False,
        catalog_id__isnull=False,
    ).values("id", "catalog_id", "date_image_taken")[:10000]

    for poi_row in pois_with_dates:
        catalog_id = str(poi_row["catalog_id"])
        if catalog_id in etl_dates:
            etl_date = etl_dates[catalog_id]
            if etl_date and poi_row["date_image_taken"] != etl_date:
                date_mismatches += 1

    if date_mismatches > 0:
        warnings.append(
            f"{date_mismatches} POIs have date_image_taken != ETL.date (sampled)"
        )

    # 6. ETL → Source catalog linkage
    etl_vendor_ids = set(
        str(vid) for vid in
        ExtractTransformLoad.objects.filter(vendor_id__isnull=False)
        .values_list("vendor_id", flat=True).distinct()
    )

    source_vendor_ids = set()

    EarthExplorer, has_ee = get_optional_model("EarthExplorer")
    if has_ee:
        ee_vendor_ids = set(
            str(vid) for vid in
            EarthExplorer.objects.filter(vendor_id__isnull=False)
            .values_list("vendor_id", flat=True).distinct()
        )
        source_vendor_ids.update(ee_vendor_ids)

    if source_vendor_ids:
        orphaned_etl = etl_vendor_ids - source_vendor_ids
        if orphaned_etl:
            warnings.append(
                f"{len(orphaned_etl)} ETL vendor_ids not found in EarthExplorer"
            )

    # 7. Source catalog → AOI linkage
    AreaOfInterest, has_aoi = get_optional_model("AreaOfInterest")
    if has_aoi and has_ee:
        aoi_ids = set(AreaOfInterest.objects.values_list("id", flat=True))

        ee_orphaned_aoi = EarthExplorer.objects.filter(
            aoi_id__isnull=False,
        ).exclude(aoi_id__in=aoi_ids).count()

        if ee_orphaned_aoi > 0:
            issues.append(
                f"{ee_orphaned_aoi} EarthExplorer records reference "
                f"non-existent AOIs"
            )

    # Chain counts
    chain_counts["PointsOfInterest"] = PointsOfInterest.objects.count()
    chain_counts["ExtractTransformLoad"] = ExtractTransformLoad.objects.count()

    if has_aoi:
        chain_counts["AreaOfInterest"] = AreaOfInterest.objects.count()
    if has_ee:
        chain_counts["EarthExplorer"] = EarthExplorer.objects.count()

    GEOINTDiscovery, has_gegd = get_optional_model("GEOINTDiscovery")
    if has_gegd:
        chain_counts["GEOINTDiscovery"] = GEOINTDiscovery.objects.count()

    MaxarGeospatialPlatform, has_mgp = get_optional_model(
        "MaxarGeospatialPlatform"
    )
    if has_mgp:
        chain_counts["MaxarGeospatialPlatform"] = (
            MaxarGeospatialPlatform.objects.count()
        )

    return {
        "issues": issues,
        "warnings": warnings,
        "chain_counts": chain_counts,
    }
