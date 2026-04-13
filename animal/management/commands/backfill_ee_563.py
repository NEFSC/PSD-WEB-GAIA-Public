"""
One-time EE record backfill for five WV-2 CCB acquisitions.

Queries USGS by catalog_id, passes the response through
gdf_from_ee() for field parsing, then creates EarthExplorer
records with fully populated fields. The EE INSERT trigger
populates ETL downstream automatically.

Dependency: GAIFAGP-484 must have run first (entity_id
populated on POIs). The command verifies this at startup.

Usage:
    python manage.py backfill_ee_563 --dry-run
    python manage.py backfill_ee_563 --confirm
"""
# ----------------------------------------------------------------------
# ----- backfill_ee_563.py ---------------------------------------------
# ----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  One-time backfill — not a permanent command.
#              Creates EE records for 5 WV-2 catalog_ids that
#              have zero representation in animal_earthexplorer.
#
#    tickets:  GAIFAGP-563 (EE backfill for WV-2 CCB acquisitions)
#
#    references:
#      GAIFAGP-484 — entity_id backfill (hard dependency)
#      GAIFAGP-558 — catalog_id search filter
#      DL-017 — imagery identifier relationships
#
# ----------------------------------------------------------------------

import logging

import requests
from django.contrib.gis.geos import GEOSGeometry
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from animal.models import (
    AreaOfInterest,
    EarthExplorer,
    PointsOfInterest,
)
from animal.utils.api_utils import (
    ee_login,
    get_catalog_field_id,
    gdf_from_ee,
)
from animal.utils.poi_backfill import (
    DATASET_MAP,
    derive_sensor,
)

logger = logging.getLogger(__name__)

AOI_ID = 6  # Cape Cod Bay

TARGET_CATALOG_IDS = [
    "10300100BBB08100",  # 3,425 POIs
    "10300100BB27E800",  # 338 POIs
    "10300100BC063D00",  # 594 POIs
    "10300100BC254B00",  # 4,375 POIs
    "10300100BCAF4B00",  # 5,631 POIs
]

USGS_SCENE_SEARCH_URL = (
    "https://m2m.cr.usgs.gov"
    "/api/api/json/stable/scene-search"
)
USGS_API_TIMEOUT = 30


class Command(BaseCommand):
    help = (
        "One-time backfill: create EarthExplorer records "
        "for 5 WV-2 CCB acquisitions (GAIFAGP-563)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without writing.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Execute the backfill.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run")
        confirm = options.get("confirm")

        if not dry_run and not confirm:
            raise CommandError(
                "Requires --dry-run or --confirm."
            )
        if dry_run and confirm:
            raise CommandError(
                "Cannot use both --dry-run and --confirm."
            )

        from django.conf import settings as django_settings
        username = getattr(
            django_settings, "USGS_USERNAME", None
        )
        token = getattr(
            django_settings, "USGS_TOKEN", None
        )
        if not username or not token:
            raise CommandError(
                "USGS credentials not configured."
            )

        session = requests.Session()
        session = ee_login(session, username, token)

        try:
            aoi = AreaOfInterest.objects.get(id=AOI_ID)
        except AreaOfInterest.DoesNotExist:
            raise CommandError(f"AOI {AOI_ID} not found.")

        w = self.stdout.write
        mode = "DRY RUN" if dry_run else "EXECUTE"
        w(f"\n{'='*60}")
        w(f"GAIFAGP-563 EE Backfill — {mode}")
        w(f"{'='*60}")
        w(f"AOI: [{aoi.id}] {aoi.name}")
        w(f"Catalog IDs: {len(TARGET_CATALOG_IDS)}\n")

        # Verify 484 dependency: POIs must have entity_id
        for cid in TARGET_CATALOG_IDS:
            total = PointsOfInterest.objects.filter(
                catalog_id=cid
            ).count()
            with_eid = PointsOfInterest.objects.filter(
                catalog_id=cid,
                entity_id__isnull=False,
            ).count()
            pct = (
                (with_eid / total * 100) if total > 0
                else 0
            )
            w(f"  {cid}: {with_eid}/{total} POIs "
              f"have entity_id ({pct:.0f}%)")
            if with_eid == 0:
                raise CommandError(
                    f"{cid} has 0 POIs with entity_id. "
                    f"Run GAIFAGP-484 backfill first."
                )

        w("")

        total_created = 0
        total_skipped = 0
        errors = []

        for cid in TARGET_CATALOG_IDS:
            sensor = derive_sensor(cid)
            dataset = DATASET_MAP.get(sensor)
            if not dataset:
                errors.append(
                    f"{cid}: no dataset for {sensor}"
                )
                w(self.style.ERROR(
                    f"ERROR: {cid} — no dataset for "
                    f"{sensor}"
                ))
                continue

            poi_entity_ids = set(
                PointsOfInterest.objects.filter(
                    catalog_id=cid,
                    entity_id__isnull=False,
                ).values_list(
                    "entity_id", flat=True
                ).distinct()
            )

            # Search USGS — raw API call so we can
            # pass response to gdf_from_ee()
            catalog_field_id = get_catalog_field_id(
                dataset, session
            )
            payload = {
                "datasetName": dataset,
                "sceneFilter": {
                    "metadataFilter": {
                        "filterType": "value",
                        "filterId": catalog_field_id,
                        "value": cid,
                    }
                },
                "maxResults": 50,
                "metadataType": "full",
            }

            try:
                resp = session.post(
                    USGS_SCENE_SEARCH_URL,
                    json=payload,
                    timeout=USGS_API_TIMEOUT,
                )
                resp.raise_for_status()
                gdf = gdf_from_ee(resp, str(aoi.id))
            except Exception as e:
                errors.append(f"{cid}: {e}")
                w(self.style.ERROR(
                    f"ERROR: {cid} — {e}"
                ))
                continue

            gdf = gdf[gdf["processing_level"] == "LV1"]
            w(f"{cid}: {len(gdf)} LV1 rows, "
              f"{len(poi_entity_ids)} POI entity_ids")

            created = 0
            skipped = 0

            for _, row in gdf.iterrows():
                eid = str(row.get("entity_id", ""))
                if eid not in poi_entity_ids:
                    continue

                # Skip existing — do NOT overwrite.
                # Prevents wiping hand-corrected data
                # on re-runs.
                if EarthExplorer.objects.filter(
                    entity_id=eid
                ).exists():
                    w(f"  EXISTS {eid} — skip")
                    skipped += 1
                    continue

                if dry_run:
                    w(f"  CREATE {eid}")
                    created += 1
                    continue

                bounds = GEOSGeometry(
                    row["bounds"].wkt, srid=4326
                )

                # Normalize date formats: USGS returns
                # YYYY/MM/DD, Django DateField needs
                # YYYY-MM-DD.
                def _fix_date(val):
                    if val and isinstance(val, str):
                        return val.replace("/", "-")
                    return val or None

                with transaction.atomic():
                    EarthExplorer.objects.create(
                        entity_id=eid,
                        aoi_id=aoi,
                        catalog_id=str(
                            row.get("catalog_id", "")
                        ),
                        acquisition_date=_fix_date(
                            row.get("acquisition_date")
                        ),
                        vendor=str(
                            row.get("vendor", "")
                        ),
                        vendor_id=str(
                            row.get("vendor_id", "")
                        ),
                        cloud_cover=int(float(
                            row.get("cloud_cover", 0)
                        )),
                        satellite=str(
                            row.get("satellite", "")
                        ),
                        sensor=str(
                            row.get("sensor", "")
                        ),
                        number_of_bands=int(float(
                            row.get("number_of_bands", 0)
                        )),
                        map_projection=str(
                            row.get("map_projection", "")
                        ),
                        datum=str(
                            row.get("datum", "")
                        ),
                        processing_level=str(
                            row.get("processing_level", "")
                        ),
                        file_format=str(
                            row.get("file_format", "")
                        ),
                        license_id=int(float(
                            row.get("license_id", 0)
                        )),
                        sun_azimuth=float(
                            row.get("sun_azimuth", 0)
                        ),
                        sun_elevation=float(
                            row.get("sun_elevation", 0)
                        ),
                        pixel_size_x=float(
                            row.get("pixel_size_x", 0)
                        ),
                        pixel_size_y=float(
                            row.get("pixel_size_y", 0)
                        ),
                        license_uplift_update=_fix_date(
                            row.get("license_uplift_update")
                        ),
                        event=str(
                            row.get("event") or ""
                        ),
                        event_date=_fix_date(
                            row.get("event_date")
                        ),
                        date_entered=(
                            timezone.now().date()
                        ),
                        center_latitude_dec=(
                            bounds.centroid.y
                        ),
                        center_longitude_dec=(
                            bounds.centroid.x
                        ),
                        thumbnail=str(
                            row.get("thumbnail", "")
                        ),
                        publish_date=_fix_date(
                            row.get("publish_date")
                        ),
                        bounds=bounds,
                    )

                w(f"  CREATED {eid}")
                created += 1

            total_created += created
            total_skipped += skipped
            w(f"  -> {created} created, "
              f"{skipped} skipped (existing)\n")

        # Targeted verification — only these 5 catalog_ids
        target_orphans = PointsOfInterest.objects.filter(
            catalog_id__in=TARGET_CATALOG_IDS
        ).exclude(
            catalog_id__in=(
                EarthExplorer.objects.filter(
                    catalog_id__in=TARGET_CATALOG_IDS
                ).values_list("catalog_id", flat=True)
            )
        ).count()

        w(f"{'='*60}")
        w(f"BACKFILL {'DRY RUN ' if dry_run else ''}COMPLETE")
        w(f"{'='*60}")
        w(f"Created: {total_created}")
        w(f"Skipped (existing): {total_skipped}")
        w(f"Errors: {len(errors)}")
        w(f"Target orphan POIs: {target_orphans}")
        if errors:
            for err in errors:
                w(self.style.ERROR(f"  {err}"))
        w(f"{'='*60}")
