"""
POI field resolution utility for GAIA data governance.

Backfills sensor and entity_id on PointsOfInterest records
using ETL joins and USGS API. Pure business logic — no
command infrastructure. All public functions return
BackfillResult; the calling command handles output.
"""
# ----------------------------------------------------------------------
# ----- poi_backfill.py ------------------------------------------------
# ----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  POI field resolution utility. Backfills sensor and
#              entity_id on PointsOfInterest records using ETL joins
#              and USGS API. Pure business logic — no command
#              infrastructure.
#
#    tickets:  GAIFAGP-467 (backfill entity_id)
#              GAIFAGP-468 (backfill sensor)
#              GAIFAGP-466 (Data Governance epic)
#              GAIFAGP-482 (implementation)
#              GAIFAGP-483 (peer review)
#              GAIFAGP-484 (USGS API fix — auto-discovery)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - Catalog prefix determines sensor (SENSOR_MAP)
#      - ETL table is authoritative for entity_id
#        (1:1 join on catalog_id)
#      - USGS scene-search is fallback for catalog_ids
#        not in ETL
#      - Join key: POI.catalog_id <-> ETL.id
#        (both string identifiers)
#      - USGS metadata field IDs are discovered at runtime
#        via dataset-fields API (GAIFAGP-484), not
#        hardcoded
#
#    RETRY POLICY (explicit decision, IV&V Feb 2026):
#      - Current workload: 5 catalog_ids → 5 API calls.
#      - At ≤50 requests: no retry on transient failures.
#        Failures are logged as structured errors and
#        reported to the operator. Re-run resolves
#        intermittent issues.
#      - At >50 requests: add exponential backoff with
#        jitter (deferred to GAIFAGP-452 ingestion
#        expansion). Threshold chosen because USGS
#        rate-limits are undocumented and empirically
#        forgiving below 50 requests/session.
#
#    ACCEPTED-RISK REMEDIATIONS:
#      - GAIFAGP-486: Eliminate None-return failure
#        paths in api_utils.py download pipeline.
#      - GAIFAGP-487: Decompose poi.py into thin-wrapper
#        command + poi_ops utility module per Section 3.8.
#
#    IMPORTANT:
#      - Do NOT import entity_validation.py — it's
#        hardcoded to WV03 dataset.
#      - This module handles dataset selection dynamically
#        via DATASET_MAP.
#      - All public functions return BackfillResult. No
#        self.stdout, no CommandError, no CLI formatting.
#        The calling command handles output.
#
# ----------------------------------------------------------------------

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from requests.exceptions import RequestException, Timeout

from animal.models import (
    ExtractTransformLoad,
    PointsOfInterest,
)
from animal.utils.api_utils import (
    ee_login,
    get_catalog_field_id,
)
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)


# --- Constants --------------------------------------------------------

# Catalog prefix -> sensor name.
# Governing docs:
#   - CONOPS Appendix A §A.2.1 (catalog_id structure)
#   - CONOPS Appendix B (identifier relationships)
#   - DL-010 (identifier field decisions)
# Verified against diagnostic data (Feb 9, 2026): only
# 1040 and 1030 exist. Full known Maxar prefix set
# included so future sensors resolve without code changes.
# If a prefix appears that is NOT in this map,
# backfill_sensors() will catch it and fail on --confirm.
SENSOR_MAP = {
    "1040": "WV03",  # 119,334 POIs (as of Feb 2026)
    "1030": "WV02",  # 14,363 POIs
    "1050": "GE01",  # 0 POIs — forward compatibility
    "1020": "WV01",  # 0 POIs
    "1010": "QB02",  # 0 POIs
}

# Prefixes that are KNOWN but EXCLUDED from sensor
# backfill. These are sub-band variants that require
# explicit handling per GAIFAGP-439.
# Governing doc: DL-010 (identifier field decisions).
# If encountered, backfill_sensors() reports them
# distinctly from truly unknown prefixes.
EXCLUDED_PREFIXES = {
    "104A": "WV03-SWIR",   # Short-wave infrared
    "104C": "WV03-CAVIS",  # Cloud/aerosol/vapor
}

# Sensor -> USGS dataset name for scene-search API.
DATASET_MAP = {
    "WV03": "crssp_orderable_w3",
    "WV02": "crssp_orderable_w2",
}

DEFAULT_BATCH_SIZE = 500
USGS_SCENE_SEARCH_URL = (
    "https://m2m.cr.usgs.gov"
    "/api/api/json/stable/scene-search"
)
USGS_API_TIMEOUT = 30   # seconds per request
USGS_API_SLEEP = 0.5    # seconds between API requests

# Auto-populated on first API call per dataset per
# process. Maps dataset_name -> catalog filterId.
# Avoids repeat calls to dataset-fields endpoint.
_catalog_field_cache: dict[str, str] = {}


# --- Result dataclass -------------------------------------------------

@dataclass
class BackfillResult:
    """Structured return value for all public backfill functions."""

    total_candidates: int = 0
    updated: int = 0
    already_populated: int = 0
    unresolved: int = 0
    errors: list[str | dict[str, Any]] = field(
        default_factory=list
    )
    method: str = ""
    details: dict[str, Any] = field(
        default_factory=dict
    )


# --- Sensor resolution ------------------------------------------------

def derive_sensor(catalog_id: str) -> Optional[str]:
    """
    Derive sensor name from catalog_id prefix.

    Assumptions:
        Catalog ID prefix (first 4 chars) uniquely maps
        to a sensor per CONOPS Appendix A §A.2.1 and
        DL-010. SENSOR_MAP is the governed contract for
        this mapping.

    Args:
        catalog_id: Maxar catalog identifier
            (e.g., "104001003A5B6C00").

    Returns:
        Sensor name (e.g., "WV03") or None if prefix
        is unrecognized.
    """
    if not catalog_id or len(catalog_id) < 4:
        return None
    prefix = catalog_id[:4]
    return SENSOR_MAP.get(prefix)


def backfill_sensors(
    queryset: Optional[QuerySet] = None,
    dry_run: bool = True,
    skip_unknown: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillResult:
    """
    Populate sensor field on POIs via catalog_id prefix.

    Filters to POIs where sensor is NULL. Derives sensor
    from SENSOR_MAP. On --confirm, fails if unrecognized
    prefixes exist (unless skip_unknown).

    Prefixes in EXCLUDED_PREFIXES (104A/104C) are reported
    separately from truly unknown prefixes — they are
    known sub-band variants requiring explicit decision
    per GAIFAGP-439.

    Assumptions:
        SENSOR_MAP is the governed mapping from prefix to
        sensor (CONOPS Appendix A §A.2.1, DL-010). Atomic
        blocks are per-batch to avoid SpatiaLite locking
        (Engineering Guide §4.4).

    Args:
        queryset: Base queryset (defaults to all POIs).
            Filtered to sensor__isnull=True.
        dry_run: If True, report only. If False, write.
        skip_unknown: If True, proceed even with
            unrecognized prefixes.
        batch_size: Records per bulk_update batch.

    Returns:
        BackfillResult with details["distribution"],
        details["unknown_prefixes"], and
        details["excluded_prefixes"].

    Raises:
        ValueError: On --confirm with unrecognized
            prefixes (unless skip_unknown).
    """
    if queryset is None:
        queryset = PointsOfInterest.objects.all()

    candidates = queryset.filter(sensor__isnull=True)
    total = candidates.count()

    result = BackfillResult(
        total_candidates=total,
        method="sensor_prefix_lookup",
        details={
            "distribution": {},
            "unknown_prefixes": {},
            "excluded_prefixes": {},
        },
    )

    if total == 0:
        return result

    # Analyze distribution and collect unknowns
    distribution: dict[str, int] = {}
    unknown_prefixes: dict[str, int] = {}
    excluded_prefixes: dict[str, int] = {}
    pois_to_update = []

    for poi in candidates.only(
        "id", "catalog_id"
    ).iterator(chunk_size=batch_size):
        sensor = derive_sensor(poi.catalog_id)
        if sensor is None:
            prefix = (
                poi.catalog_id[:4]
                if poi.catalog_id
                and len(poi.catalog_id) >= 4
                else "(empty)"
            )
            if prefix in EXCLUDED_PREFIXES:
                excluded_prefixes[prefix] = (
                    excluded_prefixes.get(prefix, 0) + 1
                )
            else:
                unknown_prefixes[prefix] = (
                    unknown_prefixes.get(prefix, 0) + 1
                )
            result.unresolved += 1
            continue

        distribution[sensor] = (
            distribution.get(sensor, 0) + 1
        )
        if not dry_run:
            poi.sensor = sensor
            pois_to_update.append(poi)

    result.details["distribution"] = distribution
    result.details["unknown_prefixes"] = unknown_prefixes
    result.details["excluded_prefixes"] = (
        excluded_prefixes
    )

    # Strict failure on unrecognized prefixes during
    # confirm (excluded prefixes are a separate concern)
    if (
        unknown_prefixes
        and not dry_run
        and not skip_unknown
    ):
        prefix_report = ", ".join(
            f"{p} ({c} POIs)"
            for p, c in sorted(unknown_prefixes.items())
        )
        raise ValueError(
            f"Unrecognized catalog_id prefixes found: "
            f"{prefix_report}. Add to SENSOR_MAP or "
            f"use --skip-unknown-sensors to proceed."
        )

    if excluded_prefixes and not dry_run:
        excluded_report = ", ".join(
            f"{p}/{EXCLUDED_PREFIXES[p]} ({c} POIs)"
            for p, c in sorted(
                excluded_prefixes.items()
            )
        )
        logger.warning(
            "Excluded sub-band prefixes found",
            extra={
                "prefixes": excluded_report,
                "ref": "GAIFAGP-439",
            },
        )

    if dry_run:
        result.updated = sum(distribution.values())
        return result

    # Batch write
    updated_count = 0
    for i in range(0, len(pois_to_update), batch_size):
        batch = pois_to_update[i : i + batch_size]
        with transaction.atomic():
            PointsOfInterest.objects.bulk_update(
                batch, ["sensor"], batch_size=batch_size
            )
        updated_count += len(batch)
        logger.info(
            "Sensor backfill progress",
            extra={
                "updated": updated_count,
                "total": len(pois_to_update),
            },
        )

    result.updated = updated_count
    return result


# --- Entity ID resolution ---------------------------------------------

def backfill_entity_ids(
    queryset: Optional[QuerySet] = None,
    dry_run: bool = True,
    include_api: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillResult:
    """
    Populate entity_id on POIs via ETL join and USGS API.

    Pass 1: Joins POI.catalog_id -> ETL.id to resolve
    entity_id from ETL records.

    Pass 2 (opt-in): For remaining unresolved catalog_ids,
    queries USGS scene-search API grouped by
    sensor/dataset. Requires --include-api flag.

    Assumptions:
        ETL table has 1:1 catalog_id -> entity_id mapping
        (preprocessing_pipeline_docs.md §4.1). USGS
        credentials must be configured for Pass 2.
        Outcome classes: (1) ETL-resolved,
        (2) API-resolved, (3) excluded-prefix,
        (4) unknown-prefix, (5) IO-failure. All five
        are reported in BackfillResult.

    Args:
        queryset: Base queryset (defaults to all POIs).
            Filtered to entity_id__isnull=True.
        dry_run: If True, report only. If False, write.
        include_api: If True, run Pass 2 (USGS API).
        batch_size: Records per bulk_update batch.

    Returns:
        BackfillResult with details["pass1"] and
        optionally details["pass2"].
    """
    if queryset is None:
        queryset = PointsOfInterest.objects.all()

    candidates = queryset.filter(
        entity_id__isnull=True
    )
    total = candidates.count()

    result = BackfillResult(
        total_candidates=total,
        method=(
            "etl_join"
            + ("+usgs_api" if include_api else "")
        ),
        details={"pass1": {}, "pass2": {}},
    )

    if total == 0:
        return result

    # --- Pass 1: ETL Join ---
    pass1 = _resolve_via_etl(
        candidates, dry_run, batch_size
    )
    result.details["pass1"] = {
        "resolved": pass1["resolved"],
        "unresolved": pass1["unresolved"],
        "duplicate_warnings": pass1["duplicate_warnings"],
    }
    result.updated += pass1["resolved"]
    result.unresolved = pass1["unresolved"]

    # --- Pass 2: USGS API (opt-in) ---
    if include_api:
        if not dry_run:
            still_null = queryset.filter(
                entity_id__isnull=True
            )
        else:
            still_null = candidates

        unresolved_cids = set(
            still_null.exclude(catalog_id__isnull=True)
            .values_list("catalog_id", flat=True)
            .distinct()
        )

        # Dry run: remove IDs that Pass 1 would resolve
        if dry_run:
            etl_lookup = {
                str(k): v
                for k, v in
                ExtractTransformLoad.objects.values_list(
                    "id", "entity_id"
                )
            }
            unresolved_cids = {
                cid
                for cid in unresolved_cids
                if str(cid) not in etl_lookup
            }

        if unresolved_cids:
            pass2 = _resolve_via_api(
                unresolved_cids,
                candidates,
                dry_run,
                batch_size,
            )
            result.details["pass2"] = {
                "distinct_catalog_ids": len(
                    unresolved_cids
                ),
                "resolved": pass2["resolved"],
                "api_errors": pass2["errors"],
            }
            result.updated += pass2["resolved"]
            result.unresolved -= pass2["resolved"]
            result.errors.extend(pass2["errors"])

    return result


def _resolve_via_etl(
    candidates: QuerySet,
    dry_run: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """
    Pass 1: Resolve entity_id via POI.catalog_id -> ETL.id.

    The ETL table has 1,432 records. The join is documented
    as 1:1 (preprocessing_pipeline_docs.md §4.1). We build
    a dict for O(1) lookup.

    Assumptions:
        ETL.id stores catalog_id as string. ETL.entity_id
        is the USGS entity identifier. Relationship is 1:1
        per preprocessing_pipeline_docs.md §4.1. Duplicate
        ETL catalog_ids are logged but not expected.

    Args:
        candidates: QuerySet of POIs with null entity_id.
        dry_run: If True, count only.
        batch_size: Records per bulk_update batch.

    Returns:
        Dict with keys: resolved, unresolved,
        duplicate_warnings.
    """
    # Build ETL lookup: catalog_id (ETL.id) -> entity_id
    etl_raw = ExtractTransformLoad.objects.values_list(
        "id", "entity_id"
    )
    etl_lookup: dict[str, str] = {}
    duplicate_warnings: list[str] = []

    for etl_id, entity_id in etl_raw:
        key = str(etl_id)
        if key in etl_lookup:
            # Should not happen (1:1). Log for integrity.
            duplicate_warnings.append(
                f"Duplicate ETL catalog_id: {key} "
                f"(entity_ids: {etl_lookup[key]}, "
                f"{entity_id})"
            )
            logger.warning(
                "Duplicate ETL catalog_id detected",
                extra={"catalog_id": key},
            )
        if entity_id:
            etl_lookup[key] = str(entity_id)

    logger.info(
        "ETL lookup built",
        extra={"record_count": len(etl_lookup)},
    )

    resolved_count = 0
    unresolved_count = 0
    pois_to_update: list = []

    for poi in candidates.only(
        "id", "catalog_id"
    ).iterator(chunk_size=batch_size):
        catalog_id = (
            str(poi.catalog_id)
            if poi.catalog_id
            else None
        )

        if catalog_id and catalog_id in etl_lookup:
            if not dry_run:
                poi.entity_id = etl_lookup[catalog_id]
                pois_to_update.append(poi)
            resolved_count += 1
        else:
            unresolved_count += 1

    if not dry_run and pois_to_update:
        updated_count = 0
        for i in range(
            0, len(pois_to_update), batch_size
        ):
            batch = pois_to_update[i : i + batch_size]
            with transaction.atomic():
                PointsOfInterest.objects.bulk_update(
                    batch,
                    ["entity_id"],
                    batch_size=batch_size,
                )
            updated_count += len(batch)
            logger.info(
                "Entity ID backfill (ETL) progress",
                extra={
                    "updated": updated_count,
                    "total": len(pois_to_update),
                },
            )

    return {
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "duplicate_warnings": duplicate_warnings,
    }


def resolve_via_usgs_api(
    catalog_ids: set[str],
    session: Optional[requests.Session] = None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """
    Resolve entity_ids from USGS scene-search API.

    Groups catalog_ids by sensor (via derive_sensor) to
    select the correct USGS dataset. Uses metadataFilter
    with a runtime-discovered catalog field ID
    (GAIFAGP-484) instead of entityIds.

    Makes one scene-search request per catalog_id because
    the API returns entity_ids in USGS format (different
    from our query catalog_id), making batch response
    matching unreliable.

    One catalog_id can map to multiple USGS scenes
    (strip segments, PAN/MSI sensor modes, processing
    levels). Disambiguation uses the Vendor ID metadata
    field returned in each scene result. The resolved
    dict is keyed by vendor_id, not catalog_id.

    Assumptions:
        USGS credentials (USGS_USERNAME, USGS_TOKEN) are
        in Django settings via secrets.json. Each USGS
        scene result contains a 'Vendor ID' metadata
        field that matches a POI.vendor_id value.
        Retry policy: no automatic retry at ≤50 requests;
        transient failures logged as structured errors,
        re-run resolves intermittent issues. Backoff
        required at >50 requests (GAIFAGP-452).

    Args:
        catalog_ids: Set of catalog_id strings to resolve.
        session: Authenticated requests.Session. If None,
            creates and authenticates one.

    Returns:
        Tuple of (resolved, errors):
          resolved: dict mapping vendor_id -> entity_id
          errors: list of dicts with keys: catalog_id,
              dataset, failure_class, message

    Raises:
        ValueError: If USGS credentials not configured.
    """
    # Credential check — fail fast
    username = getattr(settings, "USGS_USERNAME", None)
    token = getattr(settings, "USGS_TOKEN", None)
    if not username or not token:
        raise ValueError(
            "USGS credentials not configured. "
            "Set USGS_USERNAME and USGS_TOKEN in "
            "settings (via secrets.json)."
        )

    # Authenticate if no session provided
    if session is None:
        session = requests.Session()
        session = ee_login(session, username, token)

    # Group catalog_ids by sensor -> dataset
    grouped: dict[str, list[str]] = {}
    skipped: list[str] = []

    for cid in catalog_ids:
        sensor = derive_sensor(cid)
        if sensor is None:
            skipped.append(cid)
            logger.warning(
                "Cannot determine sensor, skipping",
                extra={"catalog_id": cid},
            )
            continue
        dataset = DATASET_MAP.get(sensor)
        if dataset is None:
            skipped.append(cid)
            logger.warning(
                "No USGS dataset mapped for sensor",
                extra={
                    "sensor": sensor,
                    "catalog_id": cid,
                },
            )
            continue
        grouped.setdefault(dataset, []).append(cid)

    resolved: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    request_count = 0

    for dataset, cids in grouped.items():
        logger.info(
            "Starting USGS scene-search batch",
            extra={
                "dataset": dataset,
                "catalog_id_count": len(cids),
            },
        )

        # Auto-discover catalog field ID (GAIFAGP-484).
        # Cached per dataset for the process lifetime.
        if dataset not in _catalog_field_cache:
            try:
                fid = get_catalog_field_id(
                    dataset, session
                )
                _catalog_field_cache[dataset] = fid
            except ValueError as e:
                logger.error(
                    "Field discovery failed",
                    extra={
                        "dataset": dataset,
                        "error": str(e),
                    },
                )
                for cid in cids:
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": (
                            "field_discovery_error"
                        ),
                        "message": str(e),
                    })
                continue

        catalog_filter_id = _catalog_field_cache[dataset]

        for cid in cids:
            if request_count > 0:
                time.sleep(USGS_API_SLEEP)
            request_count += 1

            try:
                # GAIFAGP-484: Use metadataFilter with
                # discovered field ID instead of
                # entityIds (which expects USGS IDs,
                # not Maxar catalog_ids).
                payload = {
                    "datasetName": dataset,
                    "sceneFilter": {
                        "metadataFilter": {
                            "filterType": "value",
                            "filterId": (
                                catalog_filter_id
                            ),
                            "value": cid,
                        }
                    },
                    "maxResults": 50,
                }
                resp = session.post(
                    USGS_SCENE_SEARCH_URL,
                    json=payload,
                    timeout=USGS_API_TIMEOUT,
                )

                if resp.status_code != 200:
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": "http_error",
                        "message": (
                            f"HTTP {resp.status_code}"
                        ),
                    })
                    logger.error(
                        "USGS API HTTP error",
                        extra={
                            "catalog_id": cid,
                            "status": resp.status_code,
                        },
                    )
                    continue

                try:
                    data = resp.json()
                except ValueError as e:
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": "json_decode",
                        "message": str(e),
                    })
                    logger.error(
                        "USGS response not valid JSON",
                        extra={
                            "catalog_id": cid,
                            "error": str(e),
                        },
                    )
                    continue

                if data.get("errorCode"):
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": (
                            "usgs_api_error"
                        ),
                        "message": data.get(
                            "errorMessage", "Unknown"
                        ),
                    })
                    logger.error(
                        "USGS API error",
                        extra={
                            "catalog_id": cid,
                            "error": data.get(
                                "errorMessage",
                                "Unknown",
                            ),
                        },
                    )
                    continue

                scenes = (
                    data.get("data", {})
                    .get("results", [])
                )

                if len(scenes) == 0:
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": "no_results",
                        "message": (
                            "Scene search returned "
                            "0 results"
                        ),
                    })
                    logger.warning(
                        "USGS returned 0 results",
                        extra={
                            "catalog_id": cid,
                            "dataset": dataset,
                        },
                    )
                    continue
                # GAIFAGP-484: One catalog_id maps to
                # multiple USGS scenes (strips, PAN/MSI,
                # processing levels). Extract vendor_id
                # from each scene to build vendor_id ->
                # entity_id map. POI.vendor_id is the
                # disambiguation key.
                mapped_count = 0
                for scene in scenes:
                    eid = scene.get("entityId")
                    if not eid:
                        continue
                    meta = {
                        m["fieldName"]: m["value"]
                        for m in scene.get(
                            "metadata", []
                        )
                    }
                    vid = meta.get("Vendor ID")
                    if vid:
                        resolved[vid] = str(eid)
                        mapped_count += 1
                    else:
                        logger.warning(
                            "Scene has no Vendor ID",
                            extra={
                                "catalog_id": cid,
                                "entity_id": eid,
                                "dataset": dataset,
                            },
                        )

                if mapped_count == 0:
                    errors.append({
                        "catalog_id": cid,
                        "dataset": dataset,
                        "failure_class": (
                            "no_vendor_ids"
                        ),
                        "message": (
                            f"{len(scenes)} scenes but "
                            f"no Vendor IDs extracted"
                        ),
                    })
                else:
                    logger.info(
                        "Catalog ID resolved",
                        extra={
                            "catalog_id": cid,
                            "scene_count": len(scenes),
                            "vendor_ids_mapped": (
                                mapped_count
                            ),
                            "dataset": dataset,
                        },
                    )

            except Timeout:
                errors.append({
                    "catalog_id": cid,
                    "dataset": dataset,
                    "failure_class": "timeout",
                    "message": (
                        f"Request timed out after "
                        f"{USGS_API_TIMEOUT}s"
                    ),
                })
                logger.error(
                    "USGS API timeout",
                    extra={
                        "catalog_id": cid,
                        "dataset": dataset,
                    },
                )

            except RequestException as e:
                errors.append({
                    "catalog_id": cid,
                    "dataset": dataset,
                    "failure_class": (
                        "request_exception"
                    ),
                    "message": str(e),
                })
                logger.error(
                    "USGS API request failed",
                    extra={
                        "catalog_id": cid,
                        "error": str(e),
                    },
                )

    logger.info(
        "USGS API resolution complete",
        extra={
            "request_count": request_count,
            "resolved_count": len(resolved),
        },
    )
    return resolved, errors


def _match_vendor_id(
    vendor_id: str,
    api_map: dict[str, str],
) -> Optional[str]:
    """
    Match a POI vendor_id against the USGS api_map.

    POI vendor_ids may use S1BS (stereo) product codes
    while USGS returns M1BS (multispectral) or P1BS
    (panchromatic). Tries exact match first, then
    normalizes S1BS -> P1BS, then S1BS -> M1BS.

    GAIFAGP-484: Preference order reflects DL-006
    (pansharpening uses PAN input).

    Args:
        vendor_id: POI vendor_id string.
        api_map: Dict of vendor_id -> entity_id from
            USGS scene-search results.

    Returns:
        Matched entity_id or None.
    """
    # Exact match
    if vendor_id in api_map:
        return api_map[vendor_id]

    # Normalize S1BS -> P1BS (preferred)
    if "-S1BS-" in vendor_id:
        p1bs = vendor_id.replace("-S1BS-", "-P1BS-")
        if p1bs in api_map:
            return api_map[p1bs]

        # Normalize S1BS -> M1BS (fallback)
        m1bs = vendor_id.replace("-S1BS-", "-M1BS-")
        if m1bs in api_map:
            return api_map[m1bs]

    return None


def _resolve_via_api(
    unresolved_catalog_ids: set[str],
    candidates: QuerySet,
    dry_run: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """
    Pass 2: Apply USGS API resolution to POI records.

    Calls resolve_via_usgs_api() then applies results
    via bulk_update. API map is keyed by vendor_id
    (not catalog_id) per GAIFAGP-484 disambiguation.

    Assumptions:
        resolve_via_usgs_api() returns a dict of
        vendor_id -> entity_id for successfully resolved
        IDs, and a list of structured error dicts for
        failures. Atomic blocks are per-batch.

    Args:
        unresolved_catalog_ids: Set of catalog_id strings.
        candidates: QuerySet of POIs with null entity_id.
        dry_run: If True, count only.
        batch_size: Records per bulk_update batch.

    Returns:
        Dict with keys: resolved, errors.
    """
    try:
        api_map, api_errors = resolve_via_usgs_api(
            unresolved_catalog_ids
        )
    except ValueError as e:
        return {"resolved": 0, "errors": [str(e)]}

    errors = [
        f"{e['failure_class']}: "
        f"{e['catalog_id']} ({e['dataset']}): "
        f"{e['message']}"
        for e in api_errors
    ]

    if not api_map:
        return {"resolved": 0, "errors": errors}

    if dry_run:
        resolvable_count = 0
        for poi in candidates.only(
            "id", "vendor_id"
        ).iterator(chunk_size=batch_size):
            vid = (
                str(poi.vendor_id)
                if poi.vendor_id
                else None
            )
            if vid and _match_vendor_id(
                vid, api_map
            ):
                resolvable_count += 1
        return {
            "resolved": resolvable_count,
            "errors": errors,
        }

    # Apply to DB — re-query for still-null entity_ids
    pois_to_update: list = []
    for poi in (
        PointsOfInterest.objects.filter(
            entity_id__isnull=True
        )
        .only("id", "vendor_id")
        .iterator(chunk_size=batch_size)
    ):
        vid = (
            str(poi.vendor_id)
            if poi.vendor_id
            else None
        )
        if not vid:
            continue
        entity_id = _match_vendor_id(vid, api_map)
        if entity_id:
            poi.entity_id = entity_id
            pois_to_update.append(poi)

    updated_count = 0
    for i in range(0, len(pois_to_update), batch_size):
        batch = pois_to_update[i : i + batch_size]
        with transaction.atomic():
            PointsOfInterest.objects.bulk_update(
                batch,
                ["entity_id"],
                batch_size=batch_size,
            )
        updated_count += len(batch)
        logger.info(
            "Entity ID backfill (API) progress",
            extra={
                "updated": updated_count,
                "total": len(pois_to_update),
            },
        )

    return {"resolved": updated_count, "errors": errors}