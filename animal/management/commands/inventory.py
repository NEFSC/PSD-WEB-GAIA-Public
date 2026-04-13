# ------------------------------------------------------------------------------
# ----- inventory.py -----------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Data inventory audit tooling for GAIA.
#              Compares Azure Blob Storage vs ETL vs POI to identify orphans.
#
#    tickets:  GAIFAGP-424 (Data Inventory Baseline spike)
#              GAIFAGP-447 (POI orphan cleanup - uses this for recovery check)
#
#    usage:    python manage.py inventory --help
#              python manage.py inventory azure
#              python manage.py inventory etl
#              python manage.py inventory poi
#              python manage.py inventory compare
#              python manage.py inventory check --vendor-ids="21MAR21152114-S1BS-5"
#
# ------------------------------------------------------------------------------

import argparse
import re
from django.utils import timezone
from collections import defaultdict
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from animal.models import PointsOfInterest, ExtractTransformLoad


def _get_optional_model(model_name):
    """
    Lazy import for optional models. Returns (Model, True) or (None, False).
    """
    try:
        from animal import models
        return getattr(models, model_name, None), hasattr(models, model_name)
    except Exception:
        return None, False


class Command(BaseCommand):
    help = """
Data inventory audit for GAIA system.

Compares Azure Blob Storage imagery against ETL and POI database records
to identify orphans and data integrity issues.

ACTIONS:
  azure       List imagery in Azure Blob Storage (data/cogs/)
  etl         List records in ETL table
  poi         List POI records with catalog_id/vendor_id
  compare     Cross-reference Azure vs ETL vs POI to find orphans
  check       Check specific vendor_ids against all sources

EXAMPLES:
  # List Azure blob inventory (first 100)
  python manage.py inventory azure
  python manage.py inventory azure --limit=500

  # List ETL records
  python manage.py inventory etl

  # List POI records with NULL catalog_id
  python manage.py inventory poi --null-catalog-id

  # Full comparison (424 spike)
  python manage.py inventory compare

  # Check specific vendor_ids (447 support)
  python manage.py inventory check --vendor-ids="21MAR21152114-S1BS-5,21MAR21152059-S1BS-5"

  # Export comparison report
  python manage.py inventory compare --output=inventory_report.md

AZURE BLOB STRUCTURE:
  Container: data
  Path: cogs/
  Filename: {vendor_id}_{processing_suffix}.tif
  Example: 21JUN13215606-S1BS-505817468080_01_P002_u08mr32606.tif
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ = vendor_id

SPIKE QUESTIONS (GAIFAGP-424):
  Q1: What catalog_ids/vendor_ids exist in Azure?         → inventory azure
  Q2: What catalog_ids exist in ETL but no Azure files?   → inventory compare
  Q3: What catalog_ids exist in Azure but no ETL records? → inventory compare
  Q4: Do the 447 vendor_ids exist in Azure?               → inventory check --vendor-ids=...
  Q5: What catalog_ids are in ETL but not in any project? → inventory compare
"""

    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["azure", "etl", "poi", "compare", "check", "validate", "projects"],
            help="Action to perform"
        )

        # Filtering
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Limit number of records (default: 100, use 0 for all)"
        )
        parser.add_argument(
            "--null-catalog-id",
            action="store_true",
            help="For poi action: only show POIs with NULL catalog_id"
        )
        parser.add_argument(
            "--vendor-ids",
            type=str,
            help="Comma-separated vendor_id prefixes to check"
        )
        parser.add_argument(
            "--prefix",
            type=str,
            default="cogs/",
            help="Azure blob prefix to search (default: cogs/)"
        )

        # Output
        parser.add_argument(
            "--output", "-o",
            type=str,
            help="Write report to file (markdown format)"
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output"
        )

    def handle(self, *args, **options):
        action = options["action"]

        if action == "azure":
            self._action_azure(options)
        elif action == "etl":
            self._action_etl(options)
        elif action == "poi":
            self._action_poi(options)
        elif action == "compare":
            self._action_compare(options)
        elif action == "check":
            self._action_check(options)
        elif action == "validate":
            self._action_validate(options)
        elif action == "projects":
            self._action_projects(options)

    def _get_azure_client(self):
        """
        Get Azure BlobServiceClient using settings.
        """
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise CommandError(
                "azure-storage-blob package not installed.\n"
                "Run: pip install azure-storage-blob"
            )

        account_name = getattr(settings, 'AZURE_STORAGE_ACCOUNT_NAME', None)
        account_key = getattr(settings, 'AZURE_STORAGE_ACCOUNT_KEY', None)
        container_name = getattr(settings, 'AZURE_CONTAINER_NAME', 'data')

        if not account_name or not account_key:
            raise CommandError(
                "Azure storage not configured.\n"
                "Check AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY in settings."
            )

        connection_string = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={account_name};"
            f"AccountKey={account_key};"
            f"EndpointSuffix=core.windows.net"
        )

        client = BlobServiceClient.from_connection_string(connection_string)
        return client, container_name

    def _parse_vendor_id_from_blob(self, blob_name: str) -> str:
        """
        Extract vendor_id from blob filename.

        Blob format: cogs/{vendor_id}_{processing_suffix}.tif
        Example: cogs/21JUN13215606-S1BS-505817468080_01_P002_u08mr32606.tif
        
        Processing suffix pattern: _u08mr##### or similar
        We strip the last underscore-prefixed segment if it looks like a processing suffix.
        """
        # Remove directory prefix
        filename = blob_name.split('/')[-1]
        
        # Remove .tif extension
        if filename.lower().endswith('.tif'):
            filename = filename[:-4]
        
        # Remove processing suffix (pattern: _u08... or similar at end)
        # The suffix is typically _u08 followed by alphanumeric
        # We look for the pattern: _{processing_code} at the end
        suffix_pattern = r'_u\d{2}[a-z]{2}\d+$'
        filename = re.sub(suffix_pattern, '', filename, flags=re.IGNORECASE)
        
        return filename


    def _action_azure(self, options: dict) -> None:
        """
        List imagery in Azure Blob Storage.
        """
        limit = options.get("limit", 100)
        prefix = options.get("prefix", "cogs/")
        verbose = options.get("verbose", False)

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("AZURE BLOB STORAGE INVENTORY")
        self.stdout.write(f"{'='*70}\n")

        client, container_name = self._get_azure_client()
        container_client = client.get_container_client(container_name)

        self.stdout.write(f"Container: {container_name}")
        self.stdout.write(f"Prefix: {prefix}")
        self.stdout.write(f"Limit: {limit if limit > 0 else 'ALL'}\n")

        vendor_ids = set()
        blob_count = 0
        sample_blobs = []

        try:
            blobs = container_client.list_blobs(name_starts_with=prefix)

            for blob in blobs:
                blob_count += 1
                vendor_id = self._parse_vendor_id_from_blob(blob.name)
                vendor_ids.add(vendor_id)

                if len(sample_blobs) < 10:
                    sample_blobs.append((blob.name, vendor_id))

                if verbose and blob_count <= 50:
                    self.stdout.write(f"  {blob.name}")
                    self.stdout.write(f"    → vendor_id: {vendor_id}")

                if limit > 0 and blob_count >= limit:
                    self.stdout.write(f"\n[Stopped at limit={limit}]")
                    break

        except Exception as e:
            raise CommandError(f"Azure error: {e}")

        # Summary
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("SUMMARY:")
        self.stdout.write(f"  Blobs scanned: {blob_count}")
        self.stdout.write(f"  Unique vendor_ids: {len(vendor_ids)}")

        if sample_blobs:
            self.stdout.write(f"\nSample blobs:")
            for blob_name, vid in sample_blobs[:5]:
                self.stdout.write(f"  {blob_name}")
                self.stdout.write(f"    → {vid}")

        self.stdout.write(f"\n{'='*70}\n")

        # Store for later use
        self._azure_vendor_ids = vendor_ids
        self._azure_blob_count = blob_count

    def _action_etl(self, options: dict) -> None:
        """
        List ETL records.
        """
        limit = options.get("limit", 100)
        verbose = options.get("verbose", False)

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("ETL TABLE INVENTORY")
        self.stdout.write(f"{'='*70}\n")

        queryset = ExtractTransformLoad.objects.all()
        total_count = queryset.count()

        # Get records
        if limit > 0:
            records = list(queryset.values('id', 'vendor_id', 'entity_id', 'table_name', 'date')[:limit])
        else:
            records = list(queryset.values('id', 'vendor_id', 'entity_id', 'table_name', 'date'))

        # Build vendor_id set
        vendor_ids = set()
        catalog_ids = set()
        by_source = defaultdict(int)

        for rec in records:
            if rec['vendor_id']:
                vendor_ids.add(str(rec['vendor_id']))
            if rec['id']:
                catalog_ids.add(str(rec['id']))
            source = rec['table_name'] or 'Unknown'
            by_source[source] += 1

        self.stdout.write(f"Total ETL records: {total_count}")
        self.stdout.write(f"Records scanned: {len(records)}")
        self.stdout.write(f"Unique vendor_ids: {len(vendor_ids)}")
        self.stdout.write(f"Unique catalog_ids (ETL.id): {len(catalog_ids)}")

        self.stdout.write(f"\nBy source table:")
        for source, count in sorted(by_source.items()):
            self.stdout.write(f"  {source}: {count}")

        if verbose and records:
            self.stdout.write(f"\nSample records:")
            for rec in records[:10]:
                self.stdout.write(
                    f"  id={rec['id']}, vendor_id={rec['vendor_id'][:30] if rec['vendor_id'] else 'NULL'}, "
                    f"source={rec['table_name']}"
                )

        self.stdout.write(f"\n{'='*70}\n")

        self._etl_vendor_ids = vendor_ids
        self._etl_catalog_ids = catalog_ids

    def _action_poi(self, options: dict) -> None:
        """
        List POI records.
        """
        limit = options.get("limit", 100)
        verbose = options.get("verbose", False)
        null_catalog_only = options.get("null_catalog_id", False)

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("POI TABLE INVENTORY")
        self.stdout.write(f"{'='*70}\n")

        queryset = PointsOfInterest.objects.all()

        if null_catalog_only:
            queryset = queryset.filter(catalog_id__isnull=True)
            self.stdout.write("Filter: NULL catalog_id only\n")

        total_count = queryset.count()

        if limit > 0:
            records = list(queryset.values('id', 'catalog_id', 'vendor_id', 'project_id', 'date_image_taken')[:limit])
        else:
            records = list(queryset.values('id', 'catalog_id', 'vendor_id', 'project_id', 'date_image_taken'))

        vendor_ids = set()
        catalog_ids = set()
        null_catalog_count = 0
        null_vendor_count = 0

        for rec in records:
            if rec['vendor_id']:
                vendor_ids.add(str(rec['vendor_id']))
            else:
                null_vendor_count += 1
            if rec['catalog_id']:
                catalog_ids.add(str(rec['catalog_id']))
            else:
                null_catalog_count += 1

        self.stdout.write(f"Total POI records: {total_count}")
        self.stdout.write(f"Records scanned: {len(records)}")
        self.stdout.write(f"Unique vendor_ids: {len(vendor_ids)}")
        self.stdout.write(f"Unique catalog_ids: {len(catalog_ids)}")
        self.stdout.write(f"NULL catalog_id: {null_catalog_count}")
        self.stdout.write(f"NULL vendor_id: {null_vendor_count}")

        if verbose and records:
            self.stdout.write(f"\nSample records:")
            for rec in records[:10]:
                vid = rec['vendor_id'][:30] if rec['vendor_id'] else 'NULL'
                cid = rec['catalog_id'][:20] if rec['catalog_id'] else 'NULL'
                self.stdout.write(f"  POI {rec['id']}: catalog={cid}, vendor={vid}")

        self.stdout.write(f"\n{'='*70}\n")

        self._poi_vendor_ids = vendor_ids
        self._poi_catalog_ids = catalog_ids

    def _action_compare(self, options: dict) -> None:
        """
        Cross-reference Azure vs ETL vs POI.
        
        This is the core 424 spike output.
        """
        verbose = options.get("verbose", False)
        output_file = options.get("output")

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("DATA INVENTORY COMPARISON")
        self.stdout.write(f"{'='*70}\n")
        self.stdout.write(f"Timestamp: {timezone.now().isoformat()}\n")

        # Collect data from all sources
        self.stdout.write("Collecting Azure inventory...")
        azure_vendor_ids = self._collect_azure_vendor_ids(options)
        self.stdout.write(f"  → {len(azure_vendor_ids)} unique vendor_ids\n")

        self.stdout.write("Collecting ETL inventory...")
        etl_data = self._collect_etl_data()
        etl_vendor_ids = etl_data['vendor_ids']
        etl_catalog_ids = etl_data['catalog_ids']
        self.stdout.write(f"  → {len(etl_vendor_ids)} vendor_ids, {len(etl_catalog_ids)} catalog_ids\n")

        self.stdout.write("Collecting POI inventory...")
        poi_data = self._collect_poi_data()
        poi_vendor_ids = poi_data['vendor_ids']
        poi_catalog_ids = poi_data['catalog_ids']
        poi_null_catalog = poi_data['null_catalog_pois']
        self.stdout.write(f"  → {len(poi_vendor_ids)} vendor_ids, {len(poi_catalog_ids)} catalog_ids\n")
        self.stdout.write(f"  → {len(poi_null_catalog)} POIs with NULL catalog_id\n")

        # Cross-reference analysis
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("CROSS-REFERENCE ANALYSIS")
        self.stdout.write(f"{'-'*70}\n")

        # Q1: What's in Azure? (already answered above)

        # Q2: ETL vendor_ids with no Azure files
        etl_no_azure = etl_vendor_ids - azure_vendor_ids
        self.stdout.write(f"Q2: ETL vendor_ids NOT in Azure: {len(etl_no_azure)}")
        if verbose and etl_no_azure:
            for vid in list(etl_no_azure)[:5]:
                self.stdout.write(f"     {vid}")
            if len(etl_no_azure) > 5:
                self.stdout.write(f"     ... and {len(etl_no_azure) - 5} more")

        # Q3: Azure vendor_ids with no ETL record
        azure_no_etl = azure_vendor_ids - etl_vendor_ids
        self.stdout.write(f"\nQ3: Azure vendor_ids NOT in ETL: {len(azure_no_etl)}")
        if verbose and azure_no_etl:
            for vid in list(azure_no_etl)[:5]:
                self.stdout.write(f"     {vid}")
            if len(azure_no_etl) > 5:
                self.stdout.write(f"     ... and {len(azure_no_etl) - 5} more")

        # Q4: Check 447 vendor_ids (done in check action, but summarize here)
        self.stdout.write(f"\nQ4: POIs with NULL catalog_id: {len(poi_null_catalog)}")
        orphan_vendor_ids = set()
        for poi in poi_null_catalog:
            if poi['vendor_id']:
                orphan_vendor_ids.add(str(poi['vendor_id']))
        
        self.stdout.write(f"    Unique vendor_ids from orphan POIs: {len(orphan_vendor_ids)}")
        
        # Check if orphan vendor_ids exist in Azure
        orphans_in_azure = orphan_vendor_ids & azure_vendor_ids
        orphans_not_in_azure = orphan_vendor_ids - azure_vendor_ids
        
        # Also check with prefix matching (vendor_id might be partial)
        orphans_prefix_match = set()
        for orphan_vid in orphan_vendor_ids:
            if orphan_vid:
                prefix = orphan_vid[:13].upper()  # YYMMMDDHHmmss
                for azure_vid in azure_vendor_ids:
                    if azure_vid.upper().startswith(prefix):
                        orphans_prefix_match.add(orphan_vid)
                        break
        
        self.stdout.write(f"    Orphan vendor_ids found in Azure (exact): {len(orphans_in_azure)}")
        self.stdout.write(f"    Orphan vendor_ids found in Azure (prefix): {len(orphans_prefix_match)}")
        self.stdout.write(f"    Orphan vendor_ids NOT in Azure: {len(orphans_not_in_azure - orphans_prefix_match)}")
        
        if orphan_vendor_ids:
            self.stdout.write(f"\n    Orphan vendor_id details:")
            for vid in sorted(orphan_vendor_ids):
                in_azure = "YES (exact)" if vid in orphans_in_azure else ("YES (prefix)" if vid in orphans_prefix_match else "NO")
                in_etl = "YES" if vid in etl_vendor_ids else "NO"
                self.stdout.write(f"      {vid[:40]}")
                self.stdout.write(f"        Azure: {in_azure}, ETL: {in_etl}")

        # Q5: ETL catalog_ids not associated with any project
        # This requires checking which catalog_ids have POIs
        etl_no_poi = etl_catalog_ids - poi_catalog_ids
        self.stdout.write(f"\nQ5: ETL catalog_ids NOT in any POI: {len(etl_no_poi)}")
        if verbose and etl_no_poi:
            for cid in list(etl_no_poi)[:5]:
                self.stdout.write(f"     {cid}")
            if len(etl_no_poi) > 5:
                self.stdout.write(f"     ... and {len(etl_no_poi) - 5} more")

        # Summary
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("INVENTORY SUMMARY")
        self.stdout.write(f"{'='*70}")
        self.stdout.write(f"")
        self.stdout.write(f"| Source | Count |")
        self.stdout.write(f"|--------|-------|")
        self.stdout.write(f"| Azure vendor_ids | {len(azure_vendor_ids)} |")
        self.stdout.write(f"| ETL vendor_ids | {len(etl_vendor_ids)} |")
        self.stdout.write(f"| ETL catalog_ids | {len(etl_catalog_ids)} |")
        self.stdout.write(f"| POI vendor_ids | {len(poi_vendor_ids)} |")
        self.stdout.write(f"| POI catalog_ids | {len(poi_catalog_ids)} |")
        self.stdout.write(f"")
        self.stdout.write(f"| Orphan Category | Count |")
        self.stdout.write(f"|-----------------|-------|")
        self.stdout.write(f"| Azure without ETL | {len(azure_no_etl)} |")
        self.stdout.write(f"| ETL without Azure | {len(etl_no_azure)} |")
        self.stdout.write(f"| ETL without POI | {len(etl_no_poi)} |")
        self.stdout.write(f"| POI without catalog_id | {len(poi_null_catalog)} |")
        self.stdout.write(f"")
        self.stdout.write(f"| NULL catalog_id Recovery | Count |")
        self.stdout.write(f"|--------------------------|-------|")
        self.stdout.write(f"| Orphan vendor_ids | {len(orphan_vendor_ids)} |")
        self.stdout.write(f"| Found in Azure | {len(orphans_prefix_match)} |")
        self.stdout.write(f"| NOT in Azure | {len(orphan_vendor_ids - orphans_prefix_match)} |")
        self.stdout.write(f"{'='*70}\n")

        # Write to file if requested
        if output_file:
            self._write_report(output_file, {
                'azure_vendor_ids': azure_vendor_ids,
                'etl_vendor_ids': etl_vendor_ids,
                'etl_catalog_ids': etl_catalog_ids,
                'poi_vendor_ids': poi_vendor_ids,
                'poi_catalog_ids': poi_catalog_ids,
                'azure_no_etl': azure_no_etl,
                'etl_no_azure': etl_no_azure,
                'etl_no_poi': etl_no_poi,
                'poi_null_catalog': poi_null_catalog,
                'orphan_vendor_ids': orphan_vendor_ids,
                'orphans_in_azure': orphans_prefix_match,
            })
            self.stdout.write(f"Report written to: {output_file}")

    def _action_check(self, options: dict) -> None:
        """
        Check specific vendor_ids against all sources.
        """
        vendor_ids_str = options.get("vendor_ids")
        if not vendor_ids_str:
            raise CommandError("--vendor-ids is required for check action")

        vendor_ids_to_check = [v.strip() for v in vendor_ids_str.split(",")]

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("VENDOR_ID CHECK")
        self.stdout.write(f"{'='*70}\n")
        self.stdout.write(f"Checking {len(vendor_ids_to_check)} vendor_id(s):\n")

        # Collect data
        azure_vendor_ids = self._collect_azure_vendor_ids(options)
        etl_data = self._collect_etl_data()
        poi_data = self._collect_poi_data()

        etl_vendor_ids = etl_data['vendor_ids']
        etl_by_vendor = etl_data['by_vendor']
        poi_by_vendor = poi_data['by_vendor']

        for vid in vendor_ids_to_check:
            self.stdout.write(f"\n{'-'*70}")
            self.stdout.write(f"Vendor ID: {vid}")
            self.stdout.write(f"{'-'*70}")

            # Check Azure (prefix match)
            prefix = vid[:13].upper() if len(vid) >= 13 else vid.upper()
            azure_matches = [av for av in azure_vendor_ids if av.upper().startswith(prefix)]
            
            if azure_matches:
                self.stdout.write(self.style.SUCCESS(f"  Azure: FOUND ({len(azure_matches)} match(es))"))
                for match in azure_matches[:3]:
                    self.stdout.write(f"    → {match}")
                if len(azure_matches) > 3:
                    self.stdout.write(f"    ... and {len(azure_matches) - 3} more")
            else:
                self.stdout.write(self.style.ERROR(f"  Azure: NOT FOUND"))

            # Check ETL (prefix match)
            etl_matches = [ev for ev in etl_vendor_ids if ev.upper().startswith(prefix)]
            
            if etl_matches:
                self.stdout.write(self.style.SUCCESS(f"  ETL: FOUND ({len(etl_matches)} match(es))"))
                for match in etl_matches[:3]:
                    etl_info = etl_by_vendor.get(match, {})
                    catalog_id = etl_info.get('catalog_id', 'Unknown')
                    self.stdout.write(f"    → {match}")
                    self.stdout.write(f"      catalog_id: {catalog_id}")
            else:
                self.stdout.write(self.style.ERROR(f"  ETL: NOT FOUND"))

            # Check POI
            poi_matches = [pv for pv in poi_by_vendor.keys() if pv.upper().startswith(prefix)]
            
            if poi_matches:
                self.stdout.write(self.style.SUCCESS(f"  POI: FOUND ({len(poi_matches)} vendor_id match(es))"))
                for match in poi_matches[:3]:
                    poi_count = len(poi_by_vendor.get(match, []))
                    self.stdout.write(f"    → {match} ({poi_count} POI(s))")
            else:
                self.stdout.write(self.style.WARNING(f"  POI: NOT FOUND (or NULL vendor_id)"))

            # Recovery assessment
            self.stdout.write(f"\n  Recovery Assessment:")
            if azure_matches and etl_matches:
                self.stdout.write(self.style.SUCCESS(
                    f"    ✓ RECOVERABLE: Image exists in Azure, ETL record exists"
                ))
                self.stdout.write(f"    → Can backfill catalog_id from ETL")
            elif azure_matches and not etl_matches:
                self.stdout.write(self.style.WARNING(
                    f"    ⚠ PARTIAL: Image exists in Azure, NO ETL record"
                ))
                self.stdout.write(f"    → Need to create ETL record or investigate pipeline failure")
            elif not azure_matches and etl_matches:
                self.stdout.write(self.style.WARNING(
                    f"    ⚠ PARTIAL: ETL record exists, NO Azure image"
                ))
                self.stdout.write(f"    → Image may have been deleted or processing failed")
            else:
                self.stdout.write(self.style.ERROR(
                    f"    ✗ UNRECOVERABLE: No Azure image, No ETL record"
                ))
                self.stdout.write(f"    → Can only parse date from vendor_id; catalog_id stays NULL")

        self.stdout.write(f"\n{'='*70}\n")

    def _action_validate(self, options: dict) -> None:
        """
        Validate vendor_id formats across sources and test matching strategies.
        
        This helps diagnose why Azure/ETL/POI might not be matching.
        """
        limit = options.get("limit", 20)
        
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("VENDOR_ID FORMAT VALIDATION")
        self.stdout.write(f"{'='*70}\n")
        
        # Collect raw data
        self.stdout.write("Collecting samples from each source...\n")
        
        # Azure samples (with raw blob names)
        self.stdout.write(f"{'-'*70}")
        self.stdout.write("AZURE BLOB STORAGE")
        self.stdout.write(f"{'-'*70}")
        
        client, container_name = self._get_azure_client()
        container_client = client.get_container_client(container_name)
        
        azure_samples = []
        azure_vendor_ids = set()
        try:
            blobs = container_client.list_blobs(name_starts_with="cogs/")
            for i, blob in enumerate(blobs):
                parsed = self._parse_vendor_id_from_blob(blob.name)
                azure_vendor_ids.add(parsed)
                if i < limit:
                    azure_samples.append({
                        'raw': blob.name,
                        'parsed': parsed
                    })
                if i >= 500:  # Cap at 500 for validation
                    break
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Azure error: {e}"))
            return
        
        self.stdout.write(f"Total collected: {len(azure_vendor_ids)}")
        self.stdout.write(f"\nSample blobs (raw → parsed):")
        for s in azure_samples[:10]:
            self.stdout.write(f"  {s['raw']}")
            self.stdout.write(f"    → {s['parsed']}")
        
        # ETL samples
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("ETL TABLE")
        self.stdout.write(f"{'-'*70}")
        
        etl_records = list(ExtractTransformLoad.objects.values(
            'id', 'vendor_id', 'entity_id', 'table_name'
        )[:500])
        
        etl_vendor_ids = set()
        etl_by_vendor = {}
        for rec in etl_records:
            vid = str(rec['vendor_id']) if rec['vendor_id'] else None
            if vid:
                etl_vendor_ids.add(vid)
                etl_by_vendor[vid] = rec
        
        self.stdout.write(f"Total records: {len(etl_records)}")
        self.stdout.write(f"Unique vendor_ids: {len(etl_vendor_ids)}")
        self.stdout.write(f"\nSample ETL records:")
        for rec in etl_records[:10]:
            self.stdout.write(f"  vendor_id: {rec['vendor_id']}")
            self.stdout.write(f"    id (catalog): {rec['id']}")
            self.stdout.write(f"    table: {rec['table_name']}")
        
        # POI samples
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("POI TABLE")
        self.stdout.write(f"{'-'*70}")
        
        poi_records = list(PointsOfInterest.objects.values(
            'id', 'catalog_id', 'vendor_id'
        )[:500])
        
        poi_vendor_ids = set()
        for rec in poi_records:
            vid = str(rec['vendor_id']) if rec['vendor_id'] else None
            if vid:
                poi_vendor_ids.add(vid)
        
        self.stdout.write(f"Total records: {len(poi_records)}")
        self.stdout.write(f"Unique vendor_ids: {len(poi_vendor_ids)}")
        self.stdout.write(f"\nSample POI records:")
        for rec in poi_records[:10]:
            self.stdout.write(f"  vendor_id: {rec['vendor_id']}")
            self.stdout.write(f"    catalog_id: {rec['catalog_id']}")
        
        # Format analysis
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("FORMAT ANALYSIS")
        self.stdout.write(f"{'='*70}\n")
        
        # Show one example from each for direct comparison
        azure_example = list(azure_vendor_ids)[0] if azure_vendor_ids else "N/A"
        etl_example = list(etl_vendor_ids)[0] if etl_vendor_ids else "N/A"
        poi_example = list(poi_vendor_ids)[0] if poi_vendor_ids else "N/A"
        
        self.stdout.write(f"Example vendor_ids:")
        self.stdout.write(f"  Azure: {azure_example}")
        self.stdout.write(f"  ETL:   {etl_example}")
        self.stdout.write(f"  POI:   {poi_example}")
        
        # Test matching strategies
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("MATCHING STRATEGY TESTS")
        self.stdout.write(f"{'-'*70}\n")
        
        # Exact match
        exact_azure_etl = azure_vendor_ids & etl_vendor_ids
        exact_azure_poi = azure_vendor_ids & poi_vendor_ids
        exact_etl_poi = etl_vendor_ids & poi_vendor_ids
        
        self.stdout.write(f"EXACT MATCH:")
        self.stdout.write(f"  Azure ∩ ETL: {len(exact_azure_etl)}")
        self.stdout.write(f"  Azure ∩ POI: {len(exact_azure_poi)}")
        self.stdout.write(f"  ETL ∩ POI:   {len(exact_etl_poi)}")
        
        if exact_azure_etl:
            self.stdout.write(f"  Examples (Azure ∩ ETL):")
            for v in list(exact_azure_etl)[:3]:
                self.stdout.write(f"    {v}")
        
        if exact_azure_poi:
            self.stdout.write(f"  Examples (Azure ∩ POI):")
            for v in list(exact_azure_poi)[:3]:
                self.stdout.write(f"    {v}")
        
        # Case-insensitive match
        azure_upper = {v.upper() for v in azure_vendor_ids}
        etl_upper = {v.upper() for v in etl_vendor_ids}
        poi_upper = {v.upper() for v in poi_vendor_ids}
        
        ci_azure_etl = azure_upper & etl_upper
        ci_azure_poi = azure_upper & poi_upper
        ci_etl_poi = etl_upper & poi_upper
        
        self.stdout.write(f"\nCASE-INSENSITIVE MATCH:")
        self.stdout.write(f"  Azure ∩ ETL: {len(ci_azure_etl)}")
        self.stdout.write(f"  Azure ∩ POI: {len(ci_azure_poi)}")
        self.stdout.write(f"  ETL ∩ POI:   {len(ci_etl_poi)}")
        
        # Prefix match (first 13 chars = YYMMMDDHHmmss)
        def get_prefix(v):
            return v[:13].upper() if v and len(v) >= 13 else v.upper() if v else ""
        
        azure_prefixes = {get_prefix(v) for v in azure_vendor_ids}
        etl_prefixes = {get_prefix(v) for v in etl_vendor_ids}
        poi_prefixes = {get_prefix(v) for v in poi_vendor_ids}
        
        prefix_azure_etl = azure_prefixes & etl_prefixes
        prefix_azure_poi = azure_prefixes & poi_prefixes
        prefix_etl_poi = etl_prefixes & poi_prefixes
        
        self.stdout.write(f"\nPREFIX MATCH (first 13 chars):")
        self.stdout.write(f"  Azure ∩ ETL: {len(prefix_azure_etl)}")
        self.stdout.write(f"  Azure ∩ POI: {len(prefix_azure_poi)}")
        self.stdout.write(f"  ETL ∩ POI:   {len(prefix_etl_poi)}")
        
        if prefix_azure_etl:
            self.stdout.write(f"  Examples (Azure ∩ ETL prefix):")
            for p in list(prefix_azure_etl)[:5]:
                # Find full vendor_ids with this prefix
                azure_full = [v for v in azure_vendor_ids if get_prefix(v) == p][:1]
                etl_full = [v for v in etl_vendor_ids if get_prefix(v) == p][:1]
                self.stdout.write(f"    Prefix: {p}")
                self.stdout.write(f"      Azure: {azure_full[0] if azure_full else 'N/A'}")
                self.stdout.write(f"      ETL:   {etl_full[0] if etl_full else 'N/A'}")
        
        # Prefix + band match (first 13 chars + band type)
        def get_prefix_band(v):
            if not v or len(v) < 18:
                return v.upper() if v else ""
            # Format: YYMMMDDHHmmss-BAND-...
            parts = v.split('-')
            if len(parts) >= 2:
                return f"{parts[0][:13]}-{parts[1]}".upper()
            return v[:18].upper()
        
        azure_prefix_band = {get_prefix_band(v) for v in azure_vendor_ids}
        etl_prefix_band = {get_prefix_band(v) for v in etl_vendor_ids}
        poi_prefix_band = {get_prefix_band(v) for v in poi_vendor_ids}
        
        pb_azure_etl = azure_prefix_band & etl_prefix_band
        pb_azure_poi = azure_prefix_band & poi_prefix_band
        pb_etl_poi = etl_prefix_band & poi_prefix_band
        
        self.stdout.write(f"\nPREFIX+BAND MATCH (YYMMMDDHHmmss-BAND):")
        self.stdout.write(f"  Azure ∩ ETL: {len(pb_azure_etl)}")
        self.stdout.write(f"  Azure ∩ POI: {len(pb_azure_poi)}")
        self.stdout.write(f"  ETL ∩ POI:   {len(pb_etl_poi)}")
        
        # Summary
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("VALIDATION SUMMARY")
        self.stdout.write(f"{'='*70}")
        self.stdout.write(f"")
        self.stdout.write(f"| Match Strategy | Azure∩ETL | Azure∩POI | ETL∩POI |")
        self.stdout.write(f"|----------------|-----------|-----------|---------|")
        self.stdout.write(f"| Exact          | {len(exact_azure_etl):>9} | {len(exact_azure_poi):>9} | {len(exact_etl_poi):>7} |")
        self.stdout.write(f"| Case-insensitive | {len(ci_azure_etl):>9} | {len(ci_azure_poi):>9} | {len(ci_etl_poi):>7} |")
        self.stdout.write(f"| Prefix (13ch) | {len(prefix_azure_etl):>9} | {len(prefix_azure_poi):>9} | {len(prefix_etl_poi):>7} |")
        self.stdout.write(f"| Prefix+Band   | {len(pb_azure_etl):>9} | {len(pb_azure_poi):>9} | {len(pb_etl_poi):>7} |")
        self.stdout.write(f"")
        
        if len(exact_azure_etl) == 0 and len(prefix_azure_etl) == 0:
            self.stdout.write(self.style.WARNING(
                "WARNING: ZERO overlap between Azure and ETL by any matching strategy."
            ))
            self.stdout.write(self.style.WARNING(
                "This suggests Azure and ETL contain completely different image sets."
            ))
        
        # CRITICAL: Check the CORRECT join path: POI.catalog_id = ETL.id
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("CATALOG_ID JOIN VALIDATION (POI.catalog_id ↔ ETL.id)")
        self.stdout.write(f"{'='*70}\n")
        
        # Get all ETL catalog_ids (ETL.id column)
        etl_catalog_ids = {str(rec['id']) for rec in etl_records if rec['id']}
        
        # Get all POI catalog_ids
        poi_with_catalog = [rec for rec in poi_records if rec['catalog_id']]
        poi_without_catalog = [rec for rec in poi_records if not rec['catalog_id']]
        poi_catalog_ids = {str(rec['catalog_id']) for rec in poi_with_catalog}
        
        self.stdout.write(f"ETL catalog_ids (ETL.id): {len(etl_catalog_ids)}")
        self.stdout.write(f"POI with catalog_id: {len(poi_with_catalog)}")
        self.stdout.write(f"POI without catalog_id (NULL): {len(poi_without_catalog)}")
        self.stdout.write(f"Unique POI catalog_ids: {len(poi_catalog_ids)}")
        
        # Check join
        poi_catalog_in_etl = poi_catalog_ids & etl_catalog_ids
        poi_catalog_not_in_etl = poi_catalog_ids - etl_catalog_ids
        
        self.stdout.write(f"\nJOIN RESULTS:")
        self.stdout.write(f"  POI.catalog_id found in ETL.id: {len(poi_catalog_in_etl)}")
        self.stdout.write(f"  POI.catalog_id NOT in ETL.id: {len(poi_catalog_not_in_etl)}")
        
        if poi_catalog_in_etl:
            self.stdout.write(self.style.SUCCESS(f"\n  VALID JOINS (POI.catalog_id exists in ETL.id):"))
            for cid in list(poi_catalog_in_etl)[:5]:
                # Find matching ETL record
                etl_match = [r for r in etl_records if str(r['id']) == cid][:1]
                poi_match = [r for r in poi_with_catalog if str(r['catalog_id']) == cid][:1]
                self.stdout.write(f"    catalog_id: {cid}")
                if etl_match:
                    self.stdout.write(f"      ETL.vendor_id: {etl_match[0]['vendor_id']}")
                if poi_match:
                    self.stdout.write(f"      POI.vendor_id: {poi_match[0]['vendor_id']}")
        
        if poi_catalog_not_in_etl:
            self.stdout.write(self.style.ERROR(f"\n  BROKEN JOINS (POI.catalog_id NOT in ETL.id):"))
            for cid in list(poi_catalog_not_in_etl)[:5]:
                poi_match = [r for r in poi_with_catalog if str(r['catalog_id']) == cid][:1]
                self.stdout.write(f"    catalog_id: {cid}")
                if poi_match:
                    self.stdout.write(f"      POI.vendor_id: {poi_match[0]['vendor_id']}")
        
        # Check NULL catalog_id POIs
        if poi_without_catalog:
            self.stdout.write(self.style.WARNING(f"\n  NULL CATALOG_ID POIs ({len(poi_without_catalog)} records):"))
            null_vendor_ids = set()
            for rec in poi_without_catalog:
                if rec['vendor_id']:
                    null_vendor_ids.add(str(rec['vendor_id']))
            self.stdout.write(f"    Unique vendor_ids: {len(null_vendor_ids)}")
            for vid in list(null_vendor_ids)[:5]:
                self.stdout.write(f"      {vid}")
        
        # Band type analysis
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("BAND TYPE ANALYSIS")
        self.stdout.write(f"{'-'*70}\n")
        
        def extract_band(vendor_id):
            if not vendor_id:
                return "UNKNOWN"
            parts = vendor_id.split('-')
            if len(parts) >= 2:
                return parts[1]
            return "UNKNOWN"
        
        azure_bands = {}
        for v in azure_vendor_ids:
            band = extract_band(v)
            azure_bands[band] = azure_bands.get(band, 0) + 1
        
        etl_bands = {}
        for v in etl_vendor_ids:
            band = extract_band(v)
            etl_bands[band] = etl_bands.get(band, 0) + 1
        
        poi_bands = {}
        for v in poi_vendor_ids:
            band = extract_band(v)
            poi_bands[band] = poi_bands.get(band, 0) + 1
        
        self.stdout.write(f"Azure band types:")
        for band, count in sorted(azure_bands.items()):
            self.stdout.write(f"  {band}: {count}")
        
        self.stdout.write(f"\nETL band types:")
        for band, count in sorted(etl_bands.items()):
            self.stdout.write(f"  {band}: {count}")
        
        self.stdout.write(f"\nPOI band types:")
        for band, count in sorted(poi_bands.items()):
            self.stdout.write(f"  {band}: {count}")
        
        # ORDER ID MATCHING - this is the key recovery strategy
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("ORDER ID MATCHING STRATEGY")
        self.stdout.write(f"{'='*70}\n")
        
        def extract_order_id(vendor_id):
            """
            Extract order ID from vendor_id.
            Format: YYMMMDDHHmmss-BAND-ORDERID
            Example: 21MAR21152114-S1BS-507583593010_01_P002
                     ^^^^^^^^^^^^^ ^^^^ ^^^^^^^^^^^^^^^^^^^
                     date          band ORDER ID (this part)
            
            The order ID should be the same across PAN, MSI, and pansharpened files.
            """
            if not vendor_id:
                return None
            parts = vendor_id.split('-')
            if len(parts) >= 3:
                # Everything after the second dash is the order ID
                return '-'.join(parts[2:])
            return None
        
        # Extract order IDs from each source
        azure_order_ids = {}
        for v in azure_vendor_ids:
            oid = extract_order_id(v)
            if oid:
                if oid not in azure_order_ids:
                    azure_order_ids[oid] = []
                azure_order_ids[oid].append(v)
        
        etl_order_ids = {}
        for v in etl_vendor_ids:
            oid = extract_order_id(v)
            if oid:
                if oid not in etl_order_ids:
                    etl_order_ids[oid] = []
                etl_order_ids[oid].append(v)
        
        poi_order_ids = {}
        for v in poi_vendor_ids:
            oid = extract_order_id(v)
            if oid:
                if oid not in poi_order_ids:
                    poi_order_ids[oid] = []
                poi_order_ids[oid].append(v)
        
        self.stdout.write(f"Order IDs extracted:")
        self.stdout.write(f"  Azure: {len(azure_order_ids)} unique order IDs")
        self.stdout.write(f"  ETL:   {len(etl_order_ids)} unique order IDs")
        self.stdout.write(f"  POI:   {len(poi_order_ids)} unique order IDs")
        
        # Test order ID matching
        azure_oid_set = set(azure_order_ids.keys())
        etl_oid_set = set(etl_order_ids.keys())
        poi_oid_set = set(poi_order_ids.keys())
        
        azure_etl_match = azure_oid_set & etl_oid_set
        azure_poi_match = azure_oid_set & poi_oid_set
        etl_poi_match = etl_oid_set & poi_oid_set
        
        self.stdout.write(f"\nOrder ID intersections:")
        self.stdout.write(f"  Azure ∩ ETL: {len(azure_etl_match)}")
        self.stdout.write(f"  Azure ∩ POI: {len(azure_poi_match)}")
        self.stdout.write(f"  ETL ∩ POI:   {len(etl_poi_match)}")
        
        if etl_poi_match:
            self.stdout.write(self.style.SUCCESS(f"\n  RECOVERY POSSIBLE via order ID:"))
            for oid in list(etl_poi_match)[:5]:
                etl_vids = etl_order_ids.get(oid, [])
                poi_vids = poi_order_ids.get(oid, [])
                self.stdout.write(f"    Order ID: {oid}")
                self.stdout.write(f"      ETL vendor_ids: {etl_vids[:2]}")
                self.stdout.write(f"      POI vendor_ids: {poi_vids[:2]}")
                # Get catalog_id from ETL for this order ID
                if etl_vids:
                    etl_match = [r for r in etl_records if str(r['vendor_id']) == etl_vids[0]]
                    if etl_match:
                        self.stdout.write(f"      → ETL.id (catalog): {etl_match[0]['id']}")
        
        # Check orphan POIs specifically
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("ORPHAN POI RECOVERY CHECK (NULL catalog_id)")
        self.stdout.write(f"{'-'*70}\n")
        
        orphan_pois = list(PointsOfInterest.objects.filter(
            catalog_id__isnull=True
        ).values('id', 'vendor_id', 'entity_id')[:100])
        
        self.stdout.write(f"Orphan POIs found: {len(orphan_pois)}")
        
        recoverable = []
        not_recoverable = []
        
        for poi in orphan_pois:
            vid = poi['vendor_id']
            oid = extract_order_id(vid) if vid else None
            
            if oid and oid in etl_oid_set:
                # Can recover via order ID
                etl_vids = etl_order_ids.get(oid, [])
                if etl_vids:
                    etl_match = [r for r in etl_records if str(r['vendor_id']) == etl_vids[0]]
                    if etl_match:
                        recoverable.append({
                            'poi_id': poi['id'],
                            'poi_vendor_id': vid,
                            'order_id': oid,
                            'etl_catalog_id': etl_match[0]['id'],
                            'etl_vendor_id': etl_vids[0]
                        })
                        continue
            
            not_recoverable.append({
                'poi_id': poi['id'],
                'poi_vendor_id': vid,
                'order_id': oid
            })
        
        if recoverable:
            self.stdout.write(self.style.SUCCESS(f"\nRECOVERABLE ({len(recoverable)}):"))
            for r in recoverable[:10]:
                self.stdout.write(f"  POI {r['poi_id']}:")
                self.stdout.write(f"    POI vendor_id:  {r['poi_vendor_id']}")
                self.stdout.write(f"    Order ID:       {r['order_id']}")
                self.stdout.write(f"    → ETL catalog:  {r['etl_catalog_id']}")
                self.stdout.write(f"    → ETL vendor:   {r['etl_vendor_id']}")
        
        if not_recoverable:
            self.stdout.write(self.style.ERROR(f"\nNOT RECOVERABLE ({len(not_recoverable)}):"))
            for r in not_recoverable[:10]:
                self.stdout.write(f"  POI {r['poi_id']}:")
                self.stdout.write(f"    POI vendor_id:  {r['poi_vendor_id']}")
                self.stdout.write(f"    Order ID:       {r['order_id']}")
                self.stdout.write(f"    → No matching ETL record found")
        
        # Explain the relationship
        self.stdout.write(f"\n{'-'*70}")
        self.stdout.write("INTERPRETATION")
        self.stdout.write(f"{'-'*70}")
        self.stdout.write(f"""
Data Model:
  EE/MGP/GEGD → (triggers) → ETL
  POI is standalone (catalog_id is NOT a FK to ETL)

Vendor ID structure:
  YYMMMDDHHmmss-BAND-ORDERID
  21MAR21152114-S1BS-507583593010_01_P002
  ^^^^^^^^^^^^^ ^^^^ ^^^^^^^^^^^^^^^^^^^
  date          band ORDER ID

Band codes:
  M1BS = Multispectral (raw input)
  P1BS = Panchromatic (raw input)  
  S1BS = Pansharpened (processed output from M1BS + P1BS)

Recovery strategy:
  POI.vendor_id → extract ORDER ID → match ETL.vendor_id → get ETL.id
  The ORDER ID portion should be consistent across all file types.
""")
        
        self.stdout.write(f"{'='*70}\n")

    def _action_projects(self, options: dict) -> None:
        """
        Analyze data integrity by project.
        
        Shows which projects have POIs, which catalog_ids they reference,
        and whether those catalog_ids exist in ETL.
        """
        verbose = options.get("verbose", False)
        
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("PROJECT-BASED DATA INTEGRITY ANALYSIS")
        self.stdout.write(f"{'='*70}\n")
        
        # Import Project model
        try:
            from animal.models import Project
        except ImportError:
            raise CommandError("Project model not found")
        
        # Get all ETL catalog_ids for comparison
        etl_catalog_ids = set(
            str(r['id']) for r in 
            ExtractTransformLoad.objects.values('id') if r['id']
        )
        self.stdout.write(f"ETL catalog_ids available: {len(etl_catalog_ids)}\n")
        
        # Get all projects
        projects = Project.objects.all().order_by('id')
        
        self.stdout.write(f"{'='*70}")
        self.stdout.write("PROJECT SUMMARY")
        self.stdout.write(f"{'='*70}\n")
        
        project_stats = []
        
        for project in projects:
            # Get POIs for this project
            pois = PointsOfInterest.objects.filter(project_id=project.id)
            poi_count = pois.count()
            
            if poi_count == 0:
                continue
            
            # Get unique catalog_ids and vendor_ids
            poi_data = pois.values('catalog_id', 'vendor_id').distinct()
            
            catalog_ids = set()
            vendor_ids = set()
            null_catalog_count = 0
            
            for p in poi_data:
                if p['catalog_id']:
                    catalog_ids.add(str(p['catalog_id']))
                else:
                    null_catalog_count += 1
                if p['vendor_id']:
                    vendor_ids.add(str(p['vendor_id']))
            
            # Check which catalog_ids exist in ETL
            in_etl = catalog_ids & etl_catalog_ids
            not_in_etl = catalog_ids - etl_catalog_ids
            
            # Categorize by prefix pattern
            prefix_1040 = {c for c in catalog_ids if c.startswith('1040010')}
            prefix_1030 = {c for c in catalog_ids if c.startswith('1030010')}
            prefix_other = catalog_ids - prefix_1040 - prefix_1030
            
            project_stats.append({
                'project': project,
                'poi_count': poi_count,
                'catalog_ids': catalog_ids,
                'vendor_ids': vendor_ids,
                'null_catalog_count': null_catalog_count,
                'in_etl': in_etl,
                'not_in_etl': not_in_etl,
                'prefix_1040': prefix_1040,
                'prefix_1030': prefix_1030,
                'prefix_other': prefix_other
            })
        
        # Display summary table
        self.stdout.write(f"| {'Project':<30} | {'POIs':>8} | {'Cat IDs':>8} | {'In ETL':>8} | {'Not ETL':>8} | {'NULL':>6} |")
        self.stdout.write(f"|{'-'*32}|{'-'*10}|{'-'*10}|{'-'*10}|{'-'*10}|{'-'*8}|")
        
        for stats in project_stats:
            name = str(stats['project'].name)[:30] if hasattr(stats['project'], 'name') else f"Project {stats['project'].id}"
            self.stdout.write(
                f"| {name:<30} | {stats['poi_count']:>8} | {len(stats['catalog_ids']):>8} | "
                f"{len(stats['in_etl']):>8} | {len(stats['not_in_etl']):>8} | {stats['null_catalog_count']:>6} |"
            )
        
        # Detailed breakdown per project
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("PROJECT DETAILS")
        self.stdout.write(f"{'='*70}")
        
        for stats in project_stats:
            project = stats['project']
            name = str(project.name) if hasattr(project, 'name') else f"Project {project.id}"
            
            self.stdout.write(f"\n{'-'*70}")
            self.stdout.write(f"PROJECT: {name} (ID: {project.id})")
            self.stdout.write(f"{'-'*70}")
            self.stdout.write(f"  POI count: {stats['poi_count']}")
            self.stdout.write(f"  Unique catalog_ids: {len(stats['catalog_ids'])}")
            self.stdout.write(f"  Unique vendor_ids: {len(stats['vendor_ids'])}")
            self.stdout.write(f"  NULL catalog_id POIs: {stats['null_catalog_count']}")
            
            # Catalog ID pattern analysis
            self.stdout.write(f"\n  Catalog ID Patterns:")
            self.stdout.write(f"    1040010... (standard EE): {len(stats['prefix_1040'])}")
            self.stdout.write(f"    1030010... (unknown src): {len(stats['prefix_1030'])}")
            if stats['prefix_other']:
                self.stdout.write(f"    Other patterns: {len(stats['prefix_other'])}")
            
            # ETL linkage
            self.stdout.write(f"\n  ETL Linkage:")
            if stats['in_etl']:
                self.stdout.write(self.style.SUCCESS(f"    ✓ In ETL: {len(stats['in_etl'])}"))
                if verbose:
                    for cid in list(stats['in_etl'])[:5]:
                        self.stdout.write(f"      {cid}")
            
            if stats['not_in_etl']:
                self.stdout.write(self.style.ERROR(f"    ✗ NOT in ETL: {len(stats['not_in_etl'])}"))
                for cid in list(stats['not_in_etl'])[:10]:
                    # Find a sample vendor_id for this catalog_id
                    sample_poi = PointsOfInterest.objects.filter(
                        project_id=project.id,
                        catalog_id=cid
                    ).values('vendor_id').first()
                    vid = sample_poi['vendor_id'] if sample_poi else 'N/A'
                    self.stdout.write(f"      {cid}")
                    self.stdout.write(f"        sample vendor_id: {vid}")
            
            # NULL catalog_id details
            if stats['null_catalog_count'] > 0:
                self.stdout.write(self.style.WARNING(f"\n  NULL catalog_id POIs:"))
                null_pois = PointsOfInterest.objects.filter(
                    project_id=project.id,
                    catalog_id__isnull=True
                ).values('id', 'vendor_id')[:10]
                
                for poi in null_pois:
                    self.stdout.write(f"    POI {poi['id']}: vendor_id={poi['vendor_id']}")
        
        # Summary analysis
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("ANALYSIS SUMMARY")
        self.stdout.write(f"{'='*70}\n")
        
        total_pois = sum(s['poi_count'] for s in project_stats)
        total_in_etl = sum(len(s['in_etl']) for s in project_stats)
        total_not_in_etl = sum(len(s['not_in_etl']) for s in project_stats)
        total_null = sum(s['null_catalog_count'] for s in project_stats)
        total_1030 = sum(len(s['prefix_1030']) for s in project_stats)
        
        self.stdout.write(f"Total POIs across all projects: {total_pois}")
        self.stdout.write(f"Catalog IDs with ETL linkage: {total_in_etl}")
        self.stdout.write(f"Catalog IDs WITHOUT ETL linkage: {total_not_in_etl}")
        self.stdout.write(f"POIs with NULL catalog_id: {total_null}")
        
        if total_1030 > 0:
            self.stdout.write(self.style.WARNING(
                f"\nNOTE: {total_1030} catalog_id(s) use '1030010...' prefix."
            ))
            self.stdout.write(self.style.WARNING(
                "These appear to be from a non-standard source (not EE/MGP/GEGD)."
            ))
            self.stdout.write(self.style.WARNING(
                "Recovery via ETL is NOT POSSIBLE for these records."
            ))
        
        self.stdout.write(f"\n{'='*70}\n")

    def _collect_azure_vendor_ids(self, options: dict) -> set:
        """
        Collect all vendor_ids from Azure Blob Storage.
        """
        limit = options.get("limit", 0)  # 0 = no limit for compare
        prefix = options.get("prefix", "cogs/")

        client, container_name = self._get_azure_client()
        container_client = client.get_container_client(container_name)

        vendor_ids = set()
        count = 0

        try:
            blobs = container_client.list_blobs(name_starts_with=prefix)
            for blob in blobs:
                vendor_id = self._parse_vendor_id_from_blob(blob.name)
                vendor_ids.add(vendor_id)
                count += 1
                if limit > 0 and count >= limit:
                    break
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Azure error: {e}"))

        return vendor_ids

    def _collect_etl_data(self) -> dict:
        """
        Collect ETL table data.
        """
        records = ExtractTransformLoad.objects.values('id', 'vendor_id', 'entity_id', 'table_name', 'date')

        vendor_ids = set()
        catalog_ids = set()
        by_vendor = {}

        for rec in records:
            vid = str(rec['vendor_id']) if rec['vendor_id'] else None
            cid = str(rec['id']) if rec['id'] else None

            if vid:
                vendor_ids.add(vid)
                by_vendor[vid] = {
                    'catalog_id': cid,
                    'entity_id': rec['entity_id'],
                    'table_name': rec['table_name'],
                    'date': rec['date']
                }
            if cid:
                catalog_ids.add(cid)

        return {
            'vendor_ids': vendor_ids,
            'catalog_ids': catalog_ids,
            'by_vendor': by_vendor
        }

    def _collect_poi_data(self) -> dict:
        """
        Collect POI table data.
        """
        records = PointsOfInterest.objects.values('id', 'catalog_id', 'vendor_id', 'project_id', 'date_image_taken')

        vendor_ids = set()
        catalog_ids = set()
        by_vendor = defaultdict(list)
        null_catalog_pois = []

        for rec in records:
            vid = str(rec['vendor_id']) if rec['vendor_id'] else None
            cid = str(rec['catalog_id']) if rec['catalog_id'] else None

            if vid:
                vendor_ids.add(vid)
                by_vendor[vid].append(rec)
            if cid:
                catalog_ids.add(cid)
            else:
                null_catalog_pois.append(rec)

        return {
            'vendor_ids': vendor_ids,
            'catalog_ids': catalog_ids,
            'by_vendor': dict(by_vendor),
            'null_catalog_pois': null_catalog_pois
        }

    def _write_report(self, filepath: str, data: dict) -> None:
        """
        Write inventory report to markdown file.
        """
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# GAIA Data Inventory Report\n\n")
            f.write(f"**Generated:** {timezone.now().isoformat()}\n\n")
            f.write(f"---\n\n")

            f.write(f"## Summary\n\n")
            f.write(f"| Source | Count |\n")
            f.write(f"|--------|-------|\n")
            f.write(f"| Azure vendor_ids | {len(data['azure_vendor_ids'])} |\n")
            f.write(f"| ETL vendor_ids | {len(data['etl_vendor_ids'])} |\n")
            f.write(f"| ETL catalog_ids | {len(data['etl_catalog_ids'])} |\n")
            f.write(f"| POI vendor_ids | {len(data['poi_vendor_ids'])} |\n")
            f.write(f"| POI catalog_ids | {len(data['poi_catalog_ids'])} |\n\n")

            f.write(f"## Orphan Analysis\n\n")
            f.write(f"| Category | Count |\n")
            f.write(f"|----------|-------|\n")
            f.write(f"| Azure without ETL | {len(data['azure_no_etl'])} |\n")
            f.write(f"| ETL without Azure | {len(data['etl_no_azure'])} |\n")
            f.write(f"| ETL without POI | {len(data['etl_no_poi'])} |\n")
            f.write(f"| POI without catalog_id | {len(data['poi_null_catalog'])} |\n\n")

            f.write(f"## NULL catalog_id Recovery\n\n")
            f.write(f"| Metric | Count |\n")
            f.write(f"|--------|-------|\n")
            f.write(f"| Orphan vendor_ids | {len(data['orphan_vendor_ids'])} |\n")
            f.write(f"| Found in Azure | {len(data['orphans_in_azure'])} |\n")
            f.write(f"| NOT in Azure | {len(data['orphan_vendor_ids'] - data['orphans_in_azure'])} |\n\n")

            if data['orphan_vendor_ids']:
                f.write(f"### Orphan Vendor IDs\n\n")
                f.write(f"| vendor_id | In Azure |\n")
                f.write(f"|-----------|----------|\n")
                for vid in sorted(data['orphan_vendor_ids']):
                    in_azure = "YES" if vid in data['orphans_in_azure'] else "NO"
                    f.write(f"| {vid} | {in_azure} |\n")

            f.write(f"\n---\n\n")
            f.write(f"*End of report*\n")