"""Views for uploading POI GeoJSON files into a project."""

import base64
import logging

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import get_object_or_404, redirect, render

from ..forms import LoadPointsForm
from ..models import Fishnet, PointsOfInterest, Project
from ..utils.poi_loader import (
    decode_geojson_payload,
    extract_preview_points,
    load_pois_from_geojson_upload,
)
from ..utils.poi_utils import (
    normalize_vendor_match_key,
    parse_vendor_id_from_geojson_filename,
    parse_vendor_id_from_geojson_payload,
)

logger = logging.getLogger(__name__)


def _build_uploaded_file(file_name, file_bytes):
    """Construct a fresh upload object from raw bytes for repeat parsing."""
    return SimpleUploadedFile(
        file_name,
        file_bytes,
        content_type="application/geo+json",
    )


def _preview_session_key(project_id):
    """Build session key for pending preview payload."""
    return f"load_points_preview_{project_id}"


def _find_cog_blob(vendor_id):
    """Find COG blob for a vendor ID. Returns blob path or None."""
    try:
        from .annotation_views import check_cog_existence

        blob = check_cog_existence(vendor_id, directory='cogs/')
        if isinstance(blob, tuple):
            return None
        return blob or None
    except Exception as exc:
        logger.warning("COG lookup failed for vendor_id '%s': %s", vendor_id, exc)
        return None


def _normalize_cog_request_path(cog_blob):
    """Normalize blob path for /cogs/<path>/ endpoint usage.

    The endpoint already prefixes with "cogs/" when needed, so we pass paths
    relative to that prefix.
    """
    if not cog_blob:
        return ""

    normalized = str(cog_blob).strip().lstrip("/")
    if normalized.lower().startswith("cogs/"):
        return normalized[5:]
    return normalized


def _extract_vendor_id_from_blob_name(blob_name):
    """Extract vendor_id candidate from a COG blob path."""
    file_name = str(blob_name or "").strip().rsplit("/", 1)[-1]
    lower = file_name.lower()
    if not lower.endswith(".tif") and not lower.endswith(".tiff"):
        return ""

    try:
        return parse_vendor_id_from_geojson_filename(file_name)
    except ValueError:
        return ""


def _list_cog_vendor_ids():
    """List vendor IDs that have COG blobs in Azure storage."""
    account_name = getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", None)
    account_key = getattr(settings, "AZURE_STORAGE_ACCOUNT_KEY", None)
    container_name = getattr(settings, "AZURE_CONTAINER_NAME", None)
    if not all([account_name, account_key, container_name]):
        return []

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        logger.warning("azure-storage-blob package is not available; vendor list dropdown will be empty")
        return []

    try:
        conn_str = (
            f"DefaultEndpointsProtocol=https;AccountName={account_name};"
            f"AccountKey={account_key};EndpointSuffix=core.windows.net"
        )
        service = BlobServiceClient.from_connection_string(conn_str)
        container = service.get_container_client(container_name)

        vendor_ids = []
        for blob in container.list_blobs(name_starts_with="cogs/"):
            vendor_id = _extract_vendor_id_from_blob_name(blob.name)
            if vendor_id:
                vendor_ids.append(vendor_id)

        return _unique_preserve_order(vendor_ids)
    except Exception as exc:
        logger.warning("Failed to list COG vendor IDs from Azure: %s", exc)
        return []


def _get_project_candidate_vendor_ids(project_id):
    """Get project-scoped vendor IDs from fishnet and POI tables."""
    fishnet_vendor_ids = list(
        Fishnet.objects.filter(project_id=project_id)
        .exclude(vendor_id__isnull=True)
        .exclude(vendor_id="")
        .values_list("vendor_id", flat=True)
        .distinct()
        .order_by("vendor_id")
    )
    poi_vendor_ids = list(
        PointsOfInterest.objects.filter(project_id=project_id)
        .exclude(vendor_id__isnull=True)
        .exclude(vendor_id="")
        .values_list("vendor_id", flat=True)
        .distinct()
        .order_by("vendor_id")
    )
    return _unique_preserve_order(fishnet_vendor_ids + poi_vendor_ids)


def _get_available_cog_vendor_ids(project_id):
    """Get project vendor IDs that currently have COGs available."""
    cache_key = f"load_points_cog_vendor_ids_{project_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    candidate_vendor_ids = _get_project_candidate_vendor_ids(project_id)
    if not candidate_vendor_ids:
        cache.set(cache_key, [], timeout=600)
        return []

    cog_vendor_ids = _list_cog_vendor_ids()
    if not cog_vendor_ids:
        cache.set(cache_key, [], timeout=600)
        return []

    available_keys = {normalize_vendor_match_key(vendor_id) for vendor_id in cog_vendor_ids}
    available_vendor_ids = [
        vendor_id
        for vendor_id in candidate_vendor_ids
        if normalize_vendor_match_key(vendor_id) in available_keys
    ]

    cache.set(cache_key, available_vendor_ids, timeout=600)
    return available_vendor_ids


def _empty_ingestion_result(dry_run=False):
    """Create an empty POI ingestion result payload."""
    return {
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


def _merge_ingestion_results(target, source):
    """Merge one ingestion result dictionary into another."""
    for key in ["loaded", "skipped", "duplicates", "replaced", "total_features"]:
        target[key] = int(target.get(key, 0)) + int(source.get(key, 0))

    target.setdefault("errors", []).extend(source.get("errors", []))
    target.setdefault("etl_warnings", []).extend(source.get("etl_warnings", []))

    if not target.get("project_label") and source.get("project_label"):
        target["project_label"] = source.get("project_label")

    return target


def _unique_preserve_order(values):
    """Return unique values preserving first-seen order."""
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _process_result(request, result, project_id):
    """Emit user messages and invalidate caches based on ingestion result."""
    if result["loaded"] > 0:
        from .annotation_views import (
            invalidate_deduplication_cache,
            invalidate_multiview_vendor_cache,
        )

        invalidate_multiview_vendor_cache(project_id)
        invalidate_deduplication_cache(project_id)
        messages.success(
            request,
            (
                f"Loaded {result['loaded']} point(s). "
                f"Skipped {result['skipped']} point(s), including "
                f"{result['duplicates']} duplicate sample_idx values."
            ),
        )
    else:
        messages.warning(
            request,
            (
                "No POIs were loaded. "
                f"Skipped {result['skipped']} point(s), including "
                f"{result['duplicates']} duplicate sample_idx values."
            ),
        )

    for warning in result.get("etl_warnings", []):
        messages.warning(request, warning)

    if result.get("errors"):
        messages.error(
            request,
            f"{len(result['errors'])} feature(s) failed validation. "
            "Review file format and required point properties.",
        )


def load_points_page(request, project_id):
    """Preview and load point features from GeoJSON into POIs."""
    project = get_object_or_404(Project, id=project_id)
    session_key = _preview_session_key(project_id)
    available_cog_vendor_ids = _get_available_cog_vendor_ids(project_id)

    if request.method == "POST":
        action = (request.POST.get("action") or "preview_points").strip().lower()

        if action == "reset_preview":
            request.session.pop(session_key, None)
            messages.info(request, "Preview cleared.")
            return redirect("load_points_page", project_id=project_id)

        if action == "load_points":
            pending = request.session.get(session_key)
            if not pending:
                messages.warning(request, "No preview is available. Preview points before loading.")
                return redirect("load_points_page", project_id=project_id)

            if pending.get("manual_override"):
                messages.warning(
                    request,
                    (
                        "Manual vendor override is active. "
                        f"Using selected vendor ID '{pending.get('selected_vendor_id')}' "
                        f"instead of parsed vendor ID '{pending.get('parsed_vendor_id')}'."
                    ),
                )

            uploads = pending.get("uploads") or []
            if not uploads:
                messages.error(request, "Preview data is missing uploaded files. Please preview again.")
                request.session.pop(session_key, None)
                return redirect("load_points_page", project_id=project_id)

            result = _empty_ingestion_result(dry_run=False)
            for upload in uploads:
                vendor_id = (
                    upload.get("vendor_id")
                    or upload.get("selected_vendor_id")
                    or upload.get("parsed_vendor_id")
                )
                if not vendor_id:
                    result["errors"].append(
                        f"{upload.get('file_name', 'unknown.geojson')}: missing vendor ID"
                    )
                    continue

                try:
                    file_b64 = upload.get("file_b64", "")
                    file_name = upload.get("file_name") or "pending.geojson"
                    file_bytes = base64.b64decode(file_b64.encode("utf-8"))

                    per_file_result = load_pois_from_geojson_upload(
                        uploaded_file=_build_uploaded_file(file_name, file_bytes),
                        project_identifier=str(project_id),
                        id_type="vendor",
                        target_id=vendor_id,
                        dry_run=False,
                    )
                    _merge_ingestion_results(result, per_file_result)
                except Exception as exc:
                    logger.exception("Load points commit failed for %s", upload.get("file_name"))
                    result["errors"].append(
                        f"{upload.get('file_name', 'unknown.geojson')}: {exc}"
                    )

            _process_result(request, result, project_id)
            request.session.pop(session_key, None)

            return redirect("load_points_page", project_id=project_id)

        form = LoadPointsForm(request.POST, request.FILES, vendor_choices=available_cog_vendor_ids)
        if form.is_valid():
            try:
                uploaded_files = form.cleaned_data["geojson_files"]
                selected_vendor_id = (form.cleaned_data.get("vendor_id_select") or "").strip()
                selected_vendor_id = selected_vendor_id or ""

                preview_points = []
                preview_result = _empty_ingestion_result(dry_run=True)
                preview_uploads = []
                vendor_ids = []
                preview_cog_paths = []
                missing_cog_vendor_ids = []
                pending_uploads = []

                for uploaded_file in uploaded_files:
                    uploaded_file.seek(0)
                    file_bytes = uploaded_file.read()
                    if isinstance(file_bytes, str):
                        file_bytes = file_bytes.encode("utf-8")
                    file_name = uploaded_file.name

                    payload = decode_geojson_payload(file_bytes)
                    parsed_vendor_id = parse_vendor_id_from_geojson_payload(payload)
                    vendor_ids.append(parsed_vendor_id)

                    preview_points.extend(extract_preview_points(file_bytes))

                    pending_uploads.append(
                        {
                            "file_name": file_name,
                            "file_bytes": file_bytes,
                            "parsed_vendor_id": parsed_vendor_id,
                        }
                    )

                unique_vendor_ids = _unique_preserve_order(vendor_ids)
                if len(unique_vendor_ids) > 1:
                    form.add_error(
                        "geojson_files",
                        (
                            "Uploaded files resolve to multiple vendor IDs: "
                            + ", ".join(unique_vendor_ids)
                            + ". Please upload one vendor at a time."
                        ),
                    )
                    return render(
                        request,
                        "load_points_page.html",
                        {
                            "project": project,
                            "form": form,
                            "available_cog_vendor_count": len(available_cog_vendor_ids),
                        },
                    )

                parsed_vendor_id = unique_vendor_ids[0] if unique_vendor_ids else ""
                available_vendor_lookup = {
                    normalize_vendor_match_key(vendor_id): vendor_id
                    for vendor_id in available_cog_vendor_ids
                }
                auto_selected_vendor_id = ""
                if parsed_vendor_id and not selected_vendor_id:
                    auto_selected_vendor_id = available_vendor_lookup.get(
                        normalize_vendor_match_key(parsed_vendor_id),
                        "",
                    )

                effective_vendor_id = selected_vendor_id or auto_selected_vendor_id or parsed_vendor_id
                if not effective_vendor_id:
                    form.add_error(
                        "vendor_id_select",
                        "Unable to resolve vendor ID from upload. Select one manually and preview again.",
                    )
                    return render(
                        request,
                        "load_points_page.html",
                        {
                            "project": project,
                            "form": form,
                            "available_cog_vendor_count": len(available_cog_vendor_ids),
                        },
                    )

                manual_override = bool(
                    selected_vendor_id
                    and parsed_vendor_id
                    and normalize_vendor_match_key(selected_vendor_id) != normalize_vendor_match_key(parsed_vendor_id)
                )
                if auto_selected_vendor_id:
                    messages.info(
                        request,
                        f"Auto-selected vendor ID '{auto_selected_vendor_id}' from available COG options.",
                    )

                for pending_upload in pending_uploads:
                    file_name = pending_upload["file_name"]
                    file_bytes = pending_upload["file_bytes"]
                    parsed_file_vendor_id = pending_upload["parsed_vendor_id"]

                    per_file_result = load_pois_from_geojson_upload(
                        uploaded_file=_build_uploaded_file(file_name, file_bytes),
                        project_identifier=str(project_id),
                        id_type="vendor",
                        target_id=effective_vendor_id,
                        dry_run=True,
                    )
                    _merge_ingestion_results(preview_result, per_file_result)

                    cog_blob = _find_cog_blob(effective_vendor_id)
                    normalized_cog_path = _normalize_cog_request_path(cog_blob)
                    if normalized_cog_path:
                        preview_cog_paths.append(normalized_cog_path)
                    else:
                        missing_cog_vendor_ids.append(effective_vendor_id)

                    preview_uploads.append(
                        {
                            "file_b64": base64.b64encode(file_bytes).decode("utf-8"),
                            "file_name": file_name,
                            "parsed_vendor_id": parsed_file_vendor_id,
                            "selected_vendor_id": selected_vendor_id or auto_selected_vendor_id,
                            "vendor_id": effective_vendor_id,
                        }
                    )

                unique_cog_paths = _unique_preserve_order(preview_cog_paths)
                unique_missing_cog_vendor_ids = _unique_preserve_order(missing_cog_vendor_ids)

                request.session[session_key] = {
                    "uploads": preview_uploads,
                    "preview_points": preview_points,
                    "preview_result": preview_result,
                    "vendor_ids": [effective_vendor_id],
                    "preview_cog_paths": unique_cog_paths,
                    "missing_cog_vendor_ids": unique_missing_cog_vendor_ids,
                    "file_count": len(uploaded_files),
                    "parsed_vendor_id": parsed_vendor_id,
                    "selected_vendor_id": selected_vendor_id or auto_selected_vendor_id,
                    "effective_vendor_id": effective_vendor_id,
                    "auto_selected": bool(auto_selected_vendor_id and not selected_vendor_id),
                    "manual_override": manual_override,
                }

                preview_form = LoadPointsForm(
                    vendor_choices=available_cog_vendor_ids,
                    initial={
                        "vendor_id_select": selected_vendor_id or auto_selected_vendor_id,
                    },
                )

                return render(
                    request,
                    "load_points_page.html",
                    {
                        "project": project,
                        "form": preview_form,
                        "show_preview": True,
                        "preview_file_count": len(uploaded_files),
                        "preview_points": preview_points,
                        "preview_total_points": len(preview_points),
                        "preview_result": preview_result,
                        "preview_vendor_ids": [effective_vendor_id],
                        "preview_parsed_vendor_id": parsed_vendor_id,
                        "preview_selected_vendor_id": selected_vendor_id or auto_selected_vendor_id,
                        "preview_auto_selected": bool(auto_selected_vendor_id and not selected_vendor_id),
                        "preview_manual_override": manual_override,
                        "preview_cog_paths": unique_cog_paths,
                        "missing_cog_vendor_ids": unique_missing_cog_vendor_ids,
                        "cog_warning": bool(unique_missing_cog_vendor_ids),
                        "available_cog_vendor_count": len(available_cog_vendor_ids),
                    },
                )
            except ValueError as exc:
                form.add_error("geojson_files", str(exc))
            except Exception as exc:
                logger.exception("Load points preview failed")
                messages.error(request, f"Failed to preview points: {exc}")
    else:
        form = LoadPointsForm(vendor_choices=available_cog_vendor_ids)

    # Do not retain preview state across page refreshes. A plain GET should show
    # a clean upload form with no previous file/preview data.
    request.session.pop(session_key, None)

    return render(
        request,
        "load_points_page.html",
        {
            "project": project,
            "form": form,
            "available_cog_vendor_count": len(available_cog_vendor_ids),
        },
    )
