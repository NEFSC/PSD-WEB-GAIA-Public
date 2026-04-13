"""
POI deletion — cascade preview and atomic execution.

Returns structured results for CLI formatting by poi.py delete.
"""
# -----------------------------------------------------------------------
# ----- poi_deletion.py -------------------------------------------------
# -----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Extracted from poi.py _action_delete (GAIFAGP-573).
#              Cascade preview and atomic deletion with structured returns.
#    tickets:  GAIFAGP-573 (poi.py consolidation)
#
# -----------------------------------------------------------------------

import logging

from django.db import transaction
from django.utils import timezone

from animal.utils.model_helpers import get_optional_model

logger = logging.getLogger(__name__)


def preview_poi_deletion(queryset, verbose=False) -> dict:
    """
    Preview what a POI deletion would affect.

    Args:
        queryset: Filtered PointsOfInterest queryset.
        verbose: If True, include annotation detail records.

    Returns:
        dict with keys:
          - count: int — POIs to delete
          - preview_pois: list[dict] — first 20 POIs (id, catalog_id,
            vendor_id, project)
          - related_annotations: int — annotation count that would cascade
          - annotation_details: list[dict] — first 50 annotations (verbose)
    """
    count = queryset.count()

    preview_pois = []
    for poi in queryset.select_related("project")[:20]:
        preview_pois.append({
            "id": poi.id,
            "catalog_id": poi.catalog_id,
            "vendor_id": poi.vendor_id,
            "project": poi.project.label if poi.project else None,
        })

    poi_ids = list(queryset.values_list("id", flat=True))

    Annotations, has_annotations = get_optional_model("Annotations")
    related_annotations = 0
    annotation_details = []

    if has_annotations:
        related_annotations = Annotations.objects.filter(
            poi_id__in=poi_ids
        ).count()

        if verbose and related_annotations > 0:
            for ann in (
                Annotations.objects.filter(poi_id__in=poi_ids)
                .select_related(
                    "poi", "user", "classification", "confidence", "target"
                )
                .order_by("poi_id", "id")[:50]
            ):
                annotation_details.append(_extract_annotation(ann))

    return {
        "count": count,
        "preview_pois": preview_pois,
        "related_annotations": related_annotations,
        "annotation_details": annotation_details,
    }


def execute_poi_deletion(queryset) -> dict:
    """
    Execute POI deletion in an atomic transaction.

    Args:
        queryset: Filtered PointsOfInterest queryset.

    Returns:
        dict with keys:
          - deleted_count: int
          - cascade_details: dict[str, int]
          - timestamp: str (ISO format)

    Raises:
        Exception: if deletion fails (transaction rolls back).
    """
    with transaction.atomic():
        deleted_count, details = queryset.delete()

    return {
        "deleted_count": deleted_count,
        "cascade_details": details or {},
        "timestamp": timezone.now().isoformat(),
    }


def _extract_annotation(ann) -> dict:
    """Extract display fields from an Annotation instance."""
    annotator = "Unknown"
    if hasattr(ann, "user") and ann.user:
        annotator = getattr(ann.user, "username", str(ann.user))[:16]
    elif hasattr(ann, "user_id") and ann.user_id:
        annotator = f"user_{ann.user_id}"[:16]

    target = None
    if hasattr(ann, "target") and ann.target:
        target = getattr(ann.target, "name", str(ann.target))[:20]

    classification = None
    if hasattr(ann, "classification") and ann.classification:
        classification = getattr(
            ann.classification, "name", str(ann.classification)
        )[:12]

    confidence = None
    if hasattr(ann, "confidence") and ann.confidence:
        confidence = getattr(
            ann.confidence, "name", str(ann.confidence)
        )[:12]

    ann_date = None
    for date_field in [
        "created_at", "annotation_date", "date", "created", "timestamp",
    ]:
        if hasattr(ann, date_field):
            date_val = getattr(ann, date_field)
            if date_val:
                ann_date = str(date_val)[:12]
                break

    return {
        "id": ann.id,
        "poi_id": ann.poi_id,
        "annotator": annotator,
        "target": target,
        "classification": classification,
        "confidence": confidence,
        "date": ann_date,
    }
