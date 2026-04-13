from django.utils import timezone

from animal.models import ImageryLoadRequest


def create_or_mark_processing(
    chain_id,
    project,
    requested_by_user=None,
    requested_by_username=None,
    request_group_id=None,
    aoi_name=None,
    catalog_ids=None,
):
    """Create or refresh a project imagery request as processing."""
    username = requested_by_username or (requested_by_user.username if requested_by_user else "")
    request_obj, _ = ImageryLoadRequest.objects.update_or_create(
        chain_id=chain_id,
        defaults={
            "project": project,
            "requested_by_user": requested_by_user,
            "requested_by_username": username,
            "request_group_id": request_group_id or chain_id,
            "aoi_name": (aoi_name or "")[:100],
            "catalog_ids": (catalog_ids or "")[:500],
            "status": ImageryLoadRequest.STATUS_PROCESSING,
            "last_status_update_at": timezone.now(),
            "error_summary": "",
        },
    )
    return request_obj


def mark_loaded(chain_id):
    """Mark a request as loaded once upload completes successfully."""
    return ImageryLoadRequest.objects.filter(chain_id=chain_id).update(
        status=ImageryLoadRequest.STATUS_LOADED,
        last_status_update_at=timezone.now(),
        error_summary="",
    )


def mark_failed(chain_id, error_summary):
    """Mark a request as failed unless it is already loaded."""
    return ImageryLoadRequest.objects.filter(chain_id=chain_id).exclude(
        status=ImageryLoadRequest.STATUS_LOADED
    ).update(
        status=ImageryLoadRequest.STATUS_FAILED,
        last_status_update_at=timezone.now(),
        error_summary=(error_summary or "")[:500],
    )
