"""
POI orphan diagnosis and repair — NULL catalog_id resolution.

Diagnoses POIs lacking catalog_id and repairs via POI-to-POI matching.
Returns structured results for CLI formatting by poi.py repair.
"""
# -----------------------------------------------------------------------
# ----- poi_repair.py ---------------------------------------------------
# -----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Extracted from poi.py _repair_diagnose and _repair_execute
#              (GAIFAGP-573). Diagnosis and POI-to-POI repair with
#              structured return types.
#    tickets:  GAIFAGP-573 (poi.py consolidation)
#              GAIFAGP-447 (clean up orphaned POIs with NULL catalog_id)
#
# -----------------------------------------------------------------------

import logging
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from animal.models import ExtractTransformLoad, PointsOfInterest

from animal.utils.model_helpers import get_optional_model

logger = logging.getLogger(__name__)


def diagnose_null_catalog_ids() -> dict:
    """
    Diagnose POIs with NULL catalog_id.

    Groups by project, counts annotations/adjudication, checks ETL
    resolution paths.

    Returns:
        dict with keys:
          - total_null: int
          - all_vendor_ids: set[str]
          - resolvable: int — vendor_ids with ETL match
          - unresolvable: int — vendor_ids without ETL match
          - total_annotated: int
          - total_adjudicated: int
          - total_reviewed: int
          - project_stats: list[dict] — per-project breakdown
          - vendor_resolution: dict[str, tuple|None] — ETL resolution map
          - sample_records: list[dict] — first 5 per project, max 3 projects
    """
    null_catalog_pois = PointsOfInterest.objects.filter(
        catalog_id__isnull=True,
    ).select_related(
        "project", "final_classification", "final_species", "final_confidence",
    )

    total_null = null_catalog_pois.count()
    if total_null == 0:
        return {"total_null": 0}

    Annotations, has_annotations = get_optional_model("Annotations")

    # Build ETL lookups
    etl_by_vendor = {}
    etl_by_id = {}
    for etl in ExtractTransformLoad.objects.values("id", "vendor_id", "table_name"):
        etl_id = str(etl["id"]) if etl["id"] else None
        vendor_id = str(etl["vendor_id"]) if etl["vendor_id"] else None
        table_name = etl["table_name"] or "Unknown"
        if etl_id:
            etl_by_id[etl_id] = table_name
        if vendor_id:
            etl_by_vendor[vendor_id] = (etl_id, table_name)

    # Group by project
    by_project = defaultdict(list)
    for poi in null_catalog_pois:
        project_label = poi.project.label if poi.project else "(No Project)"
        project_id = poi.project_id if poi.project_id else 0
        by_project[(project_id, project_label)].append(poi)

    sorted_projects = sorted(
        by_project.items(), key=lambda x: len(x[1]), reverse=True,
    )

    project_stats = []
    all_vendor_ids = set()
    vendor_resolution = {}

    for (project_id, project_label), pois in sorted_projects:
        poi_ids = [p.id for p in pois]

        annotated_count = 0
        if has_annotations:
            annotated_count = (
                Annotations.objects.filter(poi_id__in=poi_ids)
                .values("poi_id").distinct().count()
            )

        adjudicated_count = sum(
            1 for p in pois
            if p.final_classification or p.final_species or p.final_confidence
        )
        reviewed_count = sum(1 for p in pois if p.final_review_date)

        project_vendors = set()
        for p in pois:
            if p.vendor_id:
                vid = str(p.vendor_id)
                all_vendor_ids.add(vid)
                project_vendors.add(vid)
                if vid not in vendor_resolution:
                    if vid in etl_by_vendor:
                        etl_id, table_name = etl_by_vendor[vid]
                        vendor_resolution[vid] = ("vendor_id", etl_id, table_name)
                    elif vid in etl_by_id:
                        vendor_resolution[vid] = ("id", vid, etl_by_id[vid])
                    else:
                        vendor_resolution[vid] = None

        project_stats.append({
            "project_id": project_id,
            "project_label": project_label,
            "poi_count": len(pois),
            "annotated_count": annotated_count,
            "adjudicated_count": adjudicated_count,
            "reviewed_count": reviewed_count,
            "vendor_count": len(project_vendors),
        })

    # Sample records
    sample_records = []
    for (_, project_label), pois in sorted_projects[:3]:
        for poi in pois[:5]:
            sample_records.append({
                "id": poi.id,
                "project": project_label,
                "vendor_id": poi.vendor_id,
                "reviewed": poi.final_review_date,
            })

    resolvable = sum(1 for v in vendor_resolution.values() if v is not None)
    unresolvable = sum(1 for v in vendor_resolution.values() if v is None)

    return {
        "total_null": total_null,
        "all_vendor_ids": all_vendor_ids,
        "resolvable": resolvable,
        "unresolvable": unresolvable,
        "total_annotated": sum(s["annotated_count"] for s in project_stats),
        "total_adjudicated": sum(s["adjudicated_count"] for s in project_stats),
        "total_reviewed": sum(s["reviewed_count"] for s in project_stats),
        "project_stats": project_stats,
        "vendor_resolution": vendor_resolution,
        "sample_records": sample_records,
    }


def repair_orphan_pois(batch_size=1000) -> dict:
    """
    Repair orphan POIs via POI-to-POI matching.

    Strategy:
      1. Find POIs with NULL catalog_id but valid vendor_id
      2. Look up other POIs with matching vendor_id that HAVE catalog_id
      3. Copy catalog_id from matched POI to orphan
      4. Fallback: match by order_id portion of vendor_id

    Does NOT use ETL lookup (per GAIFAGP-424 findings).

    Args:
        batch_size: Records per bulk_update batch.

    Returns:
        dict with keys:
          - orphan_count: int
          - matched_by_vendor_id: int
          - matched_by_order_id: int
          - no_match: int
          - updated: int — records actually written
          - remaining_null: int — NULL catalog_id after repair
          - match_details: list[dict]
          - failure_no_vendor: list[int] — POI IDs with no vendor_id
          - failure_no_match: list[tuple] — (poi_id, vendor_id)
          - timestamp: str
    """
    orphan_pois = PointsOfInterest.objects.filter(catalog_id__isnull=True)
    orphan_count = orphan_pois.count()

    if orphan_count == 0:
        return {"orphan_count": 0, "updated": 0}

    # Build lookup from POIs that HAVE catalog_id
    valid_pois = PointsOfInterest.objects.filter(
        catalog_id__isnull=False, vendor_id__isnull=False,
    ).values_list("vendor_id", "catalog_id")

    vendor_to_catalog = {}
    for vendor_id, catalog_id in valid_pois:
        if vendor_id and catalog_id:
            vendor_to_catalog[str(vendor_id)] = str(catalog_id)

    # Order ID fallback lookup
    order_to_catalog = {}
    for vendor_id, catalog_id in vendor_to_catalog.items():
        parts = vendor_id.split("-")
        if len(parts) >= 3:
            order_part = "-".join(parts[2:])
            order_to_catalog[order_part] = catalog_id

    stats = {"matched_by_vendor_id": 0, "matched_by_order_id": 0, "no_match": 0}
    failure_no_vendor = []
    failure_no_match = []
    pois_to_update = []
    match_details = []

    for poi in orphan_pois.only("id", "vendor_id").iterator(chunk_size=batch_size):
        poi_vendor_id = str(poi.vendor_id) if poi.vendor_id else None

        if not poi_vendor_id:
            stats["no_match"] += 1
            failure_no_vendor.append(poi.id)
            continue

        if poi_vendor_id in vendor_to_catalog:
            catalog_id = vendor_to_catalog[poi_vendor_id]
            poi.catalog_id = catalog_id
            pois_to_update.append(poi)
            stats["matched_by_vendor_id"] += 1
            match_details.append({
                "poi_id": poi.id, "vendor_id": poi_vendor_id,
                "catalog_id": catalog_id, "match_type": "exact_vendor_id",
            })
            continue

        parts = poi_vendor_id.split("-")
        if len(parts) >= 3:
            order_part = "-".join(parts[2:])
            if order_part in order_to_catalog:
                catalog_id = order_to_catalog[order_part]
                poi.catalog_id = catalog_id
                pois_to_update.append(poi)
                stats["matched_by_order_id"] += 1
                match_details.append({
                    "poi_id": poi.id, "vendor_id": poi_vendor_id,
                    "catalog_id": catalog_id, "match_type": "order_id",
                })
                continue

        stats["no_match"] += 1
        failure_no_match.append((poi.id, poi_vendor_id))

    # Execute bulk update
    updated = 0
    if pois_to_update:
        with transaction.atomic():
            for i in range(0, len(pois_to_update), batch_size):
                batch = pois_to_update[i:i + batch_size]
                PointsOfInterest.objects.bulk_update(
                    batch, fields=["catalog_id"], batch_size=batch_size,
                )
                updated += len(batch)

    remaining = PointsOfInterest.objects.filter(catalog_id__isnull=True).count()

    return {
        "orphan_count": orphan_count,
        "matched_by_vendor_id": stats["matched_by_vendor_id"],
        "matched_by_order_id": stats["matched_by_order_id"],
        "no_match": stats["no_match"],
        "updated": updated,
        "remaining_null": remaining,
        "match_details": match_details,
        "failure_no_vendor": failure_no_vendor,
        "failure_no_match": failure_no_match,
        "lookup_size": len(vendor_to_catalog),
        "order_patterns": len(order_to_catalog),
        "timestamp": timezone.now().isoformat(),
    }
